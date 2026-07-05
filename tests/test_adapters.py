"""Adapters exercised against fake claude/codex executables on PATH."""

import json

import pytest

from tellmphone.adapters.base import SessionLost, SpawnRequest
from tellmphone.adapters.claude import ClaudeAdapter
from tellmphone.adapters.codex import CodexAdapter
from tellmphone.adapters.gemini import GeminiAdapter
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
print(json.dumps({
    "session_id": resume_id or "claude-sess-1",
    "result": f"prompt={prompt}|system={system}|model={model}|cwd={os.getcwd()}",
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
    "skip_permissions": "--dangerously-skip-permissions" in args,
}))
'''


@pytest.fixture
def req(project):
    return SpawnRequest(message="hello there", project_dir=project)


class TestClaudeAdapter:
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
        assert not ClaudeAdapter().available() or True  # depends on host
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


class TestGeminiAdapter:
    def test_spawn_uses_sandbox_and_prompt_last(self, fake_bin, req, project):
        fake_bin("agy", FAKE_AGY)
        turn = GeminiAdapter().spawn(req)
        data = json.loads(turn.text)
        assert data["argv"] == [
            "--add-dir",
            project,
            "--sandbox",
            "--print",
            "hello there",
        ]
        assert data["prompt"] == "hello there"
        assert data["cwd"] == project
        assert turn.session_id is None

    def test_write_access_skips_interactive_permissions(self, fake_bin, req):
        fake_bin("agy", FAKE_AGY)
        req.write_access = True
        data = json.loads(GeminiAdapter().spawn(req).text)
        assert data["argv"] == [
            "--add-dir",
            req.project_dir,
            "--sandbox",
            "--dangerously-skip-permissions",
            "--print",
            "hello there",
        ]
        assert data["skip_permissions"] is True
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
            "--print",
            "again",
        ]
        assert turn.session_id == "agy-sess-9"
        with pytest.raises(SessionLost):
            adapter.resume("lost-session", "again", req)

    def test_available(self, fake_bin):
        fake_bin("agy", FAKE_AGY)
        assert GeminiAdapter().available()
