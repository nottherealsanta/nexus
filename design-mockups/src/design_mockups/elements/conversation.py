"""Conversation rows: user message, thought, assistant reply, reply footer,
errors and the empty-session screen."""

from __future__ import annotations

import re

from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..fixture import ErrorItem, Footer, Thought, UserMessage
from ..kit import Ctx, tok
from . import element

# --------------------------------------------------------- mini markdown ----


def inline(c: Ctx, text: str, base: str = "text") -> Text:
    out = Text()
    for tok_ in re.split(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)", text):
        if tok_.startswith("`"):
            out.append(tok_[1:-1], c.s("accent", c.p.tint(c.p.accent, .08)))
        elif tok_.startswith("**"):
            out.append(tok_[2:-2], c.s(base, bold=True))
        elif tok_.startswith("*") and len(tok_) > 1:
            out.append(tok_[1:-1], c.s(base, italic=True))
        else:
            out.append(tok_, c.s(base))
    return out


def markdown(c: Ctx, src: str, heading: str = "plain", code_box: bool = False):
    """Palette-aware Markdown for the fixture's subset: headings, lists, code, tables."""
    out: list = []
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            lang = line[3:] or "text"
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            code = "\n".join(lines[i + 1:j])
            syn = Syntax(code, lang, theme="monokai" if c.p.dark else "friendly", background_color=c.p.element, padding=(0, 1))
            out.append(Panel(syn, box=box.ROUNDED, border_style=c.s("border"), padding=0, title=Text(f" {lang} ", c.s("quiet")), title_align="right") if code_box else syn)
            i = j + 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [x.strip() for x in lines[i].strip("|").split("|")]
                if not set("".join(cells)) <= set("- :"):
                    rows.append(cells)
                i += 1
            t = Table(box=box.SIMPLE_HEAD, header_style=c.s("muted", bold=True), border_style=c.s("border"), pad_edge=False)
            for h in rows[0]:
                t.add_column(h)
            for r in rows[1:]:
                t.add_row(*(inline(c, x) for x in r))
            out.append(t)
            continue
        if line.startswith("## "):
            title = line[3:]
            if heading == "rule":
                out.append(c.rule(title, 60, "accent"))
            elif heading == "caps":
                out.append(Text(title.upper(), c.s("accent", bold=True)))
            else:
                out.append(Text(title, c.s("text", bold=True, underline=heading == "underline")))
        elif re.match(r"^(\d+\.|-) ", line):
            m = re.match(r"^(\d+\.|-) (.*)", line)
            out.append(Text("  ") + Text(("•" if m.group(1) == "-" else m.group(1)) + " ", c.s("accent")) + inline(c, m.group(2)))
        elif not line.strip():
            if out and not (isinstance(out[-1], Text) and not out[-1].plain):
                out.append(Text())
        else:
            out.append(inline(c, line))
        i += 1
    while out and isinstance(out[-1], Text) and not out[-1].plain:
        out.pop()
    return Group(*out)


# ------------------------------------------------------------ user msg ------


def _users(c: Ctx):
    t1, t2 = c.f.turns[0].user, c.f.turns[1].user
    return [("with attachment", t1, ""), ("short", t2, ""), ("collapsed", t1, "collapsed")]


usr = element("user-message", "User message", "The prompt you sent, at the top of each turn.", _users)


def _turn_no(c: Ctx, m: UserMessage) -> int:
    return next((t.number for t in (*c.f.turns, c.f.live_turn) if t.user is m), 0)


def _attach(c: Ctx, m: UserMessage) -> Text:
    out = Text()
    for a in m.attachments:
        out.append_text(c.tag("▣ " + a, "blue"))
    return out


