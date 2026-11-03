"""Shared guard rails for the M0 spike scripts (not shipped; run by hand against real services).

Every spike:
* works in a scratch home (`--home`, default a fresh temp directory), never in `~/.nexus` or `~/.nexus10`. A real
  home is refused unless `--allow-real-home` is given;
* prints what it will spend and what it will send, and does nothing until `--yes` is given;
* reaches a provider only through credentials you copy in on purpose (`--copy-auth-from DIR` copies one file,
  `auth.json`, into the scratch home with mode 0600).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="scratch NEXUS10_HOME to use (default: a new temporary directory)")
    parser.add_argument("--allow-real-home", action="store_true",
                        help="permit --home or $NEXUS10_HOME to be your real Nexus home (not recommended)")
    parser.add_argument("--copy-auth-from", metavar="DIR",
                        help="copy DIR/auth.json (provider sign-ins) into the scratch home; this is how a spike "
                             "gets credentials. The file is copied, never modified")
    parser.add_argument("--yes", action="store_true", help="I have read what this will spend and send; run it")


def real_homes() -> list[Path]:
    return [(Path.home() / ".nexus").resolve(), (Path.home() / ".nexus10").resolve()]


def prepare(args: argparse.Namespace, *, spends: str) -> Path:
    """Choose and create the scratch home, export it, and stop unless the user said ``--yes``."""
    chosen = args.home or os.environ.get("NEXUS10_HOME")
    home = Path(chosen).expanduser().resolve() if chosen else Path(tempfile.gettempdir()) / f"nexus-spike-{os.getpid()}"
    if home in real_homes() and not args.allow_real_home:
        sys.exit(f"refusing to use {home}: it is a real Nexus home. Pass --home to a scratch directory "
                 "(or --allow-real-home if you really mean it).")
    print(f"scratch home: {home}")
    print(f"this will spend / send: {spends}")
    if not args.yes:
        sys.exit("nothing was run. Re-run with --yes to go ahead.")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    os.environ["NEXUS10_HOME"] = str(home)
    if args.copy_auth_from:
        source = Path(args.copy_auth_from).expanduser() / "auth.json"
        if not source.is_file():
            sys.exit(f"{source} does not exist")
        target = home / "auth.json"
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        print(f"copied {source} to {target}")
    return home
