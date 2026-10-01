"""Chrome: top bar, sessions sidebar, details sidebar, logs drawer."""

from __future__ import annotations

from itertools import groupby

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ..kit import STATUS_ROLE, Ctx
from . import element


def _row(*cols, ratios=(1,), justify=None) -> Table:
    t = Table.grid(expand=True)
    for i, _ in enumerate(cols):
        ratio = ratios[i] if i < len(ratios) else None
        t.add_column(ratio=ratio, no_wrap=True, justify=(justify or {}).get(i, "left"))
    t.add_row(*cols)
    return t


STATUS_WORD = {"idle": "idle", "working": "working · 12s", "approval": "needs approval"}

# ------------------------------------------------------------- top bar ------

top = element("topbar", "Top bar", "The one-row bar above everything.",
              lambda c: [("idle", None, "idle"), ("working", None, "working"), ("needs approval", None, "approval")])


@top.variant("Today", "▌ toggles sessions · title · Context Logs Export · status · + · ▐ toggles details.")
def _t_a(c: Ctx):
    ph = c.phase or "idle"
    status = {"idle": c.t((" idle ", c.s("muted", "element"))), "working": c.t((" ● working ", c.s("accent", "element"))),
              "approval": c.t((" Needs approval ", c.s("bg", "yellow", bold=True)))}[ph]
    return _row(c.t((" ▌ ", "accent"), (c.f.session.title, c.s("text", bold=True))),
                c.t(("Context  Logs  Export  ", "muted")) + status + c.t(("  +  ", "accent"), ("▐ ", "accent")),
                ratios=(1, None), justify={1: "right"})


@top.variant("Breadcrumb", "Where you are: app › workspace › branch › session; status as a dot.")
def _t_b(c: Ctx):
    ph = c.phase or "idle"
    dot = {"idle": ("○ idle", "quiet"), "working": ("● working", "accent"), "approval": ("◆ approval", "yellow")}[ph]
    return _row(c.t((" nexus", c.s("accent", bold=True)), (" › ", "quiet"), ("auth-service", "muted"), (" › ", "quiet"),
                    (" " + c.f.branch, "purple"), (" › ", "quiet"), (c.f.session.title, c.s("text", bold=True))),
                c.t(dot, ("  ⌘K ", "quiet")), ratios=(1, None), justify={1: "right"})


@top.variant("Status-rich", "Everything about the run in the top line: agent, model, context, cost, time.")
def _t_c(c: Ctx):
    ph = c.phase or "idle"
    state = {"idle": ("■ IDLE", "quiet"), "working": ("▶ 12s", "accent"), "approval": ("◆ WAITING ON YOU", "yellow")}[ph]
    return _row(c.t((f" {c.f.session.title}", c.s("text", bold=True))),
                c.t(("BUILD ", c.s("accent", bold=True)), (c.f.model, "muted"), (" · ", "quiet"), ("high", "muted"), (" · ", "quiet"),
                    ("ctx 61%", "text"), (" · ", "quiet"), ("$0.31", "text"), ("   ", ""), state, (" ", "")),
                ratios=(1, None), justify={1: "right"})


@top.variant("Minimal", "A quiet title and a single status glyph; everything else lives in the palette.")
def _t_d(c: Ctx):
    ph = c.phase or "idle"
    g = {"idle": ("", "quiet"), "working": ("◌", "accent"), "approval": ("◆", "yellow")}[ph]
    return _row(c.t(("  " + c.f.session.title, "muted")), c.t(g, ("  ", "")), ratios=(1, None), justify={1: "right"})


@top.variant("Signal tags", "Capitalised labels on tinted tags; one solid banner when it needs you.")
def _t_e(c: Ctx):
    ph = c.phase or "idle"
    st = {"idle": c.tag("IDLE", "quiet"), "working": c.tag("● WORKING 12s", "accent"),
          "approval": Text(" NEEDS APPROVAL → ", c.s("bg", "yellow", bold=True))}[ph]
    return _row(c.t(("▌ ", "accent"), (c.f.session.title.upper(), c.s("text", bold=True))),
                c.tag("BUILD", "accent") + Text(" ") + c.tag("OPUS 5.5", "blue") + Text(" ") + st + Text(" "),
                ratios=(1, None), justify={1: "right"})


# ------------------------------------------------------------ sessions ------

ses = element("sessions", "Sessions sidebar", "The list of sessions on the left.")


