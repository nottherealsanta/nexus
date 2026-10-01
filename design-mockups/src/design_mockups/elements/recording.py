"""Recording (local dictation): how listening, transcribing and errors look.

Today the TUI shows a red dot in the root-agent row of the composer and a
consent dialog on first use. Every variant here draws all five phases.
"""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..kit import Ctx
from . import element

PHASES = [("first use", None, "consent"), ("listening", None, "listening"), ("transcribing", None, "transcribing"),
          ("inserted", None, "inserted"), ("error", None, "error")]

rec = element("recording", "Recording", "Dictation states: first-use consent, listening, transcribing, inserted, error.",
              lambda c: PHASES)

LEVELS = " ▁▂▃▄▅▆▇█"


def _wave(c: Ctx, color: str = "red", n: int | None = None) -> Text:
    lv = c.f.recording.levels[: n or None]
    return Text("".join(LEVELS[v] for v in lv), c.s(color))


def _composer(c: Ctx, body: Text, footer: Text, border: str = "border") -> Panel:
    return Panel(Group(body, Text(), footer), box=box.ROUNDED, border_style=c.s(border), padding=(0, 1), style=c.s("text", "element"))


def _agent_row(c: Ctx, extra: Text | None = None) -> Text:
    row = c.t(("BUILD", c.s("accent", bold=True)), "  ", (c.f.model, "muted"), "  ", (c.f.provider, "quiet"), "  ", (c.f.effort, "quiet"))
    return row + extra if extra is not None else row


@rec.variant("Composer dot", "Today: a red dot in the agent row; the editor stays as is.")
def _a(c: Ctx):
    r = c.f.recording
    if c.phase == "consent":
        return Panel(Group(c.t(("Voice model · about 178 MB", c.s("text", bold=True))), Text(),
                           c.t(("Download a local speech model to enable dictation? It runs on this device.", "muted")), Text(),
                           c.t((" Download model ", c.s("on_accent", "yellow", bold=True)), "  ", (" Cancel ", c.s("text", "element_hi")))),
                     box=box.ROUNDED, border_style=c.s("border_strong"), padding=(1, 2), width=64, style=c.s(None, "panel"))
    if c.phase == "listening":
        return _composer(c, c.t(("Recording… any key stops", "quiet")), _agent_row(c, c.t("  ", ("●", "red"))))
    if c.phase == "transcribing":
        return _composer(c, c.t(("Transcribing…", "quiet")), _agent_row(c, c.t("  ", ("◌", "yellow"))))
    if c.phase == "inserted":
        return _composer(c, c.t((r.partial + " refresh tests", "text"), ("▏", "accent")), _agent_row(c))
    return _composer(c, c.t(("Microphone unavailable: permission denied", "red")), _agent_row(c))


@rec.variant("Waveform strip", "A full-width strip above the composer: live level, timer against the limit, keys.")
def _b(c: Ctx):
    r = c.f.recording
    if c.phase == "consent":
        return Panel(Group(
            c.t(("◉ Dictation needs a local speech model", c.s("text", bold=True))),
            c.kv([("model", r.model), ("stored", "~/.nexus/models/voice/"), ("network", "one download, verified by SHA-256"), ("audio", "never leaves this machine")], 9),
            Text(), c.keys(("enter", "download"), ("esc", "not now"))),
            box=box.HEAVY_HEAD, border_style=c.s("red"), padding=(0, 1), style=c.s(None, "panel"))
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1, no_wrap=True)
    grid.add_column(no_wrap=True)
    if c.phase == "listening":
        grid.add_row(c.t(("● REC", c.s("red", bold=True))), _wave(c) + _wave(c, n=20), c.t((r.elapsed, "text"), (f" / {r.max}", "quiet")))
        keys = c.keys(("enter", "stop & insert"), ("esc", "discard"), ("any key", "stop"))
    elif c.phase == "transcribing":
        grid.add_row(c.t(("◌ ···", c.s("yellow", bold=True))), Text("".join(LEVELS[1] for _ in r.levels) * 2, c.s("border")),
                     c.t(("0:07 audio", "quiet")))
        keys = c.t(("transcribing locally with parakeet · esc cancels", "quiet"))
    elif c.phase == "inserted":
        grid.add_row(c.t(("✓ 8 words", c.s("green", bold=True))), c.t(("inserted at the cursor · edit before sending", "muted")), c.t(("ctrl+z", "text"), (" undo", "quiet")))
        keys = Text()
    else:
        grid.add_row(c.t(("✗ MIC", c.s("red", bold=True))), c.t(("permission denied for ", "muted"), (r.device, "text")),
                     c.t(("r", "text"), (" retry", "quiet")))
        keys = c.t(("System Settings › Privacy › Microphone › allow your terminal", "quiet"))
    return Group(Panel(grid, box=box.HORIZONTALS, border_style=c.s("red" if c.phase in ("listening", "error") else "border"),
                       padding=(0, 1), style=c.s(None, "panel")), keys)


