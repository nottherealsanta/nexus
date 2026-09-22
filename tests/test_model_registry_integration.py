"""Phase 5.5 integration: config, registry, tiers, router, runtime, and loop.

Plan section 15. The unit contracts live in ``test_model_registry.py`` and
``test_model_tiers.py``; this module proves the pieces compose:

* the canonical ``[models]`` section (with compatibility ``[model]`` and
  conflict rejection) preserves v1;
* a :class:`~nexus.runtime.Runtime` owns a registry, loads it cache-first with
  no network, and resolves tier/custom/provider/bare references through it;
* the registry's capabilities are authoritative while the adapter keeps its
  transport-only fields;
* a provider rejection of a registry-claimed capability degrades the turn:
  exactly one retry with the feature disabled, ``context.degraded`` and
  ``registry.mismatch`` emitted, and never a retry after output has streamed or
  for a refusal;
* the vendored snapshot is MIT-labelled with a packaged notice and no logos.

Everything is offline: catalogues come from the fixture or the vendored
snapshot, and no test constructs a network fetcher.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import msgspec
import pytest

from nexus.config import Config
from nexus.config.schema import (
    ConfigV2,
    ModelSection,
    ModelsSection,
    ProviderSection,
)
from nexus.core.cancel import CancelToken
from nexus.core.loop import ResolvedModel, run_turn
from nexus.core.turn import TurnState
from nexus.errors import ConfigError
from nexus.events import EVENT_TYPES, REGISTRY_EVENTS, Event
from nexus.model.capabilities import Capabilities, CapabilityRejected
from nexus.model.message import Message
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
)
from nexus.model.registry import (
    ModelInfo,
    ModelRegistry,
    RegistryStatus,
    parse_catalogue,
)
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter
from nexus.model.stream import TextDelta
from nexus.model.tiers import TierTable
from nexus.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "models"
CATALOGUE_PATH = FIXTURES / "catalogue.json"
MISSING = FIXTURES / "missing.json"
DATA_DIR = REPO_ROOT / "nexus" / "model" / "data"

ANTHROPIC_ENV = {"ANTHROPIC_API_KEY": "test-key"}
DAY = 86_400.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def catalogue_bytes() -> bytes:
    return CATALOGUE_PATH.read_bytes()


def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir(exist_ok=True)
    return path


def write_v2(workspace: Path, body: str) -> None:
    (workspace / "nexus.toml").write_text(
        "config_version = 2\n" + body, encoding="utf-8"
    )


def make_registry(
    *,
    env: dict[str, str] | None = None,
    providers: dict | None = None,
    tiers: TierTable | None = None,
) -> ModelRegistry:
    registry = ModelRegistry(
        env=env or {},
        providers=providers,
        snapshot_path=MISSING,
        tier_table=tiers,
    )
    registry.install_raw(catalogue_bytes(), source="catalogue")
    return registry


class CapsProvider:
    """A provider whose only job is to report capabilities."""

    def __init__(self, name: str, capabilities: Capabilities) -> None:
        self.name = name
        self._capabilities = capabilities

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities


def models_config(**kwargs) -> Config:
    return Config(model="anthropic/claude-opus-5", version=2, v2=ConfigV2(**kwargs))


# ---------------------------------------------------------------------------
# Config: canonical [models], compatibility [model], v1 preservation
# ---------------------------------------------------------------------------


def test_models_section_is_canonical(tmp_path):
    write_v2(
        tmp_path,
        """
[models]
default = "medium"
refresh_ttl_days = 3
catalogue_url = "https://example.test/api.json"
offline = true

[models.tiers]
high = ["anthropic/claude-opus-5"]
medium = ["anthropic/claude-sonnet-5"]
low = ["anthropic/claude-haiku-4-5"]
""",
    )
    config = Config.load(tmp_path, home=home(tmp_path), environ={})
    assert config.version == 2
    models = config.v2.models
    assert models.default == "medium"
    assert models.refresh_ttl_days == 3
    assert models.catalogue_url == "https://example.test/api.json"
    assert models.offline is True
    assert models.tiers["medium"] == ["anthropic/claude-sonnet-5"]
    assert config.model == "medium"
    assert config.v2.models_configured() is True


def test_model_section_compatibility_still_loads(tmp_path):
    write_v2(tmp_path, '[model]\ndefault = "anthropic/claude-opus-5"\n')
    config = Config.load(tmp_path, home=home(tmp_path), environ={})
    assert config.model == "anthropic/claude-opus-5"
    assert config.v2.models_configured() is False


def test_models_and_model_conflict_rejected(tmp_path):
    write_v2(
        tmp_path,
        """
