"""Context: the header that opens every conversation, the system prompt, the
tools list, skills/MCP and the context-usage meter.

"The context is clearly presented" is Nexus's core idea, so these get the most
variants.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ..kit import GROUP_ROLE, Ctx, tok
from . import element

# ---------------------------------------------------------------- helpers ----


def sections(prompt: str) -> list[tuple[str, str, list[str]]]:
    """Split the system prompt into (kind, name, lines): intro, # headings and <xml> blocks."""
    out: list[tuple[str, str, list[str]]] = [("text", "Intro", [])]
    tag: str | None = None
    for line in prompt.splitlines():
        m = re.match(r"<(\w+)[^>]*>$", line.strip())
        if tag is None and m:
            tag = m.group(1)
            out.append(("xml", f"<{tag}>", [line]))
            continue
        if tag is not None:
            out[-1][2].append(line)
            if line.strip() == f"</{tag}>":
                tag = None
            continue
        if line.startswith("# "):
            out.append(("heading", line[2:], []))
            continue
        out[-1][2].append(line)
    return [(k, n, [ln for ln in lines]) for k, n, lines in out if any(x.strip() for x in lines)]


def sec_tokens(lines: list[str]) -> int:
    return max(8, sum(len(x) for x in lines) // 4)


def tool_groups(c: Ctx) -> "OrderedDict[str, list]":
    groups: OrderedDict[str, list] = OrderedDict()
    for t in c.f.tools:
        groups.setdefault(t.group, []).append(t)
    return groups


def columns(labels: list[str], cols: int = 3) -> str:
    width = max(len(x) for x in labels) + 2
    return "\n".join("".join(x.ljust(width) for x in labels[i:i + cols]).rstrip() for i in range(0, len(labels), cols))


def _mcp_tools(c: Ctx) -> int:
    return sum(m.tools for m in c.f.mcp)


def _tools_tokens(c: Ctx) -> int:
    return sum(t.tokens for t in c.f.tools)


def highlight_prompt_line(c: Ctx, line: str) -> Text:
    if line.startswith("# "):
        return Text(line, c.s("accent", bold=True))
    if re.match(r"\s*</?\w+[^>]*>\s*$", line):
        return Text(line, c.s("purple"))
    if line.startswith("- "):
        return c.t(("- ", "quiet"), (line[2:], "text"))
    if ":" in line and re.match(r"^[a-z_-]+:", line):
        k, v = line.split(":", 1)
        return c.t((k + ":", "cyan"), (v, "text"))
    out = Text()
    for i, part in enumerate(line.split("`")):
        out.append(part, c.s("yellow" if i % 2 else "text"))
    return out


# ------------------------------------------------------------- header -------

hdr = element("context-header", "Context header", "The block that opens every conversation: system prompt, AGENTS.md, tools, skills, MCP.")


@hdr.variant("Chips", "Today: solid colored label chips with an indented, muted body under each.")
def _h_a(c: Ctx):
    rows = []
    groups = tool_groups(c)
    tools = columns([f"{g}({len(v)})" if len(v) > 1 else v[0].name for g, v in groups.items()])
    for label, color, body in (
        ("System prompt", "accent", f"Build · {tok(c.f.system_tokens)} tokens · {len(c.f.system_prompt.splitlines())} lines"),
        ("Task", "accent", "AGENTS.md · " + tok(c.f.agents_md_tokens) + " tokens"),
        ("Tools", "accent", tools),
        ("Skills", "label", "2 project · 1 global · 1 off"),
        ("MCP", "accent", "2 ready · 1 failed\n" + columns([f"{m.name}({m.tools})" for m in c.f.mcp])),
    ):
        chip = Text(f" {label} ", c.s("bg", "quiet" if color == "label" else color, bold=True))
        rows += [chip, Text("\n".join("  " + x for x in body.splitlines()), c.s("muted")), Text()]
    return Group(*rows[:-1])


@hdr.variant("Ledger table", "One table: block, what's in it, tokens, share of the window.")
def _h_b(c: Ctx):
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=c.s("quiet", bold=True), border_style=c.s("border"), pad_edge=False)
    t.add_column("CONTEXT")
    t.add_column("CONTAINS", ratio=1)
    t.add_column("TOKENS", justify="right")
    t.add_column("SHARE", width=12)
    used = c.f.used
    for name, contains, n, role in (
        ("System prompt", "Build agent · 5 sections · 44 lines", c.f.system_tokens, "blue"),
        ("AGENTS.md", "project instructions · 3 lines", c.f.agents_md_tokens, "cyan"),
        ("Tools", f"{len(c.f.tools)} built-in · " + ", ".join(tool_groups(c)), _tools_tokens(c), "purple"),
        ("Skills", "3 enabled · 1 off", 380, "yellow"),
        ("MCP", f"{_mcp_tools(c)} tools from 2 servers · sentry failed", 9800, "green"),
    ):
        t.add_row(c.t((" ■ ", role), (name, "text")), Text(contains, c.s("muted")), Text(tok(n), c.s("text")),
                  c.bar(n / used * 4, 10, role, "border", "▮", "▯"))
    t.add_row(Text("   Total", c.s("quiet")), Text("before the first message", c.s("quiet")),
              Text(tok(c.f.system_tokens + c.f.agents_md_tokens + _tools_tokens(c) + 380 + 9800), c.s("text", bold=True)), Text(""))
    return t


