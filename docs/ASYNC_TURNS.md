# Async turns

TeLLMphone should be a filesystem switchboard that can kick off one detached
agent turn. It should not be a long-running phone runtime.

## Problem

Today a live call runs inside the caller's MCP server process. `call()` or
`reply()` starts a Python worker thread, waits up to a timeout, and returns
`ringing` if the thread is still alive. That means a long call only survives
while the caller's MCP process survives. It also forces mailbox semantics,
timeouts, and progress reporting to sit behind one in-process join loop.

That is the wrong durability boundary. The durable boundary is already the
filesystem: `call.json`, `transcript.jsonl`, and per-call locks.

## Design

All live headless turns are async-first:

1. A tool call mutates the call record and transcript under file locks.
2. If a headless callee should run now, it starts one detached process:
   `tellmphone turn <call_id>`.
3. The detached turn process runs exactly one adapter turn, writes the final
   result or failure to disk, and exits.
4. Callers use `check_messages()` to observe replies. Human CLI wait behavior
   is only a convenience wrapper over the same filesystem state.

There is no daemon, runner loop, queue, PID table, or in-process worker thread.

## State

Keep the existing call statuses for compatibility:

- `ringing`: a detached headless turn is in flight.
- `answered`: the latest headless or manual answer has landed.
- `voicemail`: queued for a future/manual callee session; no process is running.
- `closed`: hung up.
- `failed`: a detached turn failed.

Remove the `working` status and progress reporting from this MVP. A call is
either in flight (`ringing`) or not. Historical `progress` transcript entries
may remain readable, but no new tool should write them.

`hang_up()` is logical. It closes the call record; it does not kill a process.
A late detached turn must reload the call before writing its final answer and
must not reopen a closed call.

## Tool Semantics

### `call(...)`

Creates a call record and appends the caller's message.

- `mode="voicemail"`: mark unread for the callee and do not spawn anything.
- `mode="wait"`: spawn `tellmphone turn <call_id>` and return immediately
  with `status="ringing"`. The name is kept for compatibility, but live calls
  no longer block inside the MCP server.

If spawning the detached turn fails, set `failed` and return the error
synchronously. Do not leave a call stuck in `ringing` because `Popen` failed.

### `reply(call_id, message)`

If the callee is replying manually, append the answer and mark it unread for
the caller.

If the caller is replying, append the message, set status to `ringing`, and
spawn one detached turn process.

If the call is already `ringing`, return `busy`.

### `check_messages(project_dir)`

Remains the primary async API. It reads unread transcript entries and open call
state from disk. Fetching marks delivered entries as read.

## Detached Turn Process

`tellmphone turn <call_id>` is internal plumbing.

It must:

1. Load the call record.
2. Refuse to run if the call is closed, failed, or not `ringing`.
3. Acquire a separate non-blocking `turn.lock` as the in-flight signal. If it
   cannot be acquired, another turn process is already running; exit without
   writing.
4. Use `call.lock` only for short call-record critical sections. Never hold
   `call.lock` while running an adapter.
5. Under `call.lock`, verify the call is still `ringing`; otherwise exit.
6. Run `adapter.spawn()` if no callee session exists; otherwise run
   `adapter.resume()`.
7. If resume is lost, run transcript replay.
8. Under `call.lock`, reload the call. If it was closed, append nothing and
   exit. Otherwise append the final callee message, store native session id,
   set `answered`, and mark the caller unread.
9. On adapter failure, set `failed` and store `last_error`, unless the call was
   closed.

The turn process may be started with `subprocess.Popen(..., start_new_session=True,
stdin=DEVNULL)`. Its stdout/stderr should go to small files in the call
directory (`turn.stdout`, `turn.stderr`); the result is the call record, not
process output.

The parent should start the same installed/current TeLLMphone command it would
register for MCP, not assume a bare `tellmphone` exists on `PATH`. The detached
process should inherit the caller environment so adapter credentials and
configuration remain available.

## Deleted From This MVP

- In-process worker thread and `join(timeout)`.
- Progress reporting as a required feature.
- `working` status.
- PID tracking.
- A persistent runner process or daemon.
- Push notifications.
- Killing adapter subprocesses on hang-up.

The MCP `report_progress` tool should be removed, or left as a compatibility
stub that returns `refused`. It is not part of the async-turn MVP.

## Compatibility

The public tool names stay stable. The biggest behavior change is that live
MCP `call()` no longer waits for an inline answer; it returns a call id and
`ringing`. The terminal CLI may preserve the old human-friendly blocking
behavior by calling `wait()` after spawning.

Old call records without newer delivery fields must still load through pydantic
defaults.

## Tests

Cover:

- live call spawns a detached turn and returns `ringing`;
- `wait()` observes the final answer;
- `check_messages()` observes the final answer;
- caller `reply()` spawns exactly one detached turn;
- callee manual reply still works without spawning;
- `ringing` call refuses another reply as busy;
- `hang_up()` before a late turn finishes does not reopen the call;
- failed adapter turn records `failed`;
- detached spawn failure records `failed`;
- stale `ringing` calls can be closed with `hang_up`;
- transcript replay still works when native session resume is lost.
