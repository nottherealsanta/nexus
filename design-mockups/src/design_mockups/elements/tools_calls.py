"""Tool calls in the timeline, file diffs, and the full tool-details view."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..fixture import ToolCall
from ..kit import GROUP_ROLE, STATUS_GLYPH, STATUS_ROLE, Ctx, tok
from . import element


def _calls(c: Ctx):
    f = c.f
    return [("Read · ok", f.call("c1"), ""), ("Grep · long output", f.call("c2"), ""), ("Bash · failed", f.call("c6"), ""),
            ("Bash · running", f.call("c10"), ""), ("Edit · ok", f.call("c4"), "")]


def _group(c: Ctx, name: str) -> str:
    return next((GROUP_ROLE[t.group] for t in c.f.tools if t.name == name), "muted")


def _summary_role(t: ToolCall) -> str:
    return {"error": "red", "running": "accent"}.get(t.status, "quiet")


tc = element("tool-call", "Tool call row", "One tool call in the timeline: running, ok, failed, with long output.", _calls)


@tc.variant("Arrow line", "Today: one muted line per call; failures turn red; details open in a modal.")
def _tc_a(c: Ctx):
    t: ToolCall = c.item
    arrow = {"running": ("→", "accent"), "error": ("✗", "red")}.get(t.status, ("←", "quiet"))
    return c.t((arrow[0] + " ", arrow[1]), (t.tool, "muted" if t.status == "ok" else arrow[1]), (" " + t.target, "muted"),
               (f"  {t.summary}", _summary_role(t)))


@tc.variant("Bullet + result", "● Tool(target), then ⎿ result with the first output lines; clipping announced.")
def _tc_b(c: Ctx):
    t: ToolCall = c.item
    dot = Text("● ", c.s(STATUS_ROLE[t.status]))
    head = dot + c.t((t.tool, c.s("text", bold=True)), ("(", "quiet"), (t.target, "text"), (")", "quiet"))
    rows = [head, c.t(("  ⎿  ", "quiet"), (t.summary, _summary_role(t) if t.status != "ok" else "muted"))]
    shown = [x for x in t.output if x.strip()][-4:] if t.status == "error" else [x for x in t.output if x.strip()][:3]
    for x in shown if len(t.output) > 1 else []:
        rows.append(Text("     " + x, c.s("red" if x.startswith(("E ", "FAILED")) else "quiet"), no_wrap=True, overflow="ellipsis"))
    if len(t.output) > len(shown) + 1:
        rows.append(Text("     ") + c.clipped(len(t.output) - len(shown), how="ctrl+o expand"))
    return Group(*rows)


@tc.variant("Labelled card", "A card: every parameter labelled, output preview, status and timing in the border.")
def _tc_c(c: Ctx):
    t: ToolCall = c.item
    role = STATUS_ROLE[t.status]
    rows = [(k, Text(v, c.s("text"))) for k, v in t.params]
    body = [c.kv(rows, 8)]
    if t.output:
        body.append(Text("─" * 20, c.s("border")))
        out = [x for x in t.output if x.strip()]
        show = out[-5:] if t.status == "error" else out[:5]
        body += [Text(x, c.s("red" if x.startswith(("E ", "FAILED")) else "muted"), no_wrap=True, overflow="ellipsis") for x in show]
        if len(out) > len(show):
            body.append(c.clipped(len(out) - len(show)))
    return Panel(Group(*body), box=box.ROUNDED, border_style=c.s(role if t.status != "ok" else "border"), padding=(0, 1),
                 title=c.t((f" {STATUS_GLYPH[t.status]} ", role), (t.tool + " ", c.s(_group(c, t.tool), bold=True))), title_align="left",
                 subtitle=Text(f" {t.summary} · {t.duration} · {tok(t.tokens)} tok ", c.s(role if t.status != "ok" else "quiet")), subtitle_align="right")


@tc.variant("Ledger row", "A fixed-column row: id, tool, params as k=v, status, time, tokens added.")
def _tc_d(c: Ctx):
    t: ToolCall = c.item
    g = Table.grid(expand=True, padding=(0, 1))
    g.add_column(width=4, no_wrap=True)
    g.add_column(width=6, no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(no_wrap=True)
    g.add_column(width=5, justify="right", no_wrap=True)
    g.add_column(width=7, justify="right", no_wrap=True)
    params = Text()
    for i, (k, v) in enumerate(t.params):
        params.append(("  " if i else "") + k + "=", c.s("quiet"))
        params.append(v, c.s("text"))
    g.add_row(Text(t.id, c.s("quiet")), Text(t.tool, c.s(_group(c, t.tool), bold=True)), params,
              c.t((STATUS_GLYPH[t.status] + " ", STATUS_ROLE[t.status]), (t.summary, _summary_role(t) if t.status != "ok" else "muted")),
              Text(t.duration, c.s("quiet")), Text(f"+{tok(t.tokens)}" if t.tokens else "", c.s("accent")))
    return g


@tc.variant("Signal tag", "Tinted TOOL tag, target, and a status square at the right edge.")
def _tc_e(c: Ctx):
    t: ToolCall = c.item
    g = Table.grid(expand=True)
    g.add_column(no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column(no_wrap=True, justify="right")
    role = STATUS_ROLE[t.status]
    g.add_row(c.tag(f"{t.tool.upper():<5}", _group(c, t.tool)), Text("  " + t.target, c.s("text")),
              c.t((t.summary.upper() + " ", c.s(role if t.status != "ok" else "quiet", bold=t.status != "ok")), ("■", role)))
    return g


@tc.variant("Sentence", "Plain English, past tense: reads like a log of what the agent did.")
def _tc_f(c: Ctx):
    t: ToolCall = c.item
    verb = {"Read": "Read", "Grep": "Searched for", "Bash": "Ran" if t.status != "running" else "Running", "Edit": "Edited", "Write": "Created"}.get(t.tool, t.tool)
    tail = {"error": (f" — {t.summary}", "red"), "running": (" …", "accent")}.get(t.status, (f" ({t.summary})", "quiet"))
    target = t.target.strip('"') if t.tool != "Grep" else t.target
    return c.t((verb + " ", "muted"), (target, c.s("text", underline=t.tool in ("Read", "Edit", "Write"))), tail)


# ----------------------------------------------------------------- diff -----

df = element("diff", "Diff", "An Edit's change to a file.", lambda c: [("2 hunks · +9 −2", c.f.call("c4"), "")])


def _split_rows(t: ToolCall):
    left, right = [], []
    for d in t.diff:
        if d.kind == "@":
            n = max(len(left), len(right))
            left += [None] * (n - len(left))
            right += [None] * (n - len(right))
            left.append(d)
            right.append(d)
        elif d.kind == "-":
            left.append(d)
        elif d.kind == "+":
            right.append(d)
        else:
            n = max(len(left), len(right))
            left += [None] * (n - len(left))
            right += [None] * (n - len(right))
            left.append(d)
            right.append(d)
    n = max(len(left), len(right))
    return list(zip(left + [None] * (n - len(left)), right + [None] * (n - len(right))))


@df.variant("Split", "Today: original left, updated right, plain filename title.")
def _df_a(c: Ctx):
    t: ToolCall = c.item
    g = Table(box=box.SQUARE, expand=True, border_style=c.s("border"), show_header=True, header_style=c.s("muted"), padding=(0, 0))
    g.add_column("", width=3, justify="right", style=c.s("quiet"))
    g.add_column(t.target, ratio=1, no_wrap=True, overflow="ellipsis")
    g.add_column("", width=3, justify="right", style=c.s("quiet"))
    g.add_column(t.target, ratio=1, no_wrap=True, overflow="ellipsis")
    for a, b in _split_rows(t):
        if a is not None and a.kind == "@":
            g.add_row("", Text(a.text, c.s("blue")), "", Text(""))
            continue
        def cell(d, side):
            if d is None:
                return "", Text("")
            st = c.s("text", c.p.tint(c.p.red, .22)) if d.kind == "-" else c.s("text", c.p.tint(c.p.green, .22)) if d.kind == "+" else c.s("muted")
            return str((d.old if side == "l" else d.new) or ""), Text(d.text, st)
        g.add_row(*cell(a, "l"), *cell(b, "r"))
    return g


@df.variant("Unified", "Single column with old/new line gutters and +/- marks.")
def _df_b(c: Ctx):
    t: ToolCall = c.item
    g = Table.grid(expand=True)
    for w in (4, 4, 2):
        g.add_column(width=w, justify="right", no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    for d in t.diff:
        if d.kind == "@":
            g.add_row("", "", "", Text(d.text, c.s("blue", c.p.tint(c.p.blue, .08))))
            continue
        bg = {"+": c.p.tint(c.p.green, .16), "-": c.p.tint(c.p.red, .16)}.get(d.kind)
        mark = {"+": ("+", "green"), "-": ("-", "red")}.get(d.kind, (" ", "quiet"))
        g.add_row(Text(str(d.old or ""), c.s("quiet", bg)), Text(str(d.new or ""), c.s("quiet", bg)), Text(mark[0] + " ", c.s(mark[1], bg)),
                  Text(d.text, c.s("text" if d.kind != " " else "muted", bg)))
    return Group(c.t(("± ", "yellow"), (t.target, c.s("text", bold=True)), (f"  +{t.added} −{t.removed}", "quiet")), g)


@df.variant("Stat only", "Just the file and a change bar; enter opens the diff.")
def _df_c(c: Ctx):
    t: ToolCall = c.item
    total = t.added + t.removed
    return c.t(("M ", "yellow"), (t.target, "text"), ("  ", ""), (f"+{t.added}", "green"), (f" −{t.removed} ", "red"),
               ("■" * round(t.added / total * 10), "green"), ("■" * round(t.removed / total * 10), "red"), ("   2 hunks · enter to view", "quiet"))


@df.variant("Changes only", "Only changed lines with new line numbers; context hidden and announced.")
def _df_d(c: Ctx):
    t: ToolCall = c.item
    rows = [c.t(("Edited ", "muted"), (t.target, c.s("text", bold=True)))]
    hidden = 0
    for d in t.diff:
        if d.kind == " ":
            hidden += 1
            continue
        if d.kind == "@":
            if hidden:
                rows.append(c.t((f"      ⋯ {hidden} unchanged", "quiet")))
                hidden = 0
            continue
        num = d.new if d.kind == "+" else d.old
        rows.append(c.t((f"{num:>4} ", "quiet"), ("+ " if d.kind == "+" else "- ", "green" if d.kind == "+" else "red"),
                        (d.text, c.s("green" if d.kind == "+" else "red"))))
    if hidden:
        rows.append(c.t((f"      ⋯ {hidden} unchanged", "quiet")))
    return Group(*rows)


# --------------------------------------------------------- tool details -----

td = element("tool-details", "Tool details", "Everything about one call, as the details view or the inspector shows it.",
             lambda c: [("failed Bash", c.f.call("c6"), ""), ("Grep", c.f.call("c2"), "")])


def _meta(c: Ctx, t: ToolCall):
    rows = [("status", c.t((STATUS_GLYPH[t.status] + " " + t.summary, STATUS_ROLE[t.status]))), ("duration", t.duration),
            ("context", f"+{tok(t.tokens)} tokens"), ("agent", "Build"), ("permission", "asked · allowed once" if t.tool == "Bash" else "auto (read-only)"),
            ("call id", f"toolu_01{t.id.upper()}x8F2")]
    if t.exit_code is not None:
        rows.insert(1, ("exit code", Text(str(t.exit_code), c.s("red" if t.exit_code else "green"))))
    return rows


@td.variant("Labelled sections", "Today: title, PARAMETERS, OUTPUT with line numbers, META; clipping announced.")
def _td_a(c: Ctx):
    t: ToolCall = c.item
    out = Table.grid(padding=(0, 1))
    out.add_column(justify="right", style=c.s("quiet"))
    out.add_column(no_wrap=True, overflow="ellipsis")
    shown = t.output[:16]
    for i, x in enumerate(shown, 1):
        out.add_row(str(i), Text(x, c.s("red" if x.startswith(("E ", "FAILED", ">")) else "text")))
    return Group(c.t((t.tool, c.s(_group(c, t.tool), bold=True)), (" " + t.target, c.s("text", bold=True))), Text(),
                 c.rule("PARAMETERS", 60), c.kv([(k, v) for k, v in t.params], 9), Text(),
                 c.rule("OUTPUT", 60, right=f"{len(t.output)} lines"), out,
                 c.clipped(len(t.output) - len(shown)) if len(t.output) > len(shown) else Text(), Text(),
                 c.rule("META", 60), c.kv(_meta(c, t), 11))


@td.variant("Two columns", "Parameters and meta on the left, output on the right.")
def _td_b(c: Ctx):
    t: ToolCall = c.item
    left = Group(c.rule("PARAMS", 30), c.kv([(k, v) for k, v in t.params], 8), Text(), c.rule("META", 30), c.kv(_meta(c, t), 10))
    right = Group(*(Text(x, c.s("red" if x.startswith(("E ", "FAILED", ">")) else "muted"), no_wrap=True, overflow="ellipsis") for x in t.output[-14:]),
                  c.clipped(max(0, len(t.output) - 14), how="showing the tail"))
    g = Table.grid(expand=True, padding=(0, 2))
    g.add_column(width=34)
    g.add_column(ratio=1)
    g.add_row(left, Panel(right, box=box.ROUNDED, border_style=c.s("border"), title=Text(" output ", c.s("quiet")), title_align="left"))
    return Group(c.t((STATUS_GLYPH[t.status] + " ", STATUS_ROLE[t.status]), (f"{t.tool} {t.target}", c.s("text", bold=True))), g)


@td.variant("Terminal replay", "The call as a terminal session: prompt, raw output, exit code footer.")
def _td_c(c: Ctx):
    t: ToolCall = c.item
    cmd = dict(t.params).get("command") or f"{t.tool.lower()} " + " ".join(f"--{k}={v}" for k, v in t.params)
    body = [c.t(("~/repos/auth-service ", "blue"), ("$ ", "green"), (cmd, c.s("text", bold=True)))]
    body += [Text(x, c.s("text"), no_wrap=True, overflow="ellipsis") for x in t.output[:14]]
    if len(t.output) > 14:
        body.append(c.clipped(len(t.output) - 14))
    foot = c.t((f"exit {t.exit_code}" if t.exit_code is not None else "done", "red" if t.exit_code else "green"), (f" · {t.duration} · +{tok(t.tokens)} tok to context", "quiet"))
    return Panel(Group(*body), box=box.HEAVY, border_style=c.s("border_strong"), style=c.s(None, "panel"), subtitle=foot, subtitle_align="right", padding=(0, 1))