@hdr.variant("One line", "Collapsed to a single summary line; enter opens the full view.")
def _h_c(c: Ctx):
    total = c.f.system_tokens + c.f.agents_md_tokens + _tools_tokens(c) + 380 + 9800
    return c.t(("▸ ", "quiet"), ("context ", c.s("muted", bold=True)), (tok(total), "text"), ("  ·  ", "quiet"),
               ("system ", "quiet"), (tok(c.f.system_tokens), "blue"), ("  agents.md ", "quiet"), (tok(c.f.agents_md_tokens), "cyan"),
               ("  tools ", "quiet"), (str(len(c.f.tools)), "purple"), ("  skills ", "quiet"), ("3", "yellow"),
               ("  mcp ", "quiet"), ("2", "green"), ("/", "quiet"), ("3", "red"), ("   enter", c.s("text", bold=True)), (" open", "quiet"))


@hdr.variant("Cards", "A row of small cards, one per block, each with a count and its top items.")
def _h_d(c: Ctx):
    def card(title: str, role: str, big: str, lines: list[str]) -> Panel:
        return Panel(Group(Text(big, c.s(role, bold=True)), *(Text(x, c.s("muted"), overflow="ellipsis", no_wrap=True) for x in lines)),
                     title=Text(f" {title} ", c.s(role, bold=True)), title_align="left", box=box.ROUNDED,
                     border_style=c.s("border"), padding=(0, 1), width=24, height=6)
    return Columns([
        card("System", "blue", tok(c.f.system_tokens) + " tok", ["Build agent", "44 lines · 5 sections", "+ AGENTS.md 410"]),
        card("Tools", "purple", f"{len(c.f.tools)} tools", ["Read Edit Write Glob", "Grep Bash Task …", tok(_tools_tokens(c)) + " tok"]),
        card("Skills", "yellow", "3 on · 1 off", [s.name for s in c.f.skills[:3]]),
        card("MCP", "green", f"{_mcp_tools(c)} tools", [f"{'●' if m.status == 'ready' else '✗'} {m.name} {m.tools}" for m in c.f.mcp]),
    ], padding=(0, 1))