@usr.variant("Box + chevron", "Today: a raised box with a ▼ chevron that collapses long prompts.")
def _u_a(c: Ctx):
    m: UserMessage = c.item
    if c.phase == "collapsed":
        return Panel(c.t(("▶ ", "quiet"), (m.text.splitlines()[0][:70] + "…", "text")), box=box.ROUNDED, border_style=c.s("border"), style=c.s(None, "element"), padding=(0, 1))
    body = [c.t(("▼ ", "quiet"), (m.text, "text"))]
    if m.attachments:
        body.append(_attach(c, m))
    return Panel(Group(*body), box=box.ROUNDED, border_style=c.s("border"), style=c.s(None, "element"), padding=(0, 1))


@usr.variant("Prompt prefix", "Like a shell: › then the text, on a faint band.")
def _u_b(c: Ctx):
    m: UserMessage = c.item
    text = m.text.splitlines()[0][:70] + " …(+1 line)" if c.phase == "collapsed" else m.text
    out = Text("› ", c.s("accent", bold=True)) + Text(text, c.s("text"))
    out.stylize(c.s(None, "panel"))
    return Group(out, _attach(c, m)) if m.attachments and c.phase != "collapsed" else out


@usr.variant("Signal rule", "YOU ───── turn n · tokens, then the text. No box.")
def _u_c(c: Ctx):
    m: UserMessage = c.item
    n = _turn_no(c, m)
    head = c.rule(f"YOU · TURN {n}", 64, "accent", right=f"{tok(m.tokens)} TOK")
    if c.phase == "collapsed":
        return Group(head, c.t((m.text.splitlines()[0][:60] + "…", "muted")))
    return Group(head, Text(m.text, c.s("text")), _attach(c, m)) if m.attachments else Group(head, Text(m.text, c.s("text")))


@usr.variant("Quote bar", "A colored bar on the left; text full width; attachments as chips under it.")
def _u_d(c: Ctx):
    m: UserMessage = c.item
    text = m.text.splitlines()[0] + " …" if c.phase == "collapsed" else m.text
    rows = [Text(text, c.s("text", bold=True))]
    if m.attachments and c.phase != "collapsed":
        rows.append(_attach(c, m))
    return c.barred(Group(*rows))


@usr.variant("Right meta", "Text left, turn number and token cost right-aligned on the first line.")
def _u_e(c: Ctx):
    m: UserMessage = c.item
    n = _turn_no(c, m)
    t = Table.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(no_wrap=True, justify="right")
    text = m.text.splitlines()[0] + " …" if c.phase == "collapsed" else m.text
    t.add_row(Text(text, c.s("text", bold=True)), c.t((f"#{n}", "accent"), (f" · {tok(m.tokens)} tok", "quiet")))
    if m.attachments and c.phase != "collapsed":
        t.add_row(_attach(c, m), Text())
    return t


# -------------------------------------------------------------- thought -----

th = element("thought", "Thought", "Model reasoning between actions.",
             lambda c: [("short", c.f.turns[0].items[0], ""), ("longer", c.f.turns[1].items[4], "")])


@th.variant("Muted line", "Today: one dim italic line, full text.")
def _th_a(c: Ctx):
    t: Thought = c.item
    return Text(t.text, c.s("quiet", italic=True))


@th.variant("Folded", "∴ Thought for 4s ▸ — the text appears on enter.")
def _th_b(c: Ctx):
    t: Thought = c.item
    return c.t(("∴ ", "purple"), (f"Thought for {t.seconds}s", "muted"), (" ▸", "quiet"))


@th.variant("Quote block", "Indented under a purple bar, labelled with its duration.")
def _th_c(c: Ctx):
    t: Thought = c.item
    return c.barred(Group(c.t((f"thinking · {t.seconds}s", c.s("purple", bold=True))), Text(t.text, c.s("muted", italic=True))), "purple", "┃ ")


@th.variant("Headline", "First sentence only, then the duration; enter reveals the rest.")
def _th_d(c: Ctx):
    t: Thought = c.item
    first = t.text.split(". ")[0] + "."
    return c.t(("◇ ", "purple"), (first, c.s("muted", italic=True)), (f"  {t.seconds}s ▸", "quiet"))


