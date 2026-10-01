"""Overlays: command palette and model picker."""

from __future__ import annotations

from itertools import groupby

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..kit import Ctx
from . import element

pal = element("command-palette", "Command palette", "ctrl+p: every command, with its key.")


def _rowkeys(label: Text, keys: str, c: Ctx, selected: bool) -> Table:
    t = Table.grid(expand=True)
    t.add_column(ratio=1, no_wrap=True)
    t.add_column(no_wrap=True, justify="right")
    bg = "element_hi" if selected else None
    label.stylize(c.s(None, bg))
    t.add_row(label, Text(keys + " ", c.s("quiet", bg)))
    return t


@pal.variant("Filter + list", "Today: a filter row, dim rows, a grey selected row, keys on the right.")
def _pa(c: Ctx):
    rows = [Text(" > ", c.s("accent", "element")) + Text("Type a command…" + " " * 40, c.s("quiet", "element")), Text()]
    for i, r in enumerate(c.f.palette):
        rows.append(_rowkeys(Text(" " + r.label, c.s("text" if i == 0 else "muted")), r.keys or r.hint, c, i == 0))
    return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s("border_strong"), width=64, padding=(0, 1), style=c.s(None, "panel"),
                 title=Text(" Commands ", c.s("text", bold=True)), title_align="left")


@pal.variant("Grouped", "Commands under group headings, slash command shown beside each.")
def _pb(c: Ctx):
    rows = []
    for g, items in groupby(c.f.palette, key=lambda r: r.group):
        rows.append(c.rule(g.upper(), 58))
        for r in items:
            label = c.t(("  " + r.label, "text"), (f"  {r.hint}" if r.hint else "", "accent"))
            rows.append(_rowkeys(label, r.keys, c, r.label == "Choose model…"))
    return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s("border_strong"), width=64, padding=(0, 1), style=c.s(None, "panel"))


@pal.variant("Fuzzy matches", "As you type, matched letters light up and the best match is first.")
def _pc(c: Ctx):
    q = "mod"
    hits = [r for r in c.f.palette if all(ch in r.label.lower() for ch in q)]

    def hl(s: str) -> Text:
        out, qi = Text(), 0
        for ch in s:
            if qi < len(q) and ch.lower() == q[qi]:
                out.append(ch, c.s("accent", bold=True, underline=True))
                qi += 1
            else:
                out.append(ch, c.s("text"))
        return out
    rows = [c.t((" ⌕ ", "quiet"), (q, c.s("text", bold=True)), ("▏", "accent"), (f"   {len(hits)} of {len(c.f.palette)}", "quiet")), Text()]
    for i, r in enumerate(hits):
        rows.append(_rowkeys(Text(" ") + hl(r.label) + Text(f"  {r.group}", c.s("quiet")), r.keys or r.hint, c, i == 0))
    return Panel(Group(*rows), box=box.HEAVY, border_style=c.s("accent"), width=64, padding=(0, 1), style=c.s(None, "panel"))


mp = element("model-picker", "Model picker", "/model: choose a model.")


@mp.variant("Grouped list", "Today: search, favorites, then by provider; ★ favorite, ✓ current.")
def _ma(c: Ctx):
    rows = [Text(" ⌕ Search models…" + " " * 30, c.s("quiet", "element")), Text(), c.rule("FAVORITES", 56)]
    for m in c.f.models:
        if m.favorite:
            rows.append(c.t((" ★ ", "yellow"), (m.name, "text"), ("  ✓" if m.current else "", "green")))
    for prov, items in groupby(c.f.models, key=lambda m: m.provider):
        rows.append(c.rule(prov.upper(), 56))
        for m in items:
            rows.append(c.t(("   ", ""), (m.name, "muted"), (f"  {m.context}", "quiet")))
    rows += [Text(), c.keys(("enter", "select"), ("f", "favorite"), ("ctrl+r", "refresh"))]
    return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s("border_strong"), width=64, padding=(0, 1), style=c.s(None, "panel"),
                 title=Text(" Model ", c.s("text", bold=True)), title_align="left")


@mp.variant("Comparison table", "A table you can sort: context, price, capabilities.")
def _mb(c: Ctx):
    t = Table(box=box.SIMPLE_HEAD, header_style=c.s("quiet", bold=True), border_style=c.s("border"), pad_edge=False, expand=True)
    for col, j in (("", "left"), ("MODEL", "left"), ("PROVIDER", "left"), ("CONTEXT ↓", "right"), ("$ IN / OUT per M", "right"), ("", "left")):
        t.add_column(col, justify=j, no_wrap=True)
    for m in c.f.models:
        sel = m.current
        st = c.s("text", "element_hi") if sel else c.s("text")
        tags = Text()
        for tg in m.tags:
            tags.append_text(c.tag(tg, {"reasoning": "purple", "vision": "blue", "fast": "green", "local": "yellow"}[tg]))
            tags.append(" ")
        t.add_row(Text("✓" if sel else ("★" if m.favorite else " "), c.s("green" if sel else "yellow")), Text(m.name, st),
                  Text(m.provider, c.s("muted")), Text(m.context, c.s("text")), Text(m.price, c.s("muted")), tags)
    return Panel(t, box=box.ROUNDED, border_style=c.s("border_strong"), width=96, style=c.s(None, "panel"), title=Text(" /model ", c.s("text", bold=True)), title_align="left")


@mp.variant("List + detail", "List on the left; the highlighted model's full card on the right.")
def _mc(c: Ctx):
    left = Group(*(Text((" › " if m.current else "   ") + m.name, c.s("text", "element_hi") if m.current else c.s("muted")) for m in c.f.models))
    m = c.f.models[0]
    right = Group(Text(m.name, c.s("text", bold=True)), Text(m.provider, c.s("quiet")), Text(),
                  c.kv([("context", m.context), ("price", m.price + " per M"), ("effort", "low · medium · high · max"), ("tools", "yes"),
                        ("vision", "yes"), ("used here", "4 turns · $0.31")], 9))
    g = Table.grid(expand=True, padding=(0, 2))
    g.add_column(width=26)
    g.add_column(ratio=1)
    g.add_row(left, right)
    return Panel(g, box=box.ROUNDED, border_style=c.s("border_strong"), width=80, padding=(0, 1), style=c.s(None, "panel"))
