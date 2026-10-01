"""The Textual app: a design viewer, an element gallery, and overlays.

Nothing here is functional. Keys only change what is shown: the state (1–0),
the palette, sidebars, the design, and which element variants you pick.
"""

from __future__ import annotations

import io

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import ListItem, ListView, Static

from .designs import DESIGNS, LAYOUTS, Design, Layout
from .elements import ELEMENTS, render
from .fixture import ErrorItem, SubAgent, Thought, ToolCall
from .kit import PALETTES, Ctx, Palette, palette
from .picks import Picks

STATES = (
    ("1", "idle", "a finished conversation"),
    ("2", "streaming", "a turn running: live text, activity, retry"),
    ("3", "permission", "a tool waiting for approval"),
    ("4", "question", "the agent asking you something"),
    ("5", "recording", "dictation listening"),
    ("6", "empty", "a new session"),
    ("7", "palette", "command palette open"),
    ("8", "model", "model picker open"),
    ("9", "tool", "tool details open (failed pytest)"),
    ("0", "narrow", "80-column layout"),
)
STATE_NAMES = [s[1] for s in STATES]
MODAL_STATES = {"palette": "command-palette", "model": "model-picker", "tool": "tool-details"}
REC_REPLACES_COMPOSER = {"a", "c", "d", "e"}  # recording variants that draw their own composer
COMPACT_TOOL = {"a", "d", "e", "f"}
COMPACT_THOUGHT = {"a", "b", "d"}
SIDEBAR_WIDTH = {"sessions": 34, "details": 42}


APP_KEYS = [
    *(Binding(k, f"state('{name}')", name, show=False) for k, name, _ in STATES),
    Binding("t", "toggle_dark", "Theme"),
    Binding("c", "cycle_palette", "Palette"),
    Binding("left_square_bracket", "toggle('left')", "Sessions"),
    Binding("right_square_bracket", "toggle('right')", "Details"),
    Binding("l", "toggle('logs')", "Logs"),
    Binding("n", "step(1)", "Next design"),
    Binding("p", "step(-1)", "Previous design"),
    Binding("L", "cycle_layout", "Layout (mix)"),
    Binding("g", "gallery", "Elements"),
    Binding("question_mark", "help", "About"),
    Binding("q", "quit", "Quit"),
]


def _static(renderable: RenderableType, **styles) -> Static:
    w = Static(renderable)
    for k, v in styles.items():
        setattr(w.styles, k, v)
    return w


def _scroll_colors(w: Widget, p: Palette) -> Widget:
    w.styles.scrollbar_color = p.border_strong
    w.styles.scrollbar_color_hover = p.accent
    w.styles.scrollbar_background = p.bg
    w.styles.scrollbar_background_hover = p.bg
    w.styles.scrollbar_background_active = p.bg
    w.styles.scrollbar_size_vertical = 1
    return w


# ------------------------------------------------------------- timeline -----


def timeline_rows(c: Ctx, state: str, lay: Layout) -> list[tuple[RenderableType, int, bool]]:
    """(renderable, margin below, selected) for every row in the conversation."""
    f = c.f
    out: list[tuple[RenderableType, int, bool]] = [(render(c, "context-header"), 1 + lay.gap, False)]
    if state == "empty":
        out.append((render(c, "empty"), 0, False))
        return out
    turns = list(f.turns)
    if state in ("streaming", "permission", "question"):
        turns.append(f.live_turn)
    tool_compact = c.picks.get("tool-call", "a") in COMPACT_TOOL
    thought_compact = c.picks.get("thought", "a") in COMPACT_THOUGHT
    for turn in turns:
        rows: list[tuple[str, object]] = [("user", turn.user)]
        items = turn.items
        if turn.live and state == "permission":
            items = items[:2]
        elif turn.live and state == "question":
            items = ()
        group_done = False
        for i, it in enumerate(items):
            if isinstance(it, Thought):
                rows.append(("thought", it))
            elif isinstance(it, ToolCall):
                rows.append(("tool", it))
                if it.diff:
                    rows.append(("diff", it))
            elif isinstance(it, SubAgent):
                if not group_done:
                    rows.append(("sub", tuple(x for x in turn.items if isinstance(x, SubAgent))))
                    group_done = True
            elif isinstance(it, ErrorItem):
                rows.append(("error", it))
            else:
                text = it + (" ▍" if turn.live and i == len(items) - 1 else "")
                rows.append(("assistant", text))
        if turn.footer:
            rows.append(("footer", turn.footer))
        for k, (kind, item) in enumerate(rows):
            slug = {"user": "user-message", "thought": "thought", "tool": "tool-call", "diff": "diff", "sub": "subagent",
                    "error": "error", "assistant": "assistant", "footer": "reply-footer"}[kind]
            nxt = rows[k + 1][0] if k + 1 < len(rows) else None
            compact = (kind == "tool" and tool_compact) or (kind == "thought" and thought_compact)
            nxt_compact = (nxt == "tool" and tool_compact) or (nxt == "thought" and thought_compact)
            margin = 1 + lay.gap if nxt is None else (0 if compact and nxt_compact else 1)
            if kind == "assistant" and nxt == "footer":
                margin = 1
            selected = lay.right == "inspector" and isinstance(item, ToolCall) and item.id == "c6" and kind == "tool"
            out.append((render(c, slug, item=item), margin, selected))
    return out