@hdr.variant("Tree", "A tree you can walk with the arrow keys; every leaf opens its full text.")
def _h_e(c: Ctx):
    lines = [c.t(("▾ ", "quiet"), ("Context", c.s("text", bold=True)), (f"  {tok(c.f.used - 96200 - 4300)} before first message", "quiet"))]
    secs = sections(c.f.system_prompt)
    lines.append(c.t(("├─▾ ", "border"), ("System prompt", "blue"), (f"  {tok(c.f.system_tokens)}", "quiet")))
    for i, (_k, name, ls) in enumerate(secs):
        lines.append(c.t(("│  " + ("└─ " if i == len(secs) - 1 else "├─ "), "border"), (name, "muted"), (f"  {tok(sec_tokens(ls))}", "quiet")))
    lines.append(c.t(("├─▸ ", "border"), ("Tools", "purple"), (f"  {len(c.f.tools)} · {tok(_tools_tokens(c))}", "quiet")))
    lines.append(c.t(("├─▸ ", "border"), ("Skills", "yellow"), ("  3 on · 1 off", "quiet")))
    lines.append(c.t(("└─▾ ", "border"), ("MCP", "green"), (f"  {_mcp_tools(c)} tools", "quiet")))
    for i, m in enumerate(c.f.mcp):
        st = {"ready": ("●", "green"), "failed": ("✗", "red")}.get(m.status, ("◌", "yellow"))
        lines.append(c.t(("   " + ("└─ " if i == len(c.f.mcp) - 1 else "├─ "), "border"), (st[0] + " ", st[1]), (m.name, "muted"),
                         (f"  {m.tools} tools · {m.transport}" if m.status == "ready" else "  401 Unauthorized", "quiet" if m.status == "ready" else "red")))
    return Group(*lines)


@hdr.variant("Stacked bar", "The window as one stacked bar with a swatch legend (Signal style).")
def _h_f(c: Ctx):
    blocks = [("SYSTEM", c.f.system_tokens, "blue"), ("AGENTS.MD", c.f.agents_md_tokens, "cyan"), ("TOOLS", _tools_tokens(c), "purple"),
              ("SKILLS", 380, "yellow"), ("MCP", 9800, "green")]
    total = sum(n for _, n, _ in blocks)
    legend = Table.grid(padding=(0, 2))
    for _ in range(3):
        legend.add_column(no_wrap=True)
    cells = [c.t((" ■ ", r), (f"{name} ", c.s("muted", bold=True)), (tok(n), r)) for name, n, r in blocks]
    cells.append(c.t((" ✗ ", "red"), ("SENTRY ", c.s("muted", bold=True)), ("failed", "red")))
    for i in range(0, len(cells), 3):
        legend.add_row(*cells[i:i + 3], *([Text()] * (3 - len(cells[i:i + 3]))))
    return Group(c.rule("CONTEXT", 0, "text", right=f"{tok(total)} BEFORE FIRST MESSAGE"),
                 c.fit(lambda w: c.stacked([(n, r) for _, n, r in blocks], total, w)), legend)


# ------------------------------------------------------- system prompt ------

sp = element("system-prompt", "System prompt", "How the system prompt is shown when opened (or previewed in the header).")


@sp.variant("Literal text", "Today: the literal prompt, wrapped, nothing interpreted.")
def _sp_a(c: Ctx):
    lines = c.f.system_prompt.splitlines()
    return Panel(Group(Text("\n".join(lines[:18]), c.s("text")), c.clipped(len(lines) - 18, how="scroll for more")),
                 title=Text(" System prompt · Build ", c.s("text", bold=True)), title_align="left",
                 subtitle=Text(f" {tok(c.f.system_tokens)} tokens ", c.s("quiet")), subtitle_align="right",
                 box=box.ROUNDED, border_style=c.s("border_strong"), padding=(0, 1))


@sp.variant("Sections", "Folded into its sections; each shows lines and tokens; one open at a time.")
def _sp_b(c: Ctx):
    out = []
    for i, (kind, name, ls) in enumerate(sections(c.f.system_prompt)):
        mark = "▾" if i == 1 else "▸"
        role = {"text": "text", "heading": "accent", "xml": "purple"}[kind]
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(c.t((f"{mark} ", "quiet"), (name, c.s(role, bold=True))),
                     c.t((f"{len([x for x in ls if x.strip()])} lines  ", "quiet"), (f"{tok(sec_tokens(ls)):>5}", "muted")))
        out.append(grid)
        if i == 1:
            out.append(Text("\n".join("    " + x for x in ls if x.strip()), c.s("muted")))
    return Group(*out)


