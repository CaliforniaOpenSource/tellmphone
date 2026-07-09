"""Call lifecycle and routing (docs/DESIGN.md §5–§6).

Invariants:
- call_id is the only cross-session key; native session ids never leave here.
- One writer per call (per-call file lock; "the line is engaged" otherwise).
- Callee-side session loss degrades to transcript replay, never data loss.
- Tool-facing methods return plain dicts with a "status" field; they raise
  only on programmer error, so the calling LLM always gets something useful.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from tellmphone.adapters.base import (
    AdapterError,
    AgentAdapter,
    SessionLost,
    SpawnRequest,
)
from tellmphone.config import CALL_ID_ENV, HOME_ENV, HOP_ENV, Config
from tellmphone.personalities import Personality, PersonalityBook, PersonalityError
from tellmphone.store import (
    CallNotFound,
    CallRecord,
    Party,
    Store,
    TranscriptEntry,
    new_call_id,
    utcnow,
)

PREVIEW_CHARS = 200


def _preview(text: str) -> str:
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


def _frame_message(message: str, context: str | None) -> str:
    if not context:
        return message
    return f"Background context:\n{context}\n\nYour task:\n{message}"


class Switchboard:
    def __init__(
        self,
        config: Config,
        store: Store,
        adapters: dict[str, AgentAdapter],
        detach_turns: bool = True,
    ):
        self.config = config
        self.store = store
        self.adapters = adapters
        self.personalities = PersonalityBook(store.personalities_dir)
        self.detach_turns = detach_turns

    # ------------------------------------------------------------------ call

    def place_call(
        self,
        callee: str,
        message: str,
        project_dir: str,
        personality: str | None = None,
        model: str | None = None,
        context: str | None = None,
        mode: str = "wait",
        write: bool = False,
    ) -> dict:
        if mode not in ("wait", "voicemail"):
            return {
                "status": "refused",
                "reason": "mode must be 'wait' or 'voicemail'",
            }
        project_path = Path(project_dir).expanduser().resolve()
        if not project_path.exists() or not project_path.is_dir():
            return {
                "status": "refused",
                "reason": f"project_dir must be an existing directory: {project_dir}",
            }
        project_dir = str(project_path)
        resolved_write = write or self.config.permissions_for(project_dir).write

        hop_count = self.config.hop_count + 1
        if hop_count > self.config.max_hops:
            return {
                "status": "refused",
                "reason": (
                    f"hop limit reached ({self.config.max_hops}): this session is "
                    "already an agent-to-agent callee and may not extend the chain. "
                    "Answer with what you have."
                ),
            }
        # Only a top-level caller may grant write; a callee inherits
        # permissions, it doesn't extend them.
        if write and self.config.hop_count > 0:
            return {
                "status": "refused",
                "reason": (
                    "write access can only be granted by a top-level caller; as "
                    "an agent-to-agent callee you cannot extend permissions down "
                    "the chain. Call again without write=True."
                ),
            }

        adapter = self.adapters.get(callee)
        if adapter is None:
            return {
                "status": "refused",
                "reason": f"unknown agent {callee!r}; known: {sorted(self.adapters)}",
            }

        persona = None
        if personality:
            try:
                persona = self.personalities.get(personality)
            except PersonalityError as exc:
                return {"status": "refused", "reason": str(exc)}
            if not persona.allows(callee):
                return {
                    "status": "refused",
                    "reason": f"personality {personality!r} is limited to {persona.agents}",
                }

        record = CallRecord(
            call_id=new_call_id(),
            project_dir=project_dir,
            caller=Party(agent=self.config.i_am),
            callee=Party(
                agent=callee,
                personality=persona.name if persona else None,
                personality_hash=persona.hash if persona else None,
                personality_body=persona.body if persona else None,
                # Pinned for the whole call so resumes and replays don't
                # silently switch brains mid-conversation.
                model=model or self.config.default_model_for(callee),
            ),
            status="voicemail" if mode == "voicemail" else "ringing",
            hop_count=hop_count,
            write=resolved_write,
            created_at=utcnow(),
            last_activity_at=utcnow(),
        )
        self.store.create_call(record)
        self._append(
            record,
            self.config.i_am,
            callee,
            _frame_message(message, context),
            from_role="caller",
            to_role="callee",
        )

        if mode == "voicemail":
            with self.store.call_lock(record.call_id):
                record.unread_for = [callee]
                self.store.save_call(record)
            return {
                "call_id": record.call_id,
                "status": "voicemail",
                "note": (
                    f"message queued for {callee}; it will be seen when a {callee} "
                    "session checks messages in this project"
                ),
            }

        if not adapter.available():
            with self.store.call_lock(record.call_id):
                record.status = "failed"
                record.last_error = f"{callee} CLI is not installed or not on PATH"
                self.store.save_call(record)
            return {"call_id": record.call_id, "status": "failed", "error": record.last_error}

        return self._start_turn(record)

    # ----------------------------------------------------------------- reply

    def reply(
        self,
        call_id: str,
        message: str,
    ) -> dict:
        try:
            record = self.store.load_call(call_id)
        except CallNotFound:
            return {
                "status": "error",
                "error": f"no call {call_id!r}; use check_messages to list open calls",
            }

        me = self.config.i_am
        if me != record.caller.agent and me != record.callee.agent:
            return {
                "call_id": call_id,
                "status": "error",
                "error": f"call {call_id} is between {record.caller.agent} and "
                f"{record.callee.agent}; you are {me}",
            }
        if me != record.caller.agent:
            return self._reply_as_callee(record, message)

        # I'm the caller: drive the callee headlessly.
        adapter = self.adapters.get(record.callee.agent)
        if adapter is None or not adapter.available():
            return {
                "call_id": call_id,
                "status": "failed",
                "error": f"{record.callee.agent} CLI is not available",
            }

        with self.store.call_lock(record.call_id):
            record = self.store.load_call(call_id)
            if status_error := self._reply_status_error(record):
                return status_error
            self._append(
                record,
                me,
                record.callee.agent,
                message,
                from_role="caller",
                to_role="callee",
            )
            record.status = "ringing"
            self.store.save_call(record)

        return self._start_turn(record)

    def _reply_as_callee(self, record: CallRecord, message: str) -> dict:
        """Answering machine path: the callee agent answers a voicemail.

        No subprocess — the answer is written to the transcript and flagged
        unread for the caller, who will pull it via check_messages.
        """
        with self.store.call_lock(record.call_id):
            record = self.store.load_call(record.call_id)
            if status_error := self._reply_status_error(record):
                return status_error
            self._append(
                record,
                record.callee.agent,
                record.caller.agent,
                message,
                from_role="callee",
                to_role="caller",
            )
            record.status = "answered"
            if record.caller.agent not in record.unread_for:
                record.unread_for.append(record.caller.agent)
            self.store.save_call(record)
        return {
            "call_id": record.call_id,
            "status": "answered",
            "note": f"reply delivered to {record.caller.agent}'s mailbox for this project",
        }

    def _reply_status_error(self, record: CallRecord) -> dict | None:
        if record.status == "closed":
            return {
                "call_id": record.call_id,
                "status": "error",
                "error": "this call was hung up; place a new call instead",
            }
        if record.status == "failed":
            return {
                "call_id": record.call_id,
                "status": "error",
                "error": "this call failed; place a new call instead",
            }
        if record.status == "ringing":
            return self._busy_reply(record.call_id)
        return None

    def _busy_reply(self, call_id: str) -> dict:
        return {
            "call_id": call_id,
            "status": "busy",
            "note": "the line is engaged (a turn is still running); check_messages later",
        }

    # -------------------------------------------------------------- mailbox

    def check_messages(self, project_dir: str) -> dict:
        me = self.config.i_am
        unread, open_calls = [], []
        for record in self.store.calls_for_project(project_dir):
            if me not in (record.caller.agent, record.callee.agent):
                continue
            with self.store.call_lock(record.call_id):
                fresh = self.store.load_call(record.call_id)
                my_role = self._role_for(fresh)
                other = (
                    fresh.callee.agent
                    if me == fresh.caller.agent
                    else fresh.caller.agent
                )
                incoming = []
                if me in fresh.unread_for:
                    transcript = self.store.read_transcript(fresh.call_id)
                    last_read = fresh.last_read_seq.get(me, 0)
                    incoming = [
                        e
                        for e in transcript
                        if (
                            self._entry_is_for(e, me, my_role)
                            and e.kind in ("message", "progress")
                            and e.seq > last_read
                        )
                    ]
                    if incoming:
                        fresh.last_read_seq[me] = max(e.seq for e in incoming)
                    if me in fresh.unread_for:
                        fresh.unread_for.remove(me)
                    self.store.save_call(fresh)
                include_open = fresh.status in ("ringing", "answered", "voicemail")

            unread.extend(
                {
                    "call_id": fresh.call_id,
                    "from": entry.from_,
                    "personality": fresh.callee.personality,
                    "kind": entry.kind,
                    "seq": entry.seq,
                    "preview": _preview(entry.body),
                    "body": entry.body,
                    "ts": entry.ts.isoformat(),
                    "is_final": entry.kind == "message",
                    "usage": entry.usage,
                }
                for entry in incoming
            )
            if include_open:
                open_calls.append(
                    {
                        "call_id": fresh.call_id,
                        "with": other,
                        "status": fresh.status,
                        "last_activity_at": fresh.last_activity_at.isoformat(),
                    }
                )
        return {
            "unread": unread,
            "open_calls": open_calls,
            "note": "use reply(call_id, message) to continue any of these",
        }

    # ----------------------------------------------------------- call lookup

    def get_call(self, call_id: str) -> dict:
        """Return one call and its transcript without changing unread state."""
        try:
            record = self.store.load_call(call_id)
        except CallNotFound:
            return {"status": "error", "error": f"no call {call_id!r}"}
        if self.config.i_am not in (record.caller.agent, record.callee.agent):
            return {
                "call_id": call_id,
                "status": "error",
                "error": f"call {call_id} is between {record.caller.agent} and "
                f"{record.callee.agent}; you are {self.config.i_am}",
            }
        return {
            "call_id": record.call_id,
            "status": record.status,
            "project_dir": record.project_dir,
            "caller": record.caller.agent,
            "callee": record.callee.agent,
            "personality": record.callee.personality,
            "model": record.callee.model,
            "write": record.write,
            "created_at": record.created_at.isoformat(),
            "last_activity_at": record.last_activity_at.isoformat(),
            "closed_reason": record.closed_reason,
            "last_error": record.last_error,
            "resumed_via": record.resumed_via,
            "transcript": [
                self._entry_dict(entry) for entry in self.store.read_transcript(call_id)
            ],
        }

    # ----------------------------------------------------------- progress

    def report_progress(self, message: str, call_id: str | None = None) -> dict:
        call_id = call_id or self.config.call_id
        if not call_id or self.config.call_id != call_id:
            return {
                "call_id": call_id,
                "status": "refused",
                "reason": "progress can only be reported from the active detached call",
            }
        try:
            with self.store.call_lock(call_id):
                record = self.store.load_call(call_id)
                if self.config.i_am != record.callee.agent:
                    return {
                        "call_id": call_id,
                        "status": "refused",
                        "reason": f"only the callee ({record.callee.agent}) can report progress",
                    }
                if record.status != "ringing":
                    return {
                        "call_id": call_id,
                        "status": "refused",
                        "reason": f"call is {record.status}, not ringing",
                    }
                entry = self._append(
                    record,
                    record.callee.agent,
                    record.caller.agent,
                    message,
                    kind="progress",
                    from_role="callee",
                    to_role="caller",
                )
                if record.caller.agent not in record.unread_for:
                    record.unread_for.append(record.caller.agent)
                self.store.save_call(record)
        except CallNotFound:
            return {
                "call_id": call_id,
                "status": "error",
                "error": f"no call {call_id!r}",
            }
        return {"call_id": call_id, "status": "reported", "seq": entry.seq}

    # -------------------------------------------------------------- hang up

    def hang_up(self, call_id: str, reason: str | None = None) -> dict:
        try:
            self.store.call_dir(call_id)
        except CallNotFound:
            return {"status": "error", "error": f"no call {call_id!r}"}
        with self.store.call_lock(call_id):
            record = self.store.load_call(call_id)
            if self.config.i_am not in (record.caller.agent, record.callee.agent):
                return {
                    "call_id": call_id,
                    "status": "error",
                    "error": f"call {call_id} is between {record.caller.agent} and "
                    f"{record.callee.agent}; you are {self.config.i_am}",
                }
            record.status = "closed"
            record.closed_reason = reason
            self._append(
                record,
                self.config.i_am,
                "*",
                f"hung up{': ' + reason if reason else ''}",
                kind="system",
                from_role="system",
            )
            self.store.save_call(record)
        return {"call_id": call_id, "status": "closed"}

    # ------------------------------------------------------------ phonebook

    def phonebook(self) -> dict:
        return {
            "you_are": self.config.i_am,
            "agents": [
                {
                    "name": name,
                    "available": adapter.available(),
                    "default_model": self.config.default_model_for(name)
                    or "(CLI default)",
                }
                for name, adapter in sorted(self.adapters.items())
            ],
            "personalities": [
                {"name": p.name, "description": p.description, "source": p.source}
                for p in self.personalities.all()
            ],
        }

    # ------------------------------------------------------------- plumbing

    def _spawn_request(self, record, message, persona) -> SpawnRequest:
        return SpawnRequest(
            message=message,
            project_dir=record.project_dir,
            personality=persona,
            model=record.callee.model,
            write_access=record.write,
            hop_count=record.hop_count,
            call_id=record.call_id,
        )

    def _persona_of(self, record: CallRecord):
        if not record.callee.personality:
            return None
        if record.callee.personality_body is not None:
            return Personality(
                name=record.callee.personality,
                description="",
                body=record.callee.personality_body,
                source="snapshot",
            )
        try:
            return self.personalities.get(record.callee.personality)
        except PersonalityError:
            return None  # old call record without a body snapshot

    def _role_for(self, record: CallRecord) -> str:
        if record.caller.agent != record.callee.agent:
            return "caller" if self.config.i_am == record.caller.agent else "callee"
        return "callee" if self.config.call_id == record.call_id else "caller"

    @staticmethod
    def _entry_is_for(entry: TranscriptEntry, agent: str, role: str) -> bool:
        if entry.to_role is not None:
            return entry.to_role == role
        return entry.to == agent

    @staticmethod
    def _entry_dict(entry: TranscriptEntry) -> dict:
        return {
            "seq": entry.seq,
            "from": entry.from_,
            "to": entry.to,
            "from_role": entry.from_role,
            "to_role": entry.to_role,
            "body": entry.body,
            "kind": entry.kind,
            "ts": entry.ts.isoformat(),
            "usage": entry.usage,
        }

    def _append(
        self,
        record,
        from_,
        to,
        body,
        kind="message",
        from_role=None,
        to_role=None,
        usage=None,
    ):
        # seq is assigned inside append_transcript, atomically under the
        # transcript's own lock — safe with or without call.lock held.
        return self.store.append_transcript(
            record.call_id,
            TranscriptEntry(
                from_=from_,
                to=to,
                body=body,
                ts=utcnow(),
                kind=kind,
                from_role=from_role,
                to_role=to_role,
                usage=usage or {},
            ),
        )

    def _replay_prompt(self, record: CallRecord) -> str:
        """Rebuild context for a fresh session when resume is impossible."""
        lines = [
            f"You are {record.callee.agent}, resuming an interrupted conversation "
            f"with {record.caller.agent} about the project at {record.project_dir}. "
            "The transcript so far follows. Continue the conversation by "
            "responding to the final message.",
            "",
        ]
        for entry in self.store.read_transcript(record.call_id):
            if entry.kind == "message":
                lines.append(f"[{entry.from_}]: {entry.body}")
        return "\n".join(lines)

    def _start_turn(self, record: CallRecord) -> dict:
        if not self.detach_turns:
            return {
                "call_id": record.call_id,
                "status": "ringing",
                "note": f"{record.callee.agent} is working; poll check_messages for the answer",
            }
        cmd = [
            sys.executable,
            "-m",
            "tellmphone.cli",
            "turn",
            record.call_id,
            "--home",
            str(self.config.home),
        ]
        call_dir = self.store.call_dir(record.call_id)
        env = os.environ.copy()
        env[HOME_ENV] = str(self.config.home)
        env[HOP_ENV] = str(record.hop_count)
        env[CALL_ID_ENV] = record.call_id
        try:
            with open(call_dir / "turn.stdout", "ab") as stdout, open(
                call_dir / "turn.stderr", "ab"
            ) as stderr:
                subprocess.Popen(
                    cmd,
                    cwd=record.project_dir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            with self.store.call_lock(record.call_id):
                fresh = self.store.load_call(record.call_id)
                fresh.status = "failed"
                fresh.last_error = f"failed to start turn process: {exc}"
                self.store.save_call(fresh)
            return {"call_id": record.call_id, "status": "failed", "error": fresh.last_error}
        return {
            "call_id": record.call_id,
            "status": "ringing",
            "note": f"{record.callee.agent} is working; poll check_messages for the answer",
        }

    def run_turn(self, call_id: str) -> dict:
        try:
            record = self.store.load_call(call_id)
        except CallNotFound:
            return {"status": "error", "error": f"no call {call_id!r}"}

        try:
            adapter = self.adapters.get(record.callee.agent)
            if adapter is None or not adapter.available():
                return self._fail_turn(
                    call_id, f"{record.callee.agent} CLI is not available"
                )

            turn_lock_path = self.store.call_dir(call_id) / "turn.lock"
            with open(turn_lock_path, "w") as lock_fh:
                try:
                    import fcntl

                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"call_id": call_id, "status": "busy"}
                try:
                    with self.store.call_lock(call_id):
                        record = self.store.load_call(call_id)
                        if record.status != "ringing":
                            return {"call_id": call_id, "status": record.status}
                    return self._run_adapter_turn(record, adapter)
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except Exception as exc:
            error = f"unexpected turn failure: {type(exc).__name__}: {exc}"
            return self._fail_turn(call_id, error)

    def _run_adapter_turn(self, record: CallRecord, adapter: AgentAdapter) -> dict:
        persona = self._persona_of(record)
        transcript = [
            e
            for e in self.store.read_transcript(record.call_id)
            if e.kind == "message"
        ]
        incoming = [e for e in transcript if e.to == record.callee.agent]
        message = incoming[-1].body if incoming else self._replay_prompt(record)
        req = self._spawn_request(record, message, persona)
        resumed_via = None
        try:
            if record.callee.session_id and incoming:
                try:
                    turn = adapter.resume(record.callee.session_id, message, req)
                except SessionLost:
                    replay_req = self._spawn_request(
                        record, self._replay_prompt(record), persona
                    )
                    turn = adapter.spawn(replay_req)
                    resumed_via = "transcript-replay"
            else:
                if len(transcript) == 1:
                    prompt = message
                else:
                    prompt = self._replay_prompt(record)
                    resumed_via = "transcript-replay"
                turn = adapter.spawn(self._spawn_request(record, prompt, persona))
        except AdapterError as exc:
            return self._fail_turn(record.call_id, str(exc))

        with self.store.call_lock(record.call_id):
            fresh = self.store.load_call(record.call_id)
            if fresh.status == "closed":
                return {"call_id": record.call_id, "status": "closed"}
            fresh.callee.session_id = turn.session_id or fresh.callee.session_id
            if resumed_via:
                fresh.resumed_via = resumed_via
            self._append(
                fresh,
                fresh.callee.agent,
                fresh.caller.agent,
                turn.text,
                from_role="callee",
                to_role="caller",
                usage=turn.usage,
            )
            fresh.status = "answered"
            if fresh.caller.agent not in fresh.unread_for:
                fresh.unread_for.append(fresh.caller.agent)
            self.store.save_call(fresh)
        return {
            "call_id": record.call_id,
            "status": "answered",
            "from": record.callee.agent,
            "response": turn.text,
            "usage": turn.usage,
        }

    def _fail_turn(self, call_id: str, error: str) -> dict:
        with self.store.call_lock(call_id):
            record = self.store.load_call(call_id)
            record.last_error = error
            if record.status != "closed":
                record.status = "failed"
            self.store.save_call(record)
        return {"call_id": call_id, "status": record.status, "error": error}

    def wait(self, call_id: str, timeout_s: int | None = None) -> dict:
        deadline = time.monotonic() + (timeout_s or self.config.timeout_s)
        while True:
            try:
                record = self.store.load_call(call_id)
            except CallNotFound:
                return {"status": "error", "error": f"no call {call_id!r}"}
            if record.status != "ringing":
                if record.status == "answered":
                    final = self._mark_call_read_and_get_final(call_id)
                    return {
                        "call_id": call_id,
                        "status": "answered",
                        "from": record.callee.agent,
                        "response": final.body if final else "",
                        "usage": final.usage if final else {},
                    }
                if record.status == "failed":
                    return {"call_id": call_id, "status": "failed", "error": record.last_error}
                return {"call_id": call_id, "status": record.status}
            if time.monotonic() >= deadline:
                return {
                    "call_id": call_id,
                    "status": "ringing",
                    "note": f"{record.callee.agent} is still working; poll check_messages for the answer",
                }
            time.sleep(0.05)

    def _mark_call_read_and_get_final(self, call_id: str) -> TranscriptEntry | None:
        me = self.config.i_am
        with self.store.call_lock(call_id):
            record = self.store.load_call(call_id)
            transcript = self.store.read_transcript(call_id)
            incoming = [
                e
                for e in transcript
                if e.to == me and e.kind in ("message", "progress")
            ]
            if incoming:
                record.last_read_seq[me] = max(e.seq for e in incoming)
            if me in record.unread_for:
                record.unread_for.remove(me)
            self.store.save_call(record)
        finals = [e for e in incoming if e.kind == "message"]
        return finals[-1] if finals else None
