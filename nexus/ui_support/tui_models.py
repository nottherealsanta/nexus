"""Settings → Models, Session titles and the agent Tiers dialog (Textual).

Mirrors the native client's pages from the shared rows and help text in
``tier_settings.py`` (plans/SESSION_TITLE_PLAN.md). Every change is a host
command through the client: ``ModelTierSet`` / ``ModelTierReset`` /
``AgentMaxTierSet`` for tiers and ``SessionTitleSettingsSet`` for titles.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, OptionList, Static, Switch
from textual.widgets._option_list import Option

from . import tier_settings as ts
from .agent_frontmatter import MAX_TIERS
from .text import sanitize


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _tier_rows(result: Any) -> list[Any]:
    return list(_field(result, "tiers", []) or [])


class ChoiceScreen(ModalScreen[str | None]):
    """A titled list of ``(label, value)`` choices; Enter returns the value."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: Sequence[tuple[str, str]], *, help_text: str = "") -> None:
        super().__init__()
        self._title, self._options, self._help = title, list(options), help_text

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Static(sanitize(self._title, 120), id="choice-title", markup=False)
            if self._help:
                yield Static(self._help, id="choice-help", markup=False)
            yield OptionList(*(Option(sanitize(label, 160), id=value) for label, value in self._options), id="choice-list")

    def on_mount(self) -> None:
        self.query_one("#choice-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#choice-list")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TierEditorScreen(ModalScreen[None]):
    """One tier's ordered models: move, remove, add, reset. Saves at every change."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "close", "Close")]

    def __init__(self, client: Any, tier: str, list_models: Any) -> None:
        super().__init__()
        self._client, self._tier, self._list_models = client, tier, list_models
        self._refs: list[str] = []
        self._row: Any = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tier-dialog"):
            yield Static(f"Tier · {sanitize(self._tier, 64)}", id="tier-title", markup=False)
            yield Static("", id="tier-lines", markup=False)
            yield VerticalScroll(id="tier-refs")
            yield Static("", id="tier-status", markup=False)
            with Horizontal(id="tier-actions"):
                yield Button("+ Add model", id="tier-add")
                yield Button("Reset to default", id="tier-reset")
                yield Button("Done", id="tier-done")

    def on_mount(self) -> None:
        self.run_worker(self._load(), group="tier-load", exclusive=True)

    def _status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#tier-status", Static).update(sanitize(text, 200))

    async def _load(self, result: Any = None) -> None:
        try:
            result = result or await self._client.model_tiers()
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Unavailable: {exc}")
            return
        row = next((row for row in _tier_rows(result) if _field(row, "name") == self._tier), None)
        if row is None or not self.is_mounted:
            return
        self._row, self._refs = row, list(_field(row, "refs", []))
        self.query_one("#tier-lines", Static).update("\n".join(ts.tier_lines(row)))
        pane = self.query_one("#tier-refs", VerticalScroll)
        await pane.remove_children()
        for index, ref in enumerate(self._refs):
            label = f"{index + 1}. {ref}" + (" · used first" if index == 0 and len(self._refs) > 1 else "")
            await pane.mount(Horizontal(
                Static(sanitize(label, 120), classes="tier-ref-label", markup=False),
                Button("↑", name=f"up|{index}", classes="tier-ref-btn"),
                Button("↓", name=f"down|{index}", classes="tier-ref-btn"),
                Button("×", name=f"remove|{index}", classes="tier-ref-btn"),
                classes="tier-ref-row",
            ))
        editable = bool(_field(row, "editable", True))
        self.query_one("#tier-add", Button).disabled = not editable
        self.query_one("#tier-reset", Button).display = _field(row, "source") == "your list"

    async def _save(self, refs: list[str], message: str = "Saved") -> None:
        if not refs:
            self._status("A tier needs at least one model. Use Reset to default to go back to the built-in models.")
            return
        try:
            result = await self._client.model_tier_set(self._tier, refs)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._status(str(exc))
            return
        self._status(f"{message} · restart the daemon to apply" if _field(result, "restart_required") else message)
        await self._load(result)

    @on(Button.Pressed, ".tier-ref-btn")
    async def _ref_action(self, event: Button.Pressed) -> None:
        action, _, raw = (event.button.name or "").partition("|")
        index = int(raw)
        refs = (ts.move_ref(self._refs, index, -1) if action == "up" else ts.move_ref(self._refs, index, 1)
                if action == "down" else ts.remove_ref(self._refs, index))
        await self._save(refs)

    @on(Button.Pressed, "#tier-add")
    def _add_pressed(self) -> None:
        self.run_worker(self._add(), group="tier-add", exclusive=True)

    async def _add(self) -> None:
        try:
            rows = [row for row in await self._list_models()
                    if isinstance(row, dict) and row.get("provider") and row.get("id")]
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Models unavailable: {exc}")
            return
        options = [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}", f"{row['provider']}/{row['id']}")
                   for row in rows if f"{row['provider']}/{row['id']}" not in self._refs]
        choice = await self.app.push_screen_wait(ChoiceScreen(f"Add model to {self._tier}", options))
        if choice:
            await self._save([*self._refs, choice], f"Added to {self._tier}")

    @on(Button.Pressed, "#tier-reset")
    async def _reset(self) -> None:
        try:
            result = await self._client.model_tier_reset(self._tier)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._status(str(exc))
            return
        self._status(f"{self._tier} reset to default")
        await self._load(result)

    @on(Button.Pressed, "#tier-done")
    def _done(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ModelsPane(VerticalScroll):
    """Settings → Models: every tier with its source and resolved model."""

    def __init__(self, client: Any | None, list_models: Any | None = None) -> None:
        super().__init__(id="models")
        self._client, self._list_models = client, list_models
        self._order: list[str] = []
        self._ceiling = "high"

    def compose(self) -> ComposeResult:
        yield Static("Models", classes="settings-heading", markup=False)
        yield Static(ts.MODELS_HELP, classes="settings-help", markup=False)
        yield Vertical(id="models-tiers")
        yield Static("Subagents never run above their highest tier, whatever their own list allows.", classes="settings-help", markup=False)
        with Horizontal(classes="settings-row"):
            yield Static("Highest tier for subagents", classes="settings-label", markup=False)
            yield Button("high", id="models-ceiling")
        yield Static("", id="models-status", markup=False)

    def reload(self) -> None:
        if self._client is not None:
            self.run_worker(self._load(), group="models-load", exclusive=True)

    def _status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#models-status", Static).update(sanitize(text, 200))

    async def _load(self, result: Any = None) -> None:
        try:
            result = result or await self._client.model_tiers()
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Unavailable: {exc}")
            return
        if not self.is_mounted:
            return
        self._order = list(_field(result, "order", []))
        self._ceiling = _field(result, "max_tier", "") or "high"
        box = self.query_one("#models-tiers", Vertical)
        await box.remove_children()
        for row in _tier_rows(result):
            await box.mount(Horizontal(
                Static(sanitize(ts.tier_label(row), 160), classes="settings-label", markup=False),
                Button("Edit models", name=str(_field(row, "name")), classes="models-edit"),
                classes="settings-row",
            ))
        self.query_one("#models-ceiling", Button).label = self._ceiling
        self._status("")

    @on(Button.Pressed, ".models-edit")
    def _edit(self, event: Button.Pressed) -> None:
        tier = event.button.name or ""
        self.app.push_screen(TierEditorScreen(self._client, tier, self._list_models), lambda _: self.reload())

    @on(Button.Pressed, "#models-ceiling")
    def _ceiling_pressed(self) -> None:
        self.run_worker(self._choose_ceiling(), group="models-ceiling", exclusive=True)

    async def _choose_ceiling(self) -> None:
        options = [(f"{tier}{' · selected' if tier == self._ceiling else ''}", tier) for tier in self._order]
        choice = await self.app.push_screen_wait(ChoiceScreen(
            "Highest tier for subagents", options, help_text="Subagents never run above this tier, whatever their own list allows."))
        if not choice:
            return
        try:
            result = await self._client.agent_max_tier_set(choice)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._status(str(exc))
            return
        await self._load(result)


class TitlesPane(VerticalScroll):
    """Settings → Session titles: the on/off switch, the model, and what it does."""

    def __init__(self, client: Any | None, list_models: Any | None = None) -> None:
        super().__init__(id="titles")
        self._client, self._list_models = client, list_models
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Static("Session titles", classes="settings-heading", markup=False)
        yield Static(ts.TITLES_HELP, id="titles-note", classes="settings-help", markup=False)
        with Horizontal(classes="settings-row"):
            yield Static("Generate titles automatically", classes="settings-label", markup=False)
            yield Static("", classes="settings-key", markup=False)
            yield Switch(value=True, id="settings-titles-enabled")
        with Horizontal(classes="settings-row"):
            yield Static("Title model", classes="settings-label", markup=False)
            yield Button("low", id="settings-titles-model")
        yield Static("", id="settings-titles-status", markup=False)

    def reload(self) -> None:
        if self._client is not None:
            self.run_worker(self._load(), group="titles-load", exclusive=True)

    def _status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#settings-titles-status", Static).update(sanitize(text, 200))

    async def _load(self, state: Any = None) -> None:
        try:
            state = state or await self._client.session_title_settings()
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Unavailable: {exc}")
            return
        if not self.is_mounted:
            return
        self._loading = True
        try:
            self.query_one("#settings-titles-enabled", Switch).value = bool(_field(state, "enabled", True))
        finally:
            self._loading = False
        model, resolved = _field(state, "model", "low"), _field(state, "resolved", "")
        self.query_one("#settings-titles-model", Button).label = sanitize(
            f"{model} → {resolved}" if resolved and resolved != model else str(model), 120)
        self.query_one("#titles-note", Static).update("\n".join(ts.titles_explanation(state)))
        self._status("")

    @on(Switch.Changed, "#settings-titles-enabled")
    async def _toggled(self, event: Switch.Changed) -> None:
        if self._loading or self._client is None:
            return
        try:
            state = await self._client.session_title_settings_set(enabled=bool(event.value))
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._status(str(exc))
            return
        await self._load(state)

    @on(Button.Pressed, "#settings-titles-model")
    def _model_pressed(self) -> None:
        self.run_worker(self._choose_model(), group="titles-model", exclusive=True)

    async def _choose_model(self) -> None:
        if self._client is None:
            return
        try:
            tiers = await self._client.model_tiers()
            models = [row for row in (await self._list_models() if self._list_models else [])
                      if isinstance(row, dict) and row.get("provider") and row.get("id")]
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Unavailable: {exc}")
            return
        options = [(f"{_field(row, 'name')} tier" + (f" → {_field(row, 'resolved')}" if _field(row, "resolved") else " · no runnable model"),
                    str(_field(row, "name"))) for row in _tier_rows(tiers)]
        options += [(f"{row.get('name') or row['id']} · {row['provider']}/{row['id']}", f"{row['provider']}/{row['id']}") for row in models]
        choice = await self.app.push_screen_wait(ChoiceScreen(
            "Title model", options,
            help_text="Choose a tier (recommended: low) or one model. Your first message is sent to it to write the title."))
        if not choice:
            return
        try:
            state = await self._client.session_title_settings_set(model=choice)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self._status(str(exc))
            return
        await self._load(state)


class AgentTiersScreen(ModalScreen[list[str] | None]):
    """Check the tiers an agent may use and pick which one is the default."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, order: Sequence[str], current: Sequence[str]) -> None:
        super().__init__()
        self._order = list(order)
        self._tiers = list(current)

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-tiers-dialog"):
            yield Static("Agent tiers", id="agent-tiers-title", markup=False)
            yield Static(ts.TIERS_HELP + "\nAt least one tier stays checked. The first is the default.", id="agent-tiers-help", markup=False)
            for tier in self._order:
                with Horizontal(classes="agent-tier-row"):
                    yield Checkbox(sanitize(tier, 64), value=tier in self._tiers, name=tier, classes="agent-tier-check")
                    yield Button("Make default", name=tier, classes="agent-tier-default")
            yield Static("", id="agent-tiers-error", markup=False)
            with Horizontal(id="agent-tiers-actions"):
                yield Button("Save", id="agent-tiers-save", variant="primary")
                yield Button("Cancel", id="agent-tiers-cancel")

    def _sync(self) -> None:
        for box in self.query(".agent-tier-check"):
            tier = box.name or ""
            box.label = sanitize(tier + (" · default" if self._tiers and self._tiers[0] == tier else ""), 80)
        for button in self.query(".agent-tier-default"):
            tier = button.name or ""
            button.display = tier in self._tiers and self._tiers[0] != tier

    def on_mount(self) -> None:
        self._sync()

    @on(Button.Pressed, ".agent-tier-default")
    def _make_default(self, event: Button.Pressed) -> None:
        self._tiers = ts.make_default_tier(self._tiers, event.button.name or "")
        self._sync()

    @on(Checkbox.Changed, ".agent-tier-check")
    def _changed(self, event: Checkbox.Changed) -> None:
        tier = event.checkbox.name or ""
        want = bool(event.value)
        if want == (tier in self._tiers):
            return
        new, error = ts.toggle_tier(self._tiers, tier, self._order)
        self.query_one("#agent-tiers-error", Static).update(error)
        if error:
            event.checkbox.value = tier in self._tiers  # undo the refused change
            return
        self._tiers = new
        self._sync()

    @on(Button.Pressed, "#agent-tiers-save")
    def _save(self) -> None:
        self.dismiss(list(self._tiers[:MAX_TIERS]) if self._tiers else None)

    @on(Button.Pressed, "#agent-tiers-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
