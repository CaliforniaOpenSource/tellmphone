"""`tellmphone install` against fake agent CLIs that log their invocations."""

import json
import shlex
import sys

from tellmphone.adapters.claude import ClaudeAdapter
from tellmphone.adapters.codex import CodexAdapter
from tellmphone.adapters.gemini import GeminiAdapter
from tellmphone.adapters.grok import GrokAdapter
from tellmphone.cli import main

RECORDING_CLI = '''#!/usr/bin/env python3
import sys
from pathlib import Path

log = Path("{log}")
log.write_text(log.read_text() + " ".join(sys.argv[1:]) + "\\n") if log.exists() \
    else log.write_text(" ".join(sys.argv[1:]) + "\\n")

marker = Path("{marker}")
if sys.argv[1:3] == ["mcp", "add"] and "{flaky}" == "yes" and not marker.exists():
    marker.touch()
    sys.stderr.write("a server named tellmphone already exists\\n")
    sys.exit(1)
'''


def recording_cli(tmp_path, name, flaky=False):
    log = tmp_path / f"{name}.log"
    return log, RECORDING_CLI.format(
        log=log, marker=tmp_path / f"{name}.marker", flaky="yes" if flaky else "no"
    )


def test_claude_register(fake_bin, tmp_path):
    log, script = recording_cli(tmp_path, "claude")
    fake_bin("claude", script)
    result = ClaudeAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "claude"])
    assert "registered" in result
    assert log.read_text().strip() == (
        "mcp add tellmphone --scope user -- uvx tellmphone serve --i-am claude"
    )


def test_codex_register_sets_tool_approval(fake_bin, tmp_path):
    _, script = recording_cli(tmp_path, "codex")
    fake_bin("codex", script)
    config = tmp_path / "codex-home" / "config.toml"
    config.write_text(
        'model = "gpt-5.5"\n'
        "[mcp_servers.tellmphone]\n"
        'command = "uv"\n'
        "[mcp_servers.other]\n"
        'command = "x"\n'
    )
    CodexAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "codex"])
    text = config.read_text()
    assert (
        '[mcp_servers.tellmphone]\ndefault_tools_approval_mode = "approve"' in text
    )
    assert 'model = "gpt-5.5"' in text  # rest of the file untouched
    # idempotent: a second install doesn't duplicate the key
    CodexAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "codex"])
    assert config.read_text().count("default_tools_approval_mode") == 1
    # a wrong existing value gets repaired in place, not duplicated
    config.write_text(config.read_text().replace('"approve"', '"prompt"'))
    CodexAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "codex"])
    fixed = config.read_text()
    assert fixed.count("default_tools_approval_mode") == 1
    assert 'default_tools_approval_mode = "approve"' in fixed


def test_gemini_register_writes_shared_mcp_config(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path / "gemini-home"))
    path = tmp_path / "gemini-home" / "config" / "mcp_config.json"

    result = GeminiAdapter().register_mcp(
        ["uvx", "tellmphone", "serve", "--i-am", "gemini"]
    )

    assert "registered" in result
    data = json.loads(path.read_text())
    assert data["mcpServers"]["tellmphone"] == {
        "command": "uvx",
        "args": ["tellmphone", "serve", "--i-am", "gemini"],
    }

    assert "removed" in GeminiAdapter().unregister_mcp()
    assert "tellmphone" not in json.loads(path.read_text())["mcpServers"]


def test_grok_register(fake_bin, tmp_path):
    log, script = recording_cli(tmp_path, "grok")
    fake_bin("grok", script)
    result = GrokAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "grok"])
    assert "registered" in result
    assert log.read_text().strip() == (
        "mcp add tellmphone -- uvx tellmphone serve --i-am grok"
    )
    sandbox = (tmp_path / "grok-home" / "sandbox.toml").read_text()
    assert "[profiles.tellmphone-read-only]" in sandbox
    assert "[profiles.tellmphone-workspace]" in sandbox
    assert str((tmp_path / "tellmphone-home").resolve()) in sandbox

    GrokAdapter().unregister_mcp()
    assert not (tmp_path / "grok-home" / "sandbox.toml").exists()


def test_register_replaces_existing(fake_bin, tmp_path):
    log, script = recording_cli(tmp_path, "codex", flaky=True)
    fake_bin("codex", script)
    result = CodexAdapter().register_mcp(["uvx", "tellmphone", "serve", "--i-am", "codex"])
    assert "registered" in result
    lines = log.read_text().splitlines()
    # add (fails: exists) -> remove -> add again
    assert [line.split()[1] for line in lines] == ["add", "remove", "add"]


def test_install_command_hits_all_detected_agents(fake_bin, tmp_path, capsys):
    claude_log, claude_script = recording_cli(tmp_path, "claude")
    codex_log, codex_script = recording_cli(tmp_path, "codex")
    grok_log, grok_script = recording_cli(tmp_path, "grok")
    fake_bin("claude", claude_script)
    fake_bin("codex", codex_script)
    fake_bin("grok", grok_script)

    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert "claude: registered" in out
    assert "codex: registered" in out
    assert "grok: registered" in out

    # each registration serves with that agent's own identity
    assert "--i-am claude" in claude_log.read_text()
    assert "--i-am codex" in codex_log.read_text()
    assert "--i-am grok" in grok_log.read_text()
    # The exact active interpreter avoids package-manager cache writes when an
    # agent launches the MCP server inside a read-only sandbox.
    assert sys.executable in shlex.split(claude_log.read_text())


def test_install_single_agent(fake_bin, tmp_path, capsys):
    _, claude_script = recording_cli(tmp_path, "claude")
    codex_log, codex_script = recording_cli(tmp_path, "codex")
    fake_bin("claude", claude_script)
    fake_bin("codex", codex_script)

    assert main(["install", "--agent", "codex"]) == 0
    out = capsys.readouterr().out
    assert "codex: registered" in out and "claude" not in out
    assert main(["install", "--agent", "nope"]) == 1


def test_uninstall(fake_bin, tmp_path, capsys):
    claude_log, claude_script = recording_cli(tmp_path, "claude")
    codex_log, codex_script = recording_cli(tmp_path, "codex")
    grok_log, grok_script = recording_cli(tmp_path, "grok")
    fake_bin("claude", claude_script)
    fake_bin("codex", codex_script)
    fake_bin("grok", grok_script)

    assert main(["uninstall"]) == 0
    assert "mcp remove tellmphone --scope user" in claude_log.read_text()
    assert "mcp remove tellmphone" in codex_log.read_text()
    assert "mcp remove tellmphone" in grok_log.read_text()


def test_install_reports_registration_failure(fake_bin, tmp_path, capsys):
    _, script = recording_cli(tmp_path, "claude")
    script += '\nsys.stderr.write("forced failure\\n")\nsys.exit(2)\n'
    fake_bin("claude", script)

    assert main(["install", "--agent", "claude"]) == 1
    output = capsys.readouterr().out
    assert "FAILED" in output
    assert "forced failure" in output


def test_serve_command_override(fake_bin, tmp_path, capsys):
    claude_log, claude_script = recording_cli(tmp_path, "claude")
    _, codex_script = recording_cli(tmp_path, "codex")
    fake_bin("claude", claude_script)
    fake_bin("codex", codex_script)

    assert main(["install", "--serve-command", "uvx tellmphone serve"]) == 0
    assert "-- uvx tellmphone serve --i-am claude" in claude_log.read_text()
