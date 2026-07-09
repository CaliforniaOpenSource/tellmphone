# TeLLMphone design

This document records the architectural rules that should remain true as the
implementation changes. The README explains how to install and use the tool;
the MCP tool docstrings are the caller-facing API reference.

## 1. Product boundary

TeLLMphone lets one local coding agent ask another local coding agent about a
project. A call is point-to-point, threaded, and recoverable across agent
restarts. The caller and callee may be different agent kinds or separate
sessions of the same agent with a different model or personality.

TeLLMphone is deliberately not:

- a daemon or general message bus;
- a remote service;
- an autonomous agent loop;
- a conference-call or workflow engine;
- a push-notification system.

Delivery is pull-based. Agents use `check_messages`; humans can use the CLI.

## 2. Terms

- **Call:** one conversation with a stable `call_id`.
- **Agent:** a supported coding CLI such as Claude Code, Codex, Gemini, or Grok.
- **Adapter:** subprocess integration for one agent CLI.
- **Switchboard:** lifecycle logic plus the filesystem store.
- **Voicemail:** a queued message that does not start a headless callee.
- **Personality:** a named, saved prompt applied to the callee.
- **Hop count:** the depth of an agent-to-agent call chain.

## 3. Process and identity model

Each host agent starts its own TeLLMphone MCP server over stdio. Instances share
state under `~/.tellmphone/`; there is no long-running TeLLMphone process.

A live call works as follows:

1. The caller records the call and first transcript entry.
2. TeLLMphone starts one detached `tellmphone turn <call_id>` process and
   returns `ringing` immediately.
3. The turn process acquires `turn.lock`, runs one adapter turn, writes the
   answer or failure to disk, and exits.
4. The caller observes progress and the final answer through `check_messages`
   or retrieves the call directly with `get_call`.

The terminal CLI may wait by polling the same call record. It does not use a
separate execution path.

A mailbox address is `(canonical project path, agent kind)`. Multiple sessions
of the same agent therefore share unread state. `get_call(call_id)` is the
durable recovery path if another session consumes a notification.

Transcript entries also store `caller`/`callee` endpoint roles. Roles are
required because agent names alone cannot distinguish a self-call.

## 4. Storage

```text
~/.tellmphone/
├── config.toml
├── personalities/*.md
└── projects/<slug>-<path-hash>/
    ├── project.json
    └── calls/<call_id>/
        ├── call.json
        ├── call.lock
        ├── turn.lock
        ├── transcript.jsonl
        ├── turn.stdout
        └── turn.stderr
```

State is outside the project repository so transcripts cannot be committed by
accident. Project keys use the canonical path, allowing restarted sessions to
find the same mailbox.

`call.json` contains the parties, native callee session id, model, personality
snapshot, permissions, hop count, timestamps, delivery cursors, and status.
Valid statuses are:

- `ringing`: a detached turn is in flight;
- `answered`: the latest answer has landed;
- `voicemail`: queued for a manual callee;
- `closed`: logically hung up;
- `failed`: the turn ended with an error.

`transcript.jsonl` is the durable conversation. Each entry contains a sequence
number, agent names, endpoint roles, body, timestamp, kind (`message`,
`progress`, or `system`), and optional usage metadata.

Lock responsibilities are separate:

- `call.lock` protects short metadata updates;
- `turn.lock` permits only one adapter turn per call;
- the transcript file lock makes sequence assignment and append atomic.

Never hold `call.lock` while an agent CLI is running.

## 5. Call lifecycle and tools

`call` validates the project, hop limit, callee, personality, and write grant.
It snapshots the resolved model and personality for the lifetime of the call.

- `mode="wait"` starts a detached turn and returns `ringing`.
- `mode="voicemail"` records the message for a future manual callee.

`reply` is direction-aware:

- a caller reply starts another detached callee turn;
- a manual callee reply writes the answer directly without spawning itself;
- a reply while `ringing` returns `busy`.

