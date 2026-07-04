"""tellmphone CLI: serve | personalities | version."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

from tellmphone import __version__


def _serve_argv(agent: str, override: str | None) -> list[str]:
    """Build the serve command an agent's MCP config should launch.

    Priority: explicit --serve-command; a source checkout (run via uv);
    an installed `tellmphone` executable; `uvx` as the last resort.
    """
    if override:
        return [*shlex.split(override), "--i-am", agent]

    import tellmphone

    root = Path(tellmphone.__file__).resolve().parents[2]
    if (root / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--project", str(root),
                "tellmphone", "serve", "--i-am", agent]

    exe = shutil.which("tellmphone")
    if exe:
        return [exe, "serve", "--i-am", agent]
    return ["uvx", "tellmphone", "serve", "--i-am", agent]


def _make_switchboard(i_am: str):
    from tellmphone.adapters import load_adapters
    from tellmphone.config import load_config
    from tellmphone.store import Store
    from tellmphone.switchboard import Switchboard

    cfg = load_config(i_am=i_am)
    board = Switchboard(cfg, Store(cfg.home), load_adapters())
    return board


def _print_turn(result: dict, started: float) -> int:
    import time

    if result["status"] == "answered":
        print()
        print(result.get("response", ""))
        print()
        elapsed = time.monotonic() - started
        print(f"— {result.get('from', '?')} · {result['call_id']} · answered in {elapsed:.0f}s")
        return 0
    detail = (
        result.get("error") or result.get("reason") or result.get("note") or ""
    )
    call_id = f"{result['call_id']}: " if result.get("call_id") else ""
    print(f"{result['status']}: {call_id}{detail}")
    return 0 if result["status"] in ("voicemail", "ringing") else 1


def _cmd_call(args) -> int:
    import time

    board = _make_switchboard(args.as_name)
    if args.timeout:
        board.config.timeout_s = args.timeout
    flavor = f" ({args.personality})" if args.personality else ""
    print(f"☎ ringing {args.callee}{flavor}…", flush=True)
    started = time.monotonic()
    result = board.place_call(
        args.callee,
        args.message,
        str(Path(args.project).resolve()),
        personality=args.personality,
        model=args.model,
        mode=args.mode,
        write=args.write,
    )
    return _print_turn(result, started)


def _cmd_reply(args) -> int:
    import time

    board = _make_switchboard(args.as_name)
    if args.timeout:
        board.config.timeout_s = args.timeout
    call_id = args.call_id
    if not call_id:
        project = str(Path(args.project).resolve())
        mine = [
            r
            for r in board.store.calls_for_project(project)
            if r.caller.agent == args.as_name and r.status not in ("closed", "failed")
        ]
        if not mine:
            print(f"no open calls from {args.as_name!r} in {project}")
            return 1
        call_id = max(mine, key=lambda r: r.last_activity_at).call_id
    print(f"☎ {call_id}…", flush=True)
    started = time.monotonic()
    return _print_turn(board.reply(call_id, args.message), started)


def _cmd_messages(args) -> int:
    board = _make_switchboard(args.as_name)
    project = str(Path(args.project).resolve())
    mailbox = board.check_messages(project)

    unread = mailbox["unread"]
    if unread:
        print(f"unread for {args.as_name} in {project}:")
        for msg in unread:
            print(f"\n{msg['call_id']} from {msg['from']} at {msg['ts']}")
            if msg.get("personality"):
                print(f"personality: {msg['personality']}")
            print(msg.get("body") or msg.get("preview", ""))
    else:
        print(f"no unread messages for {args.as_name} in {project}")

    open_calls = mailbox["open_calls"]
    if open_calls:
        print("\nopen calls:")
        for call in open_calls:
            print(
                f"{call['call_id']}  {call['status']:9}  "
                f"with {call['with']}  {call['last_activity_at']}"
            )
    return 0


def _cmd_show(args) -> int:
    from tellmphone.store import CallNotFound

    board = _make_switchboard(args.as_name)
    try:
        record = board.store.load_call(args.call_id)
    except CallNotFound:
        print(f"no call {args.call_id!r}")
        return 1

    print(f"{record.call_id}  {record.status}")
    print(f"project: {record.project_dir}")
    print(f"caller:  {record.caller.agent}")
    print(f"callee:  {record.callee.agent}")
    if record.callee.personality:
        print(f"personality: {record.callee.personality}")
    if record.callee.model:
        print(f"model: {record.callee.model}")
    print(f"write: {record.write}")
    print(f"created: {record.created_at.isoformat()}")
    print(f"last:    {record.last_activity_at.isoformat()}")

    transcript = board.store.read_transcript(record.call_id)
    if not transcript:
        print("\n(no transcript)")
        return 0

    print("\ntranscript:")
    for entry in transcript:
        print(f"\n[{entry.seq}] {entry.ts.isoformat()}  {entry.from_} -> {entry.to}")
        if entry.kind != "message":
            print(f"kind: {entry.kind}")
        print(entry.body)
    return 0


def _cmd_gc(args) -> int:
    from datetime import timedelta

    from tellmphone.store import utcnow

    board = _make_switchboard(args.as_name)
    if args.project:
        records = board.store.calls_for_project(str(Path(args.project).resolve()))
    else:
        records = board.store.all_calls()

    cutoff = utcnow() - timedelta(days=args.days)
    doomed = [
        record
        for record in records
        if record.status in ("closed", "failed") and record.last_activity_at <= cutoff
    ]

    for record in doomed:
        action = "would delete" if args.dry_run else "deleted"
        print(
            f"{action} {record.call_id}  {record.status}  "
            f"{record.last_activity_at.isoformat()}  {record.project_dir}"
        )
        if not args.dry_run:
            board.store.delete_call(record.call_id)

    suffix = " (dry run)" if args.dry_run else ""
    print(f"{len(doomed)} call(s) eligible for gc{suffix}")
    return 0


def _run_install(agents: list[str] | None, serve_command: str | None,
                 uninstall: bool = False) -> int:
    from tellmphone.adapters import load_adapters

    adapters = load_adapters()
    unknown = set(agents or []) - set(adapters)
    if unknown:
        print(f"unknown agents: {sorted(unknown)}; known: {sorted(adapters)}")
        return 1

    failures = 0
    for name, adapter in sorted(adapters.items()):
        if agents and name not in agents:
            continue
        if not adapter.available():
            print(f"{name}: skipped (CLI not installed)")
            continue
        try:
            if uninstall:
                print(adapter.unregister_mcp())
            else:
                print(adapter.register_mcp(_serve_argv(name, serve_command)))
        except NotImplementedError as exc:
            print(f"{name}: manual step needed — {exc}")
        except Exception as exc:
            print(f"{name}: FAILED — {exc}")
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tellmphone",
        description="Let your LLMs call each other — a local MCP switchboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.add_argument(
        "--i-am",
        required="TELLMPHONE_I_AM" not in os.environ,
        default=os.environ.get("TELLMPHONE_I_AM"),
        help="which agent this server is mounted in (claude, codex, ...)",
    )

    install = sub.add_parser(
        "install", help="register the MCP server with every detected agent CLI"
    )
    install.add_argument(
        "--agent", action="append", dest="agents", metavar="NAME",
        help="limit to specific agents (repeatable); default: all detected",
    )
    install.add_argument(
        "--serve-command", metavar="CMD",
        help="override the serve command to register (--i-am is appended)",
    )

    uninstall = sub.add_parser("uninstall", help="remove the MCP registrations")
    uninstall.add_argument("--agent", action="append", dest="agents", metavar="NAME")

    call = sub.add_parser("call", help="place a call yourself, from the terminal")
    call.add_argument("callee", help="agent to dial (see: tellmphone install)")
    call.add_argument("message")
    call.add_argument("--personality", help="a name from ~/.tellmphone/personalities")
    call.add_argument("--model", help="model the callee should run")
    call.add_argument("--project", default=".", help="project directory (default: cwd)")
    call.add_argument(
        "--mode",
        choices=["wait", "voicemail"],
        default="wait",
        help="wait for an answer or leave voicemail",
    )
    call.add_argument("--as", dest="as_name", default="human", help=argparse.SUPPRESS)
    call.add_argument("--timeout", type=int, help="seconds before rolling to voicemail")
    call.add_argument(
        "--write", action="store_true", help="let the callee edit files in the project"
    )

    reply = sub.add_parser("reply", help="follow up on your most recent (or a given) call")
    reply.add_argument("call_id", nargs="?", help="defaults to your latest open call here")
    reply.add_argument("message")
    reply.add_argument("--project", default=".", help="project directory (default: cwd)")
    reply.add_argument("--as", dest="as_name", default="human", help=argparse.SUPPRESS)
    reply.add_argument("--timeout", type=int, help="seconds before rolling to voicemail")

    messages = sub.add_parser("messages", help="show unread messages and open calls")
    messages.add_argument("--project", default=".", help="project directory (default: cwd)")
    messages.add_argument("--as", dest="as_name", default="human", help=argparse.SUPPRESS)

    show = sub.add_parser("show", help="show call metadata and transcript")
    show.add_argument("call_id")
    show.add_argument("--as", dest="as_name", default="human", help=argparse.SUPPRESS)

    gc = sub.add_parser("gc", help="delete closed/failed calls older than N days")
    gc.add_argument(
        "--days", type=int, default=30, help="age threshold in days (default: 30)"
    )
    gc.add_argument("--project", help="limit cleanup to one project")
    gc.add_argument("--dry-run", action="store_true", help="print what would be deleted")
    gc.add_argument("--as", dest="as_name", default="human", help=argparse.SUPPRESS)

    sub.add_parser("personalities", help="list installed personalities")
    sub.add_parser("version", help="print version")

    args = parser.parse_args(argv)

    if args.command == "install":
        return _run_install(args.agents, args.serve_command)
    if args.command == "uninstall":
        return _run_install(args.agents, None, uninstall=True)
    if args.command == "call":
        return _cmd_call(args)
    if args.command == "reply":
        return _cmd_reply(args)
    if args.command == "messages":
        return _cmd_messages(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "gc":
        return _cmd_gc(args)

    if args.command == "version":
        print(f"tellmphone {__version__}")
        return 0

    if args.command == "personalities":
        from tellmphone.config import tellmphone_home
        from tellmphone.personalities import PersonalityBook

        book = PersonalityBook(tellmphone_home() / "personalities")
        for p in book.all():
            print(f"{p.name:24} {'[' + p.source + ']':10} {p.description}")
        return 0

    if args.command == "serve":
        from tellmphone.config import load_config
        from tellmphone.server import create_server

        create_server(load_config(i_am=args.i_am)).run()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
