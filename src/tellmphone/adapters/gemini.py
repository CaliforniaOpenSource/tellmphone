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
    ModelInfo,
    SessionLost,
    SpawnRequest,
    framed_personality_preamble,
)

# Exact `agy --model` display names from `agy models` (slug-style ids rejected).
# Descriptions are TeLLMphone *call* routing (second opinions + personalities).
# Low/Medium/High are effort on the same family. Flash is the Antigravity default.
_MODELS = (
    ModelInfo(
        "Gemini 3.5 Flash (Medium)",
        "Default Gemini/agy callee for phone calls. Good neutral third voice and "
        "general sparring under a clear ask; start here before High/Pro. Prefer "
        "codex terra/sol for hostile reviews (grumpy-reviewer, sycophancy-cop, "
        "devils-advocate when the caller is Claude) and claude opus/fable for "
        "architect when you need max design judgment.",
        default=True,
    ),
    ModelInfo(
        "Gemini 3.5 Flash (Low)",
        "Quota-saver for short calls. Prefer for rubber-duck and tiny mechanical "
        "asks. Use Medium/High only when the task is multi-file or needs more "
        "careful reasoning.",
    ),
    ModelInfo(
        "Gemini 3.5 Flash (High)",
        "Higher-effort Flash for multi-file debugger or test-engineer style "
        "calls when Gemini is the available provider. Quota-heavy on Pro plans — "
        "deliberate pick, not silent default.",
    ),
    ModelInfo(
        "Gemini 3.1 Pro (Low)",
        "Pro-class with light thinking tax for evidence-engineer or careful "
        "review when you want more steadiness than Flash without max burn.",
    ),
    ModelInfo(
        "Gemini 3.1 Pro (High)",
        "Deep Gemini option for a second opinion on a complex diff, architecture "
        "proposal, or ambiguous debugging evidence when Gemini is the available "
        "provider. Pair with grumpy-reviewer, architect, or debugger. For the "
        "strongest Claude design path, choose opus or fable; for hostile review "
        "when the caller is Claude, prefer codex terra/sol. Shares Gemini quota "
        "with Flash and burns faster.",
    ),
    ModelInfo(
        "Claude Sonnet 4.6 (Thinking)",
        "Claude-via-agy Sonnet: transaction-cost-accountant, careful review, "
        "docs, plan-then-hand-off when you only have agy. Often Ultra-gated. "
        "Prefer native claude sonnet when the Claude CLI is installed.",
    ),
    ModelInfo(
        "Claude Opus 4.6 (Thinking)",
        "Claude-via-agy hard tier: architect, deep debugger, high-stakes design "
        "trade-offs — same call roles as native opus/fable. Scarce quota; not for "
        "rubber-duck or renames. Prefer native claude opus or fable when available.",
    ),
    ModelInfo(
        "GPT-OSS 120B (Medium)",
        "Third opinion with no shared training lineage to Gemini or Claude — "
        "use for neutral or the-algorithm when Gemini/Claude buckets are tight "
        "and you want a different blind-spot profile. Often Ultra-gated. Do not "
        "use for tiny-hacker (prefer luna/spark/composer) or security-auditor.",
    ),
)


class GeminiAdapter(AgentAdapter):
    name = "gemini"

    def available(self) -> bool:
        return shutil.which("agy") is not None

    def models(self) -> list[ModelInfo]:
        return list(_MODELS)

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
        return self._run(["agy"], prompt, req)

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        cmd = ["agy", "--conversation", session_id]
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
        cmd += ["--add-dir", req.project_dir]
        cmd.append("--sandbox")
        cmd += ["--mode", "accept-edits" if req.write_access else "plan"]
        cmd += ["--print", prompt]

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
