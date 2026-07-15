"""Codex adapter: `codex exec` headless mode with `codex exec resume`.

Codex has no system-prompt flag in exec mode, so personalities are injected
as a framed preamble on the first message. The session id is fished out of
the --json event stream defensively (event schemas have shifted across codex
releases: session_configured/session_id vs thread.started/thread_id), and the
final answer is read from --output-last-message, which is the stable
interface for exactly this purpose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

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

_SESSION_KEYS = ("session_id", "thread_id", "conversation_id")
_ERROR_EVENT_TYPES = {"error", "turn.failed"}

# Passable `codex exec --model` / `-m` slugs (ChatGPT-auth Codex, mid-2026).
# Descriptions are TeLLMphone *call* routing (second opinions + personalities).
# Probed: gpt-5.6-{sol,terra,luna}, gpt-5.5, gpt-5.4, gpt-5.4-mini,
# gpt-5.3-codex-spark. Bare `gpt-5.6` rejected. codex-auto-review is guardian-only.
# Preferred independent review vendor when the caller is Claude (hostile roles).
_MODELS = (
    ModelInfo(
        "gpt-5.6-terra",
        "Default Codex callee for routine grumpy-reviewer, sycophancy-cop, "
        "evidence-engineer, test-engineer, the-algorithm, and neutral second "
        "opinions. Prefer when the caller is Claude and needs a hostile "
        "review (personality supplies the adversarial stance; model "
        "choice alone does not). Use Sol for security-sensitive changes, "
        "risky diffs, ambiguous cross-system bugs, or devils-advocate on a "
        "high-stakes plan; use Luna or Spark for rubber-duck and tiny-hacker.",
        default=True,
    ),
    ModelInfo(
        "gpt-5.6-sol",
        "Hard Codex callee: security-auditor, grumpy-reviewer on risky or "
        "security-impacting diffs, devils-advocate, debugger on ambiguous "
        "systems bugs. Highest cost — pick on pre-call risk (auth boundaries, "
        "irreversible migration, multi-service failure modes), not after a "
        "weak Terra answer. Strong on systems/backend review and instruction "
        "fidelity; do not pick Sol solely for UI polish. Not for architect: "
        "route hard design judgment to claude opus or fable.",
    ),
    ModelInfo(
        "gpt-5.6-luna",
        "Cheap Codex leaf for phone calls. Prefer for rubber-duck and "
        "tiny-hacker (smallest runnable proof). Not for security-auditor, "
        "grumpy-reviewer, architect, or sycophancy-cop.",
    ),
    ModelInfo(
        "gpt-5.5",
        "Previous frontier. Use only if 5.6 slugs are unavailable; same call "
        "roles as Sol/Terra when you must pin a legacy pipeline.",
    ),
    ModelInfo(
        "gpt-5.4",
        "Legacy mid-tier. Prefer gpt-5.6-terra for new TeLLMphone calls.",
    ),
    ModelInfo(
        "gpt-5.4-mini",
        "Legacy cheap leaf. Prefer gpt-5.6-luna for rubber-duck / tiny-hacker.",
    ),
    ModelInfo(
        "gpt-5.3-codex-spark",
        "Ultra-fast implementer for tiny-hacker and quick mechanical edits "
        "after another model or plan already decided what to build. Weak for "
        "review, security, architect, or devils-advocate. Often plan-gated.",
    ),
)


def _find_session_id(event: object) -> str | None:
    """Recursively hunt for a session-id-ish key in a decoded JSON event."""
    if isinstance(event, dict):
        for key in _SESSION_KEYS:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        for value in event.values():
            found = _find_session_id(value)
            if found:
                return found
    elif isinstance(event, list):
        for item in event:
            found = _find_session_id(item)
            if found:
                return found
    return None


def _codex_error_detail(stdout: str) -> str | None:
    """Extract a useful failure from codex's JSON event stream."""
    messages = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in _ERROR_EVENT_TYPES:
            continue
        error = event.get("error")
        message = event.get("message")
        if isinstance(error, dict):
            message = error.get("message") or message
        elif isinstance(error, str):
            message = error
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return messages[-1] if messages else None


