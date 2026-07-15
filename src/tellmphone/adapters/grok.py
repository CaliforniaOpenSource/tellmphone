"""Grok adapter: `grok -p` headless mode with named sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from tellmphone.adapters.base import (
    SERVER_NAME,
    AdapterError,
    AgentAdapter,
    AgentTurn,
    ModelInfo,
    SessionLost,
    SpawnRequest,
    cli_register,
    framed_personality_preamble,
    scrubbed_env,
)
from tellmphone.config import HOME_ENV

_TEXT_KEYS = ("text", "response", "result", "output", "message", "content")
_SESSION_LOST_MARKERS = ("not found", "no session", "unknown session")

# Only ids from `grok models` (probed: grok-build / grok-4.3 rejected as unknown).
# Descriptions are TeLLMphone *call* routing (second opinions + personalities).
_MODELS = (
    ModelInfo(
        "grok-4.5",
        "Default Grok callee for the-algorithm, neutral second opinions, and "
        "open-ended critique of an under-specified plan. Provides an independent "
        "run; the selected personality must supply the adversarial stance. "
        "Optional third culture for devils-advocate — if the caller is Claude "
        "and needs hostile review, prefer codex terra/sol first. Prefer over "
        "Composer for judgment roles; use codex Sol for security/high-risk "
        "review and claude opus/fable for hard architecture and deep debugging.",
        default=True,
    ),
    ModelInfo(
        "grok-composer-2.5-fast",
        "Fast edit specialist for tiny-hacker, rubber-duck, and quick mechanical "
        "edits after another model or plan already decided what to build. Not "
        "for grumpy-reviewer, security-auditor, architect, sycophancy-cop, or "
        "devils-advocate.",
    ),
)
_SANDBOX_BEGIN = "# >>> tellmphone managed sandbox profiles >>>"
_SANDBOX_END = "# <<< tellmphone managed sandbox profiles <<<"
_SANDBOX_BLOCK_RE = re.compile(
    rf"\n?{re.escape(_SANDBOX_BEGIN)}.*?{re.escape(_SANDBOX_END)}\n?",
    re.DOTALL,
)


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

    def models(self) -> list[ModelInfo]:
        return list(_MODELS)

    def register_mcp(self, server_argv: list[str]) -> str:
        result = cli_register(
            "grok",
            ["add", SERVER_NAME, "--", *server_argv],
            ["remove", SERVER_NAME],
            server_argv,
        )
        self._install_sandbox_profiles()
        return result + " (sandbox profiles installed)"

    def unregister_mcp(self) -> str:
        subprocess.run(
            ["grok", "mcp", "remove", SERVER_NAME],
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        self._remove_sandbox_profiles()
        return f"grok: removed '{SERVER_NAME}' (if it was registered)"

    @staticmethod
    def _sandbox_config_path() -> Path:
        return Path(os.environ.get("GROK_HOME", "~/.grok")).expanduser() / "sandbox.toml"

    def _install_sandbox_profiles(self) -> None:
        path = self._sandbox_config_path()
        text = path.read_text() if path.exists() else ""
        text = _SANDBOX_BLOCK_RE.sub("\n", text).rstrip()
        state_dir = str(Path(os.environ.get(HOME_ENV, "~/.tellmphone")).expanduser().resolve())
        block = (
            f"{_SANDBOX_BEGIN}\n"
            "[profiles.tellmphone-read-only]\n"
            'extends = "read-only"\n'
            f"read_write = [{json.dumps(state_dir)}]\n\n"
            "[profiles.tellmphone-workspace]\n"
            'extends = "workspace"\n'
            f"read_write = [{json.dumps(state_dir)}]\n"
            f"{_SANDBOX_END}\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((text + "\n\n" if text else "") + block)

    def _remove_sandbox_profiles(self) -> None:
        path = self._sandbox_config_path()
        if not path.exists():
            return
        text = _SANDBOX_BLOCK_RE.sub("\n", path.read_text()).strip()
        if text:
            path.write_text(text + "\n")
        else:
            path.unlink()

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
        sandbox = "tellmphone-workspace" if req.write_access else "tellmphone-read-only"
        # Grok headless mode cancels tools under dontAsk/acceptEdits because
        # there is no interactive approval surface. `auto` lets the turn use
        # tools while the sandbox remains the filesystem security boundary.
        permission_mode = "auto"
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