@sp.variant("Preview + stats", "First lines, then a stats line; enter opens the full prompt.")
def _sp_c(c: Ctx):
    lines = [x for x in c.f.system_prompt.splitlines()]
    return Group(
        Text("\n".join(lines[:3]), c.s("muted")),
        c.t(("╌" * 40, "border")),
        c.t((f"{len(lines)} lines", "text"), (" · ", "quiet"), (f"{tok(c.f.system_tokens)} tokens", "text"), (" · ", "quiet"),
            ("5 sections", "text"), (" · ", "quiet"), ("includes AGENTS.md, <environment>, 3 skills", "muted"),
            ("   enter", c.s("text", bold=True)), (" read all", "quiet")),
    )


@sp.variant("Gutter + highlight", "Line numbers, headings and XML tags highlighted; literal text otherwise.")
def _sp_d(c: Ctx):
    lines = c.f.system_prompt.splitlines()
    shown = 22
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style=c.s("quiet"), no_wrap=True)
    t.add_column(ratio=1)
    for i, line in enumerate(lines[:shown], 1):
        t.add_row(str(i), highlight_prompt_line(c, line))
    return Group(t, c.clipped(len(lines) - shown))


@sp.variant("Token outline", "Only the outline, each section sized by its token share.")
def _sp_e(c: Ctx):
    secs = sections(c.f.system_prompt)
    total = sum(sec_tokens(ls) for _, _, ls in secs)
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True, width=24)
    t.add_column(no_wrap=True)
    t.add_column(justify="right", no_wrap=True)
    for kind, name, ls in secs:
        role = {"text": "blue", "heading": "accent", "xml": "purple"}[kind]
        t.add_row(Text(name, c.s("text")), c.block_bar(sec_tokens(ls) / total, 30, role), Text(tok(sec_tokens(ls)), c.s("muted")))
    return Group(c.t(("System prompt", c.s("text", bold=True)), (f"  Build · {tok(c.f.system_tokens)} tokens", "quiet")), t)


# ---------------------------------------------------------------- tools -----

tl = element("tools", "Tools", "The tools the model is given, as the header and the Tools dialog show them.")


@tl.variant("Grouped columns", "Today: group names with counts, in three columns.")
def _tl_a(c: Ctx):
    g = tool_groups(c)
    return Group(c.chip("Tools", "accent"), Text("\n".join("  " + x for x in columns(
        [f"{k}({len(v)})" if len(v) > 1 else v[0].name for k, v in g.items()]).splitlines()), c.s("muted")))


@tl.variant("Group table", "One row per group with every tool name and the group's token cost.")
def _tl_b(c: Ctx):
    t = Table(box=box.SIMPLE, expand=True, show_header=True, header_style=c.s("quiet", bold=True), pad_edge=False)
    t.add_column("GROUP", no_wrap=True)
    t.add_column("TOOLS", ratio=1)
    t.add_column("TOKENS", justify="right")
    for g, items in tool_groups(c).items():
        t.add_row(c.t(("■ ", GROUP_ROLE[g]), (g, "text")), Text("  ".join(x.name for x in items), c.s("muted")),
                  Text(tok(sum(x.tokens for x in items)), c.s("text")))
    t.add_row(c.t(("■ ", "green"), ("mcp", "text")), Text("github(26)  postgres(4)", c.s("muted")), Text("9.8k", c.s("text")))
    return t


@tl.variant("Tags", "Every tool as a tinted tag, colored by group; ask-first tools marked with ?.")
def _tl_c(c: Ctx):
    out = Text()
    for t in c.f.tools:
        out.append_text(c.tag(t.name + ("?" if t.policy == "ask" else ""), GROUP_ROLE[t.group]))
        out.append(" ")
    out.append_text(c.tag("github ×26", "green"))
    out.append(" ")
    out.append_text(c.tag("postgres ×4", "green"))
    return Group(out, c.t(("? asks before running", "quiet")))


