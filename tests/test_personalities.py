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
    assert "grumpy" in [p.name for p in book.all()]


def test_builtins_available_without_user_dir(tmp_path):
    book = PersonalityBook(tmp_path / "never-created")
    names = {p.name for p in book.all()}
    assert {"grumpy-reviewer", "security-auditor", "rubber-duck"} <= names
    assert all(p.source == "builtin" for p in book.all())


def test_neutral_builtin_has_empty_body(tmp_path):
    book = PersonalityBook(tmp_path / "never-created")
    assert book.get("neutral").body == ""


def test_user_file_is_tagged_user(book):
    assert book.get("grumpy").source == "user"


def test_user_file_overrides_builtin(book):
    (book.directory / "grumpy-reviewer.md").write_text(
        "---\nname: grumpy-reviewer\ndescription: Mine.\n---\nMy own grump.\n"
    )
    p = book.get("grumpy-reviewer")
    assert p.source == "user"
    assert p.body == "My own grump."


def test_disabled_user_file_hides_builtin(book):
    (book.directory / "rubber-duck.md").write_text(
        "---\nname: rubber-duck\ndisabled: true\n---\n"
    )
    assert "rubber-duck" not in [p.name for p in book.all()]
    with pytest.raises(PersonalityError, match="rubber-duck"):
        book.get("rubber-duck")
