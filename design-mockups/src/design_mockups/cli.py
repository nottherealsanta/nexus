"""``design`` command: open, compare and screenshot the mock-ups.

    design                         list designs and elements
    design one | 2 | ledger        open a design   [--light] [--state permission] [--palette signal]
    design elements [slug]         every variant of every element; a–f picks one for your mix
    design mix                     your picks combined   [--layout focus] [--pick recording=c ...]
    design picks [--reset]         show or clear your picks
    design shoot [designs…]        SVG screenshots + shots/index.html
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .designs import DESIGNS, LAYOUTS, NUMBER_WORDS, find
from .elements import ELEMENTS
from .kit import PALETTES
from .picks import PATH as PICKS_PATH
from .picks import Picks

ROOT = PICKS_PATH.parent


def _list() -> None:
    print("Designs  (uv run design <name>)\n")
    for i, d in enumerate(DESIGNS):
        print(f"  {NUMBER_WORDS[i]:<6} {i + 1}  {d.slug:<10} {d.idea.split('. ')[0]}.")
    print("\nOther commands\n")
    print("  elements [slug]   gallery of every element's variants (pick with a–f)")
    print("  mix               your picks combined into one design")
    print("  picks [--reset]   show or clear your picks")
    print("  shoot [names…]    screenshots → shots/index.html")
    print(f"\nElements ({len(ELEMENTS)}, {sum(len(e.variants) for e in ELEMENTS.values())} variants)\n")
    for slug, e in ELEMENTS.items():
        print(f"  {slug:<16} {len(e.variants)}  " + " · ".join(f"{v.key.upper()} {v.name}" for v in e.variants))
    print("\nIn a design: 1–0 states · t theme · c palette · [ ] sidebars · l logs · n/p design · g elements · ? about · q quit")


def _picks_show(p: Picks) -> None:
    print(f"{PICKS_PATH}\n\n  layout   {p.layout}\n  palette  {p.palette} ({'light' if p.light else 'dark'})\n")
    for slug, e in ELEMENTS.items():
        key = p.elements.get(slug)
        print(f"  {slug:<16} {(key or 'a').upper()} {e.get(key or 'a').name}{'' if key else '  (default)'}")


def _sizes(s: str) -> list[tuple[int, int]]:
    out = []
    for part in s.split(","):
        w, h = part.lower().split("x")
        out.append((int(w), int(h)))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="design", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="list")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--light", action="store_true", help="start in the light palette")
    ap.add_argument("--dark", action="store_true", help="start in the dark palette")
    ap.add_argument("--state", choices=[s for _, s, _ in __import__("design_mockups.app", fromlist=["STATES"]).STATES], default="idle")
    ap.add_argument("--palette", choices=list(PALETTES))
    ap.add_argument("--layout", choices=list(LAYOUTS), help="mix: layout")
    ap.add_argument("--pick", action="append", default=[], metavar="ELEMENT=VARIANT", help="mix: override one element")
    ap.add_argument("--reset", action="store_true", help="picks: clear picks.json")
    ap.add_argument("--sizes", default="140x42", help="shoot: comma-separated WxH (e.g. 140x42,80x24)")
    ap.add_argument("--out", default=str(ROOT / "shots"), help="shoot: output directory")
    ap.add_argument("--no-elements", action="store_true", help="shoot: skip element sheets")
    ap.add_argument("--no-designs", action="store_true", help="shoot: skip design screenshots")
    a = ap.parse_args(argv)
    light = True if a.light else False if a.dark else None
    cmd = a.command.lower()

    from .app import run  # Textual import only when needed

    if cmd in ("list", "ls", "help"):
        _list()
    elif cmd in ("elements", "element", "gallery"):
        slug = a.args[0] if a.args else ""
        if slug and slug not in ELEMENTS:
            sys.exit(f"unknown element {slug!r}; one of: {', '.join(ELEMENTS)}")
        run(light=light, palette_name=a.palette, gallery=slug)
    elif cmd == "mix":
        p = Picks.load()
        if a.layout:
            p.layout = a.layout
        if a.palette:
            p.palette = a.palette
        if light is not None:
            p.light = light
        for spec in a.pick:
            el, _, key = spec.partition("=")
            if el not in ELEMENTS or key.lower() not in {v.key for v in ELEMENTS[el].variants}:
                sys.exit(f"bad --pick {spec!r}: element=variant, e.g. recording=c")
            p.elements[el] = key.lower()
        if a.layout or a.palette or light is not None or a.pick:
            p.save()
        run(mode="mix", state=a.state, picks=p)
    elif cmd == "picks":
        if a.reset:
            Picks.reset()
            print("picks cleared")
        else:
            _picks_show(Picks.load())
    elif cmd == "shoot":
        from .shoot import shoot_designs, shoot_elements, write_index
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        targets = [t for t in a.args if t != "mix"]
        indices = [find(t) for t in targets] if targets else list(range(len(DESIGNS)))
        if None in indices:
            sys.exit(f"unknown design in {targets}")
        modes = [light] if light is not None else [False, True]
        if not a.no_designs:
            asyncio.run(shoot_designs(out, indices, _sizes(a.sizes), modes, "mix" in a.args))
        if not a.no_elements:
            shoot_elements(out, [(a.palette or "nexus", True), (a.palette or "nexus", False)] if not a.palette else [(a.palette, light is not True)])
        print(f"open {write_index(out)}")
    else:
        i = find(cmd)
        if i is None:
            _list()
            sys.exit(f"\nunknown design or command {a.command!r}")
        run(index=i, state=a.state, light=light, palette_name=a.palette)


if __name__ == "__main__":
    main()