@tl.variant("Signatures", "Each tool with its parameters and one-line description: what the model actually sees.")
def _tl_d(c: Ctx):
    t = Table.grid(padding=(0, 2), expand=True)
    t.add_column(no_wrap=True)
    t.add_column(ratio=1)
    t.add_column(justify="right", no_wrap=True)
    for tool in c.f.tools[:9]:
        name, _, rest = tool.signature.partition("(")
        sig = c.t((name, c.s(GROUP_ROLE[tool.group], bold=True)), ("(", "quiet"))
        for i, p in enumerate(rest.rstrip(")").split(", ")):
            sig.append(", " if i else "", c.s("quiet"))
            sig.append(p.rstrip("?"), c.s("text"))
            if p.endswith("?"):
                sig.append("?", c.s("quiet"))
        sig.append(")", c.s("quiet"))
        t.add_row(sig, Text(tool.summary, c.s("muted")), Text(tok(tool.tokens), c.s("quiet")))
    return Group(t, c.clipped(len(c.f.tools) - 9 + 30, "tools", "ctrl+t for all"))


@tl.variant("Permission matrix", "Tools by policy: what runs freely, what asks, what is denied.")
def _tl_e(c: Ctx):
    cols = {"allow": [], "ask": [], "deny": []}
    for t in c.f.tools:
        cols[t.policy].append(t.name)
    cols["deny"] += ["WebFetch(file://)"]
    t = Table(box=box.ROUNDED, expand=True, border_style=c.s("border"), header_style=c.s("text", bold=True))
    t.add_column(Text("✓ RUNS", c.s("green", bold=True)), ratio=1)
    t.add_column(Text("? ASKS", c.s("yellow", bold=True)), ratio=1)
    t.add_column(Text("✗ DENIED", c.s("red", bold=True)), ratio=1)
    n = max(len(v) for v in cols.values())
    for i in range(n):
        t.add_row(*(Text(v[i] if i < len(v) else "", c.s("text")) for v in cols.values()))
    return t


# --------------------------------------------------------- extensions -------

ext = element("skills-mcp", "Skills & MCP", "Skills and MCP servers with their scope and health.")


@ext.variant("Chip + counts", "Today: chip, scope counts, then server(tool count) columns.")
def _x_a(c: Ctx):
    return Group(c.chip("Skills", "quiet"), Text("  2 project · 1 global · 1 off", c.s("muted")), Text(),
                 c.chip("MCP", "accent"), Text("  2 ready · 1 failed\n  " + columns([f"{m.name}({m.tools})" for m in c.f.mcp]), c.s("muted")))


@ext.variant("Health list", "One line per item with a status glyph and the reason when it is not healthy.")
def _x_b(c: Ctx):
    rows = [c.rule("SKILLS", 56)]
    for s in c.f.skills:
        rows.append(c.t(("● " if s.enabled else "○ ", "yellow" if s.enabled else "quiet"), (f"{s.name:<16}", "text" if s.enabled else "quiet"),
                        (f"{s.scope:<9}", "quiet"), (s.summary if s.enabled else "off", "muted")))
    rows.append(c.rule("MCP SERVERS", 56))
    for m in c.f.mcp:
        g, r = {"ready": ("●", "green"), "failed": ("✗", "red")}[m.status]
        rows.append(c.t((g + " ", r), (f"{m.name:<16}", "text"), (f"{m.transport:<9}", "quiet"),
                        (f"{m.tools} tools" if m.status == "ready" else "401 Unauthorized · r retry", "muted" if m.status == "ready" else "red")))
    return Group(*rows)


@ext.variant("Toggle grid", "A switchboard: each skill/server is a toggle you can flip before the first turn.")
def _x_c(c: Ctx):
    def toggle(on: bool, name: str, note: str, role: str) -> Panel:
        sw = c.t(("[", "quiet"), ("■" if on else " ", role), ("]", "quiet"))
        return Panel(c.t(sw, " ", (name, "text" if on else "quiet"), "\n    ", (note, "quiet")), box=box.ROUNDED,
                     border_style=c.s(role if on else "border"), padding=(0, 1), width=26)
    items = [toggle(s.enabled, s.name, s.scope, "yellow") for s in c.f.skills]
    items += [toggle(m.status == "ready", m.name, f"{m.tools} tools" if m.status == "ready" else "failed", "green" if m.status == "ready" else "red") for m in c.f.mcp]
    return Columns(items, padding=(0, 1))


