"""Phase 4 Packet 1: the immutable manifest, its diff/report, and generation leases.

Covers the load-bearing concurrency contract from plan section 6.1/11: a manifest
never mutates, readers never observe a half-swapped world, and a retired
generation's cleanup callback runs only once every pin on it has been released.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import types
from types import MappingProxyType

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ProviderSection
from nexus.errors import (
    ExtensionError,
    ManifestError,
    NexusError,
    StaleGenerationError,
)
from nexus.ext.manifest import (
    CleanupFailure,
    EqualGenerationError,
    Fingerprintable,
    GenerationLease,
    Manifest,
    ManifestDiff,
    ManifestLease,
    ManifestRef,
    ModuleHandle,
    NameDelta,
    ReloadFailure,
    ReloadReport,
    SkillToolSet,
    SystemFile,
    SystemFiles,
    fingerprint,
)


class _Marker:
    """A tiny stand-in for a registered extension entry (identity is enough)."""

    __slots__ = ("generation",)

    def __init__(self, generation: int):
        self.generation = generation


class _NamedTool:
    """A stand-in for a RegisteredTool: only ``name`` matters to a SkillToolSet."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


def _manifest_for(generation: int) -> Manifest:
    return Manifest(
        generation=generation,
        tools={f"t{generation}": _Marker(generation)},
    )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_manifest_struct_is_frozen():
    manifest = Manifest(generation=1)
    with pytest.raises(AttributeError):
        manifest.generation = 2  # type: ignore[misc]


def test_manifest_nested_mappings_are_read_only_proxies():
    manifest = Manifest(
        generation=1,
        tools={"Read": _Marker(1)},
        skills={"s": _Marker(1)},
        agents={"a": _Marker(1)},
        hooks={"h": (_Marker(1),)},
        providers={"p": _Marker(1)},
        mcp={"m": _Marker(1)},
        modules={"mod": ModuleHandle(name="mod", path="mod.py")},
    )
    for mapping in (
        manifest.tools,
        manifest.skills,
        manifest.agents,
        manifest.hooks,
        manifest.providers,
        manifest.mcp,
        manifest.modules,
    ):
        assert isinstance(mapping, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.tools["Write"] = _Marker(1)  # type: ignore[index]


def test_manifest_isolates_from_caller_mappings():
    tools = {"Read": _Marker(1)}
    skills = {"s": _Marker(1)}
    hooks = {"h": [_Marker(1)]}
    manifest = Manifest(generation=1, tools=tools, skills=skills, hooks=hooks)

    # Mutating the caller's dicts after construction must not reach the manifest.
    tools["Write"] = _Marker(1)
    skills["t"] = _Marker(1)
    hooks["h"].append(_Marker(1))

    assert set(manifest.tools) == {"Read"}
    assert set(manifest.skills) == {"s"}
    assert len(manifest.hooks["h"]) == 1
    assert isinstance(manifest.hooks["h"], tuple)


def test_manifest_rejects_non_string_keys_and_non_mappings():
    with pytest.raises(ManifestError):
        Manifest(generation=1, tools={1: _Marker(1)})  # type: ignore[dict-item]
    with pytest.raises(ManifestError):
        Manifest(generation=1, tools=[("A", _Marker(1))])  # type: ignore[arg-type]


def test_system_files_mapping_is_immutable_and_isolated():
    files = {"soul": SystemFile(name="soul", content="be kind")}
    system_files = SystemFiles(files=files)
    files["memory"] = SystemFile(name="memory", content="notes")

    assert set(system_files.names()) == {"soul"}
    assert system_files.soul is not None
    assert system_files.soul.content == "be kind"
    assert system_files.memory is None
    assert isinstance(system_files.files, MappingProxyType)
    with pytest.raises(TypeError):
        system_files.files["x"] = files["soul"]  # type: ignore[index]


def test_reserved_maps_are_empty_and_still_freeze():
    manifest = Manifest()
    assert dict(manifest.agents) == {}
    assert dict(manifest.hooks) == {}
    assert dict(manifest.providers) == {}
    assert dict(manifest.mcp) == {}

    populated = Manifest(
        generation=1,
        agents={"a": _Marker(1)},
        mcp={"server": _Marker(1)},
    )
    assert set(populated.agents) == {"a"}
    assert set(populated.mcp) == {"server"}
    assert isinstance(populated.agents, MappingProxyType)


# ---------------------------------------------------------------------------
# System files and module provenance
# ---------------------------------------------------------------------------


def test_system_file_hash_and_size_are_derived():
    system_file = SystemFile(name="soul", content="hello")
    assert system_file.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert system_file.size == 5

    explicit = SystemFile(name="soul", content="hello", sha256="deadbeef", size=99)
    assert explicit.sha256 == "deadbeef"
    assert explicit.size == 99


def test_module_handle_serialization_excludes_live_module():
    module = types.ModuleType("nexus_ext.demo__g3")
    handle = ModuleHandle(
        name="nexus_ext.demo__g3",
        path="/tmp/demo.py",
        generation=3,
        sha256="abc",
        module=module,
    )
    assert handle.loaded is True
    payload = handle.to_dict()
    assert payload["name"] == "nexus_ext.demo__g3"
    assert payload["loaded"] is True
    assert "module" not in payload
    assert ModuleHandle(name="m", path="p").loaded is False


def test_manifest_to_dict_is_sanitized():
    secret = "sk-ant-SUPERSECRET"
    v2 = ConfigV2(providers={"anthropic": ProviderSection(api_key=secret)})
    manifest = Manifest(
        generation=7,
        config=Config(v2=v2, version=2),
        modules={"m": ModuleHandle(name="m", path="m.py")},
        system_files=SystemFiles(files={"soul": SystemFile(name="soul", content="hi")}),
    )
    payload = manifest.to_dict()
    assert payload["generation"] == 7
    assert payload["modules"]["m"]["name"] == "m"
    assert payload["system_files"]["soul"]["name"] == "soul"
    assert secret not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 1.5, "1", True])
