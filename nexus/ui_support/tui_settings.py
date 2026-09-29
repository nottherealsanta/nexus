"""Full-screen Settings page backed entirely by host inventory commands (plan §4).

A left sidebar lists every configurable area: the shell's own preferences
(Appearance, Layout, Keyboard, Workspace), provider sign-in (Providers, see
``tui_providers``), and the file-backed categories (Agents, Tools, MCP,
Skills, Hooks, Config, Soul). File-backed areas list items
from ``SettingsInventory`` and edit them through ``SettingsRead``/``Write``/
``Delete``, so every write is validated and scoped by the host.

Agents get a "New sessions start with" row that writes ``[agent] name`` to the
selected scope (``AgentDefaultSet``), and a form above the prompt editor: one
model row (provider, model and effort chosen together in the shared
``ModelPickerScreen``) and an ordered list of fallbacks, each picked the same
way. Built-in agents are listed alongside custom ones; saving one
writes an override in the selected scope (``~/.nexus`` by default) and
"Reset to default" moves that override to trash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Static, TextArea
from textual.widgets._option_list import Option

from .agent_frontmatter import (
    MAX_FALLBACKS,
    agent_fields,
    fallback_items,
    set_agent_fields,
)
from .text import sanitize
from .tui_model_picker import ModelPickerScreen
from .tui_panels import SettingsScreen, TuiPreferences
from .tui_providers import ProvidersPane


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


class ConfirmSettingsAction(ModalScreen[bool]):
    """Small confirmation used for deletion and unsaved changes."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-confirm-dialog"):
            yield Static(self.prompt, id="settings-confirm-text", markup=False)
            with Horizontal():
                yield Button("Cancel", id="settings-confirm-no")
                yield Button("Continue", id="settings-confirm-yes", variant="warning")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "settings-confirm-yes")


