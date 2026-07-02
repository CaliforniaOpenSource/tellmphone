"""Global configuration: ~/.tellmphone/config.toml plus process environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOP_ENV = "TELLMPHONE_HOP"
HOME_ENV = "TELLMPHONE_HOME"

DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_HOPS = 2


def tellmphone_home() -> Path:
    return Path(os.environ.get(HOME_ENV, "~/.tellmphone")).expanduser()


@dataclass
class ProjectPermissions:
    write: bool = False


@dataclass
class AgentDefaults:
    model: str | None = None


@dataclass
class Config:
    i_am: str
    home: Path = field(default_factory=tellmphone_home)
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_hops: int = DEFAULT_MAX_HOPS
    # Keyed by canonical project path. Only humans edit this (config.toml);
    # callers cannot escalate a callee's permissions through tool arguments.
    permissions: dict[str, ProjectPermissions] = field(default_factory=dict)
    # Per-agent defaults ([agents.<name>] in config.toml), e.g. a default model.
    agents: dict[str, AgentDefaults] = field(default_factory=dict)
    hop_count: int = 0

    def permissions_for(self, project_dir: str | Path) -> ProjectPermissions:
        canonical = str(Path(project_dir).expanduser().resolve())
        return self.permissions.get(canonical, ProjectPermissions())

    def default_model_for(self, agent: str) -> str | None:
        return self.agents.get(agent, AgentDefaults()).model


def load_config(i_am: str, home: Path | None = None) -> Config:
    home = home or tellmphone_home()
    cfg = Config(i_am=i_am, home=home)
    cfg.hop_count = int(os.environ.get(HOP_ENV, "0"))

    path = home / "config.toml"
    if path.exists():
        data = tomllib.loads(path.read_text())
        defaults = data.get("defaults", {})
        cfg.timeout_s = int(defaults.get("timeout_s", cfg.timeout_s))
        cfg.max_hops = int(defaults.get("max_hops", cfg.max_hops))
        for raw_path, perms in data.get("permissions", {}).items():
            canonical = str(Path(raw_path).expanduser().resolve())
            cfg.permissions[canonical] = ProjectPermissions(
                write=bool(perms.get("write", False))
            )
        for agent, defaults in data.get("agents", {}).items():
            cfg.agents[agent] = AgentDefaults(model=defaults.get("model"))
    return cfg
