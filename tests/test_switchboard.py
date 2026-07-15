import subprocess
import threading

from tellmphone.config import CALL_ID_ENV, HOME_ENV, HOP_ENV, Config
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


def finish_turn(board, call_id):
    return board.run_turn(call_id)


# ---------------------------------------------------------------- live calls


def test_call_starts_async_turn_and_turn_answers(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "review my code", project)
    assert result["status"] == "ringing"
    answer = finish_turn(caller, result["call_id"])
    assert answer["status"] == "answered"
    assert answer["response"] == "spawn-reply to: review my code"
    record = store.load_call(result["call_id"])
    assert record.status == "answered"
    assert record.callee.session_id == "fake-sess-1"
    assert record.unread_for == ["claude"]
    assert fake_adapter.spawns[0].call_id == result["call_id"]
    transcript = store.read_transcript(result["call_id"])
    assert [e.from_ for e in transcript] == ["claude", "fake"]


def test_context_is_framed(boards, fake_adapter, project):
    caller, _ = boards
    result = caller.place_call("fake", "the ask", project, context="the background")
    finish_turn(caller, result["call_id"])
    prompt = fake_adapter.spawns[0].message
    assert "Background context:\nthe background" in prompt
    assert "Your task:\nthe ask" in prompt


def test_personality_reaches_adapter(boards, fake_adapter, project, store):
    caller, _ = boards
    install_personality(store)
    result = caller.place_call("fake", "hi", project, personality="grumpy")
    finish_turn(caller, result["call_id"])
    assert fake_adapter.spawns[0].personality.name == "grumpy"
    callee = store.load_call(result["call_id"]).callee
    assert callee.personality_hash.startswith("sha256:")
    assert callee.personality_body == "Be grumpy."


def test_personality_replay_uses_call_snapshot(boards, fake_adapter, project, store):
    caller, _ = boards
    install_personality(store)
    left = caller.place_call(
        "fake", "original ask", project, personality="grumpy", mode="voicemail"
    )
    install_personality(
        store,
        "---\nname: grumpy\ndescription: Changed.\n---\nBe cheerful.\n",
    )

    result = caller.reply(left["call_id"], "actually, live please")
    finish_turn(caller, result["call_id"])

    assert fake_adapter.spawns[0].personality.body == "Be grumpy."


def test_model_is_pinned_for_the_whole_call(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "hi", project, model="fake-xl")
    finish_turn(caller, result["call_id"])
    assert fake_adapter.spawns[0].model == "fake-xl"
    assert store.load_call(result["call_id"]).callee.model == "fake-xl"
    # a reply that falls back to replay-spawn still carries the pinned model
    fake_adapter.lose_session = True
    reply = caller.reply(result["call_id"], "more")
    finish_turn(caller, reply["call_id"])
    assert fake_adapter.spawns[-1].model == "fake-xl"


def test_model_defaults_from_config(store, home, fake_adapter, project):
    from tellmphone.config import AgentDefaults

    cfg = Config(i_am="claude", home=home, agents={"fake": AgentDefaults(model="fake-mini")})
    board = Switchboard(cfg, store, {"fake": fake_adapter}, detach_turns=False)
    first = board.place_call("fake", "hi", project)
    finish_turn(board, first["call_id"])
    assert fake_adapter.spawns[0].model == "fake-mini"
    # explicit choice beats the config default
    second = board.place_call("fake", "hi", project, model="fake-xl")
    finish_turn(board, second["call_id"])
    assert fake_adapter.spawns[1].model == "fake-xl"


def test_model_and_personality_are_independent(boards, fake_adapter, project, store):
    caller, _ = boards
    install_personality(store)
    first = caller.place_call("fake", "hi", project, personality="grumpy", model="fake-xl")
    finish_turn(caller, first["call_id"])
    req = fake_adapter.spawns[0]
    assert req.personality.name == "grumpy" and req.model == "fake-xl"
    second = caller.place_call("fake", "hi", project, personality="grumpy")
    finish_turn(caller, second["call_id"])
    assert fake_adapter.spawns[1].model is None


def test_unknown_personality_refused(boards, project):
    caller, _ = boards
    result = caller.place_call("fake", "hi", project, personality="nope")
    assert result["status"] == "refused"
    assert "unknown personality" in result["reason"]


def test_reply_resumes_same_session(boards, fake_adapter, project):
    caller, _ = boards
    placed = caller.place_call("fake", "first", project)
    finish_turn(caller, placed["call_id"])
    result = caller.reply(placed["call_id"], "second")
    assert result["status"] == "ringing"
    finish_turn(caller, result["call_id"])
    assert fake_adapter.resumes == [("fake-sess-1", "second")]