def _status_mark(c: Ctx, s) -> Text:
    return {"running": Text("●", c.s("accent")), "approval": Text("◆", c.s("yellow")), "error": Text("✗", c.s("red"))}.get(s.status, Text("·", c.s("quiet")))


@ses.variant("Today", "New session button, filter, day groups, status dot, time; footer with connection.")
def _s_a(c: Ctx):
    rows = [Text(" + New session ", c.s("on_accent", "accent", bold=True)), Text(), Text(" Filter sessions…", c.s("quiet", "element")), Text()]
    for group, items in groupby(c.f.sessions, key=lambda s: s.group):
        items = list(items)
        rows.append(c.t((group.upper(), c.s("quiet", bold=True)), (f"  {len(items)}", "quiet")))
        for i, s in enumerate(items):
            sel = i == 0 and group == "Today"
            st = c.s("text", "element_hi") if sel else c.s("muted")
            rows.append(_row(_status_mark(c, s) + Text(" " + s.title, st), Text(s.when + " ", c.s("quiet", "element_hi" if sel else None)),
                             ratios=(1, None), justify={1: "right"}))
        rows.append(Text())
    rows += [Text("↵ open · ctrl+a archive · / filter", c.s("quiet")), c.t(("■ ", "green"), ("Live sync", "muted"), ("   ⟳  ⌘  ⚙", "quiet"))]
    return Group(*rows)


@ses.variant("Glyph column", "No groups; a status column and relative time only. Dense.")
def _s_b(c: Ctx):
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=1)
    t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    t.add_column(no_wrap=True, justify="right")
    for i, s in enumerate(x for x in c.f.sessions if not x.archived):
        t.add_row(_status_mark(c, s), Text(s.title, c.s("text" if i == 0 else "muted", bold=i == 0)), Text(s.when, c.s("quiet")))
    return Group(c.t(("SESSIONS", c.s("quiet", bold=True)), ("  ctrl+n new", "quiet")), t, c.t(("+2 archived", "quiet")))


@ses.variant("Table", "Title, agent, turns and cost, so expensive sessions stand out.")
def _s_c(c: Ctx):
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=c.s("quiet", bold=True), pad_edge=False, border_style=c.s("border"))
    t.add_column("SESSION", ratio=1, no_wrap=True, overflow="ellipsis")
    t.add_column("AGT", no_wrap=True)
    t.add_column("$", justify="right", no_wrap=True)
    for s in c.f.sessions[:10]:
        t.add_row(_status_mark(c, s) + Text(" " + s.title, c.s("text")), Text(s.agent[:3].upper(), c.s("accent" if s.agent == "Build" else "purple")),
                  Text(s.cost, c.s("red" if float(s.cost[1:]) > 1 else "muted")))
    return t


@ses.variant("Two-line cards", "Each session as two lines: title, then agent · turns · status in words.")
def _s_d(c: Ctx):
    rows = []
    for i, s in enumerate(c.f.sessions[:7]):
        words = {"running": "working now", "approval": "waiting for approval", "error": "last turn failed"}.get(s.status, f"{s.turns} turns")
        bar = Text("▌", c.s("accent" if i == 0 else "bg"))
        rows.append(bar + Text(" " + s.title, c.s("text", bold=i == 0)))
        rows.append(bar + c.t((f" {s.agent} · ", "quiet"), (words, STATUS_ROLE.get(s.status, "quiet") if s.status != "running" else "accent"), (f" · {s.when}", "quiet")))
        rows.append(Text())
    return Group(*rows)


# ------------------------------------------------------------- details ------

det = element("details", "Details sidebar", "The right-hand sidebar: session facts, files, agents, jobs, MCP.")


@det.variant("Tabs + sections", "Today: tabs, then SESSION, MODIFIED FILES and MCP SERVERS.")
def _d_a(c: Ctx):
    tabs = c.t((" Session ", c.s("text", "element_hi", bold=True)), ("  Tools  Agents  Trees  Logs", "quiet"))
    rows = [tabs, Text(), c.rule("SESSION", 38)]
    rows.append(c.kv([("Status", c.t(("working", "accent"))), ("Agent", "Build"), ("Model", c.f.model), ("Effort", "high"),
                      ("Turns", "4"), ("Tokens", "122k in · 3.4k out"), ("Cost", "$0.31")], 8))
    rows += [Text(), c.rule("MODIFIED FILES", 38)]
    for k, path, a, d in c.f.modified:
        rows.append(_row(c.t((k + " ", "yellow" if k == "M" else "green"), (path, "text")),
                         c.t((f"+{a}", "green"), (f" -{d}", "red")), ratios=(1, None)))
    rows += [Text(), c.rule("MCP SERVERS", 38)]
    for m in c.f.mcp:
        rows.append(c.t(("● " if m.status == "ready" else "✗ ", "green" if m.status == "ready" else "red"), (m.name, "text"),
                        (f"  {m.tools} tools" if m.status == "ready" else "  failed", "quiet")))
    return Group(*rows)


