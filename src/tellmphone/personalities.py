"""Personalities: named, human-curated system prompts (docs/DESIGN.md §7).

A personality is a Markdown file with YAML-ish frontmatter:

    ---
    name: grumpy-reviewer
    description: Hostile-but-fair senior reviewer.
    agents: [claude, codex]
    ---
    You are a grumpy but rigorous senior engineer...

There is deliberately no MCP tool to write these; agents pick by name only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


class PersonalityError(Exception):
    pass


@dataclass
class Personality:
    name: str
    description: str
    body: str
    agents: list[str] = field(default_factory=list)  # empty = any agent

    @property
    def hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.body.encode()).hexdigest()[:16]

    def allows(self, agent: str) -> bool:
        return not self.agents or agent in self.agents


def _parse_frontmatter(text: str, source: str) -> Personality:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, re.DOTALL)
    if not match:
        raise PersonalityError(f"{source}: missing frontmatter block")
    meta_block, body = match.groups()

    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    name = meta.get("name")
    if not name:
        raise PersonalityError(f"{source}: frontmatter has no 'name'")

    agents_raw = meta.get("agents", "")
    agents = [a.strip() for a in agents_raw.strip("[]").split(",") if a.strip()]

    return Personality(
        name=name,
        description=meta.get("description", ""),
        body=body.strip(),
        agents=agents,
    )


class PersonalityBook:
    def __init__(self, directory: Path):
        self.directory = directory

    def ensure_starters(self) -> None:
        """Copy the bundled starter personas on first run only."""
        if self.directory.exists():
            return
        self.directory.mkdir(parents=True)
        starters = resources.files("tellmphone") / "data" / "personalities"
        for entry in starters.iterdir():
            if entry.name.endswith(".md"):
                (self.directory / entry.name).write_text(entry.read_text())

    def all(self) -> list[Personality]:
        if not self.directory.exists():
            return []
        found = []
        for path in sorted(self.directory.glob("*.md")):
            try:
                found.append(_parse_frontmatter(path.read_text(), str(path)))
            except PersonalityError:
                continue  # a malformed file shouldn't break the phonebook
        return found

    def get(self, name: str) -> Personality:
        for personality in self.all():
            if personality.name == name:
                return personality
        known = ", ".join(p.name for p in self.all()) or "(none installed)"
        raise PersonalityError(f"unknown personality {name!r}; available: {known}")
