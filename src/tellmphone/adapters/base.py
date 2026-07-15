"""Adapter interface: subprocess wranglers, nothing more (docs/DESIGN.md §8).

An adapter knows how to spawn one kind of agent CLI headlessly, resume one of
its sessions, and extract (session_id, response_text) from its output. All
threading, state, and personality logic lives in the switchboard.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points

from tellmphone.config import CALL_ID_ENV, HOP_ENV
from tellmphone.personalities import Personality


SERVER_NAME = "tellmphone"

_HOST_CLAUDE_MARKERS = {
    "CLAUDECODE",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
}


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ minus the host agent's session markers.

    Claude Code refuses to start when it detects it is nested inside another
    Claude Code process via these variables - and they describe the host, not
    the child, so they are lies in any subprocess we spawn. Other
    CLAUDE_CODE_* variables can be real auth, provider, or gateway config and
    must pass through.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _HOST_CLAUDE_MARKERS
    }
    if extra:
        env.update(extra)
    return env


class AdapterError(Exception):
    """The callee CLI failed in a way that isn't a lost session."""


class SessionLost(AdapterError):
    """The native session can't be resumed; switchboard falls back to replay."""


@dataclass
class SpawnRequest:
    message: str
    project_dir: str
    personality: Personality | None = None
    model: str | None = None  # passed through to the CLI; the CLI validates it
    write_access: bool = False
    hop_count: int = 1
    call_id: str | None = None


@dataclass
class AgentTurn:
    session_id: str | None
    text: str
    usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    """A model id the CLI accepts via --model, with guidance for callers.

    `id` must be a string safe to pass as the CLI's model flag. Catalogs are
    hardcoded and periodically refreshed — prefer what `cli models` / probes
    accept over marketing names.

    `description` is TeLLMphone call routing: which personalities and second-
    opinion shapes fit this model, when to escalate tier, and when to prefer
    another vendor (not generic IDE marketing copy).
    """

    id: str
    description: str
    # Exactly one entry per adapter should be True: the suggested pick when
    # the caller has no preference and TeLLMphone has no configured_model.
    default: bool = False


class AgentAdapter(ABC):
    name: str = ""

    @abstractmethod
    def available(self) -> bool:
        """Is the CLI installed and runnable?"""

    @abstractmethod
    def spawn(self, req: SpawnRequest) -> AgentTurn:
        """First message of a call: start a fresh session, return its id."""

    @abstractmethod
    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        """Follow-up on an existing session. Raises SessionLost if it's gone."""

    def models(self) -> list[ModelInfo]:
        """Passable --model values for this CLI (hardcoded, periodically refreshed).

        Callers use this via phonebook to pick a valid model without guessing.
        Empty means TeLLMphone has no catalog; omit `model` on call() and let
        the CLI choose.
        """
        return []

    def catalog_default_model(self) -> str | None:
        """Id marked default in models(), if any."""
        for model in self.models():
            if model.default:
                return model.id
        return None

    def child_env(self, req: SpawnRequest) -> dict[str, str]:
        """Scrubbed environment plus the hop count, so a callee's own
        TeLLMphone refuses runaway chains."""
        extra = {HOP_ENV: str(req.hop_count)}
        if req.call_id:
            extra[CALL_ID_ENV] = req.call_id
        return scrubbed_env(extra)

    def register_mcp(self, server_argv: list[str]) -> str:
        """Register the TeLLMphone MCP server with this agent's CLI.

        `server_argv` is the full serve command (including --i-am). Returns a
        one-line human-readable result; raises AdapterError on failure. The
        default says "do it by hand" so third-party adapters without a
        registration CLI still work with `tellmphone install`.
        """
        raise NotImplementedError(
            f"{self.name}: no automatic registration; add this command to the "
            f"agent's MCP config manually: {' '.join(server_argv)}"
        )

    def unregister_mcp(self) -> str:
        raise NotImplementedError(f"{self.name}: remove the MCP entry manually")


def cli_register(
    cli: str, add_args: list[str], remove_args: list[str], server_argv: list[str]
) -> str:
    """Shared `<cli> mcp add` flow: add, and on 'already exists' replace."""
    import subprocess

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [cli, "mcp", *args],
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )

    proc = run(add_args)
    if proc.returncode != 0 and "already exists" in (proc.stderr + proc.stdout).lower():
        run(remove_args)
        proc = run(add_args)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:300]
        raise AdapterError(f"{cli} mcp add failed: {detail}")
    return f"{cli}: registered '{SERVER_NAME}' -> {' '.join(server_argv)}"


def framed_personality_preamble(personality: Personality) -> str:
    """Personality injection for CLIs without a system-prompt flag."""
    return (
        f"<persona name={personality.name!r}>\n"
        f"Adopt the following persona for this entire conversation:\n\n"
        f"{personality.body}\n"
        f"</persona>\n\n"
    )


def load_adapters() -> dict[str, AgentAdapter]:
    """Built-in adapters, overridable/extendable via entry points."""
    from tellmphone.adapters.claude import ClaudeAdapter
    from tellmphone.adapters.codex import CodexAdapter
    from tellmphone.adapters.gemini import GeminiAdapter
    from tellmphone.adapters.grok import GrokAdapter

    adapters: dict[str, AgentAdapter] = {
        ClaudeAdapter.name: ClaudeAdapter(),
        CodexAdapter.name: CodexAdapter(),
        GeminiAdapter.name: GeminiAdapter(),
        GrokAdapter.name: GrokAdapter(),
    }
    for ep in entry_points(group="tellmphone.adapters"):
        try:
            cls = ep.load()
            adapters[ep.name] = cls()
        except Exception:
            continue  # a broken third-party adapter shouldn't kill the phone
    return adapters
