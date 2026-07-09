"""Claude Code adapter: `claude -p` headless mode with `--resume`."""

from __future__ import annotations

import json
import shutil
import subprocess

from tellmphone.adapters.base import (
    SERVER_NAME,
    AdapterError,
    AgentAdapter,
    AgentTurn,
    SessionLost,
    SpawnRequest,
    cli_register,
    scrubbed_env,
)

_USAGE_KEYS = ("total_cost_usd", "duration_ms", "num_turns")


class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def register_mcp(self, server_argv: list[str]) -> str:
        return cli_register(
            "claude",
            ["add", SERVER_NAME, "--scope", "user", "--", *server_argv],
            ["remove", SERVER_NAME, "--scope", "user"],
            server_argv,
        )

    def unregister_mcp(self) -> str:
        subprocess.run(
            ["claude", "mcp", "remove", SERVER_NAME, "--scope", "user"],
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        return f"claude: removed '{SERVER_NAME}' (if it was registered)"

    def spawn(self, req: SpawnRequest) -> AgentTurn:
        cmd = ["claude", "-p", req.message, "--output-format", "json"]
        if req.personality and req.personality.body:
            cmd += ["--append-system-prompt", req.personality.body]
        return self._run(cmd + self._common_flags(req), req)

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        cmd = [
            "claude", "-p", message,
            "--output-format", "json",
            "--resume", session_id,
        ]
        return self._run(cmd + self._common_flags(req), req, resuming=True)

    def _common_flags(self, req: SpawnRequest) -> list[str]:
        flags = []
        if req.model:
            flags += ["--model", req.model]
        mode = "acceptEdits" if req.write_access else "dontAsk"
        flags += ["--permission-mode", mode]
        return flags

    def _run(self, cmd: list[str], req: SpawnRequest, resuming: bool = False) -> AgentTurn:
        proc = subprocess.run(
            cmd,
            cwd=req.project_dir,
            env=self.child_env(req),
            # never inherit stdin: in server mode it's the MCP transport,
            # and both CLIs read a piped stdin as extra prompt input
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            blob = (proc.stderr + proc.stdout).lower()
            # claude reports many failures on stdout, not stderr
            detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
            if resuming and ("no conversation" in blob or "not found" in blob):
                raise SessionLost(f"claude session gone: {detail}")
            raise AdapterError(f"claude exited {proc.returncode}: {detail}")

        try:
            data = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"claude produced non-JSON output: {proc.stdout[:500]!r}"
            ) from exc

        if data.get("is_error"):
            raise AdapterError(f"claude reported an error: {data.get('result', '')[:500]}")

        return AgentTurn(
            session_id=data.get("session_id"),
            text=data.get("result", ""),
            usage={k: data[k] for k in _USAGE_KEYS if k in data},
        )
