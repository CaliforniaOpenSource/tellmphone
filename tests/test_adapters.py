"""Adapters exercised against fake claude/codex executables on PATH."""

import json
import uuid

import pytest

from tellmphone.adapters.base import AdapterError, SessionLost, SpawnRequest, scrubbed_env
from tellmphone.adapters.claude import ClaudeAdapter
from tellmphone.adapters.codex import CodexAdapter
from tellmphone.adapters.gemini import GeminiAdapter
from tellmphone.adapters.grok import GrokAdapter
from tellmphone.personalities import Personality

FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]

def flag(name):
    return args[args.index(name) + 1] if name in args else None

resume_id = flag("--resume")
if resume_id == "lost-session":
    sys.stderr.write("No conversation found with session ID: lost-session\\n")
    sys.exit(1)

prompt = flag("-p") or ""
system = flag("--append-system-prompt") or ""
model = flag("--model") or ""
permission_mode = flag("--permission-mode") or ""
print(json.dumps({
    "session_id": resume_id or "claude-sess-1",
    "result": f"prompt={prompt}|system={system}|model={model}|permission={permission_mode}|cwd={os.getcwd()}",
    "is_error": False,
    "total_cost_usd": 0.01,
    "num_turns": 1,
}))
'''

FAKE_CODEX = '''#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
if args[:2] == ["exec", "resume"]:
    if "--cd" in args or "--sandbox" in args:
        # matches real codex: cwd/sandbox are spawn-only, inherited on resume
        sys.stderr.write("error: unexpected argument '--cd' found\\n")
        sys.exit(2)
    session_id = args[2]
    if session_id == "lost-session":
        sys.stderr.write("session not found: lost-session\\n")
        sys.exit(1)
else:
    session_id = "codex-sess-1"

def flag(name):
    return args[args.index(name) + 1] if name in args else None

prompt = args[-1]
out_file = flag("--output-last-message")
model = flag("--model") or ""
if prompt == "json-error":
    sys.stderr.write("Reading additional input from stdin...\\n")
    print(json.dumps({"type": "error", "message": "Missing environment variable: AMD_LLM_API_KEY."}))
    print(json.dumps({"type": "turn.failed", "error": {"message": "Missing environment variable: AMD_LLM_API_KEY."}}))
    sys.exit(1)
print("some non-json preamble")
print(json.dumps({"type": "session_configured", "session_id": session_id}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}))
with open(out_file, "w") as fh:
    fh.write(f"codex answer to: {prompt}|model={model}")
'''

FAKE_AGY = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]

def flag(name):
    return args[args.index(name) + 1] if name in args else None

def print_prompt():
    return flag("--print") or ""

session_id = flag("--conversation")
if session_id == "lost-session":
    sys.stderr.write("conversation does not exist: lost-session\\n")
    sys.exit(1)

print(json.dumps({
    "argv": args,
    "cwd": os.getcwd(),
    "prompt": print_prompt(),
    "model": flag("--model") or "",
    "sandbox": "--sandbox" in args,
    "mode": flag("--mode") or "",
}))
'''

FAKE_GROK = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]

def flag(name):
    return args[args.index(name) + 1] if name in args else None

session_id = flag("--session-id") or flag("--resume")
if session_id == "lost-session":
    sys.stderr.write("session not found: lost-session\\n")
    sys.exit(1)