# --------------------------------------------------------------- screens ----


class Overlay(ModalScreen[None]):
    """A centred static overlay (palette, model picker, tool details, help)."""

    # Modal screens don't see app bindings, so overlays forward the same keys.
    BINDINGS = [Binding("escape", "app.close_overlay", "Close"),
                *(Binding(b.key, f"app.{b.action}", b.description, show=False) for b in APP_KEYS)]

    def __init__(self, renderable: RenderableType, p: Palette, kind: str, width: int | None = None) -> None:
        super().__init__()
        self.renderable, self.p, self.kind, self.box_width = renderable, p, kind, width

    def compose(self) -> ComposeResult:
        box = _scroll_colors(VerticalScroll(_static(self.renderable), id="ov"), self.p)
        framed = self.kind in ("tool", "help")
        # A scroll container can't size itself to its content, so measure the renderable.
        width = self.box_width or Console(width=120, file=io.StringIO()).measure(self.renderable).maximum
        box.styles.width = min(width, 120) + (6 if framed else 1)
        box.styles.background = self.p.panel if framed else None
        box.styles.padding = (1, 2) if framed else 0
        if framed:
            box.styles.border = ("round", self.p.border_strong)
        yield box

    def on_mount(self) -> None:
        self.styles.background = Color.parse(self.p.bg).with_alpha(0.72)


