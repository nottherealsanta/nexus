"""Permission rules: `allow`, `ask` or `deny` per tool and per pattern (PLAN.md D7).

Adapted from the grammar of old Nexus nexus/tools/permissions.py, cut down to
tools and their one natural key. A rule is `tool` (every call) or
`tool(pattern)` (a glob over the call's key) with an action:

    bash                      = "ask"      # every bash command asks
    "bash(git status*)"       = "allow"    # except this one
    "bash(git push*)"         = "deny"     # never
    "read(*.env)"             = "deny"
    web_fetch                 = "ask"
    "edit(src/*)"             = "allow"

The key is `bash`: the command; `read`, `grep`, `glob`, `ls`, `edit`, `write`,
`multiedit`: the path (relative to the session directory with symlinks resolved,
or absolute when outside it; an `edit` patch has one key per file it touches);
`web_fetch`: the URL without credentials, query or fragment; `web_search`: the
query. Other tools have no key, so only a bare `tool` rule can match them. In a
pattern `*` matches any characters (including `/`), `?` one character, and `\\`
escapes.

Order of decision:

1. Any matching `deny` wins, wherever it is (config or agent).
2. Otherwise the last matching rule of `[tools.permission]` in the config file
   decides `allow` or `ask`.
3. An agent file's `permission:` rules (only `ask` and `deny`: an agent narrows
   what the user allowed, it never widens it) add `ask`.
4. No rule: the session's `permission` setting, and the tools that never ask.

For `bash` a compound command (`;`, `&&`, `|`, `$(…)`, …) is never matched by an
`allow` pattern, and `deny`/`ask` patterns are tested on the whole command and on
every part, so `bash(rm -rf*)` also catches `cd build && rm -rf .`. This is a
guard against mistakes, not a sandbox: bash can still reach files that `read`
would refuse.
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .shell_syntax import analyze_command

ACTIONS = ("allow", "ask", "deny")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_*?]{0,63}\Z")
MAX_PATTERN = 1024
MAX_RULES = 256
PATH_TOOLS = ("read", "grep", "glob", "ls", "edit", "write", "multiedit")


@dataclass(frozen=True, slots=True)
class Rule:
    tool: str  # a tool name or a glob over names (`mcp__github__*`)
    pattern: str | None  # None: every call of the tool
    action: str

    @property
    def text(self) -> str:
        return self.tool if self.pattern is None else f"{self.tool}({self.pattern})"


@dataclass(frozen=True, slots=True)
class Verdict:
    action: str  # allow | ask | deny
    rule: str
    source: str  # config | agent

    @property
    def reason(self) -> str:
        return f"permission rule `{self.rule}` ({self.source})"


def parse_rule(text: str) -> tuple[str, str | None]:
    """`(tool, pattern)` of a rule key, or `ValueError` saying what is wrong."""
    if not isinstance(text, str):
        raise TypeError("rule must be text")
    name, paren, rest = text.strip().partition("(")
    if paren:
        if not rest.endswith(")"):
            raise ValueError(f"{text!r}: missing closing ')'")
        pattern: str | None = rest[:-1]
        if not pattern:
            raise ValueError(f"{text!r}: empty pattern")
        if len(pattern) > MAX_PATTERN:
            raise ValueError(f"{text!r}: pattern is too long")
    else:
        pattern = None
    if not _NAME.fullmatch(name):
        raise ValueError(f"{text!r}: {name!r} is not a tool name")
    return name, pattern


def parse_rules(value: Any, *, agent: bool = False) -> tuple[list[Rule], list[str]]:
    """Rules from a config table or frontmatter map, in order, and a problem per rejected entry.

    Each entry is `rule = action`. Order is the file's order. `agent=True`
    refuses `allow`.
    """
    rules: list[Rule] = []
    problems: list[str] = []
    if not isinstance(value, dict):
        return rules, ["expected a table of rule = action"]

    def add(tool_text: str, action: Any) -> None:
        if len(rules) >= MAX_RULES:
            problems.append(f"more than {MAX_RULES} rules")
            return
        try:
            tool, pattern = parse_rule(tool_text)
        except ValueError as exc:
            problems.append(str(exc))
            return
        if action not in ACTIONS:
            problems.append(f"{tool_text!r}: action must be one of {', '.join(ACTIONS)}")
        elif agent and action == "allow":
            problems.append(f"{tool_text!r}: an agent's rules can only ask or deny; allow comes from the config file")
        else:
            rules.append(Rule(tool, pattern, action))

    for key, action in value.items():
        add(key, action)
    return rules, problems


# -- keys ----------------------------------------------------------------------


def path_key(workspace: str | Path, raw: str) -> str:
    root = os.path.realpath(workspace)
    target = os.path.realpath(os.path.join(root, raw))
    if target == root:
        return "."
    return os.path.relpath(target, root) if target.startswith(root + os.sep) else target


def call_keys(tool: str, args: dict[str, Any], workspace: str | Path) -> list[str] | None:
    """The strings a rule pattern is matched against, or None when this tool has no key."""
    if tool == "bash":
        command = args.get("command")
        return [command] if isinstance(command, str) else None
    if tool in PATH_TOOLS:
        if tool == "edit" and isinstance(args.get("patch"), str):
            from .tools.base import ToolError
            from .tools.edit import parse_patch
            try:
                ops = parse_patch(args["patch"])
            except ToolError:
                return None  # the tool will refuse it
            paths = [p for op in ops for p in (op.path, op.move_to) if p]
            return [path_key(workspace, p) for p in paths] or None
        raw = args.get("path", ".") if tool in ("grep", "glob", "ls") else args.get("path")
        return [path_key(workspace, raw or ".")] if isinstance(raw, str) else None
    if tool == "web_fetch":
        url = args.get("url")
        if not isinstance(url, str):
            return None
        try:
            parts = urlsplit(url.strip())
            host = (parts.hostname or "").lower()
            authority = f"[{host}]" if ":" in host else host
            if parts.port:
                authority += f":{parts.port}"
        except ValueError:
            return None
        return [f"{parts.scheme.lower()}://{authority}{parts.path}"]
    if tool == "web_search":
        query = args.get("query")
        return [" ".join(query.split())] if isinstance(query, str) else None
    return None


# -- matching ------------------------------------------------------------------


@functools.lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    out, i = ["(?s:"], 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 1
        elif char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r")\Z")


def _bash_matches(pattern: str, command: str, action: str) -> bool:
    parts, compound = analyze_command(command)
    if action == "allow":
        return not compound and _glob(pattern).match(command) is not None
    return _glob(pattern).match(command) is not None or (
        compound and any(_glob(pattern).match(part) for part in parts))


def matches(rule: Rule, tool: str, keys: list[str] | None) -> bool:
    if not fnmatchcase(tool, rule.tool):
        return False
    if rule.pattern is None:
        return True
    if not keys:
        return False
    if tool == "bash":
        return _bash_matches(rule.pattern, keys[0], rule.action)
    found = [_glob(rule.pattern).match(key) is not None for key in keys]
    return all(found) if rule.action == "allow" else any(found)  # one denied file denies a patch


def decide(tool: str, args: dict[str, Any], workspace: str | Path, config: list[Rule],
           agent: list[Rule]) -> Verdict | None:
    """The verdict of the rules for this call, or None when none applies (see the module docstring)."""
    keys = call_keys(tool, args, workspace)
    hits = [(rule, "config") for rule in config if matches(rule, tool, keys)]
    hits += [(rule, "agent") for rule in agent if matches(rule, tool, keys)]
    for rule, source in hits:
        if rule.action == "deny":
            return Verdict("deny", rule.text, source)
    last_config = next(((r, s) for r, s in reversed(hits) if s == "config"), None)
    asks = [(r, s) for r, s in hits if r.action == "ask" and s == "agent"]
    if last_config is not None and last_config[0].action == "ask":
        return Verdict("ask", last_config[0].text, "config")
    if asks:
        return Verdict("ask", asks[0][0].text, "agent")
    if last_config is not None:
        return Verdict("allow", last_config[0].text, "config")
    return None


# -- the config file's rules -----------------------------------------------------

_cache: dict[str, tuple[tuple[int, int], list[Rule]]] = {}


def config_rules() -> list[Rule]:
    """`[tools.permission]` from the config file; rules that do not parse are skipped (and listed by Settings)."""
    from .config import ConfigFile
    path = ConfigFile().path
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    rules = parse_rules(ConfigFile().read()["values"]["tools"]["permission"])[0]
    _cache[str(path)] = (stamp, rules)
    return rules