# ------------------------------------------------------------ assistant -----

asst = element("assistant", "Assistant reply", "The model's Markdown answer.",
               lambda c: [("headings, list, code", c.f.turns[0].items[4], ""), ("table", c.f.turns[1].items[-1], "")])


@asst.variant("Plain Markdown", "Today: Markdown straight on the canvas.")
def _as_a(c: Ctx):
    return markdown(c, c.item)


@asst.variant("Agent gutter", "◆ Build label above; reply indented two cells.")
def _as_b(c: Ctx):
    return Group(c.t(("◆ ", "accent"), ("Build", c.s("accent", bold=True))), Padding(markdown(c, c.item, "underline"), (0, 0, 0, 2)))


LEFT_BAR = box.Box("    \n▏   \n▏   \n▏   \n▏   \n▏   \n▏   \n    \n")


@asst.variant("Left rule", "A thin rule on the left ties a long reply together.")
def _as_c(c: Ctx):
    return Panel(markdown(c, c.item, "caps", code_box=True), box=LEFT_BAR, border_style=c.s("border_strong"), padding=(0, 1, 0, 1))


@asst.variant("Reading column", "Narrow measure, headings with rules, boxed code: for long reviews.")
def _as_d(c: Ctx):
    return Padding(markdown(c, c.item, "rule", code_box=True), (0, 4))


# --------------------------------------------------------------- footer -----

ft = element("reply-footer", "Reply footer", "The line under a finished reply.", lambda c: [("", c.f.turns[1].footer, "")])


@ft.variant("Today", "AGENT · model · duration.")
def _f_a(c: Ctx):
    f: Footer = c.item
    return c.t((f.agent.upper(), c.s("accent", bold=True)), (f" · {f.model} · {f.duration}", "quiet"))


@ft.variant("Right-aligned stats", "Quiet, right-aligned: time, tokens, cost, cache.")
def _f_b(c: Ctx):
    f: Footer = c.item
    t = Table.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right", no_wrap=True)
    t.add_row(Text(""), c.t((f"{f.duration} · ↑{tok(f.tokens_in)} ↓{tok(f.tokens_out)} · {f.cache_hit}% cached · {f.cost}", "quiet")))
    return t


@ft.variant("Chips", "Each fact as a tinted tag.")
def _f_c(c: Ctx):
    f: Footer = c.item
    return c.tag(f.agent.upper(), "accent") + Text(" ") + c.tag(f.model, "blue") + Text(" ") + c.tag(f.duration, "text", .1) + Text(" ") + \
        c.tag(f"{tok(f.tokens_in + f.tokens_out)} tok", "purple") + Text(" ") + c.tag(f.cost, "green")


@ft.variant("Rule with stats", "A full-width rule that closes the turn, stats embedded.")
def _f_d(c: Ctx):
    f: Footer = c.item
    return c.rule(f"✓ {f.agent} · {f.duration}", 72, "green", right=f"in {tok(f.tokens_in)} · out {tok(f.tokens_out)} · cache {f.cache_hit}% · {f.cost}")


# --------------------------------------------------------------- errors -----

err = element("error", "Errors", "A failed tool call (policy) and a provider error that is retrying.",
              lambda c: [("tool / policy", c.f.turns[1].items[3], ""), ("provider retry", c.f.live_turn.items[0], "")])


@err.variant("Red line", "Today: ✗ title in red, detail muted below.")
def _e_a(c: Ctx):
    e: ErrorItem = c.item
    tail = f" · retrying in {e.retry_in}s ({e.attempt})" if e.retry_in else ""
    return Group(c.t(("✗ ", "red"), (e.title, "red"), (tail, "quiet")), Text("  " + e.detail, c.s("muted")))