[models]
default = "medium"
[model]
default = "anthropic/claude-opus-5"
""",
    )
    with pytest.raises(ConfigError, match="conflicting model"):
        Config.load(tmp_path, home=home(tmp_path), environ={})


def test_models_and_model_agreeing_defaults_allowed(tmp_path):
    write_v2(
        tmp_path,
        """
[models]
default = "anthropic/claude-opus-5"
[model]
default = "anthropic/claude-opus-5"
fast = "anthropic/claude-haiku-4-5"
""",
    )
    config = Config.load(tmp_path, home=home(tmp_path), environ={})
    assert config.model == "anthropic/claude-opus-5"
    assert config.v2.model_fast() == "anthropic/claude-haiku-4-5"


def test_v1_flat_config_is_preserved(tmp_path):
    (tmp_path / "nexus.toml").write_text(
        'model = "anthropic/claude-opus-5"\nsandbox = "read-only"\n'
    )
    config = Config.load(tmp_path, home=home(tmp_path), environ={})
    assert config.version == 1
    assert config.v2 is None
    assert config.model == "anthropic/claude-opus-5"


@pytest.mark.parametrize(
    "body",
    [
        "[models]\nrefresh_ttl_days = -1\n",
        "[models]\nrefresh_ttl_days = nan\n",
        '[models]\ncatalogue_url = "file:///etc/passwd"\n',
        '[models.tiers]\nhigh = "anthropic/claude-opus-5"\n',
        "[models.tiers]\nhigh = []\n",
    ],
)
def test_models_section_rejects_invalid_values(tmp_path, body):
    write_v2(tmp_path, body)
    with pytest.raises(ConfigError):
        Config.load(tmp_path, home=home(tmp_path), environ={})


def test_models_env_overlay_is_schema_aware(tmp_path):
    write_v2(tmp_path, "[models]\noffline = false\n")
    config = Config.load(
        tmp_path,
        home=home(tmp_path),
        environ={
            "NEXUS_MODELS__OFFLINE": "true",
            "NEXUS_MODELS__REFRESH_TTL_DAYS": "2",
        },
    )
    assert config.v2.models.offline is True
    assert config.v2.models.refresh_ttl_days == 2


# ---------------------------------------------------------------------------
# Registry events and attribution
# ---------------------------------------------------------------------------


def test_registry_events_are_in_the_catalogue():
    assert REGISTRY_EVENTS == (
        "registry.refreshed",
        "registry.stale",
        "registry.failed",
        "registry.mismatch",
    )
    for name in REGISTRY_EVENTS:
        assert name in EVENT_TYPES


def test_vendored_snapshot_is_mit_labelled_with_notice_and_no_logos():
    notice = DATA_DIR / "NOTICE"
    assert notice.exists()
    text = notice.read_text(encoding="utf-8")
    assert "MIT License" in text and "models.dev" in text

    raw = (DATA_DIR / "models.min.json").read_bytes()
    assert b"pending" not in raw.lower()
    assert b'"logo' not in raw.lower()
    catalogue = parse_catalogue(raw)
    assert catalogue.license_pending is False


def test_catalogue_cannot_redirect_urls_or_carry_logos():
    registry = make_registry(env={"EVIL_API_KEY": "x"})
    status = registry.provider_status("evil")
    assert status is not None
    assert status.selectable is False
    assert status.adapter is None
    assert "attacker" not in (status.reason or "")
    assert "logo" not in ModelInfo.__struct_fields__
    blob = msgspec.json.encode(registry.list())
    assert b"attacker" not in blob
    assert b"logo" not in blob.lower()


# ---------------------------------------------------------------------------
# Router: registry wins, tiers resolve, aliases resolve
# ---------------------------------------------------------------------------


def test_registry_capabilities_win_but_transport_fields_survive():
    provider = CapsProvider(
        "anthropic",
        Capabilities(
            tools=False,
            parallel_tool_calls=True,
            prompt_caching=True,
            streaming=True,
        ),
    )
    registry = make_registry(
        env=ANTHROPIC_ENV, providers={"anthropic": {"kind": "anthropic"}}
    )
    router = ModelRouter(
        {"anthropic": provider},
        registry=registry,
        tiers=TierTable(),
        default="anthropic/claude-opus-5",
    )
    resolved = router.resolve(ModelRequest(messages=[]))
    assert resolved.provider is provider
    assert resolved.model == "claude-opus-5"
    # The registry is authoritative for tool support...
    assert resolved.capabilities.tools is True
    # ...while the adapter keeps the fields the catalogue cannot know.
    assert resolved.capabilities.prompt_caching is True
    assert resolved.capabilities.parallel_tool_calls is True


def test_router_resolves_bare_and_aggregator_aliases_to_model_info():
    provider = CapsProvider("anthropic", Capabilities(tools=True))
    registry = make_registry(
        env={
            **ANTHROPIC_ENV,
            "OPENAI_API_KEY": "x",
            "OPENROUTER_API_KEY": "x",
        }
    )
    router = ModelRouter(
        {"anthropic": provider},
        registry=registry,
        tiers=TierTable(),
        default="anthropic/claude-opus-5",
    )
    bare = router.resolve(ModelRequest(messages=[], model="claude-opus-5"))
    aggregator = router.resolve(
        ModelRequest(messages=[], model="openrouter/anthropic/claude-opus-5")
    )
    assert (bare.provider, bare.model) == (provider, "claude-opus-5")
    assert (aggregator.provider, aggregator.model) == (provider, "claude-opus-5")


def test_custom_and_builtin_tier_names_resolve():
    provider = CapsProvider("anthropic", Capabilities(tools=True))
    tiers = TierTable(overrides={"ultra": ["anthropic/claude-opus-5"]})
    registry = make_registry(
        env=ANTHROPIC_ENV,
        providers={"anthropic": {"kind": "anthropic"}},
        tiers=tiers,
    )
    router = ModelRouter(
        {"anthropic": provider},
        registry=registry,
        tiers=tiers,
        default="ultra",
    )
    assert router.resolve(ModelRequest(messages=[])).model == "claude-opus-5"
    assert (
        router.resolve(ModelRequest(messages=[], model="ultra")).model
        == "claude-opus-5"
    )


def test_tier_names_used_by_skills_resolve_through_the_router():
    # Skills keep their ``model:`` field opaque; the router is what resolves a
    # tier name. Distinct pins make each built-in tier deterministic here.
    providers = {
        "anthropic": CapsProvider("anthropic", Capabilities(tools=True)),
        "openai": CapsProvider("openai", Capabilities(tools=True)),
        "google": CapsProvider("google", Capabilities(tools=True)),
    }
    tiers = TierTable(
        overrides={
            "high": ["anthropic/claude-opus-5"],
            "medium": ["openai/gpt-5.6"],
            "low": ["google/gemini-3-pro"],
        }
    )
    registry = make_registry(
        env={
            "ANTHROPIC_API_KEY": "x",
            "OPENAI_API_KEY": "x",
            "GEMINI_API_KEY": "x",
        },
        providers={
            "anthropic": {"kind": "anthropic"},
            "openai": {"kind": "openai"},
            "google": {"kind": "gemini"},
        },
        tiers=tiers,
    )
    router = ModelRouter(providers, registry=registry, tiers=tiers)
    expected = {
        "low": "gemini-3-pro",
        "medium": "gpt-5.6",
        "high": "claude-opus-5",
    }
    for tier, model in expected.items():
        assert router.resolve(ModelRequest(messages=[], model=tier)).model == model


# ---------------------------------------------------------------------------
# Runtime: owns the registry, cache-first, no network
# ---------------------------------------------------------------------------


def _runtime_config(*, default: str = "anthropic/claude-opus-5", **models) -> Config:
    return Config(
        model=default,
        version=2,
        v2=ConfigV2(
            models=ModelsSection(default=default, **models),
            providers={"anthropic": ProviderSection(kind="anthropic")},
        ),
    )


async def test_runtime_without_models_section_has_no_registry(tmp_path):
    config = models_config(model=ModelSection(default="anthropic/claude-opus-5"))
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": CapsProvider("anthropic", Capabilities(tools=True))},
    )
    try:
        assert runtime.registry is None
        assert runtime.router.registry is None
    finally:
        await runtime.aclose()


async def test_runtime_owns_registry_and_resolves_a_tier_from_cache(tmp_path):
    cache_dir = tmp_path / ".nexus" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models.dev.json").write_bytes(catalogue_bytes())
    config = _runtime_config(
        default="medium",
        offline=True,
        tiers={"medium": ["anthropic/claude-opus-5"]},
    )
    provider = ScriptedProvider(text_response("ok"), name="anthropic")
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        environ=ANTHROPIC_ENV,
    )
    subscription = runtime.extension_events.subscribe()
    try:
        assert runtime.registry is not None
        await runtime.ensure_started()
        status = runtime.registry.status()
        assert status.source == "cache"
        assert status.stale is False

        event = await asyncio.wait_for(subscription.get(), 0.5)
        assert event.type == "registry.refreshed"

        resolved = runtime.router.resolve(ModelRequest(messages=[]))
        assert resolved.provider is provider
        assert resolved.model == "claude-opus-5"
    finally:
        await runtime.aclose()


async def test_runtime_stale_cache_emits_registry_stale(tmp_path):
    cache_dir = tmp_path / ".nexus" / "cache"
    cache_dir.mkdir(parents=True)
    cache = cache_dir / "models.dev.json"
    cache.write_bytes(catalogue_bytes())
    old = time.time() - 8 * DAY
    os.utime(cache, (old, old))
    config = _runtime_config(offline=True)
    provider = ScriptedProvider(text_response("ok"), name="anthropic")
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        environ=ANTHROPIC_ENV,
    )
    subscription = runtime.extension_events.subscribe()
    try:
        await runtime.ensure_started()
        assert runtime.registry.status().stale is True
        event = await asyncio.wait_for(subscription.get(), 0.5)
        assert event.type == "registry.stale"
    finally:
        await runtime.aclose()


async def test_runtime_corrupt_cache_falls_back_to_the_vendored_snapshot(tmp_path):
    cache_dir = tmp_path / ".nexus" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models.dev.json").write_bytes(b"{ not valid json")
    config = _runtime_config(offline=True)
    provider = ScriptedProvider(text_response("ok"), name="anthropic")
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        environ=ANTHROPIC_ENV,
    )
    try:
        await runtime.ensure_started()
        status = runtime.registry.status()
        assert status.source == "snapshot"
        assert status.license_pending is False
        assert runtime.registry.get("anthropic/claude-opus-5") is not None
    finally:
        await runtime.aclose()


async def test_refresh_models_forces_a_reload(tmp_path):
    cache_dir = tmp_path / ".nexus" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models.dev.json").write_bytes(catalogue_bytes())
    config = _runtime_config(offline=True)
    provider = ScriptedProvider(text_response("ok"), name="anthropic")
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        environ=ANTHROPIC_ENV,
    )
    try:
        await runtime.ensure_started()
        assert runtime.registry.status().stale is False
        status = await runtime.refresh_models()
        # ``offline`` pins to the cache, and a forced reload bypasses freshness.
        assert status.source == "cache"
        assert status.stale is True
    finally:
        await runtime.aclose()


class _BrokenRegistry:
    """A registry whose acquisition raises, to exercise ``registry.failed``."""

    async def load(self, *, force: bool = False):
        raise RuntimeError("catalogue exploded")

    def status(self):
        return None


async def test_registry_load_failure_emits_registry_failed(tmp_path):
    config = _runtime_config(offline=True)
    provider = ScriptedProvider(text_response("ok"), name="anthropic")
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        registry=_BrokenRegistry(),  # type: ignore[arg-type]
        environ=ANTHROPIC_ENV,
    )
    subscription = runtime.extension_events.subscribe()
    try:
        await runtime.ensure_started()
        event = await asyncio.wait_for(subscription.get(), 0.5)
        assert event.type == "registry.failed"
        assert "catalogue exploded" in event.data["error"]
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Capability rejection: one retry, disabled feature, mismatch events
# ---------------------------------------------------------------------------


async def test_capability_rejection_retries_once_and_completes(tmp_path):
    registry = make_registry(
        env=ANTHROPIC_ENV, providers={"anthropic": {"kind": "anthropic"}}
    )
    assert registry.get("anthropic/claude-opus-5").tool_call is True
    provider = ScriptedProvider(
        [CapabilityRejected("tools")],
        text_response("recovered"),
        name="anthropic",
        # The adapter says it cannot do tools; the registry says it can. The
        # registry wins, the provider rejects, and the turn degrades.
        capabilities=Capabilities(tools=False, streaming=True),
    )
    config = models_config(model=ModelSection(default="anthropic/claude-opus-5"))
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        registry=registry,
        environ=ANTHROPIC_ENV,
    )
    try:
        session = runtime.session("mismatch")
        events = [event async for event in session.send("use a tool")]
        types = [event.type for event in events]
        assert types.count("context.degraded") == 1
        assert types.count("registry.mismatch") == 1
        assert types[-1] == "turn.completed"
        assert provider.calls == 2

        mismatch = next(e for e in events if e.type == "registry.mismatch")
        assert mismatch.data["feature"] == "tools"
        assert mismatch.data["provider"] == "anthropic"
        assert mismatch.data["model"] == "claude-opus-5"
        assert mismatch.data["source"] == "provider-rejection"
        degraded = next(e for e in events if e.type == "context.degraded")
        assert degraded.data["feature"] == "tools"
        assert degraded.data["retry"] == 1
    finally:
        await runtime.aclose()


async def test_second_capability_rejection_fails_without_a_third_attempt(tmp_path):
    registry = make_registry(
        env=ANTHROPIC_ENV, providers={"anthropic": {"kind": "anthropic"}}
    )
    provider = ScriptedProvider(
        [CapabilityRejected("tools")],
        [CapabilityRejected("tools")],
        name="anthropic",
        capabilities=Capabilities(tools=False, streaming=True),
    )
    config = models_config(model=ModelSection(default="anthropic/claude-opus-5"))
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"anthropic": provider},
        registry=registry,
        environ=ANTHROPIC_ENV,
    )
    try:
        session = runtime.session("mismatch2")
        events = [event async for event in session.send("use a tool")]
        types = [event.type for event in events]
        assert types.count("registry.mismatch") == 1
        assert types[-1] == "turn.failed"
        assert provider.calls == 2
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Loop: a mid-stream rejection or refusal is never retried
# ---------------------------------------------------------------------------


class _MessageRecord:
    def __init__(self, seq: int, message: Message):
        self.seq = seq
        self.message = message


class _FakeLease:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.state = TurnState.new(turn_id=turn_id, session_id="s1").start()
        self.cancel_token = CancelToken()
        self.limits = None

    def release(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.id = "s1"
        self._messages: list[Message] = []
        self._seq = 0
        self._begun = 0

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def append_message(self, message: Message, *, seq: int | None = None) -> object:
        self._seq += 1
        self._messages.append(message)
        return _MessageRecord(self._seq, message)

    def begin_turn(self, *, limits=None) -> _FakeLease:
        self._begun += 1
        return _FakeLease(f"turn-{self._begun}")


class _FakeSink:
    def __init__(self) -> None:
        self.types: list[str] = []

    def emit(self, event: Event) -> Event:
        self.types.append(event.type)
        return event


class _FakeAssembler:
    def assemble(self, session: _FakeSession) -> ModelRequest:
        return ModelRequest(messages=session.messages)


def _resolver(provider: ScriptedProvider, capabilities: Capabilities):
    def resolve(request: ModelRequest) -> ResolvedModel:
        return ResolvedModel(provider, "claude-opus-5", capabilities)

    return resolve


async def test_mid_stream_capability_rejection_is_not_retried():
    provider = ScriptedProvider(
        [TextDelta(text="partial"), CapabilityRejected("tools")],
        name="anthropic",
        capabilities=Capabilities(tools=True, streaming=True),
    )
    session = _FakeSession()
    sink = _FakeSink()
    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=_FakeAssembler(),
        provider_for=_resolver(provider, provider.capabilities("m")),
        emit=sink,
        lease=session.begin_turn(),
    )
    assert outcome.phase == "failed"
    assert provider.calls == 1
    assert "registry.mismatch" not in sink.types
    assert "context.degraded" not in sink.types


async def test_refusal_is_not_treated_as_a_capability_rejection():
    provider = ScriptedProvider(
        text_response("no", stop_reason="refusal"),
        name="anthropic",
        capabilities=Capabilities(tools=True, streaming=True),
    )
    session = _FakeSession()
    sink = _FakeSink()
    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=_FakeAssembler(),
        provider_for=_resolver(provider, provider.capabilities("m")),
        emit=sink,
        lease=session.begin_turn(),
    )
    assert outcome.phase == "completed"
    assert outcome.stop_reason == "refusal"
    assert provider.calls == 1
    assert "registry.mismatch" not in sink.types


# ---------------------------------------------------------------------------
# Tier resolution: only runnable providers, custom names with a slash
# ---------------------------------------------------------------------------


def test_tier_resolution_requires_a_runnable_provider():
    # Pin the only ``high`` catalogue entry to the provider this router does
    # *not* have, so no runnable provider can satisfy the tier.
    tiers = TierTable(
        overrides={
            "high": ["anthropic/claude-opus-5"],
            "low": ["openai/gpt-5.6"],
        }
    )
    registry = make_registry(
        env={"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x"},
        providers={
            "anthropic": {"kind": "anthropic"},
            "openai": {"kind": "openai"},
        },
        tiers=tiers,
    )
    openai = CapsProvider("openai", Capabilities(tools=True))
    router = ModelRouter({"openai": openai}, registry=registry, tiers=tiers)
    with pytest.raises(ConfigError) as excinfo:
        router.resolve(ModelRequest(messages=[], model="high"))
    message = str(excinfo.value)
    assert "runnable" in message
    assert "openai" in message
    # The non-runnable anthropic candidate must not be selected behind the
    # caller's back.
    assert "claude-opus-5" not in message or "anthropic" in message


def test_custom_tier_name_containing_a_slash_resolves():
    tiers = TierTable(overrides={"team/high": ["anthropic/claude-opus-5"]})
    registry = make_registry(
        env=ANTHROPIC_ENV,
        providers={"anthropic": {"kind": "anthropic"}},
        tiers=tiers,
    )
    provider = CapsProvider("anthropic", Capabilities(tools=True))
    router = ModelRouter(
        {"anthropic": provider}, registry=registry, tiers=tiers
    )
    resolved = router.resolve(ModelRequest(messages=[], model="team/high"))
    assert resolved.provider is provider
    assert resolved.model == "claude-opus-5"


# ---------------------------------------------------------------------------
# Registry load: single-flight and flag-only-after-success
# ---------------------------------------------------------------------------


class _CountingRegistry:
    """A registry that blocks in ``load`` so concurrency can be observed."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, *, force: bool = False):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("catalogue exploded")
        return RegistryStatus(source="cache", model_count=1, provider_count=1)

    def status(self):
        return RegistryStatus()


