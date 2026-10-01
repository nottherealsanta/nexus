"""Things that wait on the user: permission requests and agent questions."""

from __future__ import annotations

from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..fixture import Permission, Question
from ..kit import Ctx
from . import element

perm = element("permission", "Permission prompt", "A tool asking to run.", lambda c: [("Bash", c.f.permission, "")])


@perm.variant("Docked list", "Today: a box docked above the composer with a selectable list of choices.")
def _p_a(c: Ctx):
    p: Permission = c.item
    rows = [c.t(("Build wants to run ", "muted"), (p.tool, c.s("text", bold=True))),
            Text("  $ " + p.command, c.s("text", "element")), Text()]
    for i, (_k, label) in enumerate(p.choices):
        rows.append(Text(f" {'›' if i == 0 else ' '} {label} ", c.s("text", "element_hi") if i == 0 else c.s("muted")))
    return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s("yellow"), padding=(0, 1), style=c.s(None, "panel"))


@perm.variant("One line", "A single line in the timeline with letter keys; nothing docks.")
def _p_b(c: Ctx):
    p: Permission = c.item
    return c.t(("? ", c.s("yellow", bold=True)), (f"{p.tool} ", c.s("text", bold=True)), (p.command, "text"), ("   ", ""),
               ("y", c.s("text", bold=True)), (" once  ", "quiet"), ("a", c.s("text", bold=True)), (" always  ", "quiet"),
               ("n", c.s("text", bold=True)), (" deny  ", "quiet"), ("?", c.s("text", bold=True)), (" why", "quiet"))


@perm.variant("Signal banner", "NEEDS APPROVAL banner and offset-shadow buttons.")
def _p_c(c: Ctx):
    p: Permission = c.item
    head = Table.grid(expand=True)
    head.add_column(no_wrap=True)
    head.add_column(ratio=1)
    head.add_row(Text(" ◆ NEEDS APPROVAL ", c.s("bg", "yellow", bold=True)), Text(f" {p.tool.upper()} · {p.agent.upper()}", c.s("yellow", c.p.tint(c.p.yellow, .15), bold=True)))

    def btn(label: str, role: str) -> Text:
        return Text(f" {label} → ", c.s(role, c.p.tint(c.p.role(role), .22), bold=True))
    shadow = Text(" " * 18, c.s(None, c.p.tint(c.p.yellow, .5)))
    return Group(head, Text("$ " + p.command, c.s("text", bold=True)), Text(),
                 btn("ALLOW ONCE", "yellow") + Text("  ") + btn("ALWAYS", "green") + Text("  ") + btn("DENY", "red"),
                 Text(" ") + shadow)


@perm.variant("Full detail", "Every field labelled: command, cwd, why the agent wants it, what it can touch.")
def _p_d(c: Ctx):
    p: Permission = c.item
    body = c.kv([("tool", Text(p.tool, c.s("accent", bold=True))), ("command", Text(p.command, c.s("text", bold=True))), ("cwd", p.cwd),
                 ("reason", Text(p.reason, c.s("muted"))), ("risk", Text(p.risk, c.s("yellow"))), ("agent", p.agent),
                 ("rule", Text("tools.bash.ask = [\"*\"]  (settings.toml:14)", c.s("quiet")))], 8)
    keys = c.keys(("y", "allow once"), ("a", "always allow `pytest`"), ("n", "deny"), ("e", "edit command"))
    return Panel(Group(body, Text(), keys), box=box.DOUBLE, border_style=c.s("yellow"), padding=(0, 1),
                 title=Text(" Permission required ", c.s("yellow", bold=True)), title_align="left")


q = element("question", "Agent question", "The agent asking a multiple-choice question.", lambda c: [("", c.f.question, "")])


@q.variant("List picker", "Today: question, then a list with a grey selected row, docked above the composer.")
def _q_a(c: Ctx):
    x: Question = c.item
    rows = [Text(x.prompt, c.s("text", bold=True)), Text()]
    for i, (label, desc) in enumerate(x.options):
        st = c.s("text", "element_hi") if i == x.selected else c.s("muted")
        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_row(Text(f" {label}  ", st) + Text(desc, c.s("quiet", "element_hi" if i == x.selected else None)))
        rows.append(row)
    rows += [Text(), c.keys(("↑↓", "move"), ("enter", "choose"), ("tab", "write your own"))]
    return Panel(Group(*rows), box=box.ROUNDED, border_style=c.s("blue"), padding=(0, 1), style=c.s(None, "panel"))


@q.variant("Numbered", "Numbered options inline in the timeline; press the number.")
def _q_b(c: Ctx):
    x: Question = c.item
    rows = [c.t(("? ", c.s("blue", bold=True)), (x.prompt, c.s("text", bold=True)))]
    for i, (label, desc) in enumerate(x.options, 1):
        rows.append(c.t((f"  {i} ", c.s("bg", "blue", bold=True)), (f" {label}", c.s("text", bold=True)), (f" — {desc}", "muted")))
    rows.append(c.t(("  4 ", c.s("blue", "element", bold=True)), (" something else…", "quiet")))
    return Group(*rows)


@q.variant("Option cards", "Each option a card side by side; the selected one framed.")
def _q_c(c: Ctx):
    x: Question = c.item
    cards = [Panel(Group(Text(label, c.s("text", bold=True)), Text(desc, c.s("muted"))), width=28, height=5, box=box.ROUNDED,
                   border_style=c.s("blue" if i == x.selected else "border"), padding=(0, 1),
                   title=Text(f" {i + 1} ", c.s("bg", "blue") if i == x.selected else c.s("quiet")), title_align="left")
             for i, (label, desc) in enumerate(x.options)]
    return Group(c.t((x.agent + " asks: ", "blue"), (x.prompt, c.s("text", bold=True))), Columns(cards))
