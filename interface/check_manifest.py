#!/usr/bin/env python3
"""Validate interface/manifest.toml. Stdlib only, Python 3.11+.

Usage: python3 interface/check_manifest.py [path/to/manifest.toml]
"""
import sys
import tomllib
import re
from collections import defaultdict
from pathlib import Path

SECTION_ORDER = [
    "appearance", "providers", "agents", "tools", "zmx", "mcp",
    "context", "dictation", "manager", "keybindings", "notifications", "about",
]
TABS = ["sessions", "files", "git"]
RIGHT = ["session_info"]
ROW_KINDS = {"choice", "toggle", "number", "text", "list", "action", "info", "keymap"}
SCOPES = {"core", "client"}
CLIENTS = {"both", "tui", "nsh"}
MODS = ["cmd", "ctrl", "alt", "shift"]


KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# Where clients read a string: ui_text("key", ...) in Rust, uiText("key", ...) in TypeScript.
USE_RE = re.compile(r"\b(?:ui_text|uiText)\(\s*\"([^\"]+)\"")
USE_COND_RE = re.compile(r"\b(?:ui_text|uiText)\(\s*[^\"\n]*?\"([a-z][a-z0-9_.]*)\"\s*:\s*\"([a-z][a-z0-9_.]*)\"")


def check_strings(m: dict, path: Path, err) -> None:
    """[strings]: dotted lower-case keys, well-formed placeholders, every key a client uses defined."""
    strings = m.get("strings", {})
    for key, text in strings.items():
        if not KEY_RE.match(key):
            err(f"strings.{key}: key must be dotted lower case")
        if not isinstance(text, str) or not text:
            err(f"strings.{key}: text must be a non-empty string")
            continue
        stripped = PLACEHOLDER_RE.sub("", text)
        if "{" in stripped or "}" in stripped:
            err(f"strings.{key}: malformed placeholder in {text!r}")
    root = path.resolve().parent.parent
    used: set[str] = set()
    literals: set[str] = set()
    for base, exts in (("nexus-tui/src", {".rs"}), ("nexus-nsh/src", {".ts", ".tsx"})):
        for f in (root / base).rglob("*") if (root / base).is_dir() else []:
            if f.suffix in exts and "gen" not in f.relative_to(root / base).parts:
                src = f.read_text()
                used.update(USE_RE.findall(src))
                literals.update(k for k in strings if f'"{k}"' in src)
                for pair in USE_COND_RE.findall(src):
                    used.update(pair)
    for key in sorted(used):
        if key not in strings:
            err(f"strings: key {key!r} is used by a client but not defined")
    for key in sorted(set(strings) - literals):
        err(f"strings.{key}: defined but no client uses it")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("manifest.toml")
    with path.open("rb") as f:
        m = tomllib.load(f)
    errs: list[str] = []
    err = errs.append

    vocab = m.get("vocabulary", {})
    terms = set(vocab.get("terms", []))
    placements = set(vocab.get("placements", []))
    statuses = set(vocab.get("statuses", []))
    categories = set(vocab.get("categories", []))
    contexts = set(m.get("keymap", {}).get("contexts", []))
    for name, v in (("terms", terms), ("placements", placements), ("categories", categories), ("contexts", contexts)):
        if not v:
            err(f"vocabulary/keymap: {name} is empty")

    cmds = m.get("command", [])
    ids: dict[str, int] = {}
    slashes: dict[str, str] = {}
    keys: dict[tuple[str, str, str], str] = defaultdict(str)

    def check_key(cid: str, client: str, key: str) -> None:
        parts = key.split("+")
        mods, base = parts[:-1], parts[-1]
        if not base or key != key.lower():
            err(f"{cid}: key {key!r} must be lower case")
        if client == "tui" and "cmd" in mods:
            err(f"{cid}: tui key {key!r} uses cmd")
        order = [MODS.index(x) for x in mods if x in MODS]
        if len(order) != len(mods):
            err(f"{cid}: key {key!r} has an unknown modifier")
        elif order != sorted(order):
            err(f"{cid}: key {key!r} modifiers must be in order cmd, ctrl, alt, shift")

    def bind_key(cid: str, client: str, ctx: str, key: str) -> None:
        if ctx == "escape_chain":
            return  # resolved by keymap.escape, checked below
        slot = (client, ctx, key)
        if keys[slot]:
            err(f"key conflict ({client}, {ctx}, {key}): {keys[slot]} and {cid}")
        keys[slot] = cid

    for c in cmds:
        cid = c.get("id", "<missing id>")
        for req in ("id", "title", "category", "when", "placement"):
            if req not in c:
                err(f"{cid}: missing {req}")
        if cid in ids:
            err(f"duplicate command id {cid!r}")
        ids[cid] = 1
        if "." not in cid:
            err(f"{cid}: id must be dotted")
        if c.get("category") not in categories:
            err(f"{cid}: unknown category {c.get('category')!r}")
        for p in c.get("placement", []):
            if p not in placements:
                err(f"{cid}: unknown placement {p!r}")
        if "status" in c and c["status"] not in statuses:
            err(f"{cid}: unknown status {c['status']!r}")
        if c.get("clients", "both") not in CLIENTS:
            err(f"{cid}: bad clients value")
        names = ([c["slash"]] if "slash" in c else []) + c.get("slash_aliases", [])
        for n in names:
            if n in slashes:
                err(f"slash name {n!r} used by {slashes[n]} and {cid}")
            slashes[n] = cid
            if not re.fullmatch(r"[a-z]+(?:-[a-z]+)*", n):
                err(f"{cid}: slash name {n!r} must be lower-case letters separated by hyphens")
        if "slash_aliases" in c and "slash" not in c:
            err(f"{cid}: slash_aliases without slash")
        for t in c.get("when", []):
            if t.lstrip("!") not in terms:
                err(f"{cid}: when term {t!r} not in vocabulary")
        # availability must be explainable
        if c.get("when") not in (["always"], []) and not c.get("unavailable_reason"):
            err(f"{cid}: conditional command needs unavailable_reason")
        k = c.get("keys")
        if k:
            ctx = c.get("key_context")
            if ctx not in contexts:
                err(f"{cid}: key_context {ctx!r} not declared in keymap.contexts")
            for client, key in k.items():
                if client not in ("tui", "nsh"):
                    err(f"{cid}: keys.{client} unknown client")
                    continue
                check_key(cid, client, key)
                bind_key(cid, client, ctx, key)
        elif "key_context" in c:
            err(f"{cid}: key_context without keys")
        aliases = c.get("key_aliases")
        if aliases is not None:
            if not k:
                err(f"{cid}: key_aliases without keys")
            if not isinstance(aliases, dict):
                err(f"{cid}: key_aliases must be a table")
                continue
            ctx = c.get("key_context")
            for client, extra in aliases.items():
                if client not in ("tui", "nsh"):
                    err(f"{cid}: key_aliases.{client} unknown client")
                    continue
                if not isinstance(extra, list) or not all(isinstance(key, str) for key in extra):
                    err(f"{cid}: key_aliases.{client} must be a list of keys")
                    continue
                for key in extra:
                    check_key(cid, client, key)
                    if ctx:
                        bind_key(cid, client, ctx, key)

    # keymap.escape
    esc = m.get("keymap", {}).get("escape", [])
    eids = set()
    escape_actions = set()
    if not esc:
        err("keymap.escape is empty")
    for r in esc:
        rid = r.get("id")
        if rid in eids:
            err(f"keymap.escape: duplicate rule id {rid!r}")
        eids.add(rid)
        for t in r.get("when", []):
            if t.lstrip("!") not in terms:
                err(f"keymap.escape.{rid}: when term {t!r} not in vocabulary")
        if r.get("action") not in ids:
            err(f"keymap.escape.{rid}: action {r.get('action')!r} is not a command")
        escape_actions.add(r.get("action"))
    if esc and esc[-1].get("when") != ["always"]:
        err("keymap.escape: last rule must be the unconditional fallback")
    for c in cmds:
        if c.get("key_context") == "escape_chain" and c["id"] not in escape_actions:
            err(f"{c['id']}: escape_chain command is not an action in keymap.escape")
        if c.get("key_context") == "escape_chain":
            for client, key in c["keys"].items():
                if key != "esc":
                    err(f"{c['id']}: escape_chain commands must be bound to esc")

    # settings
    secs = m.get("settings", {}).get("section", [])
    got = [s.get("id") for s in secs]
    if got != SECTION_ORDER:
        err(f"settings section order mismatch:\n  want {SECTION_ORDER}\n  got  {got}")
    rows_seen: set[str] = set()
    for s in secs:
        for r in s.get("row", []):
            rid = r.get("id", "<missing>")
            if rid in rows_seen:
                err(f"duplicate settings row id {rid!r}")
            rows_seen.add(rid)
            if not rid.startswith(s["id"] + "."):
                err(f"settings row {rid}: id must start with {s['id']}.")
            for req in ("label", "kind", "scope", "clients"):
                if req not in r:
                    err(f"settings row {rid}: missing {req}")
            if r.get("kind") not in ROW_KINDS:
                err(f"settings row {rid}: bad kind")
            if r.get("scope") not in SCOPES:
                err(f"settings row {rid}: bad scope")
            if r.get("clients") not in CLIENTS:
                err(f"settings row {rid}: bad clients")
            if "command" in r and r["command"] not in ids:
                err(f"settings row {rid}: command {r['command']!r} unknown")
        if not s.get("row") and not s.get("page"):
            err(f"settings section {s.get('id')}: no rows")
        if "page" in s and not isinstance(s["page"], bool):
            err(f"settings section {s.get('id')}: page must be a boolean")

    mobile = m.get("settings", {}).get("mobile", {})
    by_section = {s.get("id"): s for s in secs}
    for sid in mobile.get("sections", []):
        if sid not in by_section:
            err(f"settings.mobile: unknown section {sid!r}")
    for rid in mobile.get("rows", []):
        if rid not in rows_seen:
            err(f"settings.mobile: unknown row {rid!r}")
        elif rid.split(".", 1)[0] not in mobile.get("sections", []):
            err(f"settings.mobile: row {rid!r} is outside the mobile sections")
    for sid in mobile.get("sections", []):
        s = by_section.get(sid, {})
        if not s.get("page") and not any(r.get("id") in mobile.get("rows", []) for r in s.get("row", [])):
            err(f"settings.mobile: section {sid!r} has no mobile rows")

    # navigation
    nav = m.get("navigation", {})
    tabs = [t["id"] for t in sorted(nav.get("left_sidebar", {}).get("tab", []), key=lambda t: t["order"])]
    if tabs != TABS:
        err(f"navigation.left_sidebar tabs must be {TABS}, got {tabs}")
    rs = [t["id"] for t in sorted(nav.get("right_sidebar", {}).get("section", []), key=lambda t: t["order"])]
    if rs != RIGHT:
        err(f"navigation.right_sidebar sections must be {RIGHT}, got {rs}")
    if nav.get("left_sidebar", {}).get("settings_position") != "bottom_fixed":
        err("navigation.left_sidebar.settings_position must be bottom_fixed")
    h = nav.get("header", {})
    if (h.get("leading"), h.get("trailing")) != ("sidebar.left.toggle", "sidebar.right.toggle"):
        err("navigation.header leading/trailing must be the left/right sidebar toggles")
    if nav.get("centre", {}).get("views") != ["session", "new_session", "settings"]:
        err("navigation.centre.views mismatch")
    panel = nav.get("panel", {})
    if panel.get("views") != ["file", "diff"]:
        err("navigation.panel.views must be ['file', 'diff']")
    if panel.get("anchored") != "centre" or panel.get("default") != "file":
        err("navigation.panel must be anchored centre and default to file")
    for term in ("panel.file", "panel.diff"):
        if term not in terms:
            err(f"navigation.panel needs the vocabulary term {term!r}")
    ref_ids = [h.get("leading"), h.get("trailing"), h.get("title_click"), nav.get("left_sidebar", {}).get("settings_entry"),
               nav.get("notifications", {}).get("command")]
    ref_ids += h.get("extra", []) + [t.get("command") for t in nav.get("left_sidebar", {}).get("tab", [])]
    for r in ref_ids:
        if r and r not in ids:
            err(f"navigation references unknown command {r!r}")
    if nav.get("waiting_card", {}).get("position") != "above_composer":
        err("navigation.waiting_card.position must be above_composer")

    check_strings(m, path, err)

    if errs:
        print(f"{path}: {len(errs)} problem(s)")
        for e in errs:
            print("  -", e)
        return 1
    nrows = sum(len(s.get("row", [])) for s in secs)
    print(f"ok: {len(cmds)} commands, {len(slashes)} slash names, {len(secs)} settings sections, {nrows} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