def _runtime_with_registry(tmp_path, registry) -> Runtime:
    return Runtime(
        tmp_path,
        config=_runtime_config(offline=True),
        providers={"anthropic": ScriptedProvider(text_response("ok"), name="anthropic")},
        registry=registry,
        environ=ANTHROPIC_ENV,
    )


async def test_concurrent_ensure_started_loads_the_registry_once(tmp_path):
    registry = _CountingRegistry()
    runtime = _runtime_with_registry(tmp_path, registry)
    try:
        first = asyncio.create_task(runtime.ensure_started())
        await asyncio.wait_for(registry.entered.wait(), 0.5)
        second = asyncio.create_task(runtime.ensure_started())
        await asyncio.sleep(0)
        registry.release.set()
        await asyncio.gather(first, second)
        assert registry.calls == 1
    finally:
        await runtime.aclose()


async def test_failed_registry_load_is_retried_and_events_are_redacted(tmp_path):
    registry = _CountingRegistry(fail_first=True)
    runtime = _runtime_with_registry(tmp_path, registry)
    subscription = runtime.extension_events.subscribe()
    registry.release.set()
    try:
        await runtime.ensure_started()
        event = await asyncio.wait_for(subscription.get(), 0.5)
        assert event.type == "registry.failed"
        # The flag was not set on failure, so a second boundary retries.
        await runtime.ensure_started()
        assert registry.calls == 2
    finally:
        await runtime.aclose()