class DesignScreen(Screen):
    """One design in one state, built from the app's current settings."""

    def compose(self) -> ComposeResult:
        app: MockupsApp = self.app  # type: ignore[assignment]
        d, lay, p = app.design(), app.layout(), app.pal()
        c = Ctx(p, picks=d.picks)
        st = app.state
        narrow = st == "narrow"
        self.styles.background = p.bg

        phase = {"streaming": "working", "permission": "approval", "question": "approval"}.get(st, "idle")
        top = _static(render(c, "topbar", phase=phase), background=p.panel, padding=(0, 1), height="auto")
        if lay.rules or lay.boxed:
            top.styles.border_bottom = ("solid", p.border)
        yield top
        if lay.meter == "top":
            yield _static(render(c, "context-meter"), background=p.panel, padding=(0, 2), height="auto")

        body = Horizontal(id="body")
        if lay.center_max or narrow:
            body.styles.align_horizontal = "center"
        with body:
            if lay.left and app.show_left and not narrow:
                left = _scroll_colors(VerticalScroll(_static(render(c, "sessions")), id="left"), p)
                left.styles.width = lay.left_width
                left.styles.background = p.panel
                left.styles.padding = (1, 1)
                self._frame(left, lay, p, "Sessions", "right")
                yield left
            center = Vertical(id="center")
            if lay.center_max or narrow:
                center.styles.max_width = 80 if narrow else lay.center_max
            self._frame(center, lay, p, "Conversation", None)
            with center:
                tl = _scroll_colors(VerticalScroll(id="timeline"), p)
                tl.styles.padding = (1, lay.pad)
                with tl:
                    for renderable, margin, selected in timeline_rows(c, st if st != "narrow" else "idle", lay):
                        w = _static(renderable, margin=(0, 0, margin, 0))
                        if selected:
                            w.styles.background = p.element_hi
                            w.styles.border_left = ("outer", p.accent)
                        yield w
                if st in ("streaming", "permission", "question", "recording"):
                    self.call_after_refresh(tl.scroll_end, animate=False)
                if app.show_logs:
                    logs = _scroll_colors(VerticalScroll(_static(render(c, "logs")), id="logs"), p)
                    logs.styles.height = 12
                    logs.styles.background = p.panel
                    logs.styles.padding = (0, lay.pad)
                    logs.styles.border_top = ("solid", p.border)
                    yield logs
                dock = Vertical(id="dock")
                dock.styles.padding = (0, lay.pad, 1 if lay.meter != "dock" else 0, lay.pad)
                with dock:
                    for r in self._dock(c, st, lay):
                        yield _static(r, height="auto")
            if lay.right and app.show_right and not narrow:
                if lay.right == "inspector":
                    content = Group(c.t(("INSPECTOR", c.s("quiet", bold=True)), ("  selected: Bash uv run pytest -q", "quiet")), Text(),
                                    render(c, "tool-details", item=c.f.call("c6")))
                    title = "Inspector"
                else:
                    content, title = render(c, "details"), "Details"
                right = _scroll_colors(VerticalScroll(_static(content), id="right"), p)
                right.styles.width = lay.right_width
                right.styles.background = p.panel
                right.styles.padding = (1, 1)
                self._frame(right, lay, p, title, "left")
                yield right
        yield self._info(app, d, p)

    def _frame(self, w: Widget, lay: Layout, p: Palette, title: str, rule_side: str | None) -> None:
        if lay.boxed:
            w.styles.border = ("round", p.border_strong if title != "Conversation" else p.accent)
            w.border_title = f"{title}"
            w.styles.border_title_color = p.accent if title == "Conversation" else p.muted
            w.styles.border_title_style = "bold"
        elif lay.rules and rule_side:
            setattr(w.styles, f"border_{rule_side}", ("solid", p.border))

    def _dock(self, c: Ctx, st: str, lay: Layout) -> list[RenderableType]:
        out: list[RenderableType] = []
        if st == "permission":
            out.append(render(c, "permission", item=c.f.permission))
        if st == "question":
            out.append(render(c, "question", item=c.f.question))
        composer = render(c, "composer", phase="busy" if st == "streaming" else "empty")
        if st == "recording":
            rec = render(c, "recording", phase="listening")
            key = c.picks.get("recording", "a")
            out += [rec] if key in REC_REPLACES_COMPOSER else ([composer, rec] if key == "f" else [rec, composer])
        else:
            out.append(composer)
        if st == "streaming":
            out.append(render(c, "activity", phase="working"))
        if lay.meter == "dock":
            out.append(render(c, "context-meter"))
        return out

    def _info(self, app: "MockupsApp", d: Design, p: Palette) -> Static:
        where = "mix" if app.mode == "mix" else f"design {app.index + 1}/{len(DESIGNS)}"
        line = Text.assemble(
            (f" {where} ", f"bold {p.on_accent} on {p.accent}"), (f" {d.title} ", f"bold {p.text}"),
            (f"· {app.state} · {app.pal().name} {'dark' if app.dark else 'light'} · layout {app.layout().name}   ", p.muted),
            ("1-0 ", f"bold {p.text}"), ("states  ", p.quiet), ("t ", f"bold {p.text}"), ("theme  ", p.quiet),
            ("c ", f"bold {p.text}"), ("palette  ", p.quiet), ("[ ] l ", f"bold {p.text}"), ("panels/logs  ", p.quiet),
            ("n p ", f"bold {p.text}"), ("design  ", p.quiet), ("g ", f"bold {p.text}"), ("elements  ", p.quiet),
            ("? ", f"bold {p.text}"), ("about  ", p.quiet), ("q ", f"bold {p.text}"), ("quit", p.quiet),
        )
        line.no_wrap = True
        line.overflow = "ellipsis"
        return _static(line, height=1, background=p.element, dock="bottom")