@det.variant("Stacked panes", "No tabs: files, agents, jobs and worktrees stacked so all are visible.")
def _d_b(c: Ctx):
    def pane(title: str, n: int, body) -> Panel:
        return Panel(body, title=Text(f" {title} ", c.s("text", bold=True)), title_align="left",
                     subtitle=Text(f" {n} ", c.s("quiet")), subtitle_align="right", box=box.ROUNDED, border_style=c.s("border"), padding=(0, 1))
    files = Group(*(c.t((k + " ", "yellow" if k == "M" else "green"), (p.split("/")[-1], "text"), (f"  +{a} -{d}", "quiet")) for k, p, a, d in c.f.modified))
    agents = Group(*(c.t((("✓ " if a.status == "done" else "◌ " if a.status == "running" else "✗ "), STATUS_ROLE[a.status]),
                         (a.agent, "text"), (f"  {a.duration}", "quiet")) for a in c.f.subagents()))
    jobs = Group(*(c.t((j, "accent"), (" " + cmd[:22], "muted"), ("\n   " + st, "quiet")) for j, cmd, st in c.f.jobs))
    trees = Group(*(c.t((n, "purple"), (f"  {k}", "quiet")) for n, _, k in c.f.worktrees))
    return Group(pane("Files", 3, files), pane("Agents", 3, agents), pane("Jobs", 2, jobs), pane("Worktrees", 2, trees))


@det.variant("Swatch legend", "Signal style: swatch, tinted tag, outlined bar, one color per row.")
def _d_c(c: Ctx):
    rows = [c.rule("RUN", 38, "text")]
    for label, val, frac, role in (("TOKENS", "122k", .61, "accent"), ("CACHE", "81%", .81, "green"), ("COST", "$0.31", .31, "yellow"),
                                   ("TIME", "2m 14s", .45, "blue")):
        rows.append(c.t(("■ ", role)) + c.tag(f"{label:<6}", role) + Text(" ") + c.bar(frac, 14, role, "border", "▮", "▯") + c.t((f" {val}", "text")))
    rows += [Text(), c.rule("CHANGES", 38, "text")]
    for k, p, a, d in c.f.modified:
        rows.append(c.tag(k, "yellow" if k == "M" else "green") + c.t((" " + p, "text"), (f"  +{a}/-{d}", "quiet")))
    rows += [Text(), c.rule("AGENTS", 38, "text")]
    for a in c.f.subagents():
        row = c.tag(a.status.upper(), STATUS_ROLE[a.status]) + c.t((f" {a.agent} ", "text"), (a.task, "quiet"))
        row.no_wrap, row.overflow = True, "ellipsis"
        rows.append(row)
    return Group(*rows)


@det.variant("One-liners", "Counts only; each line opens its full list.")
def _d_d(c: Ctx):
    lines = [("◆", "accent", "Build · opus-5.5 · high", ""), ("±", "yellow", "3 files changed", "+77 -5"), ("⑂", "green", "3 subagents", "1 running"),
             ("▶", "accent", "2 background jobs", "1 running"), ("⌥", "purple", "2 worktrees", ""), ("⚡", "green", "2/3 MCP servers", "sentry failed"),
             ("$", "text", "$0.31 · 122k tok", "81% cached")]
    return Group(*(_row(c.t((g + " ", r), (txt, "text")), Text(rt, c.s("red" if "failed" in rt else "quiet")), ratios=(1, None)) for g, r, txt, rt in lines))


# --------------------------------------------------------------- logs -------

logs = element("logs", "Logs drawer", "Daemon and turn logs, shown in a drawer under the timeline.")
LEVEL = {"DEBUG": "quiet", "INFO": "blue", "WARN": "yellow", "ERROR": "red"}


