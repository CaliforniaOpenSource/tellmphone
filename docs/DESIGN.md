# TeLLMphone — Design

Status: **implemented (v0.1)** — written before implementation, updated as
built; §13 records what implementation taught us. `[verify]` markers on CLI
details are resolved there.

## 1. Goals

1. Let a coding agent (the **caller**) send a message to another coding agent
   (the **callee**) for a given project directory, and get a response.
2. Keep a call **conversational**: follow-up messages go to the same callee
   session, with full context, even if either side's session was interrupted,
   compacted, or restarted in between.
3. **Personalities**: callers pick a named, locally stored persona for the
   callee instead of sending system prompts inline. The persona is applied
   consistently for the lifetime of the call.
4. **Answering machine**: an agent can ask "any messages for me in this
   project?" and reply to them.
5. Support **Claude Code and Codex** first; make adding other agents a
   plugin, not a fork.

### Non-goals (v0.x)

- No push notifications to a running agent mid-turn. Delivery is pull-based
  (`check_messages`). Hooks-based nudges are a future idea (§12).
- No remote/networked calls. Everything is one machine, one user.
- No autonomous agent-to-agent loops without a human-owned session at the
  root. Hop limits enforce this (§10).
- Not a general message bus. It's a telephone: point-to-point, threaded.

## 2. Glossary (the telephone metaphor, precisely)

| Term | Meaning |
|---|---|
| **Agent** | A CLI coding assistant TeLLMphone can drive headlessly (Claude Code, Codex). |
| **Adapter** | The Python class that knows how to spawn/resume one agent kind. |
| **Call** | A threaded conversation between a caller and a callee, scoped to a project directory. Has a stable `call_id`. |
| **Switchboard** | The on-disk state store (`~/.tellmphone/`) plus the logic that routes messages to adapters. Not a daemon. |
| **Personality** | A named, saved system-prompt package applied to the callee for the duration of a call. |
| **Voicemail** | A message delivered into the mailbox instead of answered live — either because the caller chose async, or because a live call timed out. |
| **Phonebook** | The registry of available adapters and personalities. |
| **Hop count** | How many agent-to-agent links deep a call chain is. Bounded. |

## 3. Architecture

### 3.1 Process model: no daemon, shared disk state

Each agent process runs its **own instance** of the TeLLMphone MCP server over
stdio (this is how both Claude Code and Codex launch MCP servers). Instances
coordinate exclusively through the filesystem under `~/.tellmphone/`, guarded
by per-call file locks.

Rejected alternative — a single long-lived HTTP/SSE daemon both agents connect
to. It would make cross-instance coordination trivial, but adds lifecycle
management (who starts it, when does it die, stale-port recovery) that is
miserable for a local tool. Disk state + locks is enough at "a handful of
calls per day" volume. If contention ever becomes real, the storage layer is
isolated enough to swap in SQLite (§13).

### 3.2 How a call executes

Placing a call does **not** talk to a running agent. The caller's TeLLMphone
instance spawns the callee's CLI **headlessly as a subprocess**:

- Codex callee: `codex exec` with the project as working directory `[verify: --cd / -C flag]`,
  JSON output mode to capture the session id `[verify: --json event stream and/or --output-last-message]`.
- Claude callee: `claude -p <msg> --output-format json`, which returns
  `session_id` and `result` `[verify]`.

Follow-ups resume the callee's native session:

- Codex: `codex exec resume <session_id> <msg>` `[verify]`
- Claude: `claude -p <msg> --resume <session_id>` `[verify]`

This is the crux of "session matching": **TeLLMphone owns a stable `call_id`
and stores each side's volatile native session ids under it.** Callers never
handle native session ids; adapters never see call ids.

### 3.3 Who is "me"? Identity for the mailbox

A mailbox address is the pair **(project directory, agent kind)** — e.g.
"claude at ~/ws/foo". When Claude runs `check_messages` in a project, it asks
for messages addressed to `claude` there. The MCP server knows which agent
kind it's mounted in via server configuration (the `claude mcp add` entry
passes `--i-am claude`; the Codex config passes `--i-am codex`). Project
directory is passed per-call by the calling model (it knows its cwd; MCP
servers can't reliably infer the client's cwd `[verify: whether client roots
are exposed]`).

## 4. Storage layout

