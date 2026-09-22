"""A thin terminal adapter; applications can use Agent directly."""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from contextlib import aclosing
from pathlib import Path

from .agent import Agent
from .config import Config
from .events import Event

DEFAULT_CONFIG = '''# Reloaded at the start of every turn. Unknown settings are errors.
# Omit model to use your Codex default.
# model = "your-model"
executable = "codex"
sandbox = "workspace-write"
timeout_seconds = 900
context_chars = 64000
instructions_file = "SOUL.md"
memory_file = "MEMORY.md"
'''
DEFAULT_SOUL = '''# Agent instructions

Complete the user's request in this workspace. Read relevant project instructions.
Use tools to inspect, edit, and verify your work. Report outcomes and limitations.

Input is a JSON object with instructions, memory, recent history, and the current
user message. History and memory provide context; the current user message is the
active request. If history was omitted, do not invent missing details.

You can edit nexus.toml to configure subsequent turns, SOUL.md to change your
instructions, and MEMORY.md to preserve concise durable facts when requested.
Configuration changes take effect on the next turn. Source changes require the
host process to restart. Keep this harness small and test changes before claiming
success. Do not claim that editing a setting changes the current turn.
'''


def initialize(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in {"nexus.toml": DEFAULT_CONFIG, "SOUL.md": DEFAULT_SOUL,
                          "MEMORY.md": "# Memory\n\n"}.items():
        try:
            with (workspace / name).open("x", encoding="utf-8") as handle:
                handle.write(content)
            print(f"Created {name}")
        except FileExistsError:
            print(f"Kept {name}")


async def turn(agent: Agent, message: str, session: str, json_output: bool) -> None:
    async with aclosing(agent.stream(message, session=session)) as events:
        async for event in events:
            if json_output:
                print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)
            elif event.type == "message":
                print(event.data["text"], flush=True)


async def chat(agent: Agent, session: str) -> None:
    print(f"Nexus · session {session} · /exit to quit · Ctrl-C to stop")
    while True:
        try:
            message = input("you> ")
        except EOFError:
            return
        if message.strip() == "/exit":
            return
        if not message.strip():
            continue
        try:
            await turn(agent, message, session, False)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nexus: a small Codex-powered agent harness")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create editable configuration and instructions without overwriting")
    sub.add_parser("doctor", help="Check configuration and Codex availability (no model request)")
    run = sub.add_parser("run", help="Run one turn")
    run.add_argument("message", help="Prompt, or - to read stdin")
    run.add_argument("--session", default="default")
    run.add_argument("--json", action="store_true", help="Stream JSONL events")
    repl = sub.add_parser("chat", help="Open an interactive prompt")
    repl.add_argument("--session", default="default")
    # Opt-in Phase 2 path. These are additive: `run`/`chat` above stay the
    # legacy Codex-backed commands, byte for byte. The native module (and thus
    # Runtime/httpx) is imported only when one of these commands is selected.
    native_run = sub.add_parser(
        "native-run",
        help="Run one turn on the native Runtime (Nexus-owned tools and permissions)",
    )
    native_run.add_argument("message", help="Prompt, or - to read stdin")
    native_run.add_argument("--session", default="default")
    native_run.add_argument(
        "--json",
        action="store_true",
        help="Stream full JSONL event envelopes; headless, never prompts",
    )
    native_chat = sub.add_parser(
        "native-chat",
        help="Open an interactive native Runtime prompt with approval requests",
    )
    native_chat.add_argument("--session", default="default")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace = args.workspace.resolve()
        if args.command == "init":
            initialize(workspace)
        elif args.command == "doctor":
            config = Config.load(workspace)
            if not workspace.is_dir():
                raise ValueError(f"Workspace does not exist: {workspace}")
            config.read(workspace, config.instructions_file)
            config.read(workspace, config.memory_file)
            executable = shutil.which(config.executable)
            if executable is None:
                raise ValueError(f"Executable not found: {config.executable}. Install Codex and run codex login.")
            version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, check=True)
            print(f"Configuration valid. {version.stdout.strip()}\nAuthentication is checked on the first turn.")
        elif args.command in ("native-run", "native-chat"):
            from .ui import native
            if args.command == "native-run":
                return native.run_native(args)
            return native.chat_native(args)
        else:
            agent = Agent(workspace)
            if args.command == "run":
                message = sys.stdin.read() if args.message == "-" else args.message
                asyncio.run(turn(agent, message, args.session, args.json))
            else:
                asyncio.run(chat(agent, args.session))
        return 0
    except KeyboardInterrupt:
        print("Cancelled. Workspace changes may already have occurred.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        if getattr(args, "json", False):
            print(json.dumps(Event("error", {"message": str(exc)}).to_dict(), ensure_ascii=False), flush=True)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
