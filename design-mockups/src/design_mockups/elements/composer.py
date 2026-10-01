"""The composer: editor, agent/model/effort row, and key hints."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..kit import Ctx
from . import element

TYPED = "Fix the jobs.py one too and summarise everything for the PR. @src/auth/jobs.py"

comp = element("composer", "Composer", "Where you type. Empty, typing (with an @file), and while a turn runs.",
               lambda c: [("empty", None, "empty"), ("typing", None, "typing"), ("turn running", None, "busy")])


def _text(c: Ctx) -> Text:
    if c.phase == "typing":
        out = Text()
        a, _, b = TYPED.partition("@")
        out.append(a, c.s("text"))
        out.append("@" + b, c.s("blue", c.p.tint(c.p.blue, .15)))
        out.append("▏", c.s("accent"))
        return out
    if c.phase == "busy":
        return c.t(("Add a note to steer, or wait…", "quiet"))
    return c.t(("Ask anything…  ", "quiet"), ("/", c.s("muted", bold=True)), (" commands  ", "quiet"), ("@", c.s("muted", bold=True)), (" files", "quiet"))


def _agent(c: Ctx) -> Text:
    return c.t(("BUILD", c.s("accent", bold=True)), ("  " + c.f.model, "muted"), ("  " + c.f.provider, "quiet"), ("  " + c.f.effort, "quiet"))


def _row(left: Text, right: Text) -> Table:
    t = Table.grid(expand=True)
    t.add_column(ratio=1, no_wrap=True)
    t.add_column(no_wrap=True, justify="right")
    t.add_row(left, right)
    return t


@comp.variant("Box + agent row", "Today: rounded editor box, agent · model · provider · effort below the text.")
def _c_a(c: Ctx):
    right = c.t(("ctrl+enter", c.s("muted", bold=True)), (" steer  ", "quiet"), ("esc", c.s("muted", bold=True)), (" stop", "quiet")) if c.phase == "busy" else Text()
    return Panel(Group(_text(c), Text(), _row(_agent(c), right)), box=box.ROUNDED,
                 border_style=c.s("border_strong" if c.phase == "typing" else "border"), padding=(0, 1), style=c.s(None, "element"))


@comp.variant("Prompt line", "A single › line; agent and model as quiet text on the right.")
def _c_b(c: Ctx):
    return Group(Text("─" * 200, c.s("border"), no_wrap=True, overflow="crop"),
                 _row(Text("› ", c.s("accent", bold=True)) + _text(c), c.t(("build · opus-5.5 · high", "quiet"))),
                 Text("─" * 200, c.s("border"), no_wrap=True, overflow="crop"),
                 c.t(("  ? for shortcuts", "quiet")) if c.phase != "busy" else c.t(("  esc to interrupt · ctrl+enter to steer", "quiet")))


@comp.variant("Hint footer", "Editor on top, a key-hint footer row that changes with the state.")
def _c_c(c: Ctx):
    hints = {
        "empty": c.keys(("enter", "send"), ("shift+enter", "newline"), ("/", "commands"), ("@", "file"), ("ctrl+x v", "dictate")),
        "typing": c.keys(("enter", "send"), ("tab", "complete @file"), ("ctrl+e", "open in $EDITOR")),
        "busy": c.keys(("ctrl+enter", "steer now"), ("alt+enter", "interrupt + send"), ("esc", "stop")),
    }[c.phase or "empty"]
    return Group(Panel(Group(_text(c), _row(Text(""), _agent(c))), box=box.ROUNDED,
                       border_style=c.s("accent" if c.phase == "typing" else "border"), padding=(0, 1)), hints)


@comp.variant("Signal frame", "Square frame with a MESSAGE label; agent and model as tags; light focus frame.")
def _c_d(c: Ctx):
    focus = c.phase == "typing"
    tags = c.tag("BUILD", "accent") + Text(" ") + c.tag("OPUS 5.5", "blue") + Text(" ") + c.tag("HIGH", "purple")
    send = Text(" SEND → ", c.s("on_accent", "accent", bold=True)) if focus else Text(" SEND → ", c.s("quiet", "element"))
    return Panel(Group(_text(c), Text(), _row(tags, send)), box=box.SQUARE, border_style=c.s("text" if focus else "border_strong"),
                 title=Text(" MESSAGE ", c.s("text" if focus else "quiet", bold=True)), title_align="left", padding=(0, 1))


@comp.variant("Status line", "Borderless editor over a vim-style status line of solid segments.")
def _c_e(c: Ctx):
    mode = {"empty": (" INSERT ", "blue"), "typing": (" INSERT ", "blue"), "busy": (" RUNNING ", "accent")}[c.phase or "empty"]
    line = Text(mode[0], c.s("bg", mode[1], bold=True)) + Text(" Build ", c.s("text", "element_hi")) + Text(f" {c.f.model} ", c.s("muted", "element")) + \
        Text(" high ", c.s("quiet", "element"))
    right = Text(" ctx 61% ", c.s("muted", "element")) + Text(" $0.31 ", c.s("text", "element_hi"))
    t = Table.grid(expand=True)
    t.add_column(no_wrap=True)
    t.add_column(ratio=1)
    t.add_column(no_wrap=True)
    t.add_row(line, Text("", style=c.s(None, "panel")), right)
    return Group(Text("  ") + _text(c), Text(), t)