@rec.variant("Pill", "A compact pill on the composer's right edge; nothing else moves.")
def _c(c: Ctx):
    r = c.f.recording
    if c.phase == "consent":
        return c.t((" 🎙 ", c.s("bg", "yellow")), (" Voice needs a 178 MB local model ", c.s("text", "element")),
                   (" ↵ get ", c.s("on_accent", "accent", bold=True)), (" esc ", c.s("muted", "element_hi")))
    pill = {
        "listening": c.tag(f"● {r.elapsed}", "red") + Text(" ") + _wave(c, n=8),
        "transcribing": c.tag("◌ transcribing", "yellow"),
        "inserted": c.tag("✓ inserted", "green"),
        "error": c.tag("✗ mic blocked", "red"),
    }[c.phase]
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(no_wrap=True, justify="right")
    body = c.t((r.partial + " refresh tests", "text")) if c.phase == "inserted" else c.t(("Ask anything…  / commands  @ files", "quiet"))
    grid.add_row(body, pill)
    return Panel(Group(grid, _agent_row(c)), box=box.ROUNDED, border_style=c.s("border"), padding=(0, 1), style=c.s(None, "element"))


@rec.variant("Signal banner", "Solid red banner across the composer: impossible to miss that the mic is live.")
def _d(c: Ctx):
    r = c.f.recording
    w = 60
    if c.phase == "consent":
        return Group(c.rule("VOICE ─ FIRST USE", w, "yellow"),
                     c.t(("■ ", "yellow"), ("MODEL    ", "quiet"), ("parakeet-redux · 178 MB · local only", "text")),
                     c.t(("■ ", "blue"), ("STORAGE  ", "quiet"), ("~/.nexus/models/voice", "text")),
                     c.t((" DOWNLOAD → ", c.s("yellow", c.p.tint(c.p.yellow, .25), bold=True)), " ", (" CANCEL ", c.s("muted", "element"))))
    color, label, right = {
        "listening": ("red", " ● RECORDING ", f"{r.elapsed} / {r.max}"),
        "transcribing": ("yellow", " ◌ TRANSCRIBING ", "local · 0:07 audio"),
        "inserted": ("green", " ✓ INSERTED ", "8 words"),
        "error": ("red", " ✗ MICROPHONE BLOCKED ", "press R to retry"),
    }[c.phase]
    grid = Table.grid(expand=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_column(no_wrap=True, justify="right")
    grid.add_row(Text(label, c.s("bg", color, bold=True)), Text(" " + ("ANY KEY STOPS" if c.phase == "listening" else ""), c.s(color, c.p.tint(c.p.role(color), .18), bold=True)),
                 Text(f" {right} ", c.s(color, c.p.tint(c.p.role(color), .18), bold=True)))
    return Group(grid, Panel(c.t(("Ask anything…", "quiet")) if c.phase != "inserted" else c.t((r.partial + " refresh tests", "text")),
                             box=box.SQUARE, border_style=c.s(color), padding=(0, 1), style=c.s(None, "element")))


@rec.variant("Live transcript", "The words appear as ghost text in the editor while you speak.")
def _e(c: Ctx):
    r = c.f.recording
    if c.phase == "consent":
        return _composer(c, c.t(("Dictation is off. ", "muted"), ("ctrl+x v", c.s("text", bold=True)), (" downloads a 178 MB local model first.", "muted")),
                         c.keys(("enter", "download now"), ("esc", "cancel")), "yellow")
    body = {
        "listening": c.t((r.partial, c.s("muted", italic=True)), ("▍", "red")),
        "transcribing": c.t((r.partial + " refresh", c.s("muted", italic=True)), (" …", "yellow")),
        "inserted": c.t((r.partial + " refresh tests", "text"), ("▏", "accent")),
        "error": c.t((r.partial, c.s("quiet", italic=True)), ("  ✗ stopped: input device disconnected", "red")),
    }[c.phase]
    foot = {
        "listening": c.t(("● ", "red"), ("listening ", "muted"), (r.elapsed, "text"), ("  ", ""), ("enter", c.s("text", bold=True)), (" keep  ", "quiet"),
                         ("esc", c.s("text", bold=True)), (" discard", "quiet")),
        "transcribing": c.t(("◌ ", "yellow"), ("finalising transcript…", "muted")),
        "inserted": c.t(("✓ ", "green"), ("dictated text is editable · enter sends", "muted")),
        "error": c.t(("✗ ", "red"), ("kept the partial text above · ", "muted"), ("r", c.s("text", bold=True)), (" retry", "quiet")),
    }[c.phase]
    border = {"listening": "red", "transcribing": "yellow", "inserted": "border", "error": "red"}[c.phase]
    return _composer(c, body, foot, border)


@rec.variant("Limit meter", "A thin meter that fills toward voice.max_seconds, under the composer.")
def _f(c: Ctx):
    r = c.f.recording
    w = 56
    if c.phase == "consent":
        return Group(c.t(("voice ", "quiet"), ("not installed", "yellow"), ("  ·  178 MB  ·  ", "quiet"), ("ctrl+x v", c.s("text", bold=True)), (" to download", "quiet")),
                     c.bar(0, w, "yellow", "border", "─", "─"))
    head = {
        "listening": c.t(("● rec ", "red"), (r.elapsed, "text"), ("  ", ""), (f"{int(r.fraction * 100)}% of {r.max}", "quiet")),
        "transcribing": c.t(("◌ transcribing", "yellow")),
        "inserted": c.t(("✓ inserted 8 words", "green")),
        "error": c.t(("✗ recording stopped at limit (2:00)", "red")),
    }[c.phase]
    frac = {"listening": r.fraction, "transcribing": 1.0, "inserted": 0.0, "error": 1.0}[c.phase]
    color = {"listening": "red", "transcribing": "yellow", "inserted": "green", "error": "red"}[c.phase]
    return Group(c.bar(frac, w, color, "border", "▬", "─"), head)