def test_concurrent_replies_do_not_double_spawn(boards, fake_adapter, project, store):
    caller, _ = boards
    placed = caller.place_call("fake", "first", project)
    finish_turn(caller, placed["call_id"])
    barrier = threading.Barrier(2)
    results = []

    def reply(i):
        barrier.wait()
        results.append(caller.reply(placed["call_id"], f"second-{i}"))

    threads = [threading.Thread(target=reply, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result["status"] for result in results) == ["busy", "ringing"]
    finish_turn(caller, placed["call_id"])
    assert len(fake_adapter.resumes) == 1
    caller_messages = [
        entry.body
        for entry in store.read_transcript(placed["call_id"])
        if entry.from_ == "claude"
    ]
    assert len([body for body in caller_messages if body.startswith("second-")]) == 1


def test_lost_session_falls_back_to_replay(boards, fake_adapter, project, store):
    caller, _ = boards
    placed = caller.place_call("fake", "first", project)
    finish_turn(caller, placed["call_id"])
    fake_adapter.lose_session = True
    result = caller.reply(placed["call_id"], "second")
    assert result["status"] == "ringing"
    answer = finish_turn(caller, result["call_id"])
    assert answer["status"] == "answered"
    assert store.load_call(placed["call_id"]).resumed_via == "transcript-replay"
    replay = fake_adapter.spawns[-1].message
    assert "[claude]: first" in replay and "[claude]: second" in replay


def test_hang_up_mid_turn_stays_closed(boards, fake_adapter, project, store):
    caller, _ = boards
    fake_adapter.release = threading.Event()
    result = caller.place_call("fake", "slow one", project)
    assert result["status"] == "ringing"
    worker = threading.Thread(target=finish_turn, args=(caller, result["call_id"]))
    worker.start()
    assert fake_adapter.started.wait(timeout=1)

    caller.hang_up(result["call_id"], reason="changed my mind")
    fake_adapter.release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    record = store.load_call(result["call_id"])
    assert record.status == "closed"  # the late answer must not reopen the call
    assert record.unread_for == []
    assert not any("spawn-reply" in e.body for e in store.read_transcript(result["call_id"]))


def test_async_answer_goes_to_mailbox(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "slow one", project)
    assert result["status"] == "ringing"
    finish_turn(caller, result["call_id"])
    record = store.load_call(result["call_id"])
    assert record.status == "answered"
    assert record.unread_for == ["claude"]
    mailbox = caller.check_messages(project)
    assert mailbox["unread"][0]["call_id"] == result["call_id"]
    assert "spawn-reply" in mailbox["unread"][0]["preview"]


def test_progress_is_delivered_while_turn_is_running(
    boards, fake_adapter, project, store
):
    caller, callee = boards
    fake_adapter.release = threading.Event()
    placed = caller.place_call("fake", "slow one", project)
    callee.config.call_id = placed["call_id"]
    worker = threading.Thread(target=finish_turn, args=(caller, placed["call_id"]))
    worker.start()
    assert fake_adapter.started.wait(timeout=1)

    progress = callee.report_progress("halfway")

    assert progress["status"] == "reported"
    assert store.load_call(placed["call_id"]).status == "ringing"
    unread = caller.check_messages(project)["unread"]
    assert [(entry["kind"], entry["body"]) for entry in unread] == [
        ("progress", "halfway")
    ]

    fake_adapter.release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    final = caller.check_messages(project)["unread"]
    assert len(final) == 1
    assert final[0]["is_final"]


def test_progress_requires_active_call_context(boards, project):
    caller, callee = boards
    placed = caller.place_call("fake", "slow one", project)
    assert callee.report_progress("halfway", placed["call_id"])["status"] == "refused"
    caller.config.call_id = placed["call_id"]
    result = caller.report_progress("not the callee")
    assert result["status"] == "refused"
    assert "only the callee" in result["reason"]


def test_check_messages_returns_full_unread_body(boards, project):
    caller, callee = boards
    message = "please read all of this: " + ("x" * 300)
    left = caller.place_call("fake", message, project, mode="voicemail")

    unread = callee.check_messages(project)["unread"][0]

    assert unread["call_id"] == left["call_id"]
    assert unread["body"] == message
    assert len(unread["preview"]) < len(message)


# ------------------------------------------------------------- write access


def test_caller_grants_write_for_the_whole_call(boards, fake_adapter, project, store):
    caller, _ = boards
    result = caller.place_call("fake", "fix the bug", project, write=True)
    finish_turn(caller, result["call_id"])
    assert fake_adapter.spawns[0].write_access
    assert store.load_call(result["call_id"]).write
    # a reply that falls back to replay-spawn still carries the grant
    fake_adapter.lose_session = True
    reply = caller.reply(result["call_id"], "and the tests")
    finish_turn(caller, reply["call_id"])
    assert fake_adapter.spawns[1].write_access


def test_write_defaults_off(boards, fake_adapter, project):
    caller, _ = boards
    result = caller.place_call("fake", "just look", project)
    finish_turn(caller, result["call_id"])
    assert not fake_adapter.spawns[0].write_access


def test_callee_cannot_grant_write(boards, store, project, fake_adapter):
    _, _ = boards
    deep = Switchboard(
        Config(i_am="claude", home=store.home, max_hops=2),
        store,
        {"fake": fake_adapter},
    )
    deep.config.hop_count = 1  # this session is itself a callee
    result = deep.place_call("fake", "go change files", project, write=True)
    assert result["status"] == "refused"
    assert "top-level caller" in result["reason"]
    assert fake_adapter.spawns == []


def test_config_grant_still_works(store, home, fake_adapter, project):
    from pathlib import Path

    from tellmphone.config import ProjectPermissions

    cfg = Config(i_am="claude", home=home, timeout_s=30)
    cfg.permissions[str(Path(project).resolve())] = ProjectPermissions(write=True)
    board = Switchboard(cfg, store, {"fake": fake_adapter}, detach_turns=False)
    result = board.place_call("fake", "hi", project)
    finish_turn(board, result["call_id"])
    assert fake_adapter.spawns[0].write_access
    assert store.load_call(result["call_id"]).write
    cfg.permissions.clear()
    fake_adapter.lose_session = True
    reply = board.reply(result["call_id"], "keep editing")
    finish_turn(board, reply["call_id"])
    assert fake_adapter.spawns[1].write_access


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


def test_self_call_uses_headless_sibling_session(boards, project):
    caller, _ = boards
    claude_adapter = caller.adapters["claude"]

    result = caller.place_call("claude", "hello me", project)
    assert result["status"] == "ringing"
    finish_turn(caller, result["call_id"])
    assert claude_adapter.spawns[0].message == "hello me"

    followup = caller.reply(result["call_id"], "keep going")
    assert followup["status"] == "ringing"
    finish_turn(caller, followup["call_id"])
    assert claude_adapter.resumes == [("fake-sess-1", "keep going")]


def test_self_call_mailbox_does_not_return_its_own_question(boards, project):
    caller, _ = boards
    placed = caller.place_call("claude", "hello me", project)
    finish_turn(caller, placed["call_id"])

    unread = caller.check_messages(project)["unread"]

    assert [entry["body"] for entry in unread] == ["spawn-reply to: hello me"]


def test_invalid_mode_refused(boards, project):
    caller, _ = boards
    result = caller.place_call("fake", "hi", project, mode="later")
    assert result["status"] == "refused"
    assert "mode" in result["reason"]


def test_missing_project_dir_refused(boards, tmp_path):
    caller, _ = boards
    result = caller.place_call("fake", "hi", str(tmp_path / "missing"))
    assert result["status"] == "refused"
    assert "project_dir" in result["reason"]


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


def test_callee_reply_while_ringing_is_busy(boards, project, store):
    caller, callee = boards
    placed = caller.place_call("fake", "hi", project)
    record = store.load_call(placed["call_id"])
    record.status = "ringing"
    store.save_call(record)
    assert callee.reply(placed["call_id"], "hurry up")["status"] == "busy"


def test_reply_to_failed_call_is_refused(boards, project, store):
    caller, callee = boards
    placed = caller.place_call("fake", "hi", project)
    record = store.load_call(placed["call_id"])
    record.status = "failed"
    record.last_error = "boom"
    store.save_call(record)

    assert caller.reply(placed["call_id"], "retry")["status"] == "error"
    assert callee.reply(placed["call_id"], "manual answer")["status"] == "error"


def test_hang_up_stale_ringing_call(boards, project, store):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    record = store.load_call(placed["call_id"])
    record.status = "ringing"
    store.save_call(record)

    result = caller.hang_up(placed["call_id"], "stale")

    assert result["status"] == "closed"
    assert store.load_call(placed["call_id"]).status == "closed"


def test_third_party_cannot_hang_up(boards, store, project, fake_adapter):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    stranger = Switchboard(
        Config(i_am="gemini", home=store.home), store, {"fake": fake_adapter}
    )

    assert stranger.hang_up(placed["call_id"])["status"] == "error"
    assert stranger.hang_up("*")["status"] == "error"
    assert store.load_call(placed["call_id"]).status == "ringing"


def test_wait_observes_final_answer(boards, project):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    finish_turn(caller, placed["call_id"])

    result = caller.wait(placed["call_id"], timeout_s=1)

    assert result["status"] == "answered"
    assert result["response"] == "spawn-reply to: hi"
    assert result["usage"] == {"turns": 1}
    assert caller.check_messages(project)["unread"] == []


def test_detached_spawn_failure_records_failed(home, store, fake_adapter, project, monkeypatch):
    def fail_popen(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr("tellmphone.switchboard.subprocess.Popen", fail_popen)
    board = Switchboard(Config(i_am="claude", home=home), store, {"fake": fake_adapter})

    result = board.place_call("fake", "hi", project)

    assert result["status"] == "failed"
    assert "failed to start turn process" in result["error"]
    assert store.load_call(result["call_id"]).status == "failed"


def test_detached_spawn_uses_turn_command_and_env(home, store, fake_adapter, project, monkeypatch):
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        assert not kwargs["stdout"].closed
        assert not kwargs["stderr"].closed
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("tellmphone.switchboard.subprocess.Popen", fake_popen)
    board = Switchboard(Config(i_am="claude", home=home), store, {"fake": fake_adapter})

    result = board.place_call("fake", "hi", project)

    assert result["status"] == "ringing"
    assert seen["cmd"][1:4] == ["-m", "tellmphone.cli", "turn"]
    assert seen["cmd"][4] == result["call_id"]
    assert seen["kwargs"]["cwd"] == project
    assert seen["kwargs"]["env"][HOME_ENV] == str(home)
    assert seen["kwargs"]["env"][HOP_ENV] == "1"
    assert seen["kwargs"]["env"][CALL_ID_ENV] == result["call_id"]
    call_dir = store.call_dir(result["call_id"])
    assert (call_dir / "turn.stdout").exists()
    assert (call_dir / "turn.stderr").exists()


def test_unexpected_turn_failure_is_persisted(
    home, store, fake_adapter, project, monkeypatch
):
    board = Switchboard(
        Config(i_am="claude", home=home),
        store,
        {"fake": fake_adapter},
        detach_turns=False,
    )
    placed = board.place_call("fake", "hi", project)

    def explode(_request):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(fake_adapter, "spawn", explode)
    result = board.run_turn(placed["call_id"])

    assert result["status"] == "failed"
    record = store.load_call(placed["call_id"])
    assert record.status == "failed"
    assert "RuntimeError: parser exploded" in record.last_error


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
    assert answer["from"] == "fake"
    assert answer["response"] == "pong, saw it"
    assert fake_adapter.spawns == [] and fake_adapter.resumes == []

    # caller finds the reply
    box = caller.check_messages(project)
    assert box["unread"][0]["call_id"] == left["call_id"]
    assert "pong" in box["unread"][0]["preview"]


def test_caller_reply_on_voicemail_spawns_with_replay(boards, fake_adapter, project, store):
    caller, _ = boards
    left = caller.place_call("fake", "original ask", project, mode="voicemail")
    result = caller.reply(left["call_id"], "actually, live please")
    assert result["status"] == "ringing"
    answer = finish_turn(caller, result["call_id"])
    assert answer["status"] == "answered"
    assert store.load_call(left["call_id"]).resumed_via == "transcript-replay"
    assert "[claude]: original ask" in fake_adapter.spawns[0].message


def test_third_party_cannot_reply(boards, store, project, fake_adapter):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    stranger = Switchboard(
        Config(i_am="gemini", home=store.home), store, {"fake": fake_adapter}
    )
    result = stranger.reply(placed["call_id"], "let me in")
    assert result["status"] == "error"


def test_get_call_recovers_answer_after_another_session_reads_it(
    boards, store, project, fake_adapter
):
    caller, _ = boards
    placed = caller.place_call("fake", "hi", project)
    finish_turn(caller, placed["call_id"])
    other_session = Switchboard(
        Config(i_am="claude", home=store.home), store, {"fake": fake_adapter}
    )
    assert other_session.check_messages(project)["unread"]
    assert caller.check_messages(project)["unread"] == []

    recovered = caller.get_call(placed["call_id"])

    assert recovered["status"] == "answered"
    assert recovered["transcript"][-1]["body"] == "spawn-reply to: hi"
    assert recovered["transcript"][-1]["usage"] == {"turns": 1}

    stranger = Switchboard(
        Config(i_am="gemini", home=store.home), store, {"fake": fake_adapter}
    )
    assert stranger.get_call(placed["call_id"])["status"] == "error"


def test_phonebook(boards, store):
    caller, _ = boards
    install_personality(store)
    book = caller.phonebook()
    assert book["you_are"] == "claude"
    fake_entry = next(a for a in book["agents"] if a["name"] == "fake")
    assert fake_entry["available"] is True
    assert fake_entry["configured_model"] is None
    assert fake_entry["models"] == []  # FakeAdapter has no catalog
    assert any(p["name"] == "grumpy" for p in book["personalities"])