@err.variant("Card + actions", "Bordered card, structured fields, the keys that act on it.")
def _e_b(c: Ctx):
    e: ErrorItem = c.item
    rows = [("what", e.title), ("why", e.detail)]
    if e.retry_in:
        rows.append(("next", c.t((f"retry in {e.retry_in}s", "yellow"), (f" · {e.attempt}", "quiet"))))
    keys = c.keys(("r", "retry now"), ("m", "switch model"), ("esc", "stop")) if e.retry_in else c.keys(("p", "edit policy"), ("enter", "details"))
    return Panel(Group(c.kv(rows, 5), Text(), keys), box=box.ROUNDED, border_style=c.s("red"), padding=(0, 1),
                 title=Text(" provider error " if e.kind == "provider" else " tool error ", c.s("red", bold=True)), title_align="left")


@err.variant("Tinted band", "A full-width tinted band; retry countdown as a draining bar.")
def _e_c(c: Ctx):
    e: ErrorItem = c.item
    color = "yellow" if e.retry_in else "red"
    band = Table.grid(expand=True)
    band.add_column(ratio=1)
    band.add_column(no_wrap=True, justify="right")
    band.add_row(c.t((" ! ", c.s("bg", color, bold=True)), (" " + e.title, c.s(color, bold=True))),
                 c.bar(e.retry_in / 10, 10, color, "border", "▮", "▯") + c.t((f" {e.retry_in}s ", "muted")) if e.retry_in else Text(""))
    return Panel(Group(band, Text("   " + e.detail, c.s("muted"))), box=box.SIMPLE, style=c.s(None, c.p.tint(c.p.role(color), .08)), padding=0)


@err.variant("Gutter mark", "Only a ! in the gutter and a one-line summary; details on enter.")
def _e_d(c: Ctx):
    e: ErrorItem = c.item
    return c.t(("! ", c.s("red", bold=True)), (e.title, "text"), (f" · retry {e.retry_in}s" if e.retry_in else " · enter for details", "quiet"))


# ---------------------------------------------------------------- empty -----

emp = element("empty", "Empty session", "A brand-new session before the first message.")


@emp.variant("Hints", "Today-ish: the context header, then a few hints.")
def _em_a(c: Ctx):
    return Group(c.t(("New session · Build", c.s("text", bold=True))), Text(),
                 *(c.t(("  " + k, c.s("text", bold=True)), ("  " + v, "quiet")) for k, v in
                   (("/", "commands"), ("@", "attach a file"), ("tab", "switch agent"), ("ctrl+x v", "dictate"), ("ctrl+i", "inspect context"))))


@emp.variant("Wordmark", "A large quiet wordmark centred, with the workspace under it.")
def _em_b(c: Ctx):
    mark = ("█▄ █ █▀▀ ▀▄▀ █ █ █▀▀\n█ ▀█ ██▄ █ █ █▄█ ▄▄█")
    return Group(Text(mark, c.s("accent"), justify="center"), Text(), Text(f"{c.f.workspace} · {c.f.branch}", c.s("quiet"), justify="center"),
                 Text("Build · claude-opus-5-5 · 21.7k tokens of context loaded", c.s("muted"), justify="center"))


@emp.variant("Start cards", "Three cards: resume, suggested prompts, and keys.")
def _em_c(c: Ctx):
    def card(title, rows, role):
        return Panel(Group(*rows), title=Text(f" {title} ", c.s(role, bold=True)), title_align="left", box=box.ROUNDED, border_style=c.s("border"), width=30, padding=(0, 1))
    from rich.columns import Columns
    return Columns([
        card("Resume", [c.t(("◆ ", "yellow"), (s.title[:22], "text")) for s in c.f.sessions[1:4]], "accent"),
        card("Try", [Text(x, c.s("muted")) for x in ("Explain this repo", "Find flaky tests", "Review my diff")], "blue"),
        card("Keys", [c.keys(("/", "commands")), c.keys(("@", "files")), c.keys(("ctrl+x v", "voice"))], "purple"),
    ])
