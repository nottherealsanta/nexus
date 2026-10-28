#!/usr/bin/env python3
"""Build shots/index.html: a chooser with one section per decision (options.json),
each option showing its TUI shots, NSH shots and/or 'before' captures.
Choices and notes persist in localStorage and export as choices.json/Markdown."""
import json
import pathlib

here = pathlib.Path(__file__).resolve().parent
opts = json.loads((here / "options.json").read_text())
shots = here / "shots"


def images(o):
    """Resolve an option's image refs to paths relative to shots/ (None when not rendered yet)."""
    out = []
    for tid in o.get("tui", []):
        for th in ("dark", "light", "mono", "ascii"):
            p = shots / "tui" / f"{tid}-{th}.png"
            if p.exists():
                out.append({"src": f"tui/{p.name}", "theme": th, "client": "TUI", "label": tid})
        if not any(i["label"] == tid for i in out):
            out.append({"src": None, "theme": "dark", "client": "TUI", "label": tid})
    for nid in o.get("nsh", []):
        found = False
        for th in ("dark", "light"):
            p = shots / "nsh" / f"{nid}-{th}.png"
            if p.exists():
                found = True
                out.append({"src": f"nsh/{p.name}", "theme": th, "client": "NSH", "label": nid})
        if not found:
            out.append({"src": None, "theme": "dark", "client": "NSH", "label": nid})
    for b in o.get("before", []):
        p = here / "before" / b
        out.append({"src": f"../before/{b}" if p.exists() else None, "theme": "light" if "light" in b else "dark", "client": "TUI", "label": "today: " + b})
    for r in o.get("reference", []):
        out.append({"src": f"../reference/{r}", "theme": "dark", "client": "TUI", "label": "old Nexus: " + r})
    return out


for d in opts["decisions"]:
    for o in d["options"]:
        o["images"] = images(o)

page = (here / "gallery.html").read_text().replace("/*DATA*/null", json.dumps(opts))
(shots / "index.html").write_text(page)
n = sum(len(d["options"]) for d in opts["decisions"])
print(f"wrote shots/index.html: {len(opts['decisions'])} decisions, {n} options")