print("some non-json preamble")
print(json.dumps({
    "session_id": session_id,
    "response": json.dumps({
        "argv": args,
        "cwd": os.getcwd(),
        "prompt": flag("-p") or "",
        "model": flag("--model") or "",
        "sandbox": flag("--sandbox") or "",
        "permission_mode": flag("--permission-mode") or "",
    }),
}))
'''


@pytest.fixture
def req(project):
    return SpawnRequest(message="hello there", project_dir=project)


class TestClaudeAdapter:
    def test_scrubbed_env_preserves_claude_auth_and_gateway_config(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")

        env = scrubbed_env()

        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_CHILD_SESSION" not in env
        assert "CLAUDE_CODE_SESSION_ID" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "token"
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"

    def test_spawn_parses_json(self, fake_bin, req, project):
        fake_bin("claude", FAKE_CLAUDE)
        turn = ClaudeAdapter().spawn(req)
        assert turn.session_id == "claude-sess-1"
        assert "prompt=hello there" in turn.text
        assert f"cwd={project}" in turn.text  # ran in the project dir
        assert turn.usage["total_cost_usd"] == 0.01

    def test_personality_via_system_prompt_flag(self, fake_bin, req):
        fake_bin("claude", FAKE_CLAUDE)
        req.personality = Personality(name="g", description="", body="Be grumpy.")
        turn = ClaudeAdapter().spawn(req)
        assert "system=Be grumpy." in turn.text

    def test_empty_body_personality_injects_nothing(self, fake_bin, req):
        fake_bin("claude", FAKE_CLAUDE)
        req.personality = Personality(name="neutral", description="", body="")
        turn = ClaudeAdapter().spawn(req)
        assert "system=|" in turn.text  # no --append-system-prompt sent

    def test_permission_mode(self, fake_bin, req):
        fake_bin("claude", FAKE_CLAUDE)
        adapter = ClaudeAdapter()
        assert "permission=dontAsk" in adapter.spawn(req).text
        req.write_access = True
        assert "permission=acceptEdits" in adapter.spawn(req).text

    def test_model_flag(self, fake_bin, req):
        fake_bin("claude", FAKE_CLAUDE)
        req.model = "opus"
        adapter = ClaudeAdapter()
        assert "model=opus" in adapter.spawn(req).text
        assert "model=opus" in adapter.resume("claude-sess-1", "again", req).text

    def test_resume_and_session_lost(self, fake_bin, req):
        fake_bin("claude", FAKE_CLAUDE)
        adapter = ClaudeAdapter()
        turn = adapter.resume("claude-sess-1", "again", req)
        assert turn.session_id == "claude-sess-1"
        with pytest.raises(SessionLost):
            adapter.resume("lost-session", "again", req)

    def test_available(self, fake_bin):
        fake_bin("claude", FAKE_CLAUDE)
        assert ClaudeAdapter().available()


class TestCodexAdapter:
    def test_spawn_fishes_session_id_and_last_message(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        turn = CodexAdapter().spawn(req)
        assert turn.session_id == "codex-sess-1"
        assert turn.text.startswith("codex answer to: hello there")

    def test_model_flag(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        req.model = "gpt-5.1-codex"
        turn = CodexAdapter().spawn(req)
        assert "model=gpt-5.1-codex" in turn.text
        assert turn.text.startswith("codex answer to: hello there")  # prompt stays last arg

    def test_personality_becomes_preamble(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        req.personality = Personality(name="g", description="", body="Be grumpy.")
        turn = CodexAdapter().spawn(req)
        assert "Be grumpy." in turn.text  # preamble is part of the prompt

    def test_empty_body_personality_injects_nothing(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        req.personality = Personality(name="neutral", description="", body="")
        turn = CodexAdapter().spawn(req)
        assert turn.text.startswith("codex answer to: hello there")  # no preamble

    def test_resume_and_session_lost(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        adapter = CodexAdapter()
        turn = adapter.resume("codex-sess-9", "again", req)
        assert turn.session_id == "codex-sess-9"
        with pytest.raises(SessionLost):
            adapter.resume("lost-session", "again", req)

    def test_failure_prefers_json_error_over_stderr_banner(self, fake_bin, req):
        fake_bin("codex", FAKE_CODEX)
        req.message = "json-error"

        with pytest.raises(AdapterError) as exc:
            CodexAdapter().spawn(req)

        message = str(exc.value)
        assert "Missing environment variable: AMD_LLM_API_KEY." in message
        assert "Reading additional input from stdin" not in message


class TestGeminiAdapter:
    def test_spawn_uses_sandbox_and_prompt_last(self, fake_bin, req, project):
        fake_bin("agy", FAKE_AGY)
        turn = GeminiAdapter().spawn(req)
        data = json.loads(turn.text)
        assert data["argv"] == [
            "--add-dir",
            project,
            "--sandbox",
            "--mode",
            "plan",
            "--print",
            "hello there",
        ]
        assert data["prompt"] == "hello there"
        assert data["cwd"] == project
        assert turn.session_id is None

    def test_write_access_accepts_edits_without_skipping_permissions(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        req.write_access = True
        data = json.loads(GeminiAdapter().spawn(req).text)
        assert data["argv"] == [
            "--add-dir",
            req.project_dir,
            "--sandbox",
            "--mode",
            "accept-edits",
            "--print",
            "hello there",
        ]
        assert data["mode"] == "accept-edits"
        assert data["sandbox"] is True

    def test_model_flag_stays_before_prompt(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        req.model = "Gemini 3.1 Pro (High)"
        data = json.loads(GeminiAdapter().spawn(req).text)
        assert data["argv"] == [
            "--model",
            "Gemini 3.1 Pro (High)",
            "--add-dir",
            req.project_dir,
            "--sandbox",
            "--mode",
            "plan",
            "--print",
            "hello there",
        ]
        assert data["model"] == "Gemini 3.1 Pro (High)"

    def test_personality_becomes_preamble(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        req.personality = Personality(name="g", description="", body="Be grumpy.")
        data = json.loads(GeminiAdapter().spawn(req).text)
        assert "Be grumpy." in data["prompt"]
        assert data["prompt"].endswith("hello there")

    def test_empty_body_personality_injects_nothing(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        req.personality = Personality(name="neutral", description="", body="")
        data = json.loads(GeminiAdapter().spawn(req).text)
        assert data["prompt"] == "hello there"

    def test_resume_preserves_existing_session_and_session_lost(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        adapter = GeminiAdapter()
        turn = adapter.resume("agy-sess-9", "again", req)
        data = json.loads(turn.text)
        assert data["argv"] == [
            "--conversation",
            "agy-sess-9",
            "--add-dir",
            req.project_dir,
            "--sandbox",
            "--mode",
            "plan",
            "--print",
            "again",
        ]
        assert turn.session_id == "agy-sess-9"
        with pytest.raises(SessionLost):
            adapter.resume("lost-session", "again", req)

    def test_available(self, fake_bin):
        fake_bin("agy", FAKE_AGY)
        assert GeminiAdapter().available()


class TestGrokAdapter:
    def test_spawn_uses_read_only_headless_session(self, fake_bin, req, project):
        fake_bin("grok", FAKE_GROK)
        turn = GrokAdapter().spawn(req)
        data = json.loads(turn.text)
        assert uuid.UUID(turn.session_id)
        assert data["prompt"] == "hello there"
        assert data["cwd"] == project
        assert data["sandbox"] == "read-only"
        assert data["permission_mode"] == "dontAsk"
        assert "--no-auto-update" in data["argv"]
        assert "--no-alt-screen" in data["argv"]
        assert "--session-id" in data["argv"]

    def test_write_access_uses_workspace_and_accept_edits(self, fake_bin, req):
        fake_bin("grok", FAKE_GROK)
        req.write_access = True
        data = json.loads(GrokAdapter().spawn(req).text)
        assert data["sandbox"] == "workspace"
        assert data["permission_mode"] == "acceptEdits"

    def test_model_flag(self, fake_bin, req):
        fake_bin("grok", FAKE_GROK)
        req.model = "grok-build"
        data = json.loads(GrokAdapter().spawn(req).text)
        assert data["model"] == "grok-build"

    def test_personality_becomes_preamble(self, fake_bin, req):
        fake_bin("grok", FAKE_GROK)
        req.personality = Personality(name="g", description="", body="Be grumpy.")
        data = json.loads(GrokAdapter().spawn(req).text)
        assert "Be grumpy." in data["prompt"]
        assert data["prompt"].endswith("hello there")

    def test_empty_body_personality_injects_nothing(self, fake_bin, req):
        fake_bin("grok", FAKE_GROK)
        req.personality = Personality(name="neutral", description="", body="")
        data = json.loads(GrokAdapter().spawn(req).text)
        assert data["prompt"] == "hello there"

    def test_resume_preserves_session_and_session_lost(self, fake_bin, req):
        fake_bin("grok", FAKE_GROK)
        adapter = GrokAdapter()
        turn = adapter.resume("grok-sess-9", "again", req)
        data = json.loads(turn.text)
        assert turn.session_id == "grok-sess-9"
        assert data["prompt"] == "again"
        assert data["argv"][-2:] == ["--resume", "grok-sess-9"]
        with pytest.raises(SessionLost):
            adapter.resume("lost-session", "again", req)

    def test_available(self, fake_bin):
        fake_bin("grok", FAKE_GROK)
        assert GrokAdapter().available()
