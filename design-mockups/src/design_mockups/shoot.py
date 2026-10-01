"""``design shoot``: SVG screenshots of every design × state, element sheets
with every variant, and an ``index.html`` to compare them in one browser tab."""

from __future__ import annotations

import html
import io
from pathlib import Path

from rich.console import Console, Group
from rich.table import Table
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from .app import STATES, MockupsApp
from .designs import DESIGNS
from .elements import ELEMENTS
from .kit import Ctx, Palette, palette
from .picks import Picks

SIDEBAR_WIDTH = {"sessions": 34, "details": 42}


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def terminal_theme(p: Palette) -> TerminalTheme:
    normal = [_rgb(x) for x in (p.bg, p.red, p.green, p.yellow, p.blue, p.purple, p.cyan, p.text)]
    return TerminalTheme(_rgb(p.bg), _rgb(p.text), normal, normal)


def element_sheet(slug: str, p: Palette, width: int = 112) -> str:
    el = ELEMENTS[slug]
    c = Ctx(p)
    con = Console(record=True, width=width, file=io.StringIO(), color_system="truecolor")
    con.print(Text.assemble((f" {el.title} ", f"bold {p.on_accent} on {p.accent}"), (f"  {el.description}", p.muted)))
    con.print()
    blocks = []
    for v in el.variants:
        users = [d.title for d in DESIGNS if d.picks.get(slug, "a") == v.key]
        parts = [Text.assemble((f" {v.key.upper()} ", f"bold {p.on_accent} on {p.muted}"), (f" {v.name}", f"bold {p.text}")),
                 Text.assemble((v.note, p.muted), ("  · " + ", ".join(users) if users else "", p.quiet)), Text()]
        for caption, item, phase in el.samples(c):
            if caption:
                parts.append(Text(f"· {caption}", p.quiet))
            parts += [v.render(c.with_(item=item, phase=phase)), Text()]
        blocks.append(Group(*parts))
    w = SIDEBAR_WIDTH.get(slug)
    if w:
        grid = Table.grid(padding=(0, 3))
        for _ in blocks:
            grid.add_column(width=w)
        grid.add_row(*blocks)
        con.width = max(width, (w + 3) * len(blocks))
        con.print(grid)
    else:
        for b in blocks:
            con.print(Text("─" * width, p.border))
            con.print(b)
    return con.export_svg(title=f"{el.title} · {p.name}", theme=terminal_theme(p))


async def shoot_designs(out: Path, indices: list[int], sizes: list[tuple[int, int]], modes: list[bool], mix: bool) -> list[Path]:
    paths: list[Path] = []
    runs = [("design", i) for i in indices] + ([("mix", 0)] if mix else [])
    for mode, i in runs:
        slug = "mix" if mode == "mix" else f"{i + 1:02d}-{DESIGNS[i].slug}"
        for light in modes:
            for w, h in sizes:
                app = MockupsApp(index=i, light=light, mode=mode, picks=Picks.load())
                async with app.run_test(size=(w, h)) as pilot:
                    await pilot.pause()
                    for key, name, _ in STATES:
                        await pilot.press(key)
                        await pilot.pause()
                        d = out / slug
                        d.mkdir(parents=True, exist_ok=True)
                        fn = f"{key}-{name}-{'light' if light else 'dark'}-{w}x{h}.svg"
                        app.save_screenshot(fn, str(d))
                        paths.append(d / fn)
                print(f"  {slug} {'light' if light else 'dark'} {w}x{h}: {len(STATES)} states")
    return paths


def shoot_elements(out: Path, palettes: list[tuple[str, bool]]) -> list[Path]:
    paths = []
    d = out / "elements"
    d.mkdir(parents=True, exist_ok=True)
    for name, dark in palettes:
        p = palette(name, dark)
        for slug in ELEMENTS:
            fn = d / f"{slug}-{name}-{'dark' if dark else 'light'}.svg"
            fn.write_text(element_sheet(slug, p))
            paths.append(fn)
    print(f"  elements: {len(paths)} sheets")
    return paths


def write_index(out: Path) -> Path:
    def imgs(paths: list[Path]) -> str:
        return "".join(f'<figure><a href="{html.escape(str(x.relative_to(out)))}"><img loading="lazy" src="{html.escape(str(x.relative_to(out)))}"></a>'
                       f"<figcaption>{html.escape(x.stem)}</figcaption></figure>" for x in paths)
    sections = []
    for d in sorted(p for p in out.iterdir() if p.is_dir() and p.name != "elements"):
        sections.append(f"<h2>{html.escape(d.name)}</h2><div class=grid>{imgs(sorted(d.glob('*.svg')))}</div>")
    el = out / "elements"
    if el.exists():
        for slug, e in ELEMENTS.items():
            sections.append(f"<h2>element · {html.escape(e.title)}</h2><div class=grid wide>{imgs(sorted(el.glob(f'{slug}-*.svg')))}</div>")
    page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nexus TUI mock-ups</title>
<style>
:root{{color-scheme:dark}} body{{margin:0;padding:16px 24px;background:#111;color:#ddd;font:14px/1.4 system-ui,sans-serif}}
h1{{font-size:20px}} h2{{font-size:15px;margin:32px 0 8px;color:#fab283}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}}
.grid.wide{{grid-template-columns:repeat(auto-fill,minmax(640px,1fr))}}
figure{{margin:0}} img{{width:100%;border:1px solid #333;border-radius:6px;background:#000}}
figcaption{{font-size:12px;color:#888;margin-top:4px}}
</style><h1>Nexus TUI mock-ups</h1><p>Designs × states, then every element's variants. Click to open full size.</p>
{''.join(sections)}"""
    path = out / "index.html"
    path.write_text(page)
    return path