```
~/.tellmphone/
├── config.toml                  # global settings: timeouts, hop limit, permissions
├── personalities/
│   ├── grumpy-reviewer.md       # frontmatter + prompt body (§7)
│   └── security-auditor.md
└── projects/
    └── <slug>-<sha1[:12] of canonical path>/     # e.g. tellmphone-a1b2c3d4e5f6
        ├── project.json         # canonical path (for reverse lookup)
        └── calls/
            └── <call_id>/
                ├── call.json    # metadata: parties, sessions, personality, status
                ├── call.lock    # advisory lock during spawn/resume/append
                └── transcript.jsonl
```

Decisions baked in here:

- **Central state, not in-repo.** No `.tellmphone/` inside projects: nothing
  to gitignore, no risk of committing transcripts, works for non-repo dirs.
  Keyed by canonicalized (realpath) project path so `check_messages` from any
  session of any agent finds the same box.
- **JSON files, not a database.** Human-inspectable, trivially debuggable,
  no migration story needed pre-1.0. One lock file per call serializes the
  only real race (two processes appending to one call).
- `call_id` is a short human-friendly id (e.g. `call-7f3k`), because the
  calling LLM will read and re-type it.

### 4.1 `call.json`

```json
{
  "call_id": "call-7f3k",
  "project_dir": "/Users/kdewald/ws/foo",
  "caller": {"agent": "claude"},
  "callee": {
    "agent": "codex",
    "session_id": "0197c-...",        // native id, owned by the adapter
    "personality": "grumpy-reviewer",
    "personality_hash": "sha256:ab12…" // snapshot so edits don't mutate live calls
  },
  "status": "answered",               // ringing | answered | voicemail | closed | failed
  "hop_count": 1,
  "created_at": "2026-07-02T18:04:11Z",
  "last_activity_at": "2026-07-02T18:09:42Z",
  "unread_for": ["claude"]            // who has messages they haven't fetched
}
```

### 4.2 `transcript.jsonl`

One JSON object per message: `{seq, from, to, body, ts, read}`. The transcript
is the durable record; `unread_for` in `call.json` is the cheap index that
lets `check_messages` scan a project without opening every transcript.

## 5. The MCP tool surface

Five tools. Deliberately few — every tool is context the calling model pays
for on every turn.

### 5.1 `call`

```
call(
  callee: str,              # "codex" | "claude" | any registered adapter
  message: str,             # the actual ask
  project_dir: str,         # absolute path; callee runs here
  personality: str = None,  # name from the phonebook; adapter default otherwise
  model: str = None,        # which model the callee runs; see below
  context: str = None,      # optional extra background, kept separate from the ask
  mode: "wait" | "voicemail" = "wait",
  timeout_s: int = None,    # default from config (suggest 300)
)
→ {call_id, status, response?}
```

- `mode="wait"`: spawn the callee, block, return its answer inline. If
  `timeout_s` elapses, the subprocess **keeps running detached**; the tool
  returns `status="ringing"` with the call id and a note to check back —
  the answer lands in the mailbox when the callee finishes. This matters
  because MCP clients have their own tool timeouts and codex runs can take
  minutes; the caller must never lose a slow answer.
- `mode="voicemail"`: enqueue only. The callee is *not* spawned; the message
  waits until some session of the callee agent in that project runs
  `check_messages` and chooses to answer. (Human-in-the-loop async.)
