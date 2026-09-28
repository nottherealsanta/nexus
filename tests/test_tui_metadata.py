"""Truthful runtime metadata shown by the TUI header."""

from __future__ import annotations

from textual.app import App, ComposeResult

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelParams,
    ModelSection,
    PermissionsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.ui.tui.widgets import RootAgentBar, agent_color, context_usage
from nexus.view import initial_state


class _HeaderApp(App[None]):
    def compose(self) -> ComposeResult:
        yield RootAgentBar(id="root-agent")


def _config(*, model: str | None, thinking_budget: int | None = None) -> Config:
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(
                default=model,
                params=ModelParams(thinking_budget=thinking_budget),
            ),
            permissions=PermissionsSection(mode="allow"),
        ),
    )


async def test_host_reports_resolved_config_model_and_distinct_thinking_budget(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=_config(model="scripted/configured-model", thinking_budget=4096),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    try:
        result = await HostFacade(runtime).handle(p.AgentCurrent(session="s"))
        assert isinstance(result, p.AgentCurrentResult)
        # A legacy ``general`` config resolves to the built-in root agent.
        assert (result.name, result.source) == ("build", "config")
        assert (result.provider, result.model) == ("scripted", "configured-model")
        assert result.reasoning_effort is None
        assert result.thinking_budget == 4096
    finally:
        await runtime.aclose()


async def test_host_reports_session_model_override_and_unconfigured_metadata(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=_config(model=None),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    facade = HostFacade(runtime)
    try:
        unconfigured = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(unconfigured, p.AgentCurrentResult)
        assert (unconfigured.provider, unconfigured.model) == (None, None)
        assert unconfigured.reasoning_effort is None
        assert unconfigured.thinking_budget is None

        selected = await facade.handle(p.ModelSelect(session="s", ref="scripted/selected-model"))
        assert isinstance(selected, p.ModelSelectResult)
        result = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(result, p.AgentCurrentResult)
        assert (result.provider, result.model) == ("scripted", "selected-model")
        assert result.reasoning_effort is None
        assert result.thinking_budget is None
    finally:
        await runtime.aclose()


async def test_agent_metadata_is_compact_optional_and_color_scoped():
    async with _HeaderApp().run_test() as pilot:
        header = pilot.app.query_one("#root-agent", RootAgentBar)
        header.set_agent(
            "general",
            "config",
            "idle",
            color="#12ab34",
            provider="scripted",
            model="configured-model",
            reasoning_effort=None,
            thinking_budget=4096,
        )
        rendered = header.summary()
        assert rendered.plain == "general  ·  configured-model  ·  scripted  ·  unknown"
        assert "#12ab34" in str(rendered.spans[0].style)
        assert all("idle" not in str(span.style) for span in rendered.spans)

        header.set_agent("general", "default", "idle")
        assert header.summary().plain == "general  ·  Default  ·  unknown"


async def test_agent_metadata_handles_partial_fields_and_host_color_fallback():
    async with _HeaderApp().run_test() as pilot:
        header = pilot.app.query_one("#root-agent", RootAgentBar)
        header.set_agent("custom-agent", "default", provider="vendor")
        rendered = header.summary()
        assert rendered.plain == "custom-agent  ·  Default  ·  vendor  ·  unknown"
        assert agent_color("custom-agent") in str(rendered.spans[0].style)
        assert agent_color("custom-agent") == agent_color("CUSTOM-AGENT")


async def test_agent_metadata_hides_effort_source_and_keeps_provider_non_italic():
    async with _HeaderApp().run_test() as pilot:
        header = pilot.app.query_one("#root-agent", RootAgentBar)
        header.set_effort_metadata(supported_levels=["xhigh"], effort_source="session")
        header.set_agent(
            "build", provider="openai", model="gpt-6-luna", reasoning_effort="xhigh"
        )

        assert header.summary().plain == "build  ·  GPT-6 Luna  ·  OpenAI  ·  xhigh"
        provider = header.query_one("#root-provider")
        assert provider.render().plain == "OpenAI"
        assert not provider.rich_style.italic


def test_context_metadata_only_uses_present_integer_values():
    view = initial_state("s")
    assert context_usage(view) == "0 (0%)"
    view.context = {"context": {"used_tokens": 23_500, "input_budget": 100_000}}
    assert context_usage(view) == "24k (24%)"
    view.context = {"context": {"used_tokens": 23_500}}
    assert context_usage(view) == "24k (0%)"
