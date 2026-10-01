"""Subagent (Task) rows: finished, running, failed, and a parallel group."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..fixture import SubAgent
from ..kit import STATUS_GLYPH, STATUS_ROLE, Ctx, tok
from . import element


def _samples(c: Ctx):
    a = c.f.subagents()
    return [("done", a[0], ""), ("running", a[1], ""), ("failed", a[2], ""), ("parallel group", tuple(a), "")]


sub = element("subagent", "Subagent", "A Task handed to a child agent, alone and as a parallel group.", _samples)


def _each(c: Ctx, one, header: Text | None = None):
    if isinstance(c.item, tuple):
        head = header if header is not None else c.t(("⑂ ", "green"), (f"{len(c.item)} agents in parallel", c.s("text", bold=True)),
                                                     ("  1 done · 1 running · 1 failed", "quiet"))
        return Group(head, *(one(c.with_(item=a)) for a in c.item))
    return one(c)


def _status(c: Ctx, a: SubAgent) -> Text:
    return c.t((STATUS_GLYPH[a.status] + " ", STATUS_ROLE[a.status]), (a.status, STATUS_ROLE[a.status]))


@sub.variant("Link row", "Today: one row with agent, task and status; enter opens the child transcript.")
def _sa(c: Ctx):
    def one(c: Ctx):
        a: SubAgent = c.item
        return c.t(("⑂ ", "green"), (a.agent, c.s("text", bold=True)), (" · " + a.task, "muted"), ("  ", ""), _status(c, a),
                   (f" · {a.duration}", "quiet"), ("  open ›", "accent"))
    return _each(c, one)


@sub.variant("Card", "A card per agent: task, model, its tool calls, and its result.")
def _sb(c: Ctx):
    def one(c: Ctx):
        a: SubAgent = c.item
        rows = [Text(a.task, c.s("text"))]
        for call in a.calls:
            rows.append(c.t(("  " + STATUS_GLYPH[call.status] + " ", STATUS_ROLE[call.status]), (call.tool, "muted"), (" " + call.target, "quiet"),
                            (f"  {call.summary}", "quiet")))
        if a.status == "running":
            rows.append(c.t(("  ◌ ", "accent"), ("thinking…", "quiet")))
        if a.result:
            rows.append(c.t(("→ ", STATUS_ROLE[a.status]), (a.result, "text" if a.status == "done" else "red")))
        return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s(STATUS_ROLE[a.status] if a.status != "done" else "border"), padding=(0, 1),
                     title=c.t((f" {a.agent} ", c.s("green", bold=True)), (f"· {a.model} ", "quiet")), title_align="left",
                     subtitle=c.t((f" {STATUS_GLYPH[a.status]} {a.status} · {a.duration} · {tok(a.tokens)} tok ", STATUS_ROLE[a.status])), subtitle_align="right")
    return _each(c, one)


def _branch(c: Ctx, prefix: str, body: Text) -> Table:
    """A tree row whose text wraps under itself, not under the tree lines."""
    g = Table.grid()
    g.add_column(no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(Text(prefix, c.s("border")), body)
    return g


@sub.variant("Tree", "Nested under the parent with tree lines; the child's calls are its branches.")
def _sc(c: Ctx):
    def one(c: Ctx, last: bool = True):
        a: SubAgent = c.item
        stem, pad = ("└─ ", "   ") if last else ("├─ ", "│  ")
        rows = [_branch(c, stem, c.t((a.agent, c.s("green", bold=True)), (f" {a.task}", "text"), ("  ", ""), _status(c, a), (f" {a.duration}", "quiet")))]
        for i, call in enumerate(a.calls):
            rows.append(_branch(c, pad + ("└─ " if i == len(a.calls) - 1 and not a.result else "├─ "), c.t((call.tool, "muted"), (" " + call.target, "quiet"))))
        if a.result:
            rows.append(_branch(c, pad + "└─ ", c.t(("⇒ " + a.result, "text" if a.status == "done" else "red"))))
        return Group(*rows)
    if isinstance(c.item, tuple):
        return Group(c.t(("Task ×3", c.s("text", bold=True)), ("  parallel", "quiet")),
                     *(one(c.with_(item=a), i == len(c.item) - 1) for i, a in enumerate(c.item)))
    return one(c)


@sub.variant("Lanes", "Each agent a lane with a progress track; good for many in parallel.")
def _sd(c: Ctx):
    items = c.item if isinstance(c.item, tuple) else (c.item,)
    g = Table.grid(expand=True, padding=(0, 1))
    g.add_column(width=9, no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(width=18, no_wrap=True)
    g.add_column(width=12, no_wrap=True)
    for a in items:
        frac = {"done": 1.0, "running": .55, "failed": .2}[a.status]
        g.add_row(Text(a.agent, c.s("green", bold=True)), Text(a.task, c.s("muted")), c.bar(frac, 18, STATUS_ROLE[a.status], "border", "█", "░"),
                  c.t((f"{STATUS_GLYPH[a.status]} {a.duration}", STATUS_ROLE[a.status])))
    return g