class CodexAdapter(AgentAdapter):
    name = "codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    def models(self) -> list[ModelInfo]:
        return list(_MODELS)

    def register_mcp(self, server_argv: list[str]) -> str:
        result = cli_register(
            "codex",
            ["add", SERVER_NAME, "--", *server_argv],
            ["remove", SERVER_NAME],
            server_argv,
        )
        self._approve_tools()
        return result + " (tools auto-approved)"

    def _approve_tools(self) -> None:
        """Set default_tools_approval_mode = "approve" for our server.

        Without it, codex's non-interactive exec mode auto-rejects every MCP
        tool call as "user cancelled". `codex mcp add` has no flag for this,
        so we patch config.toml directly (insert-after-header keeps the rest
        of the user's file byte-identical).
        """
        path = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
        if not path.exists():
            return
        text = path.read_text()
        entry = tomllib.loads(text).get("mcp_servers", {}).get(SERVER_NAME, {})
        current = entry.get("default_tools_approval_mode")
        if current == "approve":
            return
        lines = text.splitlines()
        header = f"[mcp_servers.{SERVER_NAME}]"
        for i, line in enumerate(lines):
            if line.strip() != header:
                continue
            if current is not None:
                # key exists with a wrong value somewhere in this table; fix it
                for j in range(i + 1, len(lines)):
                    if lines[j].strip().startswith("["):
                        break
                    if lines[j].split("=")[0].strip() == "default_tools_approval_mode":
                        lines[j] = 'default_tools_approval_mode = "approve"'
                        break
            else:
                lines.insert(i + 1, 'default_tools_approval_mode = "approve"')
            path.write_text("\n".join(lines) + "\n")
            return

    def unregister_mcp(self) -> str:
        subprocess.run(
            ["codex", "mcp", "remove", SERVER_NAME],
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        return f"codex: removed '{SERVER_NAME}' (if it was registered)"

    def spawn(self, req: SpawnRequest) -> AgentTurn:
        prompt = req.message
        if req.personality and req.personality.body:
            prompt = framed_personality_preamble(req.personality) + prompt
        return self._run(["codex", "exec"], prompt, req)

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        try:
            turn = self._run(
                ["codex", "exec", "resume", session_id], message, req, resuming=True
            )
        except AdapterError as exc:
            blob = str(exc).lower()
            if "not found" in blob or "no session" in blob or "no rollout" in blob:
                raise SessionLost(str(exc)) from exc
            raise
        # resume may not re-emit the id; keep the one we had
        if not turn.session_id:
            turn.session_id = session_id
        return turn

    def _run(
        self, base_cmd: list[str], prompt: str, req: SpawnRequest, resuming: bool = False
    ) -> AgentTurn:
        with tempfile.NamedTemporaryFile(
            mode="r", suffix=".txt", prefix="tellmphone-last-"
        ) as last_msg:
            cmd = base_cmd + [
                "--json",
                "--skip-git-repo-check",
                "--output-last-message", last_msg.name,
            ]
            # Codex resume does not reliably propagate the parent process's
            # dynamic call context to stdio MCP servers.  Pin it in the MCP
            # server config for this process so report_progress keeps working.
            if req.call_id:
                cmd += [
                    "--config",
                    "mcp_servers.tellmphone.env.TELLMPHONE_CALL_ID="
                    + json.dumps(req.call_id),
                    "--config",
                    "mcp_servers.tellmphone.env.TELLMPHONE_HOP="
                    + json.dumps(str(req.hop_count)),
                ]
            if not resuming:
                # cwd and sandbox are session properties: settable at spawn,
                # rejected (and inherited) on `exec resume`
                sandbox = "workspace-write" if req.write_access else "read-only"
                cmd += ["--cd", req.project_dir, "--sandbox", sandbox]
            if req.model:
                cmd += ["--model", req.model]
            cmd.append(prompt)
            proc = subprocess.run(
                cmd,
                env=self.child_env(req),
                # never inherit stdin: in server mode it's the MCP transport,
                # and codex appends a piped stdin to the prompt
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                detail = (
                    _codex_error_detail(proc.stdout)
                    or proc.stderr.strip()
                    or proc.stdout.strip()
                )[:500]
                raise AdapterError(f"codex exited {proc.returncode}: {detail}")

            session_id = None
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = _find_session_id(event)
                if session_id:
                    break

            text = Path(last_msg.name).read_text().strip()
            if not text:
                raise AdapterError("codex finished but produced no final message")

            return AgentTurn(session_id=session_id, text=text)