@logs.variant("Plain lines", "Today: time, level, source, message.")
def _l_a(c: Ctx):
    return Group(*(c.t((f"{ts} ", "quiet"), (f"{lv:<5} ", LEVEL[lv]), (f"{src:<8}", "muted"), (msg, "text")) for ts, lv, src, msg in c.f.logs))


@logs.variant("Table + filters", "Columns and a filter row; errors keep their color.")
def _l_b(c: Ctx):
    head = c.t(("Level ", "quiet")) + c.tag("ALL", "text", .12) + Text(" ") + c.tag("WARN+", "yellow") + c.t(("   Source ", "quiet")) + c.tag("any", "text", .12)
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=c.s("quiet", bold=True), pad_edge=False)
    for col in ("TIME", "LVL", "SRC"):
        t.add_column(col, no_wrap=True)
    t.add_column("MESSAGE", ratio=1)
    for ts, lv, src, msg in c.f.logs:
        t.add_row(Text(ts, c.s("quiet")), Text(lv, c.s(LEVEL[lv], bold=lv == "ERROR")), Text(src, c.s("muted")), Text(msg, c.s("text")))
    return Group(head, t)


@logs.variant("Problems first", "Warnings and errors pinned on top as cards; the rest folded.")
def _l_c(c: Ctx):
    bad = [x for x in c.f.logs if x[1] in ("WARN", "ERROR")]
    out = [c.t(("PROBLEMS ", c.s("text", bold=True)), (str(len(bad)), "red"))]
    for ts, lv, src, msg in bad:
        out.append(c.t(("▌", LEVEL[lv]), (f" {src}: ", c.s("text", bold=True)), (msg, "text"), (f"  {ts}", "quiet")))
    out.append(c.t((f"▸ {len(c.f.logs) - len(bad)} info/debug lines", "quiet")))
    return Group(*out)


# ------------------------------------------------------------ activity ------

act = element("activity", "Activity indicator", "What shows that a turn is running (and retrying).",
              lambda c: [("working", None, "working"), ("retrying", None, "retry"), ("idle", None, "idle")])


@act.variant("Moving bar", "Today: a one-row meter with a moving segment and a timer while running.")
def _a_a(c: Ctx):
    if c.phase == "idle":
        return ProgressBar(total=1, completed=.61, style=c.s("border"), complete_style=c.s("border_strong"))
    color = "yellow" if c.phase == "retry" else "accent"
    g = Table.grid(expand=True)
    g.add_column(ratio=1)
    g.add_column(no_wrap=True)
    g.add_row(ProgressBar(pulse=True, animation_time=0.6, style=c.s("border"), pulse_style=c.s(color)), c.t(("  12s", "muted")))
    return g


@act.variant("Spinner line", "Spinner, verb, elapsed, live token rate, and the interrupt key.")
def _a_b(c: Ctx):
    if c.phase == "idle":
        return c.t(("✓ done in 41.7s", "quiet"))
    if c.phase == "retry":
        return c.t(("⠼ ", "yellow"), ("Rate limited", "yellow"), (" · retrying in 8s (2/5) · ", "quiet"), ("esc", c.s("text", bold=True)), (" stop", "quiet"))
    return c.t(("⠼ ", "accent"), ("Running tests", "text"), (" · 12s · ↓ 1.2k tok · 48 tok/s · ", "quiet"), ("esc", c.s("text", bold=True)), (" interrupt", "quiet"))


@act.variant("Step list", "The current step and the next ones, like a todo list.")
def _a_c(c: Ctx):
    if c.phase == "idle":
        return c.t(("✓ ", "green"), ("4 of 4 steps done", "quiet"))
    rows = [c.t(("✓ ", "green"), ("Edit jobs.py", "quiet")),
            c.t(("◉ ", "yellow" if c.phase == "retry" else "accent"), ("Run job tests" if c.phase == "working" else "Waiting for provider · 8s", c.s("text", bold=True))),
            c.t(("○ ", "quiet"), ("Write PR summary", "muted"))]
    return Group(*rows)


@act.variant("Braille pulse", "A narrow pulse next to the composer hint; minimal motion.")
def _a_d(c: Ctx):
    if c.phase == "idle":
        return c.t(("⣿ ", "border"), ("ready", "quiet"))
    color = "yellow" if c.phase == "retry" else "accent"
    return c.t(("⣀⣤⣶⣿⣶⣤⣀ ", color), ("12s" if c.phase == "working" else "retry 8s", "muted"))
