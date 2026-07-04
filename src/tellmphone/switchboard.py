"""Call lifecycle and routing (docs/DESIGN.md §5–§6).

Invariants:
- call_id is the only cross-session key; native session ids never leave here.
- One writer per call (per-call file lock; "the line is engaged" otherwise).
- Callee-side session loss degrades to transcript replay, never data loss.
- Tool-facing methods return plain dicts with a "status" field; they raise
  only on programmer error, so the calling LLM always gets something useful.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from tellmphone.adapters.base import (
    AdapterError,
    AgentAdapter,
    SessionLost,
    SpawnRequest,
)
from tellmphone.config import Config
from tellmphone.personalities import Personality, PersonalityBook, PersonalityError
from tellmphone.store import (
    CallBusy,
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


@dataclass
class _TurnResult:
    ok: bool = False
    text: str = ""
    usage: dict | None = None
    error: str = ""


class Switchboard:
    def __init__(self, config: Config, store: Store, adapters: dict[str, AgentAdapter]):
        self.config = config
        self.store = store
        self.adapters = adapters
        self.personalities = PersonalityBook(store.personalities_dir)

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
        timeout_s: int | None = None,
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
            write=write,
            created_at=utcnow(),
            last_activity_at=utcnow(),
        )
        self.store.create_call(record)
        self._append(record, self.config.i_am, callee, _frame_message(message, context))

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

        req = self._spawn_request(record, _frame_message(message, context), persona)
        return self._live_turn(record, timeout_s, lambda: adapter.spawn(req))

    # ----------------------------------------------------------------- reply

    def reply(
        self,
        call_id: str,
        message: str,
        timeout_s: int | None = None,
    ) -> dict:
        try:
            record = self.store.load_call(call_id)
        except CallNotFound:
            return {
                "status": "error",
                "error": f"no call {call_id!r}; use check_messages to list open calls",
            }

        if record.status == "closed":
            return {
                "call_id": call_id,
                "status": "error",
                "error": "this call was hung up; place a new call instead",
            }
        if record.status == "ringing":
            return {
                "call_id": call_id,
                "status": "busy",
                "note": "the line is engaged (a turn is still running); check_messages later",
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

        self._append(record, me, record.callee.agent, message)
        persona = self._persona_of(record)
        req = self._spawn_request(record, message, persona)
        session_id = record.callee.session_id

        def run_turn():
            if session_id is None:
                # voicemail being converted to a live call, or first live turn
                replay_req = self._spawn_request(
                    record, self._replay_prompt(record), persona
                )
                record.resumed_via = "transcript-replay"
                return adapter.spawn(replay_req)
            try:
                return adapter.resume(session_id, message, req)
            except SessionLost:
                replay_req = self._spawn_request(
                    record, self._replay_prompt(record), persona
                )
                record.resumed_via = "transcript-replay"
                return adapter.spawn(replay_req)

        return self._live_turn(record, timeout_s, run_turn, message_already_logged=True)

    def _reply_as_callee(self, record: CallRecord, message: str) -> dict:
        """Answering machine path: the callee agent answers a voicemail.

        No subprocess — the answer is written to the transcript and flagged
        unread for the caller, who will pull it via check_messages.
        """
        with self.store.call_lock(record.call_id):
            self._append(record, record.callee.agent, record.caller.agent, message)
            record.status = "answered"
            if record.caller.agent not in record.unread_for:
                record.unread_for.append(record.caller.agent)
            self.store.save_call(record)
        return {
            "call_id": record.call_id,
            "status": "answered",
            "note": f"reply delivered to {record.caller.agent}'s mailbox for this project",
        }

    # -------------------------------------------------------------- mailbox

    def check_messages(self, project_dir: str) -> dict:
        me = self.config.i_am
        unread, open_calls = [], []
        for record in self.store.calls_for_project(project_dir):
            other = (
                record.callee.agent
                if me == record.caller.agent
                else record.caller.agent
            )
            if me in record.unread_for:
                transcript = self.store.read_transcript(record.call_id)
                incoming = [e for e in transcript if e.to == me and e.kind == "message"]
                last = incoming[-1] if incoming else None
                unread.append(
                    {
                        "call_id": record.call_id,
                        "from": other,
                        "personality": record.callee.personality,
                        "preview": _preview(last.body) if last else "",
                        "body": last.body if last else "",
                        "ts": last.ts.isoformat() if last else record.last_activity_at.isoformat(),
                    }
                )
                with self.store.call_lock(record.call_id):
                    fresh = self.store.load_call(record.call_id)
                    if me in fresh.unread_for:
                        fresh.unread_for.remove(me)
                        self.store.save_call(fresh)
            if record.status in ("ringing", "answered", "voicemail") and me in (
                record.caller.agent,
                record.callee.agent,
            ):
                open_calls.append(
                    {
                        "call_id": record.call_id,
                        "with": other,
                        "status": record.status,
                        "last_activity_at": record.last_activity_at.isoformat(),
                    }
                )
        return {
            "unread": unread,
            "open_calls": open_calls,
            "note": "use reply(call_id, message) to continue any of these",
        }

    # -------------------------------------------------------------- hang up

    def hang_up(self, call_id: str, reason: str | None = None) -> dict:
        try:
            self.store.call_dir(call_id)
        except CallNotFound:
            return {"status": "error", "error": f"no call {call_id!r}"}
        with self.store.call_lock(call_id):
            record = self.store.load_call(call_id)
            record.status = "closed"
            record.closed_reason = reason
            self._append(
                record,
                self.config.i_am,
                "*",
                f"hung up{': ' + reason if reason else ''}",
                kind="system",
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
            write_access=record.write
            or self.config.permissions_for(record.project_dir).write,
            hop_count=record.hop_count,
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

    def _append(self, record, from_, to, body, kind="message") -> None:
        # seq is assigned inside append_transcript, atomically under the
        # transcript's own lock — safe with or without call.lock held.
        self.store.append_transcript(
            record.call_id,
            TranscriptEntry(from_=from_, to=to, body=body, ts=utcnow(), kind=kind),
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

    def _live_turn(
        self,
        record: CallRecord,
        timeout_s: int | None,
        run,
        message_already_logged: bool = False,
    ) -> dict:
        """Run one callee turn with wait-then-voicemail timeout semantics.

        The worker always finishes the bookkeeping (transcript + unread flag),
        even after the tool call has returned "ringing" — a slow answer lands
        in the mailbox instead of being lost. Requires the MCP server process
        to stay alive, which it does for the length of the host agent session
        (open question §14.4 covers the crash case).
        """
        timeout_s = timeout_s or self.config.timeout_s
        with self.store.call_lock(record.call_id):
            record.status = "ringing"
            self.store.save_call(record)
        result = _TurnResult()

        def worker():
            # The turn can run for minutes; check_messages and hang_up may
            # commit in between. Reload under the lock and touch only this
            # turn's fields, so the save can't resurrect cleared unread flags
            # or reopen a call that was hung up mid-turn.
            try:
                turn = run()
                result.ok, result.text, result.usage = True, turn.text, turn.usage
                with self.store.call_lock(record.call_id):
                    fresh = self.store.load_call(record.call_id)
                    fresh.callee.session_id = turn.session_id or fresh.callee.session_id
                    if record.resumed_via:
                        fresh.resumed_via = record.resumed_via
                    self._append(fresh, fresh.callee.agent, fresh.caller.agent, turn.text)
                    if fresh.status != "closed":
                        fresh.status = "answered"
                        if fresh.caller.agent not in fresh.unread_for:
                            fresh.unread_for.append(fresh.caller.agent)
                    self.store.save_call(fresh)
            except AdapterError as exc:
                result.error = str(exc)
                with self.store.call_lock(record.call_id):
                    fresh = self.store.load_call(record.call_id)
                    fresh.last_error = result.error
                    if fresh.status != "closed":
                        fresh.status = "failed"
                    self.store.save_call(fresh)

        thread = threading.Thread(target=worker, daemon=False)
        thread.start()
        thread.join(timeout_s)

        if thread.is_alive():
            return {
                "call_id": record.call_id,
                "status": "ringing",
                "note": (
                    f"{record.callee.agent} is still working (>{timeout_s}s); the answer "
                    "will land in this project's mailbox — poll with check_messages"
                ),
            }

        if not result.ok:
            return {"call_id": record.call_id, "status": "failed", "error": result.error}

        # Delivered inline; don't leave it flagged unread as well.
        with self.store.call_lock(record.call_id):
            fresh = self.store.load_call(record.call_id)
            if record.caller.agent in fresh.unread_for:
                fresh.unread_for.remove(record.caller.agent)
                self.store.save_call(fresh)

        response: dict = {
            "call_id": record.call_id,
            "status": "answered",
            "from": record.callee.agent,
            "response": result.text,
        }
        if record.callee.personality:
            response["personality"] = record.callee.personality
        if record.callee.model:
            response["model"] = record.callee.model
        if record.resumed_via:
            response["resumed_via"] = record.resumed_via
        if result.usage:
            response["usage"] = result.usage
        return response