- `context` vs `message`: adapters frame them distinctly ("Background: … /
  Your task: …") so callees don't chase the background as the ask.
- `model` is deliberately **orthogonal to personality**: a personality says how
  the callee behaves, the model says which brain runs it, and any combination
  is valid. Resolution order: explicit `model` argument → `[agents.<name>]
  model` default in config.toml → the CLI's own default. The resolved model is
  **pinned in `call.json` for the lifetime of the call** — every resume and
  transcript-replay uses it, so a conversation never silently switches brains
  mid-thread. Names are passed through unvalidated (`claude --model`,
  `codex exec -m`); a bad name comes back as a `failed` status carrying the
  CLI's error. Changing model mid-conversation means placing a new call.

### 5.2 `reply`

```
reply(call_id: str, message: str, mode="wait", timeout_s=None)
→ {call_id, status, response?}
```

Resumes the callee's stored native session. If the callee's session can't be
resumed (deleted, CLI upgraded, etc.), the adapter falls back to spawning a
fresh session **primed with the transcript so far**, records the new native
session id, and flags `"resumed_via": "transcript-replay"` in the response so
nobody is silently confused. The transcript makes calls durable even when
native sessions aren't.

### 5.3 `check_messages`

```
check_messages(project_dir: str) → {
  unread: [{call_id, from, personality?, last_message_preview, ts}],
  open_calls: [{call_id, with, status, last_activity_at}],
}
```

Answers "anything for me here?" for the agent identity this server instance
is mounted in (§3.3). Fetching marks messages read. This is also the recovery
path: a brand-new session that knows nothing can rediscover every open thread
in the project.

### 5.4 `hang_up`

`hang_up(call_id, reason?)` → marks the call `closed`. Transcript is retained.
Replying to a closed call fails with a clear error suggesting a new `call`.

### 5.5 `phonebook`

`phonebook()` → registered adapters (with availability: is the CLI actually
installed?) and personalities (name + one-line description). Lets the caller
discover what it can dial without any out-of-band knowledge.

## 6. Call lifecycle & session matching (the hard part)

```
caller (claude)                switchboard                    callee (codex)
   │  call(codex, msg, dir)         │                              │
   ├───────────────────────────────►│ create call-7f3k, lock       │
   │                                ├── spawn: codex exec ────────►│
   │                                │   … capture session_id …     │ runs in dir
   │                                │◄── final message + id ───────┤
   │◄── {call-7f3k, response} ──────┤ store session_id, transcript │
   │                                │                              │
   │  (caller session dies, user restarts Claude tomorrow)         │
   │                                │                              │
   │  check_messages(dir)           │                              │
   ├───────────────────────────────►│ scan project box             │
   │◄── open_calls: [call-7f3k] ────┤                              │
   │  reply(call-7f3k, "but…")      │                              │
   ├───────────────────────────────►├── codex exec resume <id> ───►│ same context
```

Invariants:

1. **`call_id` is the only cross-session key.** It appears in every tool
   result and in `check_messages`, so no side ever needs to persist anything
   in its own context to recover a thread.
2. **Native session ids are adapter-private.** They're stored in `call.json`
   and never surfaced to models — they're volatile and agent-specific.
3. **Caller-side interruption costs nothing** (the caller is stateless w.r.t.
   TeLLMphone). **Callee-side session loss degrades gracefully** via
   transcript replay (§5.2).
4. **One writer per call.** The per-call lock serializes spawn/resume/append;
   a `reply` while the callee is still running returns `status="busy"`
   ("the line is engaged") rather than forking the conversation.

## 7. Personalities

A personality is a Markdown file in `~/.tellmphone/personalities/`:

```markdown
---
name: grumpy-reviewer
description: Hostile-but-fair senior reviewer. Hunts for real bugs, hates nits.
agents: [claude, codex]        # optional allowlist; default any
---
You are a grumpy but rigorous senior engineer reviewing a colleague's work…
```

Rules:

- **Selected by name, injected by the switchboard.** The caller sends
  `personality="grumpy-reviewer"`; the callee-side injection mechanism is the
  adapter's business: Claude gets `--append-system-prompt` `[verify]`; Codex
  has no clean system-prompt flag in exec mode `[verify]`, so its adapter
  prepends a clearly framed preamble to the first message.
- **Snapshot at call time.** `call.json` records the personality's content
  hash. Editing a personality file never changes the behavior of an in-flight
  call — a resumed session already has the old prompt in its context, and the
  hash makes that explicit rather than accidental.
- **First message only.** Personalities are injected once at spawn; resumes
  rely on the callee's own session memory. (Transcript-replay fallback
  re-injects from the snapshot stored in the transcript's first entry.)
- Personalities are user-managed files (create/edit with any editor). A
  `tellmphone personalities` CLI listing + a few starter personas ship in the
  box. No MCP tool for *writing* personalities in v0 — that's a human curation
  job, and letting agents author each other's system prompts is a footgun.

## 8. Adapter interface

```python
class AgentAdapter(ABC):
    name: str                      # "claude", "codex"

    @abstractmethod
    def available(self) -> bool: ...          # CLI installed & sane version?

    @abstractmethod
    def spawn(self, req: SpawnRequest) -> AgentTurn: ...
        # first message; returns (native_session_id, response_text, usage?)

    @abstractmethod
    def resume(self, session_id: str, message: str,
               req: TurnRequest) -> AgentTurn: ...
        # raises SessionLost -> switchboard does transcript replay via spawn()

    def default_permissions(self) -> Permissions: ...   # §10
```

- Adapters are **subprocess wranglers, nothing more**: build argv, set cwd,
  parse output, extract session id. All threading/state/personality logic
  lives in the switchboard so adapters stay ~100 lines.
- Third-party adapters register via the `tellmphone.adapters` entry-point
  group; `phonebook()` reflects whatever is installed. Gemini CLI, opencode,
  etc. become `pip install tellmphone-gemini`, no core changes.

## 9. Client setup

`tellmphone install` registers the server with every detected agent CLI;
`tellmphone uninstall` reverses it. Registration lives on the adapter
(`AgentAdapter.register_mcp`), because plugging into an agent is per-agent
knowledge just like spawning it — a third-party adapter brings its own.

- **claude**: `claude mcp add tellmphone --scope user -- <serve-cmd>`.
- **codex**: `codex mcp add tellmphone -- <serve-cmd>`, then patches
  `default_tools_approval_mode = "approve"` into the server's table in
  `config.toml` — without it, codex's non-interactive exec mode auto-rejects
  every MCP tool call as "user cancelled" (the value `"auto"` does NOT mean
  auto-approve; empirically only `"approve"` pre-approves tools).

The serve command is resolved automatically: a source checkout registers
`uv run --project <root> tellmphone serve --i-am <agent>`; an installed
package registers the `tellmphone` executable; `--serve-command` overrides.

Optional companion packages, generated by `tellmphone install-etiquette`:

- A **Claude skill** (`~/.claude/skills/tellmphone/SKILL.md`): when a second
  opinion is worth it, how to phrase asks, check messages when starting work
  in a project, always relay the callee's answer to the human.
- An **`AGENTS.md` snippet** for Codex with the same guidance.

These are instructions, not machinery — the MCP server works without them,
but they make agents *use* the phone well.

## 10. Security & safety model

- **Callee permissions are config, not caller choice.** Default: callees run
  in the most restricted headless mode available (Claude: default `-p`
  permission mode, no `--dangerously-skip-permissions`; Codex: default
  read-only sandbox `[verify]`). Granting a callee write/exec access is a
  per-project entry in `config.toml` that only the human edits. A caller
  asking for more than configured gets a refusal in the tool result.
- **Hop limit.** Every spawned callee gets the TeLLMphone server too, so
  Codex could call Claude could call Codex… `hop_count` travels in an env var
  (`TELLMPHONE_HOP=1`) set on spawned subprocesses; at the configured max
  (default **2**) the `call` tool refuses. This is the "no infinite game of
  telephone" rule.
- **Transcripts are untrusted content.** Callee output relayed to the caller
  is another model's text, subject to prompt injection like any tool result.
  The etiquette skill says so explicitly; the server wraps relayed messages
  in a frame identifying them as quoted agent output.
- **Cost visibility.** Every live call burns callee-side tokens on the
  human's accounts. Tool results include duration and, where the CLI reports
  it, token usage `[verify]`, so the human sees what a call cost.
- Transcripts may contain code and secrets that flowed through prompts;
  they're `0600` under the user's home dir, and `hang_up` + a
  `tellmphone gc` command allow cleanup.

## 11. Package shape

```
tellmphone/
├── pyproject.toml            # uv-managed; deps: mcp (official SDK), pydantic
├── src/tellmphone/
│   ├── server.py             # FastMCP app: the 5 tools
│   ├── switchboard.py        # call lifecycle, locking, mailbox
│   ├── store.py              # ~/.tellmphone layout, JSON I/O, locks
│   ├── personalities.py
│   ├── adapters/
│   │   ├── base.py           # AgentAdapter ABC + entry-point loading
│   │   ├── claude.py
│   │   └── codex.py
│   └── cli.py                # tellmphone serve | personalities | gc | install-etiquette
└── tests/                    # adapters tested against fake CLI scripts
```

Python ≥3.11. `uvx tellmphone serve` is the blessed run mode (no install step
for users who have uv).

## 12. Roadmap

- **v0.1 — dial tone.** `call`/`reply`/`hang_up`/`phonebook`, wait mode only,
  Claude + Codex adapters, personalities. Prove session matching round-trips.
- **v0.2 — answering machine.** `check_messages`, voicemail mode, ringing→
  mailbox timeout flow, unread tracking, etiquette skill + AGENTS.md.
- **v0.3 — switchboard upgrades.** Entry-point adapter plugins documented for
  third parties, `tellmphone gc`, token/cost reporting, transcript replay
  hardening.
- **Later / maybe.** Conference calls (fan-out one ask to N callees, collect
  answers); hooks-based new-message nudges so agents notice voicemail without
  polling; SQLite backend if JSON+locks ever hurts; remote calls (explicitly
  out of scope until there's a real user).

## 13. Implementation notes (v0.1, 2026-07-02)

Learned while building; the sections above remain the intent.

- **Nested-Claude guard.** Claude Code refuses to start when it detects it is
  running inside another Claude Code process (via `CLAUDECODE` /
  `CLAUDE_CODE_*` environment variables). Adapters therefore scrub those
  variables from every callee subprocess environment (`AgentAdapter.child_env`)
  — the markers describe the host, not the child, so removing them is
  correcting a lie, not evading a safety check we care about; TeLLMphone's
  own hop limit (`TELLMPHONE_HOP`, set in the same place) is what prevents
  runaway nesting.
- **`reply` is direction-aware.** When the *caller* replies, the callee is
  driven headlessly (resume/spawn). When the *callee* replies — answering a
  voicemail from its own live session — no subprocess runs at all: the answer
  is appended to the transcript and flagged unread for the caller. Same tool,
  two mechanics; §5.2's description covers only the first, this note is the
  spec for the second.
- **Voicemail → live conversion.** A caller replying on a call whose callee
  has no native session yet (voicemail never spawned one) gets a fresh spawn
  primed via transcript replay — the same path as lost-session recovery.
- **Codex adapter live-verified** (codex-cli 0.142.5, 2026-07-02): real
  `codex exec` call in a project dir, session id captured from the `--json`
  stream (`session_configured` event), follow-up via native
  `codex exec resume <id>` with correct context carry-over, and `-m/--model`
  pass-through (invalid names surface the API error as `failed`, as
  designed). Two flag facts learned: `--cd` and `--sandbox` are **spawn-only**
  — `exec resume` rejects them and inherits both from the original session —
  and codex treats a piped stdin as extra prompt input.
- **Callee subprocesses get `stdin=/dev/null`.** In server mode our stdin is
  the MCP JSON-RPC transport; a callee inheriting it could consume protocol
  bytes meant for the host (and codex would happily read it as prompt).
- **Claude adapter live-verified** (claude CLI 2.1.42, 2026-07-02): real
  `claude -p` call spawned from inside a Claude Code session (env scrub
  working as designed), with `--append-system-prompt` (personality),
  `--model` pinning, session id captured from the JSON result, native
  `--resume` follow-up with correct context carry-over, and cost/usage
  reported back. Gotcha for troubleshooting: claude reports many failures as
  JSON on **stdout** with an empty stderr (e.g. a stale CLI OAuth token →
  `is_error: true` + 401), so adapter errors must include stdout.
- **Callee sandboxes constrain tooling, not just the repo.** Codex's
  workspace-write sandbox blocked `uv`'s cache under `~/.cache/uv`; the fix
  (relayed over a live `reply`, which codex applied) was `UV_CACHE_DIR=$TMPDIR/…`.
  Expect callees to occasionally need this kind of nudge — a good example for
  the etiquette skill.

## 14. Open questions

1. **Codex session-id capture**: exactly which `codex exec` output mode
   yields the session id most robustly (JSONL event vs. session file under
   `~/.codex/sessions`)? Decide during adapter spike.
2. **Client roots**: can the server learn the client's cwd via MCP `roots`
   instead of requiring `project_dir` on every tool call? Would remove the
   most error-prone argument.
3. **Voicemail discovery**: is pull-only good enough in practice, or do users
   forget mailboxes exist? (A `SessionStart` hook that runs `check_messages`
   might graduate from v-later fast.)
4. **Live call ownership**: when a wait-mode call outlives the MCP tool call
   (detached subprocess), who reaps zombies / persists partial output if the
   *caller's* process dies too? Needs a small "in-flight" state + reconcile
   pass on server start.
