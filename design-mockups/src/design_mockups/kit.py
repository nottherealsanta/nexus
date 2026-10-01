"""Palettes, render context and small drawing helpers shared by every element.

Each palette has the same role names (copied from nexus/ui/tui/theme.py, where
``nx-*`` roles exist) so variants can be judged in any palette.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .fixture import FIXTURE, Fixture

ROLES = (
    "bg", "panel", "element", "element_hi", "border", "border_strong", "text", "muted", "quiet",
    "accent", "on_accent", "blue", "purple", "green", "yellow", "red", "cyan",
)


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    bg: str
    panel: str
    element: str
    element_hi: str
    border: str
    border_strong: str
    text: str
    muted: str
    quiet: str
    accent: str
    on_accent: str
    blue: str
    purple: str
    green: str
    yellow: str
    red: str
    cyan: str

    def role(self, name: str) -> str:
        return getattr(self, name)

    def tint(self, color: str, amount: float = 0.18) -> str:
        """``color`` mixed into the background: the fill behind tinted tags."""
        return mix(self.bg, color, amount)


def mix(a: str, b: str, t: float) -> str:
    ar, ag, ab = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    br, bg, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(round(ar + (br - ar) * t), round(ag + (bg - ag) * t), round(ab + (bb - ab) * t))


def _p(name: str, dark: bool, **c: str) -> Palette:
    return Palette(name=name, dark=dark, **c)


PALETTES: dict[str, tuple[Palette, Palette]] = {
    # Today's Nexus (opencode-style) palette.
    "nexus": (
        _p("nexus", True, bg="#0a0a0a", panel="#141414", element="#1e1e1e", element_hi="#282828", border="#2c2c2c",
           border_strong="#484848", text="#eeeeee", muted="#a3a3a3", quiet="#6f6f6f", accent="#fab283", on_accent="#1a0f08",
           blue="#5c9cf5", purple="#9d7cd8", green="#7fd88f", yellow="#f5a742", red="#e06c75", cyan="#56d4dd"),
        _p("nexus", False, bg="#ffffff", panel="#f5f5f4", element="#ececea", element_hi="#e2e2df", border="#dcdcd8",
           border_strong="#b9b9b4", text="#1b1b1b", muted="#555555", quiet="#8a8a8a", accent="#c8672f", on_accent="#ffffff",
           blue="#2f6fd6", purple="#7a52c7", green="#268044", yellow="#a86200", red="#c23a4a", cyan="#0e7490"),
    ),
    # The web app's original "Signal" language: flat near-black, saturated signal colors.
    "signal": (
        _p("signal", True, bg="#111111", panel="#0b0b0b", element="#1a1a1a", element_hi="#242424", border="#2e2e2e",
           border_strong="#4a4a4a", text="#ededed", muted="#a0a0a0", quiet="#6b6b6b", accent="#ff6a2b", on_accent="#0b0b0b",
           blue="#3d8bff", purple="#b06bff", green="#2ee08a", yellow="#ffd23d", red="#ff3d5a", cyan="#2fd9e8"),
        _p("signal", False, bg="#f4f4f2", panel="#ececea", element="#fafaf8", element_hi="#e6e6e2", border="#d2d2cc",
           border_strong="#a9a9a2", text="#141414", muted="#555555", quiet="#8a8a86", accent="#e0480b", on_accent="#ffffff",
           blue="#0a5fe0", purple="#7d2fd6", green="#0f9b55", yellow="#b38600", red="#d6142f", cyan="#0a8a96"),
    ),
    # Cool, low-contrast blues for the ledger: numbers and gutters carry the eye.
    "ledger": (
        _p("ledger", True, bg="#0d1117", panel="#11161e", element="#171d27", element_hi="#1f2733", border="#253041",
           border_strong="#3a4a60", text="#dbe4ef", muted="#93a1b3", quiet="#5d6b7d", accent="#6cb6ff", on_accent="#06101c",
           blue="#6cb6ff", purple="#c39ef5", green="#6bd49a", yellow="#e8c46a", red="#f2777a", cyan="#5fd1d1"),
        _p("ledger", False, bg="#fbfcfe", panel="#f1f4f8", element="#e8edf3", element_hi="#dde4ec", border="#d3dbe5",
           border_strong="#a9b6c6", text="#17202b", muted="#4b5a6c", quiet="#8492a3", accent="#1f6feb", on_accent="#ffffff",
           blue="#1f6feb", purple="#8250df", green="#1a7f37", yellow="#9a6700", red="#cf222e", cyan="#0b7a85"),
    ),
    # Warm, gruvbox-like workbench.
    "workbench": (
        _p("workbench", True, bg="#1d2021", panel="#282828", element="#32302f", element_hi="#3c3836", border="#504945",
           border_strong="#665c54", text="#ebdbb2", muted="#bdae93", quiet="#7c6f64", accent="#fabd2f", on_accent="#1d2021",
           blue="#83a598", purple="#d3869b", green="#b8bb26", yellow="#fabd2f", red="#fb4934", cyan="#8ec07c"),
        _p("workbench", False, bg="#fbf1c7", panel="#f2e5bc", element="#ebdbb2", element_hi="#e0cfa3", border="#d5c4a1",
           border_strong="#bdae93", text="#3c3836", muted="#665c54", quiet="#928374", accent="#b57614", on_accent="#fbf1c7",
           blue="#076678", purple="#8f3f71", green="#79740e", yellow="#b57614", red="#9d0006", cyan="#427b58"),
    ),
    # Reading mode: light paper first, warm sepia-dark second.
    "paper": (
        _p("paper", True, bg="#1c1a17", panel="#211f1b", element="#2a2722", element_hi="#332f29", border="#3a352e",
           border_strong="#544d43", text="#e8e0d2", muted="#b3a893", quiet="#7d7464", accent="#d9a35b", on_accent="#1c1a17",
           blue="#8fb3d9", purple="#c2a3d6", green="#a3c28a", yellow="#d9b95b", red="#d98a7a", cyan="#8ac2b8"),
        _p("paper", False, bg="#faf7f0", panel="#f3eee3", element="#ebe5d6", element_hi="#e2dac8", border="#ddd5c3",
           border_strong="#bfb5a0", text="#2b2620", muted="#5e554a", quiet="#978c7c", accent="#a35a1f", on_accent="#faf7f0",
           blue="#2d5b8a", purple="#6b4a8a", green="#4d6b2d", yellow="#8a6a14", red="#a33a2d", cyan="#2d6b6b"),
    ),
}


def palette(name: str, dark: bool = True) -> Palette:
    pair = PALETTES[name]
    return pair[0] if dark else pair[1]


class Fit:
    """A renderable drawn at the width it is given (optionally capped)."""

    def __init__(self, draw: Callable[[int], RenderableType], cap: int | None = None) -> None:
        self.draw, self.cap = draw, cap

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.draw(min(self.cap, options.max_width) if self.cap else options.max_width)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, min(self.cap, options.max_width) if self.cap else options.max_width)


class Barred:
    """Prefix every rendered (wrapped) line of ``inner`` with ``bar``: quote bars that wrap correctly."""

    def __init__(self, inner: RenderableType, bar: str, style: Style) -> None:
        self.inner, self.bar, self.style = inner, bar, style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(1, options.max_width - len(self.bar))
        for line in console.render_lines(self.inner, options.update(width=width), pad=False):
            yield Segment(self.bar, self.style)
            yield from line
            yield Segment.line()


@dataclass(frozen=True)
class Ctx:
    """Everything a variant needs: colors, data, the item it draws, and the phase."""

    p: Palette
    f: Fixture = FIXTURE
    item: Any = None
    phase: str = ""
    picks: Mapping[str, str] = field(default_factory=dict)

    def with_(self, **kw: Any) -> "Ctx":
        return replace(self, **kw)

    def s(self, fg: str | None = None, bg: str | None = None, *, bold: bool = False, italic: bool = False,
          dim: bool = False, underline: bool = False, reverse: bool = False) -> Style:
        """Style from role names (``"accent"``) or literal hex colors."""
        return Style(color=self._c(fg), bgcolor=self._c(bg), bold=bold or None, italic=italic or None,
                     dim=dim or None, underline=underline or None, reverse=reverse or None)

    def _c(self, v: str | None) -> str | None:
        if not v:
            return None
        return v if v.startswith("#") else self.p.role(v)

    def t(self, *parts: tuple[str, Style | str] | str | Text) -> Text:
        """Build Text from ``"plain"``, ``Text`` or ``("text", style_or_role)`` parts."""
        out = Text()
        for part in parts:
            if isinstance(part, Text):
                out.append_text(part)
            elif isinstance(part, str):
                out.append(part, self.s("text"))
            else:
                text, style = part
                out.append(text, self.s(style) if isinstance(style, str) else style)
        return out

    # ---- small shared marks -------------------------------------------------

    def chip(self, label: str, color: str = "accent", fg: str = "bg") -> Text:
        """Solid fill, dark text: today's context-header chip."""
        return Text(f" {label} ", self.s(fg, color, bold=True))

    def tag(self, label: str, color: str = "accent", amount: float = 0.2) -> Text:
        """Tinted fill with bright text (Signal's tag)."""
        return Text(f" {label} ", Style(color=self._c(color), bgcolor=self.p.tint(self._c(color), amount), bold=True))

    def swatch(self, color: str) -> Text:
        return Text("■", self.s(color))

    def bar(self, fraction: float, width: int, color: str = "accent", empty: str = "border",
            full: str = "━", rest: str = "━") -> Text:
        n = max(0, min(width, round(fraction * width)))
        return Text(full * n, self.s(color)) + Text(rest * (width - n), self.s(empty))

    def block_bar(self, fraction: float, width: int, color: str = "accent", empty: str = "element") -> Text:
        """Eighth-block bar with sub-cell precision."""
        eighths = " ▏▎▍▌▋▊▉█"
        cells = max(0.0, min(1.0, fraction)) * width
        whole = int(cells)
        part = eighths[int((cells - whole) * 8)] if whole < width else ""
        filled = "█" * whole + (part if part.strip() else "")
        return Text(filled, self.s(color, empty)) + Text(" " * (width - len(filled)), self.s(None, empty))

    def stacked(self, slices: list[tuple[int, str]], total: int, width: int, empty: str = "element", char: str = "█") -> Text:
        out = Text()
        used = 0
        for tokens, color in slices:
            n = round(tokens / total * width)
            n = min(n, width - used)
            out.append(char * n, self.s(color))
            used += n
        out.append(("░" if char == "█" else char) * (width - used), self.s(empty))
        return out

    def rule(self, label: str, width: int = 0, color: str = "quiet", fill: str = "─", right: str = "") -> Fit:
        """``LABEL ───────── right`` heading; ``width`` caps it, otherwise it fills the line."""
        def draw(w: int) -> Text:
            body = Text(label, self.s(color, bold=True))
            tail = Text(f" {right}", self.s("quiet")) if right else Text()
            if body.cell_len + tail.cell_len + 3 > w:
                tail = Text()
            n = max(1, w - body.cell_len - tail.cell_len - 1)
            out = body + Text(" " + fill * n, self.s("border")) + tail
            out.no_wrap, out.overflow = True, "crop"
            return out
        return Fit(draw, width or None)

    def barred(self, inner: RenderableType, color: str = "accent", bar: str = "▌ ") -> Barred:
        return Barred(inner, bar, self.s(color))

    def fit(self, draw: Callable[[int], RenderableType], cap: int | None = None) -> Fit:
        return Fit(draw, cap)

    def kv(self, rows: list[tuple[str, RenderableType | str]], key_width: int = 10, key_color: str = "quiet") -> Table:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=key_width, no_wrap=True)
        grid.add_column(ratio=1)
        for key, value in rows:
            grid.add_row(Text(key, self.s(key_color)), value if not isinstance(value, str) else Text(value, self.s("text")))
        return grid

    def clipped(self, hidden: int, unit: str = "lines", how: str = "enter to expand") -> Text:
        """Clipping is always announced."""
        return self.t((f"… {hidden} more {unit} hidden", "quiet"), (f" · {how}", "quiet"))

    def keys(self, *pairs: tuple[str, str], sep: str = "  ") -> Text:
        out = Text()
        for i, (key, label) in enumerate(pairs):
            if i:
                out.append(sep)
            out.append(key, self.s("text", bold=True))
            out.append(f" {label}", self.s("quiet"))
        return out


STATUS_GLYPH = {"ok": "✓", "error": "✗", "running": "◌", "denied": "⊘", "done": "✓", "failed": "✗"}
STATUS_ROLE = {"ok": "green", "error": "red", "running": "accent", "denied": "yellow", "done": "green", "failed": "red",
               "idle": "quiet", "approval": "yellow"}
GROUP_ROLE = {"files": "blue", "search": "cyan", "shell": "accent", "web": "purple", "agents": "green", "planning": "yellow"}


def tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