class SettingsConsole(SettingsScreen):
    """Full-screen Settings: sidebar of areas, list + editor for file areas."""

    #: ``(key, label)``; a ``None`` key is a non-selectable group heading.
    SECTIONS = (
        (None, "GENERAL"),
        ("appearance", "Appearance"), ("layout", "Layout"),
        ("keys", "Keyboard"), ("workspace", "Workspace"),
        (None, "CONFIGURE"),
        ("providers", "Providers"),
        ("agents", "Agents"), ("tools", "Tools"), ("mcp", "MCP servers"),
        ("skills", "Skills"), ("hooks", "Hooks"), ("config", "Config"),
        ("soul", "Soul"),
    )
    GENERAL = ("appearance", "layout", "keys", "workspace")
    FILE_CATEGORIES = ("agents", "tools", "mcp", "skills", "hooks", "config", "soul")
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "attempt_close", "Close"), ("ctrl+s", "save", "Save")
    ]
    _HELP: ClassVar[dict[str, str]] = {
        "agents": "Build is the default root agent; advisor, task and quick are subagents. "
                  "Blank model fields inherit the session model. Fallbacks are tried "
                  "in order when the model fails before replying.",
        "tools": "Python tools loaded from <scope>/tools.",
        "mcp": "MCP servers from mcp.json (mcpServers).",
        "skills": "Skills from <scope>/skills/<name>/SKILL.md.",
        "hooks": "Lifecycle hooks from hooks.toml.",
        "config": "Nexus configuration: models, fallbacks, permissions, context and more.",
        "soul": "Instructions added to every conversation (SOUL.md).",
    }

    def __init__(
        self,
        preferences: TuiPreferences,
        themes: list[tuple[str, str]],
        shortcuts: Iterable[tuple[str, str]],
        workspace: str,
        *,
        inventory: Callable[[str], Awaitable[Any]],
        read: Callable[[str, str, str], Awaitable[Any]],
        write: Callable[..., Awaitable[Any]],
        delete: Callable[[str, str, str], Awaitable[Any]],
        category: str = "appearance",
        list_agents: Callable[[], Awaitable[Any]] | None = None,
        default_agent: Callable[[], Awaitable[str]] | None = None,
        set_default_agent: Callable[[str, str], Awaitable[Any]] | None = None,
        list_models: Callable[[], Awaitable[Any]] | None = None,
        providers: Any | None = None,
    ) -> None:
        super().__init__(preferences, themes, shortcuts, workspace)
        self._inventory_call = inventory
        self._read_call = read
        self._write_call = write
        self._delete_call = delete
        self._list_agents = list_agents
        self._default_agent = default_agent
        self._set_default_agent = set_default_agent
        #: A host client exposing the ``provider_*`` calls (Settings → Providers).
        self._providers = providers
        self._list_models = list_models
        keys = [key for key, _ in self.SECTIONS if key]
        category = "appearance" if category == "general" else category
        self.category = category if category in keys else "appearance"
        #: Edits land in ~/.nexus unless the user picks the project scope.
        self.scope = "global"
        self._items: list[Any] = []
        self._visible_items: list[Any] = []
        self._current_id = ""
        self._sha: str | None = None
        self._saved_body = ""
        self._builtin = False
        self._overrides_builtin = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-console"):
            with Vertical(id="settings-nav"):
                yield Static("Settings", id="settings-title", markup=False)
                yield OptionList(*(
                    Option(label, disabled=key is None) for key, label in self.SECTIONS
                ), id="settings-sections")
            with ContentSwitcher(initial="appearance", id="settings-panes"):
                yield from self.compose_general_panes()
                yield ProvidersPane(self._providers)
                with Vertical(id="settings-file-pane"):
                    yield Static("", id="settings-file-heading", classes="settings-heading", markup=False)
                    yield Static("", id="settings-file-help", classes="settings-help", markup=False)
                    with Horizontal(id="settings-scope-row"):
                        yield Button("global", id="settings-scope-global")
                        yield Button("project", id="settings-scope-project")
                        yield Static("~/.nexus", id="settings-scope-path", markup=False)
                    with Horizontal(id="settings-default-agent-row"):
                        yield Static("New sessions start with", id="settings-default-agent-label", markup=False)
                        yield Horizontal(id="settings-default-agent-choices")
                        yield Static("", id="settings-default-agent-note", markup=False)
                    with Horizontal(id="settings-file-body"):
                        with Vertical(id="settings-file-list-pane"):
                            yield Button("✚ New", id="settings-new")
                            yield Input(placeholder="Name for new item", id="settings-new-name")
                            yield OptionList(id="settings-file-list")
                        with Vertical(id="settings-file-editor-pane"):
                            yield Static("Select an item", id="settings-file-title", markup=False)
                            with Vertical(id="agent-form"):
                                with Horizontal(classes="agent-form-row"):
                                    yield Static("Model", classes="agent-form-label", markup=False)
                                    yield Button("", id="agent-model", classes="agent-form-pick")
                                    yield Button("×", id="agent-model-clear", classes="agent-form-remove")
                                for index in range(MAX_FALLBACKS):
                                    with Horizontal(classes="agent-form-row agent-fallback-row"):
                                        yield Static("Fallbacks" if index == 0 else "", classes="agent-form-label", markup=False)
                                        yield Button("", id=f"agent-fallback-{index}", classes="agent-form-pick")
                                        yield Button("×", id=f"agent-fallback-remove-{index}", classes="agent-form-remove")
                                with Horizontal(classes="agent-form-row", id="agent-fallback-add-row"):
                                    yield Static("", id="agent-fallback-add-label", classes="agent-form-label", markup=False)
                                    yield Button("+ Add fallback", id="agent-fallback-add", classes="agent-form-pick")
                            yield TextArea(id="settings-file-editor", soft_wrap=True)
                            yield Static("", id="settings-file-status", markup=False)
                            with Horizontal(id="settings-file-actions"):
                                yield Button("Save", id="settings-save", variant="warning")
                                yield Button("Revert", id="settings-revert")
                                yield Button("Delete", id="settings-delete")
            yield Static(
                "↑↓ section · n new · e edit · d delete · g scope · ctrl+s save · esc close",
                id="settings-console-hint", markup=False,
            )

    def on_mount(self) -> None:
        # The base ``on_mount`` also runs (Textual walks the MRO); it highlights
        # ``initial_section_index`` and focuses the sidebar.
        self.query_one("#settings-new-name", Input).display = False
        self._show_category(self.category)
        self.run_worker(self._load_inventory(), group="settings-inventory", exclusive=True)

    def initial_section_index(self) -> int:
        return self._section_index(self.category)

    def _section_index(self, key: str) -> int:
        return next(i for i, (k, _) in enumerate(self.SECTIONS) if k == key)

    @property
    def dirty(self) -> bool:
        return self._current_id != "" and self.query_one("#settings-file-editor", TextArea).text != self._saved_body

    def _show_category(self, category: str) -> None:
        self.category = category
        file_area = category in self.FILE_CATEGORIES
        self.query_one("#settings-panes", ContentSwitcher).current = (
            "settings-file-pane" if file_area else category
        )
        if file_area:
            label = {k: v for k, v in self.SECTIONS if k}[category]
            self.query_one("#settings-file-heading", Static).update(label)
            self.query_one("#settings-file-help", Static).update(self._HELP.get(category, ""))
            self._clear_editor()
        if category == "providers":
            self.query_one(ProvidersPane).reload()
        row = self.query_one("#settings-default-agent-row")
        row.display = category == "agents" and self._default_agent is not None
        if row.display:
            self.run_worker(self._load_default_agent(), group="settings-default-agent", exclusive=True)
        self._render_items()

    async def _load_default_agent(self, note: str = "") -> None:
        """Offer every root-capable agent; the current default is highlighted."""
        if self._list_agents is None or self._default_agent is None:
            return
        try:
            rows, current = await self._list_agents(), await self._default_agent()
        except Exception as exc:  # noqa: BLE001 - show host/transport result
            self.query_one("#settings-default-agent-note", Static).update(f"unavailable · {sanitize(str(exc), 80)}")
            return
        if not self.is_mounted:
            return
        names = [
            sanitize(str(_field(row, "name", "")), 40) for row in rows
            if _field(row, "name") and "root" in (_field(row, "contexts", None) or ("root",))
        ][:16]
        names.sort(key=lambda name: (name != "build", name.casefold()))
        choices = self.query_one("#settings-default-agent-choices", Horizontal)
        await choices.remove_children()
        await choices.mount_all(
            Button(name, name=name, classes="settings-default-agent" + (" -current" if name == current else ""))
            for name in names
        )
        self.query_one("#settings-default-agent-note", Static).update(note)

    async def _choose_default_agent(self, name: str) -> None:
        if self._set_default_agent is None:
            return
        try:
            result = await self._set_default_agent(name, self.scope)
        except Exception as exc:  # noqa: BLE001 - host validation error
            self.query_one("#settings-default-agent-note", Static).update(sanitize(str(exc), 120))
            return
        where = "~/.nexus" if self.scope == "global" else "<project>/.agents"
        path = f"{where}/{_field(result, 'rel_path', '')}"
        effective = str(_field(result, "effective", name))
        note = f"saved to {path}" if effective == name else f"saved to {path} · {effective} still applies (project config)"
        await self._load_default_agent(sanitize(note, 120))
        hook = getattr(self.app, "_default_agent_changed", None)
        if callable(hook):
            hook()

    def _clear_editor(self) -> None:
        self._current_id = ""
        self._sha = None
        self._saved_body = ""
        self._builtin = self._overrides_builtin = False
        self.query_one("#settings-file-editor", TextArea).text = ""
        self.query_one("#settings-file-title", Static).update("Select an item")
        self._sync_agent_form()
        self._sync_actions()
        self._status("")

    async def _load_inventory(self) -> None:
        try:
            result = await self._inventory_call(self.scope)
        except Exception as exc:  # noqa: BLE001 - show host validation/transport result
            self._status(f"Inventory unavailable: {sanitize(str(exc), 160)}")
            return
        if not self.is_mounted:
            return
        self._items = list(_field(result, "items", ()))[:512]
        self.query_one("#settings-scope-path", Static).update(
            str(_field(result, "root_display", "~/.nexus" if self.scope == "global" else "<project>/.agents"))
        )
        for button in ("global", "project"):
            self.query_one(f"#settings-scope-{button}", Button).set_class(button == self.scope, "-current")
        counts = {str(_field(row, "key")): int(_field(row, "count", 0)) for row in _field(result, "categories", ())}
        sections = self.query_one("#settings-sections", OptionList)
        for index, (key, label) in enumerate(self.SECTIONS):
            if key in self.FILE_CATEGORIES:
                count = counts.get(key)
                sections.replace_option_prompt_at_index(index, f"{label}  {count}" if count else label)
        self._render_items()

    def _render_items(self) -> None:
        if not self.is_mounted:
            return
        listing = self.query_one("#settings-file-list", OptionList)
        listing.clear_options()
        rows = [row for row in self._items if _field(row, "category") == self.category]
        # Agents: build (root) first, then built-in subagents, then custom ones.
        order = {"build": 0, "orchestrator": 1, "advisor": 2, "task": 3, "quick": 4}
        rows.sort(key=lambda row: (order.get(str(_field(row, "id", "")), 9), str(_field(row, "id", "")).casefold()))
        self._visible_items = rows
        listing.add_options([Option(self._item_label(row)) for row in rows])

    @staticmethod
    def _item_label(row: Any) -> str:
        name = sanitize(str(_field(row, "label", _field(row, "id", ""))), 60)
        if _field(row, "builtin", False):
            return f"{name}  · built-in"
        if _field(row, "overrides_builtin", False):
            return f"{name}  · edited"
        return name

    async def _open_item(self, index: int) -> None:
        if not 0 <= index < len(self._visible_items):
            return
        if self.dirty and not await self.app.push_screen_wait(ConfirmSettingsAction("Discard unsaved changes?")):
            return
        item = self._visible_items[index]
        item_id = str(_field(item, "id", ""))
        try:
            result = await self._read_call(self.scope, self.category, item_id)
        except Exception as exc:  # noqa: BLE001 - host returns a safe error
            self._status(sanitize(str(exc), 160))
            return
        self._current_id = item_id
        self._sha = _field(result, "sha256")
        self._builtin = bool(_field(result, "builtin", False))
        self._overrides_builtin = bool(_field(result, "overrides_builtin", False))
        self._saved_body = str(_field(result, "body", ""))
        editor = self.query_one("#settings-file-editor", TextArea)
        editor.read_only = False
        editor.text = self._saved_body
        self.query_one("#settings-file-title", Static).update(str(_field(result, "rel_path", item_id)))
        self._sync_agent_form()
        self._sync_actions()
        where = "~/.nexus" if self.scope == "global" else "<project>/.agents"
        self._status(f"Built-in default · saving writes an override to {where}" if self._builtin else "")

    def _sync_agent_form(self) -> None:
        """Show the agent form for agents and fill it from the editor's frontmatter."""
        form = self.query_one("#agent-form", Vertical)
        form.display = self.category == "agents" and bool(self._current_id)
        if not form.display:
            return
        fields = agent_fields(self.query_one("#settings-file-editor", TextArea).text)
        model = self._model_reference(fields)
        effort = fields.get("reasoning_effort", "")
        label = f"{model} · {effort}" if model and effort else model or (
            f"inherit session model · {effort}" if effort else "inherit session model"
        )
        self.query_one("#agent-model", Button).label = sanitize(label, 120)
        self.query_one("#agent-model-clear", Button).display = bool(model or effort)
        fallbacks = fallback_items(fields.get("fallback", ""))
        for index, row in enumerate(self.query(".agent-fallback-row")):
            row.display = index < len(fallbacks)
            if row.display:
                self.query_one(f"#agent-fallback-{index}", Button).label = sanitize(fallbacks[index], 120)
        self.query_one("#agent-fallback-add-label", Static).update("" if fallbacks else "Fallbacks")
        self.query_one("#agent-fallback-add-row").display = len(fallbacks) < MAX_FALLBACKS

    @staticmethod
    def _model_reference(fields: Mapping[str, str]) -> str:
        """``provider/model`` when both are set separately, else the raw model value."""
        model, provider = fields.get("model", ""), fields.get("provider", "")
        if model and provider and "/" not in model:
            return f"{provider}/{model}"
        return model or (f"{provider}/…" if provider else "")

    def _set_fields(self, updates: Mapping[str, str]) -> None:
        editor = self.query_one("#settings-file-editor", TextArea)
        updated = set_agent_fields(editor.text, updates)
        if updated != editor.text:
            editor.text = updated
        self._sync_agent_form()

    def _fallbacks(self) -> list[str]:
        text = self.query_one("#settings-file-editor", TextArea).text
        return fallback_items(agent_fields(text).get("fallback", ""))

    async def _pick_model(self, target: str) -> None:
        """Open the shared model picker; ``target`` is ``model``, ``add``, or a fallback index."""
        if self._list_models is None or not self._current_id:
            return
        try:
            rows = [dict(row) for row in await self._list_models()
                    if isinstance(row, Mapping) and row.get("provider") and row.get("id")]
        except Exception as exc:  # noqa: BLE001 - host/transport error
            self._status(f"Models unavailable: {sanitize(str(exc), 120)}")
            return
        if not self.is_mounted:
            return
        fields = agent_fields(self.query_one("#settings-file-editor", TextArea).text)
        fallbacks = self._fallbacks()
        index = int(target) if target.isdigit() else None
        effort = (fields.get("reasoning_effort") or None) if target == "model" else None
        current = (self._model_reference(fields) if target == "model"
                   else fallbacks[index] if index is not None and index < len(fallbacks) else "")
        prefs = self._prefs
        choice = await self.app.push_screen_wait(ModelPickerScreen(
            rows, current=current, current_effort=effort,
            stored_override=effort, effort_source=None,
            favorites=prefs["model_favorites"], recent=prefs["model_recent"],
            on_favorites=lambda refs: prefs.set("model_favorites", refs),
        ))
        if choice is None or not self.is_mounted or not self._current_id:
            return
        ref, picked_effort, committed = choice
        if target == "model":
            supported = next((row.get("supported_efforts") or () for row in rows
                              if f"{row['provider']}/{row['id']}" == ref), ())
            # Keep the saved effort unless the picker changed it or the new model lacks it.
            new_effort = (picked_effort or "") if committed else (effort if effort in supported else "")
            self._set_fields({"model": ref, "provider": "", "reasoning_effort": new_effort or ""})
            return
        fallbacks = self._fallbacks()
        if index is not None and index < len(fallbacks):
            fallbacks[index] = ref
        elif ref not in fallbacks:
            fallbacks.append(ref)
        self._set_fields({"fallback": ", ".join(fallbacks[:MAX_FALLBACKS])})

    def _agent_form_pressed(self, button_id: str) -> bool:
        """Handle an agent form button; ``False`` when ``button_id`` is not one."""
        if button_id == "agent-model-clear":
            self._set_fields({"model": "", "provider": "", "reasoning_effort": ""})
            return True
        if button_id.startswith("agent-fallback-remove-"):
            index = int(button_id.rsplit("-", 1)[1])
            fallbacks = self._fallbacks()
            if index < len(fallbacks):
                del fallbacks[index]
                self._set_fields({"fallback": ", ".join(fallbacks)})
            return True
        if button_id == "agent-model":
            target = "model"
        elif button_id == "agent-fallback-add":
            target = "add"
        elif button_id.startswith("agent-fallback-"):
            target = button_id.rsplit("-", 1)[1]
        else:
            return False
        self.run_worker(self._pick_model(target), group="settings-model-pick", exclusive=True)
        return True

    def _sync_actions(self) -> None:
        delete = self.query_one("#settings-delete", Button)
        delete.label = "Reset to default" if self._overrides_builtin else "Delete"
        delete.disabled = not self._current_id or self._builtin

    @on(TextArea.Changed, "#settings-file-editor")
    def _editor_changed(self) -> None:
        # Hand edits to the frontmatter show up in the form.
        if self.category == "agents" and self._current_id:
            self._sync_agent_form()

    def _status(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#settings-file-status", Static).update(text)

    async def action_save(self) -> None:
        if not self._current_id or self.category not in self.FILE_CATEGORIES:
            return
        body = self.query_one("#settings-file-editor", TextArea).text
        try:
            result = await self._write_call(
                self.scope, self.category, self._current_id, body,
                expected_sha256=self._sha,
            )
        except Exception as exc:  # noqa: BLE001 - show validation/conflict in editor
            self._status(sanitize(str(exc), 200))
            return
        if _field(result, "status") == "conflict":
            self._status("Changed on disk since it was opened · reopen to edit")
            return
        self._sha = _field(result, "sha256")
        self._saved_body = body
        self._overrides_builtin = self._overrides_builtin or self._builtin
        self._builtin = False
        self._sync_actions()
        self._status(
            f"Saved · {len(_field(result, 'loaded', ()))} loaded · "
            f"{len(_field(result, 'failed', ()))} failed"
        )
        await self._load_inventory()
        self._refresh_app()

    def _refresh_app(self) -> None:
        for hook in ("_sync_agent_colors", "_start_context_preview"):
            method = getattr(self.app, hook, None)
            if callable(method):
                method()

    async def _delete_current(self) -> None:
        if not self._current_id or self._builtin:
            return
        prompt = (
            f"Reset {self._current_id} to the built-in default? Your edits move to trash."
            if self._overrides_builtin else f"Delete {self._current_id} to trash?"
        )
        if not await self.app.push_screen_wait(ConfirmSettingsAction(prompt)):
            return
        try:
            await self._delete_call(self.scope, self.category, self._current_id)
        except Exception as exc:  # noqa: BLE001 - host returns a safe error
            self._status(sanitize(str(exc), 200))
            return
        reset = self._overrides_builtin
        self._clear_editor()
        self._status("Reset to built-in default" if reset else "Deleted to trash")
        await self._load_inventory()
        self._refresh_app()

    async def _attempt_close(self) -> None:
        if self.dirty and not await self.app.push_screen_wait(ConfirmSettingsAction("Discard unsaved changes?")):
            return
        self.dismiss()

    def action_attempt_close(self) -> None:
        self.run_worker(self._attempt_close(), group="settings-close", exclusive=True)

    async def _change_category(self, category: str) -> None:
        if category == self.category:
            return
        if self.dirty and not await self.app.push_screen_wait(ConfirmSettingsAction("Discard unsaved changes?")):
            self.query_one("#settings-sections", OptionList).highlighted = self._section_index(self.category)
            return
        self._show_category(category)

    async def _change_scope(self, scope: str) -> None:
        if scope == self.scope:
            return
        if self.dirty and not await self.app.push_screen_wait(ConfirmSettingsAction("Discard unsaved changes?")):
            return
        self.scope = scope
        self._clear_editor()
        await self._load_inventory()
        if self.category == "agents":
            await self._load_default_agent()

    def show_section(self, index: int) -> None:
        key = self.SECTIONS[index][0]
        if key:
            self.run_worker(self._change_category(key), group="settings-category", exclusive=True)

    def enter_section(self, index: int) -> None:
        key = self.SECTIONS[index][0]
        if key == "providers":
            next(iter(self.query_one(ProvidersPane).query("Button, Input"))).focus()
        elif key in self.GENERAL:
            pane = self.query_one(f"#{key}")
            target = next(iter(pane.query("RadioSet, Switch")), None)
            (target or pane).focus()
        elif key:
            self.query_one("#settings-file-list", OptionList).focus()

    def on_key(self, event) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        file_area = self.category in self.FILE_CATEGORIES
        if event.key == "g" and file_area:
            self.run_worker(
                self._change_scope("project" if self.scope == "global" else "global"),
                group="settings-scope", exclusive=True,
            )
        elif event.key == "n" and file_area:
            field = self.query_one("#settings-new-name", Input)
            field.display = True
            field.focus()
        elif event.key == "e" and self._current_id:
            self.query_one("#settings-file-editor", TextArea).focus()
        elif event.key == "d" and self._current_id:
            self.run_worker(self._delete_current(), group="settings-delete", exclusive=True)
        else:
            return
        event.stop()
        event.prevent_default()

    @on(OptionList.OptionSelected, "#settings-file-list")
    def _item_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index is not None:
            self.run_worker(self._open_item(event.option_index), group="settings-read", exclusive=True)

    @on(Input.Submitted, "#settings-new-name")
    def _new_named(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not name:
            return
        self._current_id = name
        self._sha = None
        self._saved_body = ""
        self._builtin = self._overrides_builtin = False
        self.query_one("#settings-file-title", Static).update(name)
        editor = self.query_one("#settings-file-editor", TextArea)
        editor.read_only = False
        editor.text = (
            f"---\nname: {name}\ndescription: Describe when the root agent should use {name}.\n"
            "contexts: [subagent]\n---\nYou are a subagent. Do the task you are given and "
            "finish with a report that lists every file you changed.\n"
            if self.category == "agents" else ""
        )
        self.query_one("#settings-new-name", Input).display = False
        self._sync_agent_form()
        self._sync_actions()
        editor.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if self._agent_form_pressed(button_id):
            return
        if event.button.has_class("settings-default-agent") and event.button.name:
            self.run_worker(
                self._choose_default_agent(event.button.name),
                group="settings-default-agent", exclusive=True,
            )
        elif button_id.startswith("settings-scope-"):
            self.run_worker(
                self._change_scope(button_id.removeprefix("settings-scope-")),
                group="settings-scope", exclusive=True,
            )
        elif button_id == "settings-new":
            field = self.query_one("#settings-new-name", Input)
            field.display = True
            field.focus()
        elif button_id == "settings-save":
            self.run_worker(self.action_save(), group="settings-save", exclusive=True)
        elif button_id == "settings-revert":
            self.query_one("#settings-file-editor", TextArea).text = self._saved_body
            self._sync_agent_form()
        elif button_id == "settings-delete":
            self.run_worker(self._delete_current(), group="settings-delete", exclusive=True)