# ----------------------------------------------------------- meter ---------

meter = element("context-meter", "Context meter", "How full the context window is.",
                lambda c: [("61% used", None, ""), ("92% · compaction soon", 0.92, "")])


def _scaled(c: Ctx) -> tuple[int, list[tuple[str, int, str]]]:
    """Slices as in the fixture, or with History grown so the total hits ``c.item`` (a fraction)."""
    sl = [(s.label, s.tokens, s.role) for s in c.f.context]
    if c.item:
        others = sum(t for n, t, _ in sl if n != "History")
        sl = [(n, int(c.item * c.f.budget - others) if n == "History" else t, r) for n, t, r in sl]
    return sum(t for _, t, _ in sl), sl


@meter.variant("Thin bar", "Today: a one-row bar under the composer, no numbers.")
def _m_a(c: Ctx):
    used, _ = _scaled(c)
    return ProgressBar(total=c.f.budget, completed=used, style=c.s("border"),
                       complete_style=c.s("accent" if used / c.f.budget < .85 else "red"))


@meter.variant("Stacked + %", "Stacked by what fills it, with the percent and the absolute number.")
def _m_b(c: Ctx):
    used, sl = _scaled(c)
    g = Table.grid(expand=True)
    g.add_column(no_wrap=True)
    g.add_column(ratio=1)
    g.add_column(no_wrap=True)
    g.add_row(c.t((f"{used / c.f.budget:4.0%} ", c.s("red" if used / c.f.budget > .85 else "text", bold=True))),
              c.fit(lambda w: c.stacked([(t, r) for _, t, r in sl], c.f.budget, w), 72), c.t((f" {tok(used)}/{tok(c.f.budget)}", "quiet")))
    return g


@meter.variant("Numbers only", "Plain text for the status line.")
def _m_c(c: Ctx):
    used, _ = _scaled(c)
    pct = used / c.f.budget
    return c.t(("ctx ", "quiet"), (tok(used), "red" if pct > .85 else "text"), (f" / {tok(c.f.budget)} ", "quiet"), (f"({pct:.0%})", "red" if pct > .85 else "muted"),
               ("  · compacts at 80%" if pct > .8 else "", "yellow"))


@meter.variant("Breakdown", "Each slice on its own line: what is using the window.")
def _m_d(c: Ctx):
    used, sl = _scaled(c)
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True)
    t.add_column(justify="right", no_wrap=True)
    for name, n, r in sl:
        t.add_row(c.t(("■ ", r), (name, "muted")), c.block_bar(n / c.f.budget, 24, r), Text(tok(n), c.s("text")))
    t.add_row(Text("free", c.s("quiet")), c.block_bar((c.f.budget - used) / c.f.budget, 24, "quiet"), Text(tok(max(0, c.f.budget - used)), c.s("quiet")))
    return t


@meter.variant("Gauge + threshold", "A gauge with the auto-compaction mark drawn on it.")
def _m_e(c: Ctx):
    used, _ = _scaled(c)
    w = 50
    pct = used / c.f.budget
    mark = int(w * .8)
    filled = int(pct * w)
    out = Text()
    for i in range(w):
        if i == mark:
            out.append("┃", c.s("yellow"))
        elif i < filled:
            out.append("█", c.s("red" if i >= mark else "accent"))
        else:
            out.append("░", c.s("border"))
    return Group(out, c.t((" " * mark + "└ compaction", "yellow")),
                 c.t((f"{pct:.0%}", c.s("text", bold=True)), (f" of {tok(c.f.budget)} · {tok(c.f.budget - used)} left", "quiet")))