class GalleryScreen(Screen):
    """Every variant of one element, drawn on the same samples. a–f pick a variant for your mix."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        *(Binding(k, f"pick('{k}')", f"Pick {k.upper()}", show=False) for k in "abcdef"),
        Binding("x", "unpick", "Clear pick"),
        Binding("p", "app.cycle_palette", "Palette"),
        Binding("t", "app.toggle_dark", "Theme"),
        Binding("m", "app.open_mix", "Open mix"),
    ]

    def __init__(self, slug: str | None = None) -> None:
        super().__init__()
        self.slug = slug or next(iter(ELEMENTS))

    def compose(self) -> ComposeResult:
        app: MockupsApp = self.app  # type: ignore[assignment]
        p = app.gallery_pal()
        self.styles.background = p.bg
        el = ELEMENTS[self.slug]
        head = Text.assemble((" ELEMENTS ", f"bold {p.on_accent} on {p.accent}"), (f" {el.title} ", f"bold {p.text}"),
                             (f"— {el.description}", p.muted))
        yield _static(head, background=p.panel, padding=(0, 1), height="auto", border_bottom=("solid", p.border))
        with Horizontal():
            items = []
            for slug, e in ELEMENTS.items():
                picked = app.picks.elements.get(slug)
                label = Text.assemble((f" {e.title}", p.text), (f"  {len(e.variants)}", p.quiet),
                                      (f"  ★{picked.upper()}" if picked else "", p.accent))
                items.append(ListItem(Static(label), name=slug))
            lv = ListView(*items, initial_index=list(ELEMENTS).index(self.slug), id="els")
            lv.styles.width = 30
            lv.styles.background = p.panel
            lv.styles.border_right = ("solid", p.border)
            _scroll_colors(lv, p)
            yield lv
            var = _scroll_colors(VerticalScroll(id="variants"), p)
            var.styles.padding = (1, 2)
            with var:
                yield from self._variants(app, el, p)
        foot = Text.assemble((" ↑↓ ", f"bold {p.text}"), ("element  ", p.quiet), ("a–f ", f"bold {p.text}"), ("pick variant  ", p.quiet),
                             ("x ", f"bold {p.text}"), ("clear  ", p.quiet), ("p ", f"bold {p.text}"), (f"palette ({p.name})  ", p.quiet),
                             ("t ", f"bold {p.text}"), ("theme  ", p.quiet), ("m ", f"bold {p.text}"), ("open your mix  ", p.quiet),
                             ("esc ", f"bold {p.text}"), ("back", p.quiet))
        yield _static(foot, height=1, background=p.element, dock="bottom")

    def _variants(self, app: "MockupsApp", el, p: Palette):
        c = Ctx(p)
        picked = app.picks.elements.get(el.slug)
        users = {v.key: [d.title for d in DESIGNS if d.picks.get(el.slug, "a") == v.key] for v in el.variants}
        width = SIDEBAR_WIDTH.get(el.slug)
        blocks = []
        for v in el.variants:
            mark = ("  ★ in your mix", f"bold {p.accent}") if picked == v.key else ("", "")
            head = Text.assemble((f" {v.key.upper()} ", f"bold {p.on_accent} on {p.accent if picked == v.key else p.muted}"),
                                 (f" {v.name}", f"bold {p.text}"), mark)
            sub = Text.assemble((v.note, p.muted), ("   used by: " + ", ".join(users[v.key]) if users[v.key] else "", p.quiet))
            parts: list[Widget] = [_static(head), _static(sub, margin=(0, 0, 1, 0))]
            for caption, item, phase in el.samples(c):
                if caption:
                    parts.append(_static(Text(f"· {caption}", p.quiet)))
                parts.append(_static(v.render(c.with_(item=item, phase=phase)), margin=(0, 0, 1, 0)))
            blocks.append(parts)
        if width:
            row = Horizontal()
            row.styles.height = "auto"
            with row:
                for parts in blocks:
                    col = Vertical(*parts)
                    col.styles.width = width + 2
                    col.styles.height = "auto"
                    col.styles.margin = (0, 3, 0, 0)
                    yield col
            yield row
        else:
            for parts in blocks:
                box = Vertical(*parts)
                box.styles.height = "auto"
                box.styles.margin = (0, 0, 1, 0)
                box.styles.border_top = ("solid", p.border)
                yield box

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.name and event.item.name != self.slug:
            self.slug = event.item.name
            app: MockupsApp = self.app  # type: ignore[assignment]
            var = self.query_one("#variants", VerticalScroll)
            await var.remove_children()
            await var.mount_all(list(self._variants(app, ELEMENTS[self.slug], app.gallery_pal())))
            var.scroll_home(animate=False)
            el = ELEMENTS[self.slug]
            p = app.gallery_pal()
            self.query(Static).first().update(Text.assemble((" ELEMENTS ", f"bold {p.on_accent} on {p.accent}"),
                                                            (f" {el.title} ", f"bold {p.text}"), (f"— {el.description}", p.muted)))

    async def action_pick(self, key: str) -> None:
        app: MockupsApp = self.app  # type: ignore[assignment]
        if key in {v.key for v in ELEMENTS[self.slug].variants}:
            app.picks.elements[self.slug] = key
            app.picks.save()
            app.notify(f"{ELEMENTS[self.slug].title}: picked {key.upper()} · saved to picks.json", timeout=2)
            await self.recompose()

    async def action_unpick(self) -> None:
        app: MockupsApp = self.app  # type: ignore[assignment]
        app.picks.elements.pop(self.slug, None)
        app.picks.save()
        await self.recompose()

    def action_back(self) -> None:
        self.app.pop_screen()


# ------------------------------------------------------------------- app ----


class MockupsApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #center { width: 1fr; height: 1fr; }
    #timeline { height: 1fr; }
    #dock { height: auto; }
    #left, #right { height: 1fr; }
    Overlay { align: center middle; }
    #ov { width: auto; height: auto; max-height: 90%; max-width: 96%; }
    #variants { width: 1fr; }
    ListView > ListItem { padding: 0 0; }
    """
    BINDINGS = APP_KEYS

    def __init__(self, *, index: int = 0, state: str = "idle", light: bool | None = None, palette_name: str | None = None,
                 mode: str = "design", gallery: str | None = None, picks: Picks | None = None) -> None:
        super().__init__()
        self.index, self.state, self.mode = index, state, mode
        self.picks = picks or Picks.load()
        self.palette_override = palette_name
        self.gallery_palette = palette_name or "nexus"
        self.start_gallery = gallery
        self.forced_light = light
        self.dark = not (light if light is not None else self.design().light)
        self.show_left = self.show_right = True
        self.show_logs = False
        self.screen_design: DesignScreen | None = None

    # ---- current design ---------------------------------------------------

    def design(self) -> Design:
        if self.mode == "mix":
            return Design("mix", "Your mix", "Built from your picks (picks.json). Pick variants with `design elements`; L cycles the "
                          "layout and c the palette here.", self.picks.layout, self.picks.palette, dict(self.picks.elements), self.picks.light)
        return DESIGNS[self.index]

    def layout(self) -> Layout:
        return LAYOUTS[self.design().layout]

    def pal(self) -> Palette:
        return palette(self.palette_override or self.design().palette, self.dark)

    def gallery_pal(self) -> Palette:
        return palette(self.gallery_palette, self.dark)

    # ---- lifecycle --------------------------------------------------------

    async def on_mount(self) -> None:
        self.screen_design = DesignScreen()
        await self.push_screen(self.screen_design)
        if self.start_gallery is not None:
            await self.push_screen(GalleryScreen(self.start_gallery or None))
        elif self.state in MODAL_STATES:
            self._push_modal()

    async def rebuild(self) -> None:
        while isinstance(self.screen, Overlay):
            await self.pop_screen()
        if isinstance(self.screen, GalleryScreen):
            await self.screen.recompose()
            return
        if self.screen_design is not None:
            await self.screen_design.recompose()
        self._push_modal()

    def _push_modal(self) -> None:
        if self.state in MODAL_STATES and self.screen is self.screen_design:
            d = self.design()
            c = Ctx(self.pal(), picks=d.picks)
            slug = MODAL_STATES[self.state]
            item = c.f.call("c6") if slug == "tool-details" else None
            self.push_screen(Overlay(render(c, slug, item=item), self.pal(), self.state, 104 if slug == "tool-details" else None))

    def _on_design(self) -> bool:
        return isinstance(self.screen, (DesignScreen, Overlay))

    # ---- actions ----------------------------------------------------------

    async def action_state(self, name: str) -> None:
        if self._on_design():
            self.state = name
            await self.rebuild()

    async def action_toggle_dark(self) -> None:
        self.dark = not self.dark
        if self.mode == "mix":
            self.picks.light = not self.dark
            self.picks.save()
        await self.rebuild()

    async def action_cycle_palette(self) -> None:
        names = list(PALETTES)
        if isinstance(self.screen, GalleryScreen):
            self.gallery_palette = names[(names.index(self.gallery_palette) + 1) % len(names)]
        elif self.mode == "mix":
            self.picks.palette = names[(names.index(self.picks.palette) + 1) % len(names)]
            self.picks.save()
        else:
            cur = self.palette_override or self.design().palette
            self.palette_override = names[(names.index(cur) + 1) % len(names)]
        await self.rebuild()

    async def action_cycle_layout(self) -> None:
        if self.mode == "mix" and self._on_design():
            names = list(LAYOUTS)
            self.picks.layout = names[(names.index(self.picks.layout) + 1) % len(names)]
            self.picks.save()
            await self.rebuild()
        else:
            self.notify("L cycles the layout in your mix (design mix).", timeout=2)

    async def action_toggle(self, what: str) -> None:
        if self._on_design():
            attr = {"left": "show_left", "right": "show_right", "logs": "show_logs"}[what]
            setattr(self, attr, not getattr(self, attr))
            await self.rebuild()

    async def action_step(self, delta: int) -> None:
        if not self._on_design():
            return
        self.mode = "design"
        self.index = (self.index + delta) % len(DESIGNS)
        self.palette_override = None
        self.dark = not (self.forced_light if self.forced_light is not None else self.design().light)
        self.show_left = self.show_right = True
        await self.rebuild()

    async def action_gallery(self) -> None:
        if self._on_design():
            while isinstance(self.screen, Overlay):
                await self.pop_screen()
            self.gallery_palette = self.palette_override or self.design().palette
            await self.push_screen(GalleryScreen())

    async def action_open_mix(self) -> None:
        self.mode = "mix"
        self.dark = not self.picks.light
        while not isinstance(self.screen, DesignScreen):
            await self.pop_screen()
        await self.rebuild()

    async def action_close_overlay(self) -> None:
        top = self.screen
        if isinstance(top, Overlay):
            await self.pop_screen()
            if top.kind in MODAL_STATES:
                self.state = "idle"
                await self.rebuild()

    def action_help(self) -> None:
        if not self._on_design():
            return
        d, p = self.design(), self.pal()
        c = Ctx(p)
        keys = Table.grid(padding=(0, 2))
        keys.add_column(no_wrap=True)
        keys.add_column()
        for k, name, desc in STATES:
            keys.add_row(Text(k, f"bold {p.text}"), Text(f"{name:<11} {desc}", p.muted))
        for k, desc in (("t", "dark / light"), ("c", "cycle palette"), ("[  ]", "sessions / details sidebar"), ("l", "logs drawer"),
                        ("n  p", "next / previous design"), ("g", "element gallery: every variant of every element"),
                        ("L", "cycle layout (mix only)"), ("q", "quit")):
            keys.add_row(Text(k, f"bold {p.text}"), Text(desc, p.muted))
        picks = Table.grid(padding=(0, 2))
        picks.add_column(no_wrap=True)
        picks.add_column()
        for slug, el in ELEMENTS.items():
            key = d.picks.get(slug, "a")
            picks.add_row(Text(el.title, p.muted), Text(f"{key.upper()} · {el.get(key).name}", p.text))
        lay = self.layout()
        body = Group(Text(d.title, f"bold {p.accent}"), Text(d.idea, p.text), Text(),
                     c.kv([("layout", f"{lay.name} — {lay.note}"), ("palette", self.pal().name)], 8), Text(),
                     c.rule("ELEMENT VARIANTS", 80), picks, Text(), c.rule("KEYS", 80), keys)
        if isinstance(self.screen, Overlay):
            return
        self.push_screen(Overlay(body, p, "help", 90))


def run(**kw) -> None:
    MockupsApp(**kw).run()


__all__ = ["MockupsApp", "STATES", "STATE_NAMES", "run"]
