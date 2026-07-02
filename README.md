# TeLLMphone

A local MCP server that lets coding agents call each other. Claude Code can
hand Codex a question about the current project and get an answer back, keep
that conversation going across multiple turns, or leave a message for the
next Codex session to pick up. Conversations survive interruptions on both
sides, and the callee can be given a saved personality and a specific model.

Currently supports Claude Code and Codex; other agents can be added as
plugins.

## Requirements

- macOS or Linux
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- The agent CLIs you want to connect (`claude`, `codex`), installed and
  logged in

## Install

Not on PyPI yet; from a checkout:

```bash
git clone git@github.com:CaliforniaOpenSource/tellmphone.git
cd tellmphone
uv run tellmphone install
```

`install` registers the MCP server with every agent CLI it finds — via
`claude mcp add` and `codex mcp add` — and sets the codex config needed for
non-interactive tool approval. It is idempotent; rerun it if you move the
checkout. `tellmphone uninstall` removes the registrations (it does not
delete `~/.tellmphone/`).

## Usage

You talk to your agent; your agent works the phone. In a Claude Code
session:

> Call codex and get a second opinion on this migration. Use the
> grumpy-reviewer personality.

Claude will place the call, relay the answer, and can keep the thread going
with follow-ups. In the other direction, start a Codex session in the same
project and ask it to check its messages.

The tools the agents get:

| Tool | What it does |
|---|---|
| `call` | Send a message to another agent about a project. Optional personality and model. Waits for the answer, or leaves it as voicemail. |
| `reply` | Follow up on an existing call. The callee resumes with full context. |
| `check_messages` | List unread messages and open calls for a project. |
| `hang_up` | Close a call. The transcript is kept. |
| `phonebook` | List available agents and personalities. |

## How it works

```
┌─────────────┐   MCP (stdio)   ┌──────────────────┐   headless CLI   ┌─────────────┐
│ Claude Code │ ──────────────► │   TeLLMphone     │ ───────────────► │ codex exec  │
│  (caller)   │  call/reply/…   │   switchboard    │  spawn / resume  │  (callee)   │
└─────────────┘                 └──────────────────┘                  └─────────────┘
```

There is no daemon. Each agent runs its own TeLLMphone instance over stdio;
shared state lives under `~/.tellmphone/`. A call spawns the callee's CLI
headlessly (`codex exec`, `claude -p`) in the project directory, records the
callee's native session id under a stable call id, and replies resume that
exact session (`codex exec resume`, `claude --resume`). If a native session
is lost, the stored transcript is replayed into a fresh one. Callees run in
their CLI's read-only/sandboxed mode unless you allowlist a project for
writes, and a hop limit keeps agents from chaining calls indefinitely.

Details, including the security model, are in [docs/DESIGN.md](docs/DESIGN.md).

## Configuration

`~/.tellmphone/config.toml`:

```toml
[defaults]
timeout_s = 300   # wait this long before a live call rolls to voicemail
max_hops = 2      # agent-to-agent chain depth limit

# default model per callee; a call's explicit model argument wins.
# Use whatever model ids your CLI accepts.
[agents.codex]
model = "gpt-5.5"

# callees run read-only unless a project is allowlisted here
[permissions."/path/to/project"]
write = true
```

Personalities are Markdown files in `~/.tellmphone/personalities/` with a
small frontmatter block (`name`, `description`) followed by the system
prompt. Three starters are installed on first run (`grumpy-reviewer`,
`security-auditor`, `rubber-duck`); add your own alongside them. Callers
select personalities by name and never send system prompts inline.

## Data

Call records and transcripts live under `~/.tellmphone/projects/`.
Transcripts contain whatever flowed through the conversation, which for a
coding agent usually includes your code. Everything is plain JSON owned by
your user; delete a call directory (or all of `~/.tellmphone/`) to purge.

## Adding another agent

Adapters implement a three-method interface (`available`, `spawn`, `resume`)
plus optional install hooks, and register via the `tellmphone.adapters`
entry-point group — a separate package can add an agent without touching
this one. See `src/tellmphone/adapters/`.

## Development

```bash
uv sync
uv run pytest
```

The test suite runs against fake `claude`/`codex` executables, so it needs
neither CLI installed nor network.

## Status

Alpha. Works with Claude Code and Codex on macOS and Linux; no Windows
support yet (file locking is POSIX-only). Not yet published to PyPI.

---

TeLLMphone is a project powered by
[The California Open Source Company](https://www.californiaopensource.com).
Licensed under [Apache 2.0](LICENSE).
