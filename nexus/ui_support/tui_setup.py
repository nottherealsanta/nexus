"""First-run provider and global-model setup screen (plan section 7)."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea

from ..client.protocol import Client, ClientError
from ..host import protocol as p
from .text import sanitize


async def open_first_run_setup(app, client: Client) -> bool:
    """Open setup after bootstrap when this daemon reports first-run state."""
    try:
        status = await client.setup_status()
    except (ClientError, AssertionError):
        return False
    if status.required:
        app.push_screen(SetupScreen(client, status))
    return status.required


def block_unconfigured_turn(app, content: str) -> bool:
    """Keep the draft until a global provider route is available after restart."""
    if not getattr(app, "_setup_required", False):
        return False
    app.query_one("#chat-editor", TextArea).text = content
    app._sync_status("Connect a provider and restart the daemon", error=True)
    return True


class SetupScreen(ModalScreen[None]):
    """Choose a connected provider and save its model as the global default."""

    def __init__(self, client: Client, status: p.SetupStatusResult | None = None) -> None:
        super().__init__()
        self.client = client
        self.status = status
        self.providers: list[dict] = []
        self.models: list[dict] = []
        self.provider_id = ""
        self.model_id = ""
        self._saved = False

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-dialog"):
            yield Static("First-run setup", id="setup-title")
            yield Static("Choose a provider, then select a model for the global default.", id="setup-intro")
            yield Static("Providers", classes="setup-label")
            yield OptionList(id="setup-providers")
            yield Static("", id="setup-instruction")
            yield Input(placeholder="Filter models…", id="setup-search")
            yield OptionList(id="setup-models")
            yield Static("", id="setup-message")
            with Horizontal(id="setup-buttons"):
                yield Button("Retry status", id="setup-retry")
                yield Button("Save global default", id="setup-save", variant="primary", disabled=True)
                yield Button("Close", id="setup-close")

    async def on_mount(self) -> None:
        if self.status is None:
            await self.refresh_status()
        else:
            self._apply_status(self.status)

    async def refresh_status(self) -> None:
        message = self.query_one("#setup-message", Static)
        message.update("Checking provider status…")
        try:
            status = await self.client.setup_status()
        except (ClientError, AssertionError):
            message.update("Setup status unavailable. Check daemon compatibility, then retry.")
            return
        self._apply_status(status)

    def _apply_status(self, status: p.SetupStatusResult) -> None:
        message = self.query_one("#setup-message", Static)
        self.status = status
        self.providers = [row for row in status.providers if isinstance(row, dict)][:32]
        self.models = [row for row in status.models if isinstance(row, dict)][:512]
        connected = {str(row.get("id")) for row in self.providers if row.get("connected") is True}
        available = {str(row.get("provider")) for row in self.models}
        preferred = status.global_model.partition("/")[0]
        self.provider_id = (preferred if preferred in {str(row.get("id")) for row in self.providers} else
                            next((str(row.get("id")) for row in self.providers
                                  if str(row.get("id")) in connected and str(row.get("id")) in available),
                                 str(self.providers[0].get("id", "")) if self.providers else ""))
        self.model_id = ""
        self._show_providers()
        self._show_models()
        if status.global_model:
            message.update(f"Global default: {sanitize(status.global_model, 120)}")
        else:
            message.update("Select a connected provider and model to continue.")

    def _show_providers(self) -> None:
        options = self.query_one("#setup-providers", OptionList)
        options.clear_options()
        for row in self.providers:
            provider = str(row.get("id", ""))
            label = sanitize(row.get("label", provider), 80)
            state = "connected" if row.get("connected") is True else "not connected"
            options.add_option(f"{label} · {state}")
        selected = next((i for i, row in enumerate(self.providers)
                         if str(row.get("id", "")) == self.provider_id), None)
        options.highlighted = selected
        self._show_instruction()

    def _show_instruction(self) -> None:
        row = next((item for item in self.providers if str(item.get("id", "")) == self.provider_id), {})
        instruction = sanitize(row.get("instruction", ""), 300)
        self.query_one("#setup-instruction", Static).update(instruction or "")

    def _show_models(self) -> None:
        search = self.query_one("#setup-search", Input).value.casefold().strip()
        matches = [row for row in self.models
                   if str(row.get("provider", "")) == self.provider_id
                   and (not search or search in str(row.get("id", "")).casefold()
                        or search in str(row.get("name", "")).casefold())]
        options = self.query_one("#setup-models", OptionList)
        options.clear_options()
        for row in matches:
            name = sanitize(row.get("name") or row.get("id", "?"), 100)
            model_id = sanitize(row.get("id", ""), 120)
            date = sanitize(row.get("date", ""), 24)
            options.add_option(f"{name} · {model_id}" + (f" · {date}" if date else ""))
        options.highlighted = 0 if matches else None
        self.model_id = ""
        self.query_one("#setup-save", Button).disabled = True

    @on(OptionList.OptionSelected, "#setup-providers")
    def provider_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= len(self.providers):
            return
        self.provider_id = str(self.providers[event.option_index].get("id", ""))
        self._show_instruction()
        self._show_models()

    @on(OptionList.OptionSelected, "#setup-models")
    def model_selected(self, event: OptionList.OptionSelected) -> None:
        rows = [row for row in self.models if str(row.get("provider", "")) == self.provider_id]
        search = self.query_one("#setup-search", Input).value.casefold().strip()
        rows = [row for row in rows if not search or search in str(row.get("id", "")).casefold()
                or search in str(row.get("name", "")).casefold()]
        if event.option_index >= len(rows):
            return
        self.model_id = str(rows[event.option_index].get("id", ""))
        connected = any(str(row.get("id", "")) == self.provider_id and row.get("connected") is True
                        for row in self.providers)
        self.query_one("#setup-save", Button).disabled = not (connected and bool(self.model_id))

    @on(Input.Changed, "#setup-search")
    def search_changed(self, event: Input.Changed) -> None:
        if event.input.id != "setup-search":
            return
        self._show_models()

    @on(Button.Pressed, "#setup-retry")
    async def retry_pressed(self, _: Button.Pressed) -> None:
        await self.refresh_status()

    @on(Button.Pressed, "#setup-save")
    async def save_pressed(self, _: Button.Pressed) -> None:
        if not self.model_id or not any(str(row.get("id", "")) == self.provider_id
                                        and row.get("connected") is True for row in self.providers):
            self.query_one("#setup-message", Static).update("Connect the selected provider before saving.")
            return
        try:
            result = await self.client.setup_save(self.provider_id, self.model_id)
        except ClientError as exc:
            self.query_one("#setup-message", Static).update(f"Could not save setup: {sanitize(str(exc), 240)}")
            return
        self.query_one("#setup-message", Static).update(
            f"Saved {sanitize(result.global_model, 120)} as the global default.\n"
            "Please restart the daemon after turns finish to apply the new runtime configuration."
        )
        self.query_one("#setup-save", Button).disabled = True

    @on(Button.Pressed, "#setup-close")
    def close_pressed(self, _: Button.Pressed) -> None:
        self.dismiss(None)


__all__ = ["SetupScreen", "block_unconfigured_turn", "open_first_run_setup"]
