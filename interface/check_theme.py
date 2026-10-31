#!/usr/bin/env python3
"""Check interface/theme.toml: WCAG contrast, completeness, glyph sanity.

Stdlib only. Usage: python3 interface/check_theme.py [path/to/theme.toml]
Exit status 0 when every check passes.
"""
import sys
import tomllib
import unicodedata
from pathlib import Path

GROUPS = ("surface", "text", "border", "interactive", "status", "diff", "agent")
STATUSES = ("running", "done", "error", "denied", "waiting", "interrupted", "cancelled")
MONO_MODS = {"bold", "dim", "reverse", "underline"}


def channel(v):
    v /= 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hex_rgb(h):
    return tuple(int(h[i : i + 2], 16) for i in (1, 3, 5))


def xterm_rgb(i):
    if i < 16:
        raise ValueError("ANSI index has no fixed colour")
    if i >= 232:
        v = 8 + 10 * (i - 232)
        return (v, v, v)
    i -= 16
    lv = [0, 95, 135, 175, 215, 255]
    return (lv[i // 36], lv[(i // 6) % 6], lv[i % 6])


def flatten(theme):
    return {f"{g}.{k}": v for g in GROUPS for k, v in theme[g].items()}


def main(path):
    data = tomllib.loads(Path(path).read_text())
    meta = data["meta"]
    tmin, nmin = meta["text_min"], meta["nontext_min"]
    errors, rows = [], 0

    def check(name, fg, bg, need):
        nonlocal rows
        rows += 1
        r = ratio(fg, bg)
        if r < need:
            errors.append(f"{name}: {r:.2f} < {need}")

    surfaces = ("surface.canvas", "surface.panel", "surface.raised", "surface.sidebar")
    for tn in meta["themes"]:
        t = flatten(data[tn])
        c = {k: hex_rgb(v) for k, v in t.items()}

        def resolve(ref, c=c):
            return c[ref]

        for k in t:
            if not (len(t[k]) == 7 and t[k][0] == "#"):
                errors.append(f"{tn}.{k}: not #rrggbb")
        sets = {
            "hex": (c, "{}"),
            "256": (
                {k: xterm_rgb(v) for k, v in data["fallback256"][tn].items()},
                "256 {}",
            ),
        }
        missing = set(t) - set(data["fallback256"][tn])
        missing |= set(t) - set(data["fallback16"][tn])
        if missing:
            errors.append(f"{tn}: fallback missing {sorted(missing)}")
        for label, (cc, _) in sets.items():
            p = f"{tn}/{label}"
            for s in surfaces:
                for tk in ("text.primary", "text.secondary", "text.muted"):
                    check(f"{p} {tk} on {s}", cc[tk], cc[s], tmin)
                # Unit suffixes and other de-emphasised labels: quieter than muted,
                # so they are held to the mark floor rather than the text floor.
                check(f"{p} text.faint on {s}", cc["text.faint"], cc[s], nmin)
                for mk in ("border.focus", "interactive.accent", "interactive.multi",
                           "interactive.focus",
                           "status.success", "status.warning", "status.error", "status.info"):
                    check(f"{p} {mk} on {s}", cc[mk], cc[s], nmin)
                for agent in data[tn]["agent"]:
                    check(f"{p} agent.{agent} on {s}", cc[f"agent.{agent}"], cc[s], tmin)
                for sk in ("status.success", "status.warning", "status.error", "status.info",
                           "interactive.accent", "interactive.multi"):
                    check(f"{p} {sk} (as text) on {s}", cc[sk], cc[s], tmin)
            check(f"{p} text.primary on selection", cc["text.primary"], cc["interactive.selection"], tmin)
            check(f"{p} text.secondary on selection", cc["text.secondary"], cc["interactive.selection"], tmin)
            check(f"{p} text.muted on hover", cc["text.muted"], cc["interactive.hover"], tmin)
            check(f"{p} text.primary on hover", cc["text.primary"], cc["interactive.hover"], tmin)
            check(f"{p} on_accent on accent", cc["interactive.on_accent"], cc["interactive.accent"], tmin)
            check(f"{p} diff.add on add_bg", cc["diff.add"], cc["diff.add_bg"], tmin)
            check(f"{p} diff.del on del_bg", cc["diff.del"], cc["diff.del_bg"], tmin)
            check(f"{p} text.primary on add_bg", cc["text.primary"], cc["diff.add_bg"], tmin)
            check(f"{p} text.primary on del_bg", cc["text.primary"], cc["diff.del_bg"], tmin)
            # Surfaces must be told apart (decorative step, but never identical).
            for a, b in (("surface.canvas", "surface.panel"), ("surface.panel", "surface.raised")):
                if cc[a] == cc[b]:
                    errors.append(f"{p}: {a} == {b}")
            for name, role in data["roles"].items():
                fg, bg = cc[role["fg"]], cc[role["bg"]]
                check(f"{p} role {name} text", fg, bg, tmin)
                if role["rail"]:
                    check(f"{p} role {name} rail", cc[role["rail"]], bg,
                          1.1 if role["rail"].startswith("border.") else nmin)  # hairline rails are decorative
        check(f"{tn} border.default vs canvas (informational >=1.1)", c["border.default"], c["surface.canvas"], 1.1)
        check(f"{tn} border.strong vs canvas (decorative, informational >=1.1)", c["border.strong"], c["surface.canvas"], 1.1)

    slots = set(data["dark"]["agent"])
    if slots != set(data["light"]["agent"]):
        errors.append("agent slots differ between themes")
    pins = data["agent_pins"]
    if not set(pins.values()) <= slots or len(set(pins.values())) != len(pins):
        errors.append("agent pins must name distinct palette slots")

    # Role references resolve; monochrome covers every token and role.
    t0 = flatten(data[meta["themes"][0]])
    for name, role in data["roles"].items():
        for f in ("fg", "bg", "rail"):
            if role[f] and role[f] not in t0:
                errors.append(f"role {name}.{f}: unknown token {role[f]}")
    missing = set(t0) - set(data["monochrome"]["tokens"])
    if missing:
        errors.append(f"monochrome.tokens missing {sorted(missing)}")
    for k, mods in list(data["monochrome"]["tokens"].items()) + list(data["monochrome"]["roles"].items()):
        if not set(mods) <= MONO_MODS:
            errors.append(f"monochrome {k}: bad modifiers {mods}")
    if set(data["monochrome"]["roles"]) != set(data["roles"]):
        errors.append("monochrome.roles must list every role")

    # Glyphs.
    st = data["glyphs"]["status"]
    if set(st) != set(STATUSES):
        errors.append(f"glyphs.status must be exactly {STATUSES}")
    for table in ("status", "fold", "sidebar", "attention", "structure", "verb"):
        for name, g in data["glyphs"][table].items():
            for mode in ("unicode", "ascii"):
                s = g[mode]
                if any(unicodedata.east_asian_width(ch) in "WF" or ord(ch) > 0x1F000 for ch in s):
                    errors.append(f"glyph {table}.{name}.{mode} is wide or emoji")
                if mode == "ascii" and not s.isascii():
                    errors.append(f"glyph {table}.{name} ascii is not ASCII")
            if "token" in g and g["token"] not in t0:
                errors.append(f"glyph {table}.{name}: unknown token {g['token']}")
    verbs = {"read", "edit", "shell", "search", "code", "ask", "session", "web"}
    if set(data["glyphs"]["verb"]) != verbs:
        errors.append(f"glyphs.verb must be exactly {sorted(verbs)}")
    for mode in ("unicode", "ascii"):
        marks = [st[s][mode] for s in STATUSES]
        if len(set(marks)) != len(marks):
            errors.append(f"status {mode} glyphs are not unique: {marks}")
    if not all(st[s]["label"] == s for s in STATUSES):
        errors.append("status labels must equal status names")
    att = data["glyphs"]["attention"]
    if sorted(g["rank"] for g in att.values()) != [1, 2, 3]:
        errors.append("attention ranks must be 1..3")

    if errors:
        print("FAIL")
        for e in errors:
            print("  " + e)
        return 1
    print(f"ok: {rows} contrast checks, themes {meta['themes']}, 256 fallback, monochrome, glyphs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("theme.toml")))
