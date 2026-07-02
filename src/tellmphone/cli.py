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

    sub.add_parser("personalities", help="list installed personalities")
    sub.add_parser("version", help="print version")

    args = parser.parse_args(argv)

    if args.command == "install":
        return _run_install(args.agents, args.serve_command)
    if args.command == "uninstall":
        return _run_install(args.agents, None, uninstall=True)

    if args.command == "version":
        print(f"tellmphone {__version__}")
        return 0

    if args.command == "personalities":
        from tellmphone.config import tellmphone_home
        from tellmphone.personalities import PersonalityBook

        book = PersonalityBook(tellmphone_home() / "personalities")
        book.ensure_starters()
        for p in book.all():
            print(f"{p.name:24} {p.description}")
        return 0

    if args.command == "serve":
        from tellmphone.config import load_config
        from tellmphone.server import create_server

        create_server(load_config(i_am=args.i_am)).run()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
