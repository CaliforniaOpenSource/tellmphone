"""On-disk state under ~/.tellmphone/ — the switchboard's memory.

Layout (see docs/DESIGN.md §4):

    ~/.tellmphone/
    ├── config.toml
    ├── personalities/*.md
    └── projects/<slug>-<sha1[:12]>/
        ├── project.json
        └── calls/<call_id>/{call.json, call.lock, transcript.jsonl}
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CALL_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # no i/l/o/u lookalikes
CALL_ID_LENGTH = 8

CallStatus = Literal["ringing", "answered", "voicemail", "closed", "failed"]


class Party(BaseModel):
    agent: str
    session_id: str | None = None
    personality: str | None = None
    personality_hash: str | None = None
    personality_body: str | None = None
    model: str | None = None  # pinned at call time; every turn of the call uses it


class CallRecord(BaseModel):
    call_id: str
    project_dir: str
    caller: Party
    callee: Party
    status: CallStatus
    hop_count: int = 1
    # Caller-granted write access, pinned for the whole call (docs/DESIGN.md §6).
    write: bool = False
    created_at: datetime
    last_activity_at: datetime
    unread_for: list[str] = Field(default_factory=list)
    last_read_seq: dict[str, int] = Field(default_factory=dict)
    closed_reason: str | None = None
    last_error: str | None = None
    resumed_via: str | None = None  # "transcript-replay" when the native session was lost

    @field_validator("status", mode="before")
    @classmethod
    def _legacy_working_is_ringing(cls, value):
        return "ringing" if value == "working" else value


class TranscriptEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    seq: int = 0  # 0 = unassigned; append_transcript assigns the real seq
    from_: str = Field(alias="from")
    to: str
    body: str
    ts: datetime
    kind: Literal["message", "progress", "system"] = "message"


class CallBusy(Exception):
    """The line is engaged: another process is mid-turn on this call."""


class CallNotFound(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_call_id() -> str:
    return "call-" + "".join(
        secrets.choice(CALL_ID_ALPHABET) for _ in range(CALL_ID_LENGTH)
    )


def project_key(project_dir: str | Path) -> str:
    canonical = str(Path(project_dir).expanduser().resolve())
    slug = re.sub(r"[^a-z0-9]+", "-", Path(canonical).name.lower()).strip("-") or "project"
    digest = hashlib.sha1(canonical.encode()).hexdigest()[:12]
    return f"{slug}-{digest}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _parse_transcript(lines: list[str]) -> list[TranscriptEntry]:
    return [
        TranscriptEntry.model_validate_json(line)
        for line in lines
        if line.strip()
    ]


class Store:
    def __init__(self, home: Path):
        self.home = home

    # -- paths -------------------------------------------------------------

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    @property
    def personalities_dir(self) -> Path:
        return self.home / "personalities"

    def project_box(self, project_dir: str | Path) -> Path:
        """Ensure and return the state directory for a project."""
        canonical = str(Path(project_dir).expanduser().resolve())
        box = self.projects_dir / project_key(canonical)
        (box / "calls").mkdir(parents=True, exist_ok=True)
        os.chmod(box, 0o700)
        marker = box / "project.json"
        if not marker.exists():
            _atomic_write(marker, json.dumps({"project_dir": canonical}))
        return box

    def call_dir(self, call_id: str) -> Path:
        """Global call_id -> directory lookup (glob scan; volumes are tiny)."""
        matches = list(self.projects_dir.glob(f"*/calls/{call_id}/call.json"))
        if not matches:
            raise CallNotFound(call_id)
        return matches[0].parent

    # -- call records --------------------------------------------------------

    def create_call(self, record: CallRecord) -> Path:
        box = self.project_box(record.project_dir)
        while (box / "calls" / record.call_id).exists():
            record.call_id = new_call_id()
        call_dir = box / "calls" / record.call_id
        call_dir.mkdir(parents=True)
        os.chmod(call_dir, 0o700)
        (call_dir / "call.lock").touch(mode=0o600)
        self.save_call(record)
        return call_dir

    def save_call(self, record: CallRecord) -> None:
        record.last_activity_at = utcnow()
        call_dir = self.project_box(record.project_dir) / "calls" / record.call_id
        _atomic_write(
            call_dir / "call.json",
            record.model_dump_json(by_alias=True, indent=2),
        )

    def load_call(self, call_id: str) -> CallRecord:
        call_dir = self.call_dir(call_id)
        return CallRecord.model_validate_json((call_dir / "call.json").read_text())

    def calls_for_project(self, project_dir: str | Path) -> list[CallRecord]:
        box = self.project_box(project_dir)
        records = []
        for meta in sorted(box.glob("calls/*/call.json")):
            records.append(CallRecord.model_validate_json(meta.read_text()))
        return records

    def all_calls(self) -> list[CallRecord]:
        """Return every call record in the store."""
        if not self.projects_dir.exists():
            return []
        records = []
        for meta in sorted(self.projects_dir.glob("*/calls/*/call.json")):
            records.append(CallRecord.model_validate_json(meta.read_text()))
        return records

    def delete_call(self, call_id: str) -> None:
        shutil.rmtree(self.call_dir(call_id))

    # -- transcripts ---------------------------------------------------------

    def append_transcript(self, call_id: str, entry: TranscriptEntry) -> TranscriptEntry:
        """Append one entry, assigning its seq atomically.

        Seq assignment and the write happen under an exclusive flock on the
        transcript file itself (not call.lock, which is not reentrant), so
        concurrent writers can never share a seq or interleave lines.
        """
        path = self.call_dir(call_id) / "transcript.jsonl"
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                transcript = _parse_transcript(fh.read().splitlines())
                entry.seq = transcript[-1].seq + 1 if transcript else 1
                fh.seek(0, os.SEEK_END)
                fh.write(entry.model_dump_json(by_alias=True) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        os.chmod(path, 0o600)
        return entry

    def read_transcript(self, call_id: str) -> list[TranscriptEntry]:
        path = self.call_dir(call_id) / "transcript.jsonl"
        if not path.exists():
            return []
        with open(path) as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                return _parse_transcript(fh.read().splitlines())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -- locking ---------------------------------------------------------------

    @contextmanager
    def call_lock(self, call_id: str, blocking: bool = True) -> Iterator[None]:
        """One writer per call. Non-blocking acquisition raises CallBusy."""
        lock_path = self.call_dir(call_id) / "call.lock"
        fh = open(lock_path, "w")
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fh, flags)
            except BlockingIOError:
                raise CallBusy(call_id) from None
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
