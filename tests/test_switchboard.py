import time

from tellmphone.config import Config
from tellmphone.switchboard import Switchboard

GRUMPY = """---
name: grumpy
description: A grump.
---
Be grumpy.
"""


def install_personality(store, text=GRUMPY, filename="grumpy.md"):
    store.personalities_dir.mkdir(parents=True, exist_ok=True)
    (store.personalities_dir / filename).write_text(text)


# ---------------------------------------------------------------- live calls


def test_wait_call_answers_inline(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "review my code", project)
    assert result["status"] == "answered"
    assert result["response"] == "spawn-reply to: review my code"
    record = store.load_call(result["call_id"])
    assert record.status == "answered"
    assert record.callee.session_id == "fake-sess-1"
    assert record.unread_for == []  # delivered inline, not left unread
    transcript = store.read_transcript(result["call_id"])
    assert [e.from_ for e in transcript] == ["claude", "fake"]


def test_context_is_framed(boards, fake_adapter, project):
    caller, _ = boards
    caller.place_call("fake", "the ask", project, context="the background")
    prompt = fake_adapter.spawns[0].message
    assert "Background context:\nthe background" in prompt
    assert "Your task:\nthe ask" in prompt


def test_personality_reaches_adapter(boards, fake_adapter, project, store):
    caller, _ = boards
    install_personality(store)
    result = caller.place_call("fake", "hi", project, personality="grumpy")
    assert result["personality"] == "grumpy"
    assert fake_adapter.spawns[0].personality.name == "grumpy"
    assert store.load_call(result["call_id"]).callee.personality_hash.startswith("sha256:")


def test_model_is_pinned_for_the_whole_call(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "hi", project, model="fake-xl")
    assert result["model"] == "fake-xl"
    assert fake_adapter.spawns[0].model == "fake-xl"
    assert store.load_call(result["call_id"]).callee.model == "fake-xl"
    # a reply that falls back to replay-spawn still carries the pinned model
    fake_adapter.lose_session = True
    caller.reply(result["call_id"], "more")
    assert fake_adapter.spawns[-1].model == "fake-xl"


def test_model_defaults_from_config(store, home, fake_adapter, project):
    from tellmphone.config import AgentDefaults

    cfg = Config(i_am="claude", home=home, agents={"fake": AgentDefaults(model="fake-mini")})
    board = Switchboard(cfg, store, {"fake": fake_adapter})
    board.place_call("fake", "hi", project)
    assert fake_adapter.spawns[0].model == "fake-mini"
    # explicit choice beats the config default
    board.place_call("fake", "hi", project, model="fake-xl")
    assert fake_adapter.spawns[1].model == "fake-xl"


def test_model_and_personality_are_independent(boards, fake_adapter, project, store):
    caller, _ = boards
    install_personality(store)
    caller.place_call("fake", "hi", project, personality="grumpy", model="fake-xl")
    req = fake_adapter.spawns[0]
    assert req.personality.name == "grumpy" and req.model == "fake-xl"
    caller.place_call("fake", "hi", project, personality="grumpy")
    assert fake_adapter.spawns[1].model is None


def test_unknown_personality_refused(boards, project):
    caller, _ = boards
    result = caller.place_call("fake", "hi", project, personality="nope")
    assert result["status"] == "refused"
    assert "unknown personality" in result["reason"]


def test_reply_resumes_same_session(boards, fake_adapter, project):
    caller, _ = boards
    placed = caller.place_call("fake", "first", project)
    result = caller.reply(placed["call_id"], "second")
    assert result["status"] == "answered"
    assert fake_adapter.resumes == [("fake-sess-1", "second")]


def test_lost_session_falls_back_to_replay(boards, fake_adapter, project):
    caller, _ = boards
    placed = caller.place_call("fake", "first", project)
    fake_adapter.lose_session = True
    result = caller.reply(placed["call_id"], "second")
    assert result["status"] == "answered"
    assert result["resumed_via"] == "transcript-replay"
    replay = fake_adapter.spawns[-1].message
    assert "[claude]: first" in replay and "[claude]: second" in replay


