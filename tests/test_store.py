import threading

import pytest

from tellmphone.store import (
    CallBusy,
    CallNotFound,
    CallRecord,
    Party,
    TranscriptEntry,
    new_call_id,
    project_key,
    utcnow,
)


def make_record(project, call_id="call-t3st"):
    return CallRecord(
        call_id=call_id,
        project_dir=project,
        caller=Party(agent="claude"),
        callee=Party(agent="codex"),
        status="ringing",
        created_at=utcnow(),
        last_activity_at=utcnow(),
    )


def test_call_roundtrip(store, project):
    record = make_record(project)
    store.create_call(record)
    loaded = store.load_call("call-t3st")
    assert loaded.caller.agent == "claude"
    assert loaded.callee.agent == "codex"
    assert loaded.status == "ringing"


def test_load_unknown_call(store, project):
    store.project_box(project)
    with pytest.raises(CallNotFound):
        store.load_call("call-nope")


def test_project_key_is_stable_and_distinct(tmp_path):
    a = tmp_path / "My Project"
    b = tmp_path / "other"
    a.mkdir(), b.mkdir()
    assert project_key(a) == project_key(a)
    assert project_key(a) != project_key(b)
    assert project_key(a).startswith("my-project-")


def test_transcript_append_and_seq(store, project):
    record = make_record(project)
    store.create_call(record)
    for body in ["hello", "world"]:
        entry = store.append_transcript(
            record.call_id,
            TranscriptEntry(from_="claude", to="codex", body=body, ts=utcnow()),
        )
        assert entry.seq > 0  # append assigns the seq
    transcript = store.read_transcript(record.call_id)
    assert [e.seq for e in transcript] == [1, 2]
    assert transcript[1].body == "world"
    # "from" is the wire name
    raw = (store.call_dir(record.call_id) / "transcript.jsonl").read_text()
    assert '"from":"claude"' in raw.replace(" ", "")


def test_concurrent_appends_never_share_a_seq(store, project):
    """Regression: seq assignment and the write are atomic under the
    transcript lock, so parallel writers can't duplicate a seq or
    interleave partial lines."""
    record = make_record(project)
    store.create_call(record)
    n_threads, per_thread = 8, 5
    barrier = threading.Barrier(n_threads)

    def writer(i):
        barrier.wait()
        for j in range(per_thread):
            store.append_transcript(
                record.call_id,
                TranscriptEntry(from_="claude", to="codex", body=f"{i}-{j}", ts=utcnow()),
            )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    transcript = store.read_transcript(record.call_id)  # every line parses
    total = n_threads * per_thread
    assert [e.seq for e in transcript] == list(range(1, total + 1))
    assert {e.body for e in transcript} == {
        f"{i}-{j}" for i in range(n_threads) for j in range(per_thread)
    }


def test_lock_busy(store, project):
    record = make_record(project)
    store.create_call(record)
    with store.call_lock(record.call_id):
        with pytest.raises(CallBusy):
            with store.call_lock(record.call_id, blocking=False):
                pass


def test_new_call_id_shape():
    cid = new_call_id()
    assert cid.startswith("call-") and len(cid) == 9
