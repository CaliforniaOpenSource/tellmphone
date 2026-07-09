"""Grok adapter: `grok -p` headless mode with named sessions."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from typing import Any

from tellmphone.adapters.base import (
    SERVER_NAME,
    AdapterError,
    AgentAdapter,
    AgentTurn,
    SessionLost,
    SpawnRequest,
    cli_register,
    framed_personality_preamble,
    scrubbed_env,
)

_TEXT_KEYS = ("text", "response", "result", "output", "message", "content")
_SESSION_LOST_MARKERS = ("not found", "no session", "unknown session")


def _last_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise AdapterError("grok finished but produced no output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            raise AdapterError(f"grok produced non-JSON output: {stdout[:500]!r}")
    if not isinstance(data, dict):
        raise AdapterError(f"grok JSON output was not an object: {stdout[:500]!r}")
    return data


def _extract_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        text = "".join(part for part in parts if part)
        return text or None
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            text = _extract_text(value.get(key))
            if text:
                return text
    return None


class GrokAdapter(AgentAdapter):
    name = "grok"

    def available(self) -> bool:
        return shutil.which("grok") is not None

    def register_mcp(self, server_argv: list[str]) -> str:
        return cli_register(
            "grok",
            ["add", SERVER_NAME, "--", *server_argv],
            ["remove", SERVER_NAME],
            server_argv,
        )

    def unregister_mcp(self) -> str:
        subprocess.run(
            ["grok", "mcp", "remove", SERVER_NAME],
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        return f"grok: removed '{SERVER_NAME}' (if it was registered)"

    def spawn(self, req: SpawnRequest) -> AgentTurn:
        prompt = req.message
        if req.personality and req.personality.body:
            prompt = framed_personality_preamble(req.personality) + prompt
        session_id = str(uuid.uuid4())
        turn = self._run(["--session-id", session_id], prompt, req)
        if not turn.session_id:
            turn.session_id = session_id
        return turn

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        try:
            turn = self._run(["--resume", session_id], message, req)
        except AdapterError as exc:
            if any(marker in str(exc).lower() for marker in _SESSION_LOST_MARKERS):
                raise SessionLost(str(exc)) from exc
            raise
        if not turn.session_id:
            turn.session_id = session_id
        return turn

    def _run(
        self,
        session_flags: list[str],
        prompt: str,
        req: SpawnRequest,
    ) -> AgentTurn:
        sandbox = "workspace" if req.write_access else "read-only"
        permission_mode = "acceptEdits" if req.write_access else "dontAsk"
        cmd = [
            "grok",
            "--no-auto-update",
            "-p",
            prompt,
            "--cwd",
            req.project_dir,
            "--output-format",
            "json",
            "--no-alt-screen",
            "--sandbox",
            sandbox,
            "--permission-mode",
            permission_mode,
            *session_flags,
        ]
        if req.model:
            cmd += ["--model", req.model]

        proc = subprocess.run(
            cmd,
            cwd=req.project_dir,
            env=self.child_env(req),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
            raise AdapterError(f"grok exited {proc.returncode}: {detail}")

        data = _last_json(proc.stdout)
        text = _extract_text(data)
        if not text:
            raise AdapterError("grok finished but produced no final message")

        session_id = data.get("session_id") or data.get("sessionId")
        return AgentTurn(
            session_id=session_id if isinstance(session_id, str) else None,
            text=text,
        )
