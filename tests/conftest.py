import os
import threading

import pytest

from tellmphone.adapters.base import AgentAdapter, AgentTurn, SessionLost, SpawnRequest
from tellmphone.config import Config
from tellmphone.store import Store
from tellmphone.switchboard import Switchboard


class FakeAdapter(AgentAdapter):
    """In-process stand-in for a CLI agent."""

    name = "fake"

    def __init__(self, lose_session: bool = False):
        self.lose_session = lose_session
        self.spawns: list[SpawnRequest] = []
        self.resumes: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.release: threading.Event | None = None

    def available(self) -> bool:
        return True

    def _start(self) -> None:
        self.started.set()
        if self.release is not None and not self.release.wait(timeout=2):
            raise TimeoutError("test did not release fake adapter")

    def spawn(self, req: SpawnRequest) -> AgentTurn:
        self._start()
        self.spawns.append(req)
        return AgentTurn(
            f"fake-sess-{len(self.spawns)}",
            f"spawn-reply to: {req.message}",
            usage={"turns": 1},
        )

    def resume(self, session_id: str, message: str, req: SpawnRequest) -> AgentTurn:
        if self.lose_session:
            raise SessionLost("session evaporated")
        self._start()
        self.resumes.append((session_id, message))
        return AgentTurn(session_id, f"resume-reply to: {message}", usage={"turns": 1})


@pytest.fixture
def home(tmp_path):
    return tmp_path / "tellmphone-home"


@pytest.fixture
def store(home):
    return Store(home)


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


@pytest.fixture
def fake_adapter():
    return FakeAdapter()


@pytest.fixture
def boards(home, store, fake_adapter):
    """Two switchboards over the same store: 'claude' (caller) and 'fake' (callee)."""
    adapters = {"fake": fake_adapter, "claude": FakeAdapter()}
    adapters["claude"].name = "claude"
    caller = Switchboard(
        Config(i_am="claude", home=home, timeout_s=30),
        store,
        adapters,
        detach_turns=False,
    )
    callee = Switchboard(
        Config(i_am="fake", home=home, timeout_s=30),
        store,
        adapters,
        detach_turns=False,
    )
    return caller, callee


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """Install fake CLI executables onto PATH; returns install(name, script)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # keep adapters away from the user's real ~/.codex/config.toml
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    gemini_home = tmp_path / "gemini-home"
    gemini_home.mkdir()
    monkeypatch.setenv("GEMINI_HOME", str(gemini_home))
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("TELLMPHONE_HOME", str(tmp_path / "tellmphone-home"))

    def install(name: str, script: str):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
        return path

    return install
