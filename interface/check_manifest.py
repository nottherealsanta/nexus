#!/usr/bin/env python3
"""Validate interface/manifest.toml. Stdlib only, Python 3.11+.

Usage: python3 interface/check_manifest.py [path/to/manifest.toml]
"""
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

SECTION_ORDER = [
    "appearance", "providers", "models", "agents", "tools", "skills", "mcp",
    "context", "dictation", "keybindings", "notifications", "about",
]
TABS = ["sessions", "files", "projects"]
RIGHT = ["session_info", "git_status"]
ROW_KINDS = {"choice", "toggle", "number", "text", "list", "action", "info", "keymap"}
SCOPES = {"core", "client"}
CLIENTS = {"both", "tui", "nsh"}
MODS = ["cmd", "ctrl", "alt", "shift"]


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
            if not n.isalpha() or not n.islower():
                err(f"{cid}: slash name {n!r} must be lower-case letters")
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
                if ctx == "escape_chain":
                    continue  # resolved by keymap.escape, checked below
                slot = (client, ctx, key)
                if keys[slot]:
                    err(f"key conflict ({client}, {ctx}, {key}): {keys[slot]} and {cid}")
                keys[slot] = cid
        elif "key_context" in c:
            err(f"{cid}: key_context without keys")

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
        if not s.get("row"):
            err(f"settings section {s.get('id')}: no rows")

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
    if nav.get("centre", {}).get("views") != ["session", "new_session", "editor", "settings"]:
        err("navigation.centre.views mismatch")
    ref_ids = [h.get("leading"), h.get("trailing"), h.get("title_click"), nav.get("left_sidebar", {}).get("settings_entry"),
               nav.get("notifications", {}).get("command")]
    ref_ids += h.get("extra", []) + [t.get("command") for t in nav.get("left_sidebar", {}).get("tab", [])]
    for r in ref_ids:
        if r and r not in ids:
            err(f"navigation references unknown command {r!r}")
    if nav.get("waiting_card", {}).get("position") != "above_composer":
        err("navigation.waiting_card.position must be above_composer")

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
