"""Phase 4 P4-C: the tool loader — version-stamped, generation-unique imports.

Covers the plan section 6.1/6.11 contract the whole hot-reload design depends
on:

* every load gets a name of the form ``nexus_ext.<source>__g<N>`` and
  ``importlib.reload`` is never called, so a pinned generation is immutable;
* stages 1-4 of quarantine gate the import; the staged bytes must hash to the
  validated digest immediately before execution (the TOCTOU close);
* a failed load leaves nothing in ``sys.modules``;
* multi-generation coexistence, an in-flight old callable surviving a swap, and
  weakref collectability after release;
* release removes only loader-owned modules and is idempotent.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import sys
import weakref
from pathlib import Path

import pytest

from nexus.ext.quarantine import (
    Quarantine,
    QuarantineCode,
    QuarantineError,
    StagedSource,
)
from nexus.tools.loader import (
    MODULE_PREFIX,
    LoadOutcome,
    ToolLoader,
    ToolLoadError,
    extract_declared_tools,
    module_name_for,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extensions"


def fixture(name: str) -> Path:
    return FIXTURES / name


def staged_source_identity(name: str) -> str:
    """The stable identity ``Quarantine.open`` would give a fixture."""
    return str(fixture(name).resolve())


def quarantiner(tmp_path: Path | None = None, **overrides) -> Quarantine:
    kwargs = {
        "max_file_bytes": 100_000,
        "timeout_s": 2.0,
        "root": FIXTURES,
        "stage_root": (tmp_path / "stage") if tmp_path is not None else None,
    }
    kwargs.update(overrides)
    return Quarantine(**kwargs)


def load_fixture(
    name: str,
    generation: int,
    *,
    tmp_path: Path,
    loader: ToolLoader | None = None,
    q: Quarantine | None = None,
) -> tuple[ToolLoader, LoadOutcome]:
    loader = loader or ToolLoader()
    q = q or quarantiner(tmp_path)
    staged = q.stage(q.open(fixture(name)))
    return loader, loader.load(staged, generation)


@pytest.fixture(autouse=True)
def _clean_sys_modules():
    """Fail the test rather than leak a loader-owned module into the suite."""
    before = set(sys.modules)
    yield
    leaked = [
        name
        for name in set(sys.modules)
        if name.startswith(MODULE_PREFIX) and name not in before
    ]
    for name in leaked:
        sys.modules.pop(name, None)
    assert not leaked, f"test leaked modules: {leaked}"


# ---------------------------------------------------------------------------
# Naming: source identity + generation, never a raw path
# ---------------------------------------------------------------------------


def test_module_name_includes_source_and_generation():
    a1 = module_name_for("/workspace/tools/echo_spec.py", 1)
    a2 = module_name_for("/workspace/tools/echo_spec.py", 2)
    b1 = module_name_for("/other/place/echo_spec.py", 1)
    # The generation is part of the name, so a pinned generation is immutable.
    assert a1 != a2
    assert a1.endswith("__g1") and a2.endswith("__g2")
    # The absolute source identity is part of the name, so two sources that
    # share a stem never collide.
    assert a1 != b1
    # Deterministic: same identity + generation always yields the same name.
    assert module_name_for("/workspace/tools/echo_spec.py", 1) == a1
    assert "echo_spec" in a1


def test_module_name_sanitizes_path_like_identities():
    name = module_name_for("../evil/name", 3)
    assert name.startswith("nexus_ext.")
    assert name.endswith("__g3")
    assert ".." not in name and "/" not in name
    assert name.split(".")[1].startswith("name_")


def test_module_name_is_a_valid_identifier():
    name = module_name_for("/tmp/a weird-name!.py", 7)
    label = name.split(".", 1)[1]
    assert label.isidentifier()


def test_module_name_validates_arguments():
    with pytest.raises(ToolLoadError):
        module_name_for("", 1)
    with pytest.raises(ToolLoadError):
        module_name_for("x", -1)
    with pytest.raises(ToolLoadError):
        module_name_for("x", True)  # type: ignore[arg-type]


def test_loader_discard_staged_removes_only_private_copies(tmp_path: Path):
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    copy = q.stage(q.open(fixture("echo_spec.py")))
    assert loader.discard_staged(copy) is True
    assert copy.path.exists() is False
    # A workspace source is not private and is never deleted.
    workspace = tmp_path / "plain.py"
    workspace.write_text("SPEC = {}\n", encoding="utf-8")
    direct = q.open(workspace)
    assert loader.discard_staged(direct) is False
    assert workspace.exists()


def test_loader_never_calls_importlib_reload(monkeypatch, tmp_path: Path):
    import importlib

    called: list[str] = []
    monkeypatch.setattr(importlib, "reload", lambda module: called.append(module.__name__))
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    assert outcome.ok
    assert called == []
    loader.release(loader.owned_modules)


# ---------------------------------------------------------------------------
# Successful loads
# ---------------------------------------------------------------------------


def test_spec_form_loads_and_runs(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    assert outcome.ok is True
    assert [tool.name for tool in outcome.tools] == ["EchoFixture"]
    assert outcome.record is not None
    assert outcome.record.declaration == "spec"
    result = asyncio.run(outcome.tools[0].run({"message": "hi"}, None))
    assert result.content[0].text == "echo:hi"
    loader.release(loader.owned_modules)


def test_register_form_loads_and_runs(tmp_path: Path):
    loader, outcome = load_fixture("register_tool.py", 1, tmp_path=tmp_path)
    assert outcome.ok is True
    assert [tool.name for tool in outcome.tools] == ["RegisterFixture"]
    assert outcome.record is not None
    assert outcome.record.declaration == "register"
    result = asyncio.run(outcome.tools[0].run({}, None))
    assert result.content[0].text == "registered:ok"
    loader.release(loader.owned_modules)


def test_module_handle_metadata_is_json_safe(tmp_path: Path):
    import json

    loader, outcome = load_fixture("echo_spec.py", 4, tmp_path=tmp_path)
    handle = outcome.record.handle
    assert handle.name == module_name_for(staged_source_identity("echo_spec.py"), 4)
    assert handle.name.endswith("__g4")
    assert handle.generation == 4
    assert handle.sha256 == hashlib.sha256(fixture("echo_spec.py").read_bytes()).hexdigest()
    assert handle.origin == "ext"
    payload = handle.to_dict()
    assert payload["loaded"] is True
    assert "module" not in payload
    json.dumps(payload)
    loader.release(loader.owned_modules)


def test_registered_tool_carries_provenance(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 2, tmp_path=tmp_path)
    tool = outcome.tools[0]
    assert tool.origin == "ext"
    assert tool.generation == 2
    assert tool.source is not None
    loader.release(loader.owned_modules)


def test_loader_owns_exactly_what_it_installed(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    owned = loader.owned_modules
    assert outcome.record.handle.name in owned
    assert loader.owns(outcome.record.handle.name) is True
    assert loader.owns("os") is False
    assert loader.owns("nexus_ext.other__g9") is False
    loader.release(owned)


# ---------------------------------------------------------------------------
# Quarantine gates carried through the loader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("broken_syntax.py", QuarantineCode.SYNTAX_ERROR),
        ("no_contract.py", QuarantineCode.BAD_SPEC),
        ("sync_run.py", QuarantineCode.BAD_SPEC),
        ("bad_schema.py", QuarantineCode.BAD_SPEC),
        ("ambiguous.py", QuarantineCode.BAD_SPEC),
        ("import_error.py", QuarantineCode.IMPORT_ERROR),
    ],
)
def test_loader_refuses_invalid_modules_and_leaves_nothing(
    name: str, code: QuarantineCode, tmp_path: Path
):
    # These fixtures are safe to import *in-process* (unlike ``hangs.py`` and
    # ``hard_exit.py``); the loader must refuse each and roll back cleanly.
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture(name)))
    expected_name = module_name_for(staged.source_identity, 1)
    outcome = loader.load(staged, 1)
    assert outcome.ok is False
    assert outcome.code is code
    assert sys.modules.get(expected_name) is None
    assert loader.owned_modules == ()
    # A refused load is a partial artifact: the private staged copy is removed.
    assert staged.path.exists() is False


def test_loader_returns_refusal_for_a_module_that_cannot_be_imported(tmp_path: Path):
    # A module that imports cleanly in isolation but whose *in-process* import
    # fails is refused with IMPORT_ERROR; the loader never trusts the child.
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("import_error.py")))
    expected_name = module_name_for(staged.source_identity, 1)
    # inspect() is fine (syntax valid); the loader's own import raises.
    outcome = loader.load(staged, 1)
    assert outcome.ok is False
    assert outcome.code in (QuarantineCode.IMPORT_ERROR, QuarantineCode.IMPORT_EXIT)
    assert sys.modules.get(expected_name) is None
    assert loader.owned_modules == ()


def test_failed_load_discards_the_partial_module(tmp_path: Path, monkeypatch):
    import nexus.tools.loader as loader_mod

    loader = ToolLoader()
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("echo_spec.py")))
    name = module_name_for(staged.source_identity, 1)

    monkeypatch.setattr(
        loader_mod,
        "_extract_declared",
        lambda module: (_ for _ in ()).throw(loader_mod.ToolSpecError("boom")),
    )
    outcome = loader.load(staged, 1)
    assert outcome.ok is False
    assert outcome.code is QuarantineCode.BAD_SPEC
    assert sys.modules.get(name) is None
    assert loader.owned_modules == ()
    assert staged.path.exists() is False


def test_oversize_is_refused_before_import(tmp_path: Path):
    loader = ToolLoader()
    q = quarantiner(tmp_path, max_file_bytes=32)
    outcome = q.inspect_only(fixture("echo_spec.py"))
    assert outcome.code is QuarantineCode.OVERSIZE
    assert loader.owned_modules == ()


# ---------------------------------------------------------------------------
# TOCTOU: validated bytes must equal executed bytes
# ---------------------------------------------------------------------------


def test_load_refuses_when_the_staged_hash_is_wrong(tmp_path: Path):
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("echo_spec.py")))
    outcome = loader.load(staged, 1, expected_sha256="0" * 64)
    assert outcome.code is QuarantineCode.HASH_MISMATCH
    assert loader.owned_modules == ()
    assert staged.path.exists() is False


def test_load_uses_staged_bytes_not_the_mutable_workspace_file(tmp_path: Path):
    # Stage the original, then edit the workspace file. The loader must import
    # the *staged* content, proving validated == executed.
    source = tmp_path / "tool.py"
    source.write_text(
        "SPEC = {'name': 'Original', 'description': 'd', "
        "'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        "async def run(args, ctx):\n    return None\n",
        encoding="utf-8",
    )
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(source))
    source.write_text(
        "SPEC = {'name': 'Edited', 'description': 'd', "
        "'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        "async def run(args, ctx):\n    return None\n",
        encoding="utf-8",
    )
    loader = ToolLoader()
    outcome = loader.load(staged, 1)
    assert outcome.ok is True
    assert [tool.name for tool in outcome.tools] == ["Original"]
    loader.release(loader.owned_modules)


def test_load_result_rejects_a_stage_changed_after_staging(tmp_path: Path):
    from nexus.ext.quarantine import QuarantineResult

    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("echo_spec.py")))
    outcome = q.run_isolated(staged)
    assert outcome.ok is True
    result = QuarantineResult(outcome=outcome, staged=staged)
    staged.path.write_bytes(staged.data + b"\n# edited\n")
    loader = ToolLoader()
    with pytest.raises(QuarantineError):
        loader.load_result(result, 1)
    assert loader.owned_modules == ()


def test_load_streaming_a_stage_symlink_target_races_to_hash_mismatch(tmp_path: Path):
    # A staged file replaced by a symlink after staging still hashes differently,
    # so the loader refuses rather than following it.
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("echo_spec.py")))
    original = staged.path.read_bytes()
    staged.path.unlink()
    other = tmp_path / "other.py"
    other.write_bytes(original + b"\n# different\n")
    staged.path.symlink_to(other)
    loader = ToolLoader()
    outcome = loader.load(staged, 1)
    assert outcome.code is QuarantineCode.HASH_MISMATCH
    assert loader.owned_modules == ()


# ---------------------------------------------------------------------------
# Generations: coexistence, in-flight survival, collectability
# ---------------------------------------------------------------------------


def test_generations_coexist_in_sys_modules(tmp_path: Path):
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    names = []
    for generation in (1, 2, 3):
        staged = q.stage(q.open(fixture("echo_spec.py")))
        outcome = loader.load(staged, generation)
        assert outcome.ok
        names.append(outcome.record.handle.name)
        assert outcome.record.handle.name in sys.modules
    assert len(set(names)) == 3
    assert names == [
        module_name_for(staged_source_identity("echo_spec.py"), gen)
        for gen in (1, 2, 3)
    ]
    loader.release(names)
    assert all(name not in sys.modules for name in names)


def test_in_flight_old_callable_survives_release(tmp_path: Path):
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    staged1 = q.stage(q.open(fixture("echo_spec.py")))
    old = loader.load(staged1, 1)
    staged2 = q.stage(q.open(fixture("echo_spec.py")))
    new = loader.load(staged2, 2)
    assert old.ok and new.ok

    # Drop generation 1 from sys.modules while a callable from it is still
    # live; the callable's module globals must remain intact.
    loader.release([old.record.handle.name])
    assert old.record.handle.name not in sys.modules
    result = asyncio.run(old.tools[0].run({"message": "still"}, None))
    assert result.content[0].text == "echo:still"
    loader.release(loader.owned_modules)


def test_evil_module_absent_after_failed_generation(tmp_path: Path):
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    staged = q.stage(q.open(fixture("import_error.py")))
    expected_name = module_name_for(staged.source_identity, 5)
    outcome = loader.load(staged, 5)
    assert outcome.ok is False
    assert expected_name not in sys.modules
    assert loader.owned_modules == ()


def test_module_is_collectable_after_release(tmp_path: Path):
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    staged = q.stage(q.open(fixture("echo_spec.py")))
    outcome = loader.load(staged, 1)
    module = outcome.module
    assert module is not None
    ref = weakref.ref(module)
    name = outcome.record.handle.name
    loader.release([name])
    del module, outcome
    gc.collect()
    assert ref() is None


def test_release_only_affects_loader_owned_modules(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    assert "sys" in sys.modules
    removed = loader.release(["sys", "os", outcome.record.handle.name])
    assert removed == (outcome.record.handle.name,)
    assert "sys" in sys.modules and "os" in sys.modules


def test_release_is_idempotent(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    name = outcome.record.handle.name
    assert loader.release([name]) == (name,)
    assert loader.release([name]) == ()
    assert loader.owned_modules == ()


def test_release_record(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    assert loader.release_record(outcome.record) is True
    assert loader.release_record(outcome.record) is False


# ---------------------------------------------------------------------------
# Declaration extraction directly
# ---------------------------------------------------------------------------


def test_extract_declared_tools_validates_a_bad_schema(tmp_path: Path):
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("bad_schema.py")))
    outcome = loader.load(staged, 1)
    assert outcome.code is QuarantineCode.BAD_SPEC


def test_extract_declared_tools_returns_spec_and_kind(tmp_path: Path):
    loader, outcome = load_fixture("echo_spec.py", 1, tmp_path=tmp_path)
    declared, kind = extract_declared_tools(outcome.module)
    assert kind == "spec"
    assert [item.name for item in declared] == ["EchoFixture"]
    assert declared[0].to_spec().name == "EchoFixture"
    loader.release(loader.owned_modules)


def test_extracted_spec_round_trips_path_mode():
    from nexus.ext.quarantine import ExtractedSpec

    original = ExtractedSpec(
        name="WriteTool",
        description="d",
        input_schema={"type": "object"},
        bundle="meta",
        mutates=True,
        concurrency="exclusive",
        timeout_s=None,
        path_mode=True,
    )
    restored = ExtractedSpec.from_json(original.to_json())
    assert restored.path_mode is True
    assert restored == original


def test_hot_loaded_spec_preserves_path_mode(tmp_path: Path):
    source = tmp_path / "pathmode.py"
    source.write_text(
        "from nexus.tools.spec import ToolSpec\n"
        "def _key(data):\n"
        "    return '.nexus/tools/' + str(data['filename'])\n"
        "SPEC = ToolSpec(\n"
        "    name='PathModeTool',\n"
        "    description='d',\n"
        "    input_schema={'type': 'object'},\n"
        "    bundle='meta',\n"
        "    mutates=True,\n"
        "    permission_key=_key,\n"
        "    path_mode=True,\n"
        ")\n"
        "async def run(args, ctx):\n"
        "    return None\n",
        encoding="utf-8",
    )
    loader = ToolLoader()
    q = quarantiner(tmp_path)
    outcome = loader.load(q.stage(q.open(source)), 1)
    assert outcome.ok is True
    assert outcome.tools[0].spec.path_mode is True
    loader.release(loader.owned_modules)


def test_register_is_called_once_per_load(tmp_path: Path):
    # A register() with a side effect must not be called repeatedly in one
    # process; extraction and binding share a single call.
    source = tmp_path / "counter.py"
    source.write_text(
        "from nexus.tools.spec import ToolSpec, RegisteredTool\n"
        "CALLS = []\n"
        "async def _run(args, ctx):\n    return None\n"
        "def register():\n"
        "    CALLS.append(1)\n"
        "    return [RegisteredTool(spec=ToolSpec(name='Counter', "
        "description='d', input_schema={'type': 'object'}, bundle='fs'), run=_run)]\n",
        encoding="utf-8",
    )
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(source))
    loader = ToolLoader()
    outcome = loader.load(staged, 1)
    assert outcome.ok is True
    assert outcome.module.CALLS == [1]
    loader.release(loader.owned_modules)


def test_loader_origin_is_recorded(tmp_path: Path):
    loader = ToolLoader(origin="ext")
    q = quarantiner(tmp_path)
    staged = q.stage(q.open(fixture("echo_spec.py")))
    outcome = loader.load(staged, 1)
    assert outcome.record.handle.origin == "ext"
    loader.release(loader.owned_modules)


def test_loader_validates_origin():
    with pytest.raises(ToolLoadError):
        ToolLoader(origin="")


def test_staged_source_requires_hash_for_validation():
    from nexus.tools.loader import validate_spec_against

    staged = StagedSource(
        candidate=__import__("nexus.ext.quarantine", fromlist=["ExtCandidate"]).ExtCandidate(
            path=Path("/tmp/x.py")
        ),
        path=Path("/tmp/x.py"),
        data=b"",
        text="",
        sha256="",
        size=0,
        mode=0o644,
    )
    from nexus.ext.quarantine import ExtractedSpec

    spec = ExtractedSpec(
        name="X",
        description="d",
        input_schema={"type": "object"},
        bundle="fs",
        mutates=False,
        concurrency="parallel",
        timeout_s=None,
    )
    with pytest.raises(ToolLoadError):
        validate_spec_against(staged, [spec])


def test_two_sources_with_the_same_stem_get_distinct_module_names(tmp_path: Path):
    first = tmp_path / "a" / "tool.py"
    second = tmp_path / "b" / "tool.py"
    first.parent.mkdir()
    second.parent.mkdir()
    body = (
        "SPEC = {'name': NAME, 'description': 'd', "
        "'input_schema': {'type': 'object'}, 'bundle': 'fs'}\n"
        "async def run(args, ctx):\n    return None\n"
    )
    first.write_text(body.replace("NAME", "'FromA'"), encoding="utf-8")
    second.write_text(body.replace("NAME", "'FromB'"), encoding="utf-8")
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    staged_a = q.stage(q.open(first))
    staged_b = q.stage(q.open(second))
    out_a = loader.load(staged_a, 1)
    out_b = loader.load(staged_b, 1)
    assert out_a.ok and out_b.ok
    # The *absolute source identity* is hashed into the name, so two files that
    # share a stem never collide even at the same generation.
    assert out_a.record.handle.name != out_b.record.handle.name
    assert out_a.record.handle.name.endswith("__g1")
    assert out_b.record.handle.name.endswith("__g1")
    assert "tool" in out_a.record.handle.name
    assert [t.name for t in out_a.tools] == ["FromA"]
    assert [t.name for t in out_b.tools] == ["FromB"]
    assert out_a.record.handle.name in sys.modules
    assert out_b.record.handle.name in sys.modules
    loader.release(loader.owned_modules)


def test_same_source_fresh_stage_reloads_to_distinct_generation_name(tmp_path: Path):
    q = quarantiner(tmp_path)
    loader = ToolLoader()
    first = loader.load(q.stage(q.open(fixture("echo_spec.py"))), 11)
    second = loader.load(q.stage(q.open(fixture("echo_spec.py"))), 12)
    assert first.ok and second.ok
    assert first.record.handle.name == module_name_for(
        staged_source_identity("echo_spec.py"), 11
    )
    assert second.record.handle.name == module_name_for(
        staged_source_identity("echo_spec.py"), 12
    )
    assert first.record.handle.name != second.record.handle.name
    # Both generations coexist: the older callable keeps working after the
    # newer generation is installed.
    assert asyncio.run(first.tools[0].run({"message": "old"}, None)).content[0].text == "echo:old"
    loader.release(loader.owned_modules)