def test_manifest_rejects_bad_generation(bad: object):
    with pytest.raises(ManifestError):
        Manifest(generation=bad)  # type: ignore[arg-type]


def test_manifest_rejects_bad_config_type():
    with pytest.raises(ManifestError):
        Manifest(generation=1, config="nope")  # type: ignore[arg-type]


def test_manifest_ref_validates_arguments():
    with pytest.raises(ManifestError):
        ManifestRef("nope")  # type: ignore[arg-type]
    with pytest.raises(ManifestError):
        ManifestRef(on_retire=123)  # type: ignore[arg-type]


def test_extension_error_hierarchy():
    assert issubclass(ExtensionError, NexusError)
    assert issubclass(ManifestError, ExtensionError)
    assert issubclass(ManifestError, ValueError)
    assert issubclass(StaleGenerationError, ManifestError)


# ---------------------------------------------------------------------------
# ManifestRef: atomic reads and monotonic swaps
# ---------------------------------------------------------------------------


def test_get_and_generation():
    ref = ManifestRef(Manifest(generation=5))
    assert ref.get().generation == 5
    assert ref.generation == 5


def test_default_ref_starts_at_empty_generation_zero():
    ref = ManifestRef()
    assert ref.get().generation == 0
    assert dict(ref.get().tools) == {}


def test_swap_installs_new_and_returns_previous():
    ref = ManifestRef(Manifest(generation=1))
    new = Manifest(generation=2)
    previous = ref.swap(new)
    assert previous.generation == 1
    assert ref.get() is new


def test_swap_distinct_equal_generation_is_rejected():
    ref = ManifestRef(Manifest(generation=2))
    current = ref.get()
    with pytest.raises(EqualGenerationError):
        ref.swap(Manifest(generation=2))
    assert ref.get() is current


def test_equal_generation_error_is_a_manifest_error():
    assert issubclass(EqualGenerationError, ManifestError)
    assert issubclass(EqualGenerationError, ValueError)


def test_swap_identical_object_is_a_noop():
    manifest = Manifest(generation=1)
    ref = ManifestRef(manifest)
    assert ref.swap(manifest) is manifest


def test_swap_older_generation_raises_and_keeps_current():
    ref = ManifestRef(Manifest(generation=3))
    current = ref.get()
    with pytest.raises(StaleGenerationError):
        ref.swap(Manifest(generation=2))
    assert ref.get() is current


