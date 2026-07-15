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
    ModelInfo,
    SessionLost,
    SpawnRequest,
    cli_register,
    scrubbed_env,
)

_USAGE_KEYS = ("total_cost_usd", "duration_ms", "num_turns")
_TELLMPHONE_TOOLS = f"mcp__{SERVER_NAME}__*"

# Passable `claude --model` values (aliases preferred; resolve per account).
# Descriptions are TeLLMphone *call* routing (second opinions + personalities),
# not daily-IDE defaults. Probed aliases: sonnet/opus/haiku/best/opusplan/*[1m].
# `fable` may be account-gated; `claude-fable-5` is the explicit pin if rejected.
_MODELS = (
    ModelInfo(
        "sonnet",
        "Default Claude callee for transaction-cost-accountant, test-engineer, "
        "neutral second opinions, and UI/CSS or design-taste questions. Use "
        "opus or fable for architecture, ambiguous root-cause analysis, or "
        "subtle cross-system behavior. If the caller is Claude and needs "
        "hostile review, prefer codex terra/sol with grumpy-reviewer, "
        "sycophancy-cop, or devils-advocate.",
        default=True,
    ),
    ModelInfo(
        "opus",
        "Reliable hard Claude tier: architect, debugger (ambiguous root-cause), "
        "deep design trade-offs, subtle multi-system bugs. Use when the call "
        "needs high judgment density and you want a known available hard pin. "
        "Fable is the max-capability alternative where the account allows it.",
    ),
    ModelInfo(
        "haiku",
        "Cheap short-form Claude callee. Prefer for rubber-duck (Socratic, "
        "must not over-solve) and tiny mechanical asks. Not for grumpy-reviewer, "
        "security-auditor, architect, or any role that needs ranked defects.",
    ),
    ModelInfo(
        "fable",
        "Max Claude hard tier alongside opus: architect, hardest debugger, "
        "strategy-then-hand-off, longest-horizon design critique. Expensive and "
        "account-gated — if the fable alias is rejected, pass claude-fable-5. "
        "Prefer over sonnet only for true frontier-judgment calls.",
    ),
    ModelInfo(
        "claude-fable-5",
        "Pinned Fable 5 id when `fable` is rejected or you need a reproducible "
        "frontier pin. Same call roles as fable/opus (architect, deep debugger, "
        "high-stakes design sparring). Account-gated.",
    ),
    ModelInfo(
        "best",
        "Account-resolved strongest alias — may resolve to Opus or Fable "
        "depending on plan/account; do not assume which. Use only when you want "
        "max available without naming a family. For a guaranteed Fable-tier "
        "call, name fable or claude-fable-5; for a reliable hard pin, use opus.",
    ),
    ModelInfo(
        "opusplan",
        "Hybrid: Opus while planning, Sonnet while implementing. Use for an "
        "architect or neutral call that must turn a design decision into a "
        "draft patch. For planning-only calls needing the strongest Claude "
        "judgment, choose fable or opus directly (fable is max-capability). "
        "Not a substitute for grumpy-reviewer.",
    ),
    ModelInfo(
        "sonnet[1m]",
        "Sonnet with explicit 1M context for huge monorepos or long call "
        "transcripts. Same personality fits as sonnet; use when the working set "
        "is the bottleneck (often a no-op if Sonnet is already 1M).",
    ),
    ModelInfo(
        "opus[1m]",
        "Opus with explicit 1M context for architect/debugger calls that need a "
        "very large working set. Same hard-call roles as opus/fable; pick fable "
        "or claude-fable-5 if you need max capability instead of long context.",
    ),
)


class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def models(self) -> list[ModelInfo]:
        return list(_MODELS)

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
        flags += [
            "--permission-mode",
            mode,
            "--allowedTools",
            _TELLMPHONE_TOOLS,
        ]
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
