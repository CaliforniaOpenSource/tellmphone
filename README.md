# TeLLMphone ☎️

**Let your LLMs call each other.**

TeLLMphone is a Python library that ships a local MCP server letting one coding
agent (Claude Code, Codex, …) place a "call" to another: hand over the project
directory and some context, get a response back, and keep the conversation going
across multiple turns — even if either side's session gets interrupted.

> Claude, mid-task: *"Let me get a second opinion on this migration."*
> → `call(callee="codex", personality="grumpy-reviewer", message="Review this schema change…")`
> → Codex runs headlessly in the same project, replies, and the thread stays open
> for follow-ups.

## Why

- **Second opinions.** Different models have different blind spots. A one-tool
  call to "ask the other guy" is cheaper than a human copy-pasting between
  terminals.
- **Delegation.** Hand a subtask to another agent in the same working directory
  and pick up the result later.
- **Personalities.** The caller doesn't write system prompts inline. It picks a
  named, locally saved personality ("grumpy-reviewer", "security-auditor",
  "rubber-duck") and TeLLMphone injects it on the callee side, consistently
  across the whole call.
- **Model choice, separate from personality.** A call can pin which model the
  callee runs (`model="gpt-5.5"`, `model="opus"`) independently of how it
  behaves. The model is pinned per call — follow-ups keep talking to the same
  brain — with per-agent defaults in config.
- **Answering machine.** Agents can check whether another agent left them
  messages for the current project, and reply — asynchronous, survives session
  restarts on both ends.

## How it works (30-second version)

```
┌─────────────┐   MCP (stdio)   ┌──────────────────┐   headless CLI   ┌─────────────┐
│ Claude Code │ ──────────────► │   TeLLMphone     │ ───────────────► │ codex exec  │
│  (caller)   │  call/reply/…   │   switchboard    │  spawn / resume  │  (callee)   │
└─────────────┘                 │                  │                  └─────────────┘
                                │  ~/.tellmphone/  │
                                │  calls, mailbox, │
                                │  personalities   │
                                └──────────────────┘
```

- Each agent runs TeLLMphone as a plain **stdio MCP server**; there is no
  daemon. Shared state lives on disk under `~/.tellmphone/`.
- A **call** spawns the callee's CLI headlessly (`codex exec`, `claude -p`),
  captures the callee's native **session id**, and stores the mapping under a
  stable **call id**. Replies resume that exact session
  (`codex exec resume <id>`, `claude --resume <id>`), so context is never lost.
- The **call id is the only thing the caller needs to remember** — and it
  doesn't even need to remember it, because `check_messages(project_dir)`
  lists open calls and unread replies for the project.

## The tools (planned surface)

| Tool | What it does |
|---|---|
| `call` | Place a call: callee, message, project dir, optional personality and model. Waits for the answer (or drops to voicemail if it takes too long). |
| `reply` | Send a follow-up on an existing call id; resumes the callee's session. |
| `check_messages` | "Any messages for me in this project?" Lists incoming calls, unread replies, and open threads. |
| `hang_up` | Close a call. The transcript is kept. |
| `phonebook` | List available agents (adapters) and personalities. |

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture: call lifecycle,
session-id matching, the personality system, the mailbox protocol, adapter
interface, and security model.

## Supported agents

| Agent | Adapter | Status |
|---|---|---|
| Claude Code | `claude` (headless `claude -p` + `--resume`) | planned, v0.1 |
| Codex | `codex` (`codex exec` + `exec resume`) | planned, v0.1 |
| others | plugin adapters via Python entry points | open to contributions |

## Setup

One command registers the MCP server with every agent CLI it finds:

```bash
uv run --project /path/to/tellmphone tellmphone install
```

Each adapter knows how to plug into its own agent — `claude mcp add` for
Claude (user scope), `codex mcp add` plus the tool-approval config codex
needs for non-interactive tool calls. `tellmphone uninstall` reverses it;
`--agent NAME` limits either to specific agents. Registration is idempotent —
rerun `install` after moving the checkout.

Starter personalities are installed to `~/.tellmphone/personalities/` on first
run; add your own as Markdown files there. Everything else lives in
`~/.tellmphone/config.toml`:

```toml
[defaults]
timeout_s = 300
max_hops = 2

# default model per callee agent; a call's explicit `model` argument wins
[agents.codex]
model = "gpt-5.5"

[agents.claude]
model = "sonnet"

# callees run read-only unless a project is allowlisted here (humans only)
[permissions."/path/to/project"]
write = true
```

## Status

🚧 **v0.1 implemented, pre-release.** All five tools work (39 passing tests),
and **both adapters are live-verified** end to end against the real CLIs:
headless call, session-id capture, native session resume, personality
injection, model pinning, and the answering-machine flow. Not yet on PyPI.
Start with [docs/DESIGN.md](docs/DESIGN.md).

## FAQ

**Is this an MCP server or a skill?**
The core is an MCP server — that's what gives agents actual callable tools.
Skills can't execute anything; they're instructions that teach an agent *when*
and *how* to use tools. TeLLMphone will optionally ship a small companion skill
for Claude (and an `AGENTS.md` snippet for Codex) that teaches good phone
etiquette: when a second opinion is worth the tokens, how to write a good ask,
and to check messages when starting work in a project.

**Why "TeLLMphone"?**
Because the LLMs are on the phone. The internals lean into it: the switchboard
routes calls, the phonebook lists who you can dial, voicemail holds messages
for agents that aren't running, and hop limits stop two agents from playing
telephone forever.

---

TeLLMphone is a project powered by
[The California Open Source Company](https://www.californiaopensource.com).
Licensed under [Apache 2.0](LICENSE).