def test_swap_requires_a_manifest():
    ref = ManifestRef()
    with pytest.raises(ManifestError):
        ref.swap("nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Generation leases
# ---------------------------------------------------------------------------


def test_pin_is_synchronous_and_refcounts():
    ref = ManifestRef(Manifest(generation=4))
    lease = ref.pin()
    assert isinstance(lease, ManifestLease)
    assert lease.generation == 4
    assert lease.manifest is ref.get()
    assert dict(ref.pinned_generations) == {4: 1}

    second = ref.pin()
    assert dict(ref.pinned_generations) == {4: 2}
    second.release()
    assert dict(ref.pinned_generations) == {4: 1}
    lease.release()
    assert dict(ref.pinned_generations) == {}


def test_lease_alias_close_and_generation_lease_identity():
    assert GenerationLease is ManifestLease
    ref = ManifestRef(Manifest(generation=1))
    lease = ref.lease()
    assert lease.generation == 1
    lease.close()
    assert lease.released is True
    assert dict(ref.pinned_generations) == {}


def test_pinned_generations_is_a_read_only_snapshot():
    ref = ManifestRef(Manifest(generation=1))
    ref.pin()
    snapshot = ref.pinned_generations
    with pytest.raises(TypeError):
        snapshot[1] = 5  # type: ignore[index]


def test_pin_old_across_swap_keeps_the_snapshot_alive():
    ref = ManifestRef(_manifest_for(1))
    lease = ref.pin()
    old = lease.manifest

    ref.swap(_manifest_for(2))

    assert lease.manifest is old
    assert old.generation == 1
    assert "t1" in old.tools
    assert ref.get().generation == 2
    assert lease.released is False
    lease.release()


def test_cleanup_fires_only_after_the_last_pin_is_released():
    retired: list[Manifest] = []
    ref = ManifestRef(_manifest_for(1), on_retire=retired.append)
    lease = ref.pin()

    ref.swap(_manifest_for(2))
    assert retired == []
    assert ref.retired_generations == (1,)
    assert dict(ref.pinned_generations) == {1: 1}

    lease.release()
    assert [manifest.generation for manifest in retired] == [1]
    assert ref.retired_generations == ()
    assert dict(ref.pinned_generations) == {}


def test_cleanup_waits_for_the_last_of_several_pins():
    retired: list[Manifest] = []
    ref = ManifestRef(_manifest_for(0), on_retire=retired.append)
    first = ref.pin()
    second = ref.pin()

    ref.swap(_manifest_for(1))
    first.release()
    assert retired == []
    second.release()
    assert [manifest.generation for manifest in retired] == [0]


def test_cleanup_fires_immediately_when_unpinned():
    retired: list[Manifest] = []
    ref = ManifestRef(_manifest_for(0), on_retire=retired.append)
    ref.swap(_manifest_for(1))
    assert [manifest.generation for manifest in retired] == [0]


def test_double_release_is_idempotent():
    retired: list[Manifest] = []
    ref = ManifestRef(_manifest_for(0), on_retire=retired.append)
    lease = ref.pin()
    ref.swap(_manifest_for(1))

    lease.release()
    lease.release()
    lease.close()

    assert lease.released is True
    assert [manifest.generation for manifest in retired] == [0]


def test_context_manager_releases_on_exception():
    ref = ManifestRef(_manifest_for(0))
    with pytest.raises(RuntimeError), ref.pin() as lease:
        assert dict(ref.pinned_generations) == {0: 1}
        raise RuntimeError("boom")
    assert lease.released is True
    assert dict(ref.pinned_generations) == {}


async def test_async_context_manager_releases_on_cancellation():
    ref = ManifestRef(_manifest_for(0))
    started = asyncio.Event()
    leases: dict[str, ManifestLease] = {}

    async def worker() -> None:
        async with ref.pin() as lease:
            leases["lease"] = lease
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(worker())
    await started.wait()
    assert dict(ref.pinned_generations) == {0: 1}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert leases["lease"].released is True
    assert dict(ref.pinned_generations) == {}


# ---------------------------------------------------------------------------
# Concurrency: no torn manifests
# ---------------------------------------------------------------------------


def test_thread_readers_never_observe_a_torn_manifest():
    ref = ManifestRef(_manifest_for(0))
    stop = threading.Event()
    failures: list[Manifest] = []

    def reader() -> None:
        while not stop.is_set():
            manifest = ref.get()
            marker = manifest.tools.get(f"t{manifest.generation}")
            if marker is None or marker.generation != manifest.generation:
                failures.append(manifest)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for generation in range(1, 500):
            ref.swap(_manifest_for(generation))
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    assert failures == []


async def test_async_readers_never_observe_a_torn_manifest():
    ref = ManifestRef(_manifest_for(0))
    failures: list[Manifest] = []
    done = asyncio.Event()

    async def reader() -> None:
        while not done.is_set():
            manifest = ref.get()
            marker = manifest.tools.get(f"t{manifest.generation}")
            if marker is None or marker.generation != manifest.generation:
                failures.append(manifest)
                return
            await asyncio.sleep(0)

    readers = [asyncio.create_task(reader()) for _ in range(4)]
    for generation in range(1, 500):
        ref.swap(_manifest_for(generation))
        await asyncio.sleep(0)
    done.set()
    await asyncio.gather(*readers)

    assert failures == []


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_detects_added_removed_and_changed():
    tool_a = _Marker(1)
    tool_b = _Marker(1)
    old = Manifest(generation=1, tools={"A": tool_a})
    new = Manifest(generation=2, tools={"A": tool_b, "B": tool_b})

    diff = ManifestDiff.between(old, new)
    assert diff.from_generation == 1
    assert diff.to_generation == 2
    assert diff.tools.added == ("B",)
    assert diff.tools.removed == ()
    assert diff.tools.changed == ("A",)
    assert diff.changed is True

    unchanged = ManifestDiff.between(old, old)
    assert unchanged.changed is False
    assert unchanged.tools.empty is True


def test_diff_detects_removals_system_files_and_modules():
    old = Manifest(
        generation=1,
        system_files=SystemFiles(files={"soul": SystemFile(name="soul", content="a")}),
        modules={"m": ModuleHandle(name="m", path="m.py")},
    )
    new = Manifest(
        generation=2,
        system_files=SystemFiles(files={"soul": SystemFile(name="soul", content="b")}),
        modules={},
    )

    diff = ManifestDiff.between(old, new)
    assert diff.system_files.changed == ("soul",)
    assert diff.modules.removed == ("m",)
    assert diff.changed is True


def test_diff_config_change_uses_identity():
    config = Config()
    old = Manifest(generation=1, config=config)
    reused = Manifest(generation=2, config=config)
    reread = Manifest(generation=3, config=Config())

    assert ManifestDiff.between(old, reused).config_changed is False
    assert ManifestDiff.between(old, reread).config_changed is True


def test_diff_requires_manifests():
    with pytest.raises(ManifestError):
        ManifestDiff.between(Manifest(), "nope")  # type: ignore[arg-type]


def test_name_delta_normalizes_and_validates():
    delta = NameDelta(added=["b", "a"])
    assert delta.added == ("a", "b")
    with pytest.raises(ManifestError):
        NameDelta(added=["ok", 3])  # type: ignore[list-item]
    with pytest.raises(ManifestError):
        NameDelta(added="not-a-sequence")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Report serialization and sanitization
# ---------------------------------------------------------------------------


def test_diff_to_dict_is_json_serializable():
    old = Manifest(generation=1, tools={"A": _Marker(1)})
    new = Manifest(generation=2, tools={"B": _Marker(1)})
    blob = json.dumps(ManifestDiff.between(old, new).to_dict())
    assert '"added"' in blob
    assert '"B"' in blob


def test_reload_report_serialization_is_json_and_sanitized():
    secret = "sk-ant-SUPERSECRET"
    v2 = ConfigV2(providers={"anthropic": ProviderSection(api_key=secret)})
    old = Manifest(generation=1, config=Config(v2=v2, version=2))
    new = Manifest(generation=2, config=Config(v2=v2, version=2))
    failure = ReloadFailure(
        kind="tool",
        name="BadTool",
        error="SyntaxError: invalid syntax",
        error_type="SyntaxError",
    )

    report = ReloadReport(
        previous_generation=1,
        generation=2,
        changed=True,
        diff=ManifestDiff.between(old, new),
        failed=(failure,),
        duration_ms=3.5,
    )

    payload = report.to_dict()
    blob = json.dumps(payload)
    assert secret not in blob
    assert payload["ok"] is False
    assert payload["changed"] is True
    assert payload["failed"][0]["name"] == "BadTool"
    assert payload["diff"]["config_changed"] is True
    assert "gen 1 -> 2" in payload["summary"]


def test_reload_report_ok_and_no_change_summary():
    report = ReloadReport()
    assert report.ok is True
    assert report.summary == "gen 0 -> 0: no changes"
    assert report.to_dict()["ok"] is True
    assert report.to_dict()["failed"] == []


def test_reload_report_validates_failures():
    with pytest.raises(ManifestError):
        ReloadReport(failed=(object(),))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compare-and-swap: the serialized-reload seam
# ---------------------------------------------------------------------------


def test_compare_and_swap_installs_when_expected_matches():
    ref = ManifestRef(_manifest_for(1))
    expected = ref.get()
    new = _manifest_for(2)
    assert ref.compare_and_swap(expected, new) is expected
    assert ref.get() is new


def test_compare_and_swap_accepts_a_generation_int():
    ref = ManifestRef(_manifest_for(1))
    ref.compare_and_swap(1, _manifest_for(2))
    assert ref.generation == 2


def test_compare_and_swap_rejects_a_superseded_expectation():
    ref = ManifestRef(_manifest_for(1))
    stale = ref.get()
    ref.swap(_manifest_for(2))
    with pytest.raises(StaleGenerationError):
        ref.compare_and_swap(stale, _manifest_for(3))
    assert ref.generation == 2


def test_compare_and_swap_int_mismatch_leaves_current_untouched():
    ref = ManifestRef(_manifest_for(5))
    current = ref.get()
    with pytest.raises(StaleGenerationError):
        ref.compare_and_swap(4, _manifest_for(6))
    assert ref.get() is current


def test_try_compare_and_swap_returns_none_on_a_lost_race():
    ref = ManifestRef(_manifest_for(1))
    stale = ref.get()
    ref.swap(_manifest_for(2))
    assert ref.try_compare_and_swap(stale, _manifest_for(3)) is None
    assert ref.generation == 2


def test_compare_and_swap_validates_expected_type():
    ref = ManifestRef()
    with pytest.raises(ManifestError):
        ref.compare_and_swap("nope", _manifest_for(1))  # type: ignore[arg-type]
    with pytest.raises(ManifestError):
        ref.compare_and_swap(True, _manifest_for(1))  # type: ignore[arg-type]


def test_swap_expected_keyword_is_a_conditional_install():
    ref = ManifestRef(_manifest_for(1))
    expected = ref.get()
    ref.swap(_manifest_for(2), expected=expected)
    assert ref.generation == 2


# ---------------------------------------------------------------------------
# Cleanup failure isolation, observability, and collectability
# ---------------------------------------------------------------------------


def test_cleanup_failure_never_rolls_back_the_swap():
    def bad(_manifest: Manifest) -> None:
        raise RuntimeError("cleanup boom")

    ref = ManifestRef(_manifest_for(0), on_retire=bad)
    new = _manifest_for(1)
    ref.swap(new)

    assert ref.get() is new
    failures = ref.cleanup_failures
    assert len(failures) == 1
    assert isinstance(failures[0], CleanupFailure)
    assert failures[0].generation == 0
    assert failures[0].error_type == "RuntimeError"
    assert failures[0].error == "cleanup boom"
    assert "exception" not in failures[0].to_dict()


def test_cleanup_failures_are_drainable_and_clearable():
    def bad(_manifest: Manifest) -> None:
        raise ValueError("later")

    ref = ManifestRef(_manifest_for(0), on_retire=bad)
    ref.swap(_manifest_for(1))
    first = ref.drain_cleanup_failures()
    assert len(first) == 1
    assert ref.cleanup_failures == ()

    ref.swap(_manifest_for(2))
    assert len(ref.cleanup_failures) == 1
    ref.clear_cleanup_failures()
    assert ref.cleanup_failures == ()


def test_cleanup_failure_on_the_last_lease_release():
    def bad(_manifest: Manifest) -> None:
        raise KeyError("late")

    ref = ManifestRef(_manifest_for(0), on_retire=bad)
    lease = ref.pin()
    ref.swap(_manifest_for(1))
    assert ref.cleanup_failures == ()

    lease.release()
    failures = ref.cleanup_failures
    assert len(failures) == 1
    assert failures[0].error_type == "KeyError"
    assert ref.retired_generations == ()
    assert dict(ref.pinned_generations) == {}


def test_cleanup_error_callback_observes_the_failure():
    seen: list[tuple[int, str]] = []

    def bad(_manifest: Manifest) -> None:
        raise RuntimeError("boom")

    def on_error(manifest: Manifest, exc: BaseException) -> None:
        seen.append((manifest.generation, type(exc).__name__))

    ref = ManifestRef(_manifest_for(0), on_retire=bad, on_cleanup_error=on_error)
    ref.swap(_manifest_for(1))

    assert seen == [(0, "RuntimeError")]
    assert len(ref.cleanup_failures) == 1


def test_cleanup_error_handler_failure_is_swallowed():
    def bad(_manifest: Manifest) -> None:
        raise RuntimeError("boom")

    def bad_handler(_manifest: Manifest, _exc: BaseException) -> None:
        raise RuntimeError("handler boom")

    ref = ManifestRef(_manifest_for(0), on_retire=bad, on_cleanup_error=bad_handler)
    ref.swap(_manifest_for(1))
    assert len(ref.cleanup_failures) == 1


def test_cleanup_never_runs_under_the_ref_lock():
    observed: list[int] = []
    ref: ManifestRef

    def cleanup(manifest: Manifest) -> None:
        observed.append(manifest.generation)
        lease = ref.pin()
        observed.append(lease.generation)
        lease.release()

    ref = ManifestRef(_manifest_for(0), on_retire=cleanup)
    ref.swap(_manifest_for(1))
    assert observed == [0, 1]


def test_cleanup_failure_validates_fields():
    with pytest.raises(ManifestError):
        CleanupFailure(generation=-1)
    with pytest.raises(ManifestError):
        CleanupFailure(generation=0, error_type=1, error="x")  # type: ignore[arg-type]
    assert CleanupFailure(generation=0, error_type="E", error="m").to_dict() == {
        "generation": 0,
        "error_type": "E",
        "error": "m",
    }


def test_manifest_ref_validates_cleanup_error_callback():
    with pytest.raises(ManifestError):
        ManifestRef(on_cleanup_error=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stable fingerprints: unchanged rebuilds do not churn the diff
# ---------------------------------------------------------------------------


class _Fingered:
    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def fingerprint(self) -> str:
        return self.key


class _Dicted:
    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key}


def test_fingerprint_prefers_an_entry_method():
    assert hasattr(Fingerprintable, "fingerprint")
    assert fingerprint(_Fingered("abc")) == "fp:abc"


def test_fingerprint_falls_back_to_to_dict_then_identity():
    assert fingerprint(_Dicted("abc")) == fingerprint(_Dicted("abc"))
    assert fingerprint(_Dicted("abc")).startswith("dict:")
    assert fingerprint(_Dicted("abc")) != fingerprint(_Dicted("xyz"))
    assert fingerprint("x") == fingerprint("x")
    assert fingerprint(3) == fingerprint(3)
    assert fingerprint(None) == "null"
    # No stable surface: identity is the conservative fallback.
    assert fingerprint(_Marker(1)) != fingerprint(_Marker(1))


def test_fingerprint_handles_sequences():
    assert fingerprint((_Fingered("a"), _Fingered("b"))) == fingerprint(
        [_Fingered("a"), _Fingered("b")]
    )
    assert fingerprint((_Fingered("a"),)) != fingerprint((_Fingered("b"),))


def test_diff_does_not_churn_equal_but_rebuilt_entries():
    config = Config()
    old = Manifest(
        generation=1,
        config=config,
        tools={"A": _Fingered("stable")},
        skills={"s": _Dicted("k")},
    )
    new = Manifest(
        generation=2,
        config=config,
        tools={"A": _Fingered("stable")},
        skills={"s": _Dicted("k")},
    )

    diff = ManifestDiff.between(old, new)
    assert diff.tools.changed == ()
    assert diff.skills.changed == ()
    assert diff.changed is False
    assert old.fingerprint() == new.fingerprint()


def test_diff_still_reports_a_genuine_fingerprint_change():
    old = Manifest(generation=1, tools={"A": _Fingered("v1")})
    new = Manifest(generation=2, tools={"A": _Fingered("v2")})
    assert ManifestDiff.between(old, new).tools.changed == ("A",)
    assert old.fingerprint() != new.fingerprint()


def test_diff_per_category_fingerprints_override_the_default():
    old = Manifest(generation=1, tools={"A": _Marker(1)})
    new = Manifest(generation=2, tools={"A": _Marker(2)})

    ignored = ManifestDiff.between(old, new, fingerprints={"tools": lambda _t: "same"})
    assert ignored.tools.changed == ()

    keyed = ManifestDiff.between(
        old, new, fingerprints={"tools": lambda t: t.generation}
    )
    assert keyed.tools.changed == ("A",)


def test_diff_rejects_bad_fingerprint_arguments():
    with pytest.raises(ManifestError):
        ManifestDiff.between(Manifest(), Manifest(), fingerprints=[("tools", "x")])  # type: ignore[arg-type]
    with pytest.raises(ManifestError):
        ManifestDiff.between(Manifest(), Manifest(), fingerprints={"tools": 1})  # type: ignore[dict-item]


def test_module_fingerprint_ignores_generation_when_hashed():
    first = ModuleHandle(
        name="nexus_ext.m__g1", path="/tmp/m.py", generation=1, sha256="abc"
    )
    second = ModuleHandle(
        name="nexus_ext.m__g2", path="/tmp/m.py", generation=2, sha256="abc"
    )
    assert first.fingerprint() == second.fingerprint()

    old = Manifest(generation=1, modules={"m": first})
    new = Manifest(generation=2, modules={"m": second})
    assert ManifestDiff.between(old, new).modules.changed == ()


def test_module_fingerprint_without_a_hash_is_conservative():
    first = ModuleHandle(name="nexus_ext.m__g1", path="/tmp/m.py", generation=1)
    second = ModuleHandle(name="nexus_ext.m__g2", path="/tmp/m.py", generation=2)
    assert first.fingerprint() != second.fingerprint()


def test_manifest_fingerprint_omits_config():
    config = Config()
    first = Manifest(generation=1, config=config)
    second = Manifest(generation=2, config=config)
    assert first.fingerprint() == second.fingerprint()


# ---------------------------------------------------------------------------
# Skill-scoped bundled tools
# ---------------------------------------------------------------------------


def test_skill_tool_set_fingerprint_ignores_generation_and_module_names():
    first = SkillToolSet(
        skill="reader",
        skill_fingerprint="sfp",
        generation=1,
        tools=(_NamedTool("SkillPing"),),
        modules=("nexus_ext.skill_ping__g1",),
    )
    second = SkillToolSet(
        skill="reader",
        skill_fingerprint="sfp",
        generation=9,
        tools=(_NamedTool("SkillPing"),),
        modules=("nexus_ext.skill_ping__g9",),
    )
    assert first.fingerprint() == second.fingerprint()
    changed = SkillToolSet(
        skill="reader",
        skill_fingerprint="other",
        generation=1,
        tools=(_NamedTool("SkillPing"),),
    )
    assert first.fingerprint() != changed.fingerprint()


def test_manifest_freezes_skill_tools_and_serializes_names_only():
    entry = SkillToolSet(
        skill="reader",
        skill_fingerprint="sfp",
        generation=3,
        tools=(_NamedTool("SkillPing"),),
        modules=("nexus_ext.skill_ping__g3",),
    )
    manifest = Manifest(generation=1, skill_tools={"reader": entry})
    assert isinstance(manifest.skill_tools, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.skill_tools["x"] = entry  # type: ignore[index]
    payload = manifest.to_dict()
    assert payload["skill_tools"]["reader"]["tools"] == ["SkillPing"]
    assert payload["skill_tools"]["reader"]["generation"] == 3
    assert "module" not in payload["skill_tools"]["reader"]


def test_diff_detects_a_changed_skill_tool_set():
    old = Manifest(
        generation=1,
        skill_tools={
            "reader": SkillToolSet(skill="reader", skill_fingerprint="a")
        },
    )
    new = Manifest(
        generation=2,
        skill_tools={
            "reader": SkillToolSet(skill="reader", skill_fingerprint="b")
        },
    )
    diff = ManifestDiff.between(old, new)
    assert diff.skill_tools.changed == ("reader",)
    assert diff.changed is True
    assert "~1 skill tool" in ReloadReport(
        previous_generation=1, generation=2, changed=True, diff=diff
    ).summary


# ---------------------------------------------------------------------------
# ReloadReport summaries cover every map
# ---------------------------------------------------------------------------


def test_reload_report_summary_covers_every_map():
    old = Manifest(generation=1)
    new = Manifest(
        generation=2,
        tools={"t": _Marker(2)},
        skills={"s": _Marker(2)},
        agents={"a": _Marker(2)},
        hooks={"h": (_Marker(2),)},
        providers={"p": _Marker(2)},
        mcp={"m": _Marker(2)},
        modules={"mod": ModuleHandle(name="mod", path="mod.py")},
        system_files=SystemFiles(files={"soul": SystemFile(name="soul", content="x")}),
    )
    report = ReloadReport(
        previous_generation=1,
        generation=2,
        changed=True,
        diff=ManifestDiff.between(old, new),
    )
    summary = report.summary
    for fragment in (
        "+1 tool",
        "+1 skill",
        "+1 agent",
        "+1 hook",
        "+1 provider",
        "+1 mcp",
        "+1 module",
        "+1 system file",
    ):
        assert fragment in summary


def test_reload_report_summary_reports_removals_and_changes_everywhere():
    old = Manifest(
        generation=1,
        agents={"a": _Marker(1)},
        hooks={"h": (_Marker(1),)},
        providers={"p": _Marker(1)},
        mcp={"m": _Marker(1)},
    )
    new = Manifest(generation=2)
    report = ReloadReport(
        previous_generation=1,
        generation=2,
        changed=True,
        diff=ManifestDiff.between(old, new),
    )
    for fragment in ("-1 agent", "-1 hook", "-1 provider", "-1 mcp"):
        assert fragment in report.summary


def test_reload_report_summary_includes_config_change():
    config = Config()
    old = Manifest(generation=1, config=config)
    new = Manifest(generation=2, config=Config())
    report = ReloadReport(
        previous_generation=1,
        generation=2,
        changed=True,
        diff=ManifestDiff.between(old, new),
    )
    assert "~config" in report.summary


# ---------------------------------------------------------------------------
# Concurrency: competing writers and monotonic observation
# ---------------------------------------------------------------------------


def test_competing_cas_has_exactly_one_winner():
    ref = ManifestRef(_manifest_for(1))
    expected = ref.get()
    results: list[bool] = []
    guard = threading.Lock()
    start = threading.Barrier(4)

    def worker() -> None:
        start.wait()
        won = ref.try_compare_and_swap(expected, _manifest_for(2)) is not None
        with guard:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert ref.generation == 2


def test_competing_plain_swaps_never_corrupt_the_ref():
    ref = ManifestRef(_manifest_for(0))
    rejected: list[BaseException] = []
    start = threading.Barrier(6)

    def worker(generation: int) -> None:
        start.wait()
        try:
            ref.swap(_manifest_for(generation))
        except (StaleGenerationError, EqualGenerationError) as exc:
            rejected.append(exc)

    threads = [threading.Thread(target=worker, args=(g,)) for g in range(1, 7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    manifest = ref.get()
    marker = manifest.tools[f"t{manifest.generation}"]
    assert marker.generation == manifest.generation
    # Generation 6 can never be rejected (it is the newest), so it must win.
    assert manifest.generation == 6


async def test_async_readers_observe_only_monotonic_generations():
    ref = ManifestRef(_manifest_for(0))
    seen: list[int] = []
    done = asyncio.Event()

    async def reader() -> None:
        while not done.is_set():
            seen.append(ref.get().generation)
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(reader()) for _ in range(4)]
    for generation in range(1, 300):
        ref.swap(_manifest_for(generation))
        await asyncio.sleep(0)
    done.set()
    await asyncio.gather(*tasks)

    assert seen == sorted(seen)
    assert seen[-1] >= 0


def test_lease_lifecycle_many_pins_fire_cleanup_once():
    retired: list[int] = []
    ref = ManifestRef(
        _manifest_for(0), on_retire=lambda manifest: retired.append(manifest.generation)
    )
    leases = [ref.pin() for _ in range(25)]
    ref.swap(_manifest_for(1))

    assert retired == []
    for lease in leases[:-1]:
        lease.release()
    assert retired == []
    leases[-1].release()
    assert retired == [0]
    assert ref.retired_generations == ()
    assert dict(ref.pinned_generations) == {}
