"""Gemini Antigravity CLI adapter: `agy --print` headless mode."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from tellmphone.adapters.base import (
    SERVER_NAME,
    AdapterError,
    AgentAdapter,
    AgentTurn,
    SessionLost,
    SpawnRequest,
    framed_personality_preamble,
)


class GeminiAdapter(AgentAdapter):
    name = "gemini"

    def available(self) -> bool:
        return shutil.which("agy") is not None

    def register_mcp(self, server_argv: list[str]) -> str:
        path = self._mcp_config_path()
        data = self._read_mcp_config(path)
        servers = data.setdefault("mcpServers", {})
        servers[SERVER_NAME] = {
            "command": server_argv[0],
            "args": server_argv[1:],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return f"gemini: registered '{SERVER_NAME}' -> {' '.join(server_argv)}"

    def unregister_mcp(self) -> str:
        path = self._mcp_config_path()
        if path.exists():
            data = self._read_mcp_config(path)
            servers = data.get("mcpServers")
            if isinstance(servers, dict) and SERVER_NAME in servers:
                del servers[SERVER_NAME]
                path.write_text(json.dumps(data, indent=2) + "\n")
        return f"gemini: removed '{SERVER_NAME}' (if it was registered)"

    def spawn(self, req: SpawnRequest) -> AgentTurn:
        prompt = req.message
        if req.personality and req.personality.body:
            prompt = framed_personality_preamble(req.personality) + prompt
        return self._run(["agy", "--print"], prompt, req)

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        cmd = ["agy", "--conversation", session_id, "--print"]
        try:
            turn = self._run(cmd, message, req, resuming=True)
        except AdapterError as exc:
            blob = str(exc).lower()
            if (
                "not found" in blob
                or "invalid conversation" in blob
                or "does not exist" in blob
            ):
                raise SessionLost(str(exc)) from exc
            raise
        if not turn.session_id:
            turn.session_id = session_id
        return turn

    def _run(
        self,
        base_cmd: list[str],
        prompt: str,
        req: SpawnRequest,
        resuming: bool = False,
    ) -> AgentTurn:
        cmd = list(base_cmd)
        if req.model:
            cmd += ["--model", req.model]
        cmd.append("--sandbox")
        if req.write_access:
            cmd.append("--dangerously-skip-permissions")
        cmd.append(prompt)

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
            raise AdapterError(f"agy exited {proc.returncode}: {detail}")

        # agy currently dumps the assistant response to stdout.
        text = proc.stdout.strip()
        if not text:
            raise AdapterError("agy finished but produced no output")

        # agy --print does not expose a stable conversation id today.
        # Returning None makes the switchboard continue via transcript replay.
        session_id = None

        return AgentTurn(session_id=session_id, text=text)

    def _mcp_config_path(self) -> Path:
        root = Path(os.environ.get("GEMINI_HOME", "~/.gemini")).expanduser()
        return root / "config" / "mcp_config.json"

    def _read_mcp_config(self, path: Path) -> dict:
        text = path.read_text() if path.exists() else ""
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"gemini MCP config is invalid JSON: {path}") from exc
        if not isinstance(data, dict):
            raise AdapterError(f"gemini MCP config must be a JSON object: {path}")
        return data
