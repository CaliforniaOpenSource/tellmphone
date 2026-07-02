"""`tellmphone call` / `reply` from the terminal, against the fake codex CLI."""

from tellmphone.cli import main

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
