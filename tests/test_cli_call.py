"""`tellmphone call` / `reply` from the terminal, against the fake codex CLI."""

import pytest

from tellmphone.cli import main
from tellmphone.store import CallNotFound, Store

from test_adapters import FAKE_CODEX


def test_call_and_reply_last(fake_bin, monkeypatch, home, project, capsys):
    monkeypatch.setenv("TELLMPHONE_HOME", str(home))
    fake_bin("codex", FAKE_CODEX)

    assert main(["call", "codex", "hi there", "--project", project]) == 0
    out = capsys.readouterr().out
    assert "ringing codex" in out
    assert "codex answer to: hi there" in out
    assert "answered in" in out

    # reply with no call_id resumes the latest open call in the project
    assert main(["reply", "one more thing", "--project", project]) == 0
    out = capsys.readouterr().out
    assert "codex answer to: one more thing" in out


def test_reply_with_no_open_calls(fake_bin, monkeypatch, home, project, capsys):
    monkeypatch.setenv("TELLMPHONE_HOME", str(home))
    fake_bin("codex", FAKE_CODEX)
    assert main(["reply", "hello?", "--project", project]) == 1
    assert "no open calls" in capsys.readouterr().out


def test_call_unknown_agent_fails(fake_bin, monkeypatch, home, project, capsys):
    monkeypatch.setenv("TELLMPHONE_HOME", str(home))
    assert main(["call", "gemini", "hi", "--project", project]) == 1
    assert "refused" in capsys.readouterr().out


def test_messages_show_and_gc(fake_bin, monkeypatch, home, project, capsys):
    monkeypatch.setenv("TELLMPHONE_HOME", str(home))
    fake_bin("codex", FAKE_CODEX)
    message = "read this later"

    assert (
        main(["call", "codex", message, "--project", project, "--mode", "voicemail"])
        == 0
    )
    out = capsys.readouterr().out
    assert "voicemail: call-" in out

    store = Store(home)
    call_id = store.all_calls()[0].call_id

    assert main(["messages", "--project", project, "--as", "codex"]) == 0
    out = capsys.readouterr().out
    assert call_id in out
    assert message in out
    assert "open calls" in out

    assert main(["messages", "--project", project, "--as", "codex"]) == 0
    assert "no unread messages" in capsys.readouterr().out

    assert main(["show", call_id]) == 0
    out = capsys.readouterr().out
    assert "transcript" in out
    assert message in out

    record = store.load_call(call_id)
    record.status = "closed"
    store.save_call(record)
    assert main(["gc", "--days", "0"]) == 0
    out = capsys.readouterr().out
    assert f"deleted {call_id}" in out

    with pytest.raises(CallNotFound):
        store.load_call(call_id)