def test_timeout_goes_to_voicemail(boards, fake_adapter, project, store):
    caller, _ = boards
    fake_adapter.delay = 0.4
    result = caller.place_call("fake", "slow one", project, timeout_s=0.05)
    assert result["status"] == "ringing"
    time.sleep(0.6)  # let the detached worker finish
    record = store.load_call(result["call_id"])
    assert record.status == "answered"
    assert record.unread_for == ["claude"]
    mailbox = caller.check_messages(project)
    assert mailbox["unread"][0]["call_id"] == result["call_id"]
    assert "spawn-reply" in mailbox["unread"][0]["preview"]


# ---------------------------------------------------------------- refusals


def test_hop_limit(boards, store, project, fake_adapter):
    _, _ = boards
    deep = Switchboard(
        Config(i_am="claude", home=store.home, max_hops=2),
        store,
        {"fake": fake_adapter},
    )
    deep.config.hop_count = 2  # this session is already a hop-2 callee
    result = deep.place_call("fake", "go deeper", project)
    assert result["status"] == "refused"
    assert "hop limit" in result["reason"]


def test_self_call_refused(boards, project):
    caller, _ = boards
    result = caller.place_call("claude", "hello me", project)
    assert result["status"] == "refused"


def test_unknown_agent_refused(boards, project):
    caller, _ = boards
    result = caller.place_call("gemini", "hi", project)
    assert result["status"] == "refused"


def test_reply_after_hang_up(boards, project):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    caller.hang_up(placed["call_id"], reason="done")
    result = caller.reply(placed["call_id"], "one more thing")
    assert result["status"] == "error"
    assert "hung up" in result["error"]


def test_reply_while_ringing_is_busy(boards, project, store):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    record = store.load_call(placed["call_id"])
    record.status = "ringing"
    store.save_call(record)
    assert caller.reply(placed["call_id"], "hurry up")["status"] == "busy"


# ------------------------------------------------------------- answering machine


def test_voicemail_full_loop(boards, fake_adapter, project, store):
    caller, callee = boards

    left = caller.place_call("fake", "ping when you see this", project, mode="voicemail")
    assert left["status"] == "voicemail"
    assert fake_adapter.spawns == []  # nothing ran

    # callee discovers it
    box = callee.check_messages(project)
    assert box["unread"][0]["from"] == "claude"
    assert "ping when you see this" in box["unread"][0]["preview"]
    # fetching marks read
    assert callee.check_messages(project)["unread"] == []

    # callee answers without any subprocess
    answer = callee.reply(left["call_id"], "pong, saw it")
    assert answer["status"] == "answered"
    assert fake_adapter.spawns == [] and fake_adapter.resumes == []

    # caller finds the reply
    box = caller.check_messages(project)
    assert box["unread"][0]["call_id"] == left["call_id"]
    assert "pong" in box["unread"][0]["preview"]


def test_caller_reply_on_voicemail_spawns_with_replay(boards, fake_adapter, project):
    caller, _ = boards
    left = caller.place_call("fake", "original ask", project, mode="voicemail")
    result = caller.reply(left["call_id"], "actually, live please")
    assert result["status"] == "answered"
    assert result["resumed_via"] == "transcript-replay"
    assert "[claude]: original ask" in fake_adapter.spawns[0].message


def test_third_party_cannot_reply(boards, store, project, fake_adapter):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    stranger = Switchboard(
        Config(i_am="gemini", home=store.home), store, {"fake": fake_adapter}
    )
    result = stranger.reply(placed["call_id"], "let me in")
    assert result["status"] == "error"


def test_phonebook(boards, store):
    caller, _ = boards
    install_personality(store)
    book = caller.phonebook()
    assert book["you_are"] == "claude"
    fake_entry = next(a for a in book["agents"] if a["name"] == "fake")
    assert fake_entry["available"] is True
    assert fake_entry["default_model"] == "(CLI default)"
    assert any(p["name"] == "grumpy" for p in book["personalities"])
