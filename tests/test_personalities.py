import pytest

from tellmphone.personalities import PersonalityBook, PersonalityError

GRUMPY = """---
name: grumpy
description: A grump.
agents: [claude, codex]
---
Be grumpy but useful.
"""


@pytest.fixture
def book(tmp_path):
    d = tmp_path / "personalities"
    d.mkdir()
    (d / "grumpy.md").write_text(GRUMPY)
    return PersonalityBook(d)


def test_parse(book):
    p = book.get("grumpy")
    assert p.description == "A grump."
    assert p.agents == ["claude", "codex"]
    assert p.body == "Be grumpy but useful."
    assert p.hash.startswith("sha256:")


def test_allows(book):
    p = book.get("grumpy")
    assert p.allows("claude")
    assert not p.allows("gemini")


def test_unknown_lists_available(book):
    with pytest.raises(PersonalityError, match="grumpy"):
        book.get("nope")


def test_malformed_file_is_skipped(book):
    (book.directory / "broken.md").write_text("no frontmatter here")
    assert [p.name for p in book.all()] == ["grumpy"]


def test_ensure_starters(tmp_path):
    book = PersonalityBook(tmp_path / "fresh")
    book.ensure_starters()
    names = {p.name for p in book.all()}
    assert {"grumpy-reviewer", "security-auditor", "rubber-duck"} <= names
    # second call must not clobber user edits
    (book.directory / "grumpy-reviewer.md").write_text(GRUMPY)
    book.ensure_starters()
    assert book.get("grumpy").name == "grumpy"