Native callee sessions are resumed when possible. If a session is lost or an
adapter exposes no stable session id, TeLLMphone starts a fresh session with a
reconstructed transcript prompt and records `resumed_via="transcript-replay"`.

`report_progress` is available only inside the active detached callee. The
callee inherits `TELLMPHONE_CALL_ID`; an update appends a `progress` entry,
marks the caller unread, and leaves the call `ringing`.

`check_messages` returns unread entries and open calls for a project. Reading
advances that agent's delivery cursor. `get_call` returns an authorized call's
full transcript without changing unread state.

`hang_up` is logical: it marks the call `closed` but does not kill an agent CLI.
A late turn must re-read the record and must never reopen a closed call.

`phonebook` reports installed adapters, availability, configured models, and
personalities.

If a detached turn cannot start or raises an unexpected exception, the call
must become `failed` with `last_error`; it must not remain indefinitely
`ringing` after the worker exits.

## 6. Security and permissions

This is a same-user local tool, not a security boundary against processes that
can already read the user's home directory. It still enforces integrity at the
tool layer:

- call ids must match the generated format and are never treated as glob input;
- MCP agents may retrieve or mutate only calls they participate in;
- progress is bound to the active detached callee;
- hop limits prevent unbounded agent-to-agent chains.

Callees run in the most restrictive supported mode by default. Write access is
enabled only by a standing project grant in `config.toml` or `write=true` on a
top-level call. A callee cannot extend write permission to another agent lower
in the chain. The grant is pinned for the lifetime of the call.

Headless Claude sessions explicitly pre-approve `mcp__tellmphone__*` so they can
report progress and use the switchboard under `dontAsk`; other tools still
follow the selected restrictive permission mode.

Agent output is untrusted model-generated content. Callers should relay and
evaluate it rather than blindly execute its instructions.

Call metadata and transcripts are written with user-only permissions. They may
contain source code or secrets that entered the conversation; users can remove
closed or failed calls with `tellmphone gc` or delete the TeLLMphone home.

## 7. Personalities

Built-in personalities ship as Markdown package data. User personalities live
under `~/.tellmphone/personalities/` and override built-ins by name; a user file
with `disabled: true` hides that personality.

The switchboard resolves a personality by name, checks its optional agent
allowlist, and stores a body/hash snapshot on the call. Editing the source file
cannot change an in-flight conversation. Adapters inject the snapshot only on
the first native session or a transcript-replay spawn.

There is no MCP tool for writing personalities. They are human-managed input.

## 8. Adapters

An adapter is a small subprocess wrapper. It owns:

- CLI availability detection;
- MCP registration and removal where supported;
- argv, cwd, sandbox, and environment construction;
- first-turn spawn and native-session resume;
- output, session-id, error, and usage parsing.

Adapters return `AgentTurn` and raise `AdapterError` for ordinary failures or
`SessionLost` when replay should replace resume. They do not manage call state,
mailboxes, locking, or personalities beyond CLI-specific prompt injection.

Child environments carry the hop count and opaque call id. Host-session markers
that would falsely make a child look nested are scrubbed, while credentials and
provider configuration are preserved.

Built-in adapters are loaded first. Third-party adapters use the
`tellmphone.adapters` entry-point group and may override an adapter by name.

## 9. Change checklist

Changes to the lifecycle should preserve these invariants:

1. The filesystem is the source of truth; caller process lifetime is irrelevant.
2. A call has at most one running adapter turn.
3. Locks are held only for short filesystem operations.
4. Transcript sequence numbers are unique and ordered.
5. Native session ids never become caller-facing conversation keys.
6. Lost sessions degrade to transcript replay.
7. Late answers never reopen closed calls.
8. Worker failures always produce a terminal recorded state.
9. Write permissions and hop counts never expand down a chain.
10. Tool results from other agents remain untrusted input.
