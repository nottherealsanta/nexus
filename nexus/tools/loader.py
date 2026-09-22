"""Hot-load ``.py`` tool modules under version-stamped, generation-unique names.

Plan section 6.1. The loader is the *execution* half of quarantine: quarantine
proves a file is safe to import and freezes its exact bytes; the loader imports
those frozen bytes into a private, generation-stamped module and turns the
declared ``SPEC`` / ``register()`` into :class:`~nexus.tools.spec.RegisteredTool`
values the manifest can hold.

Why a fresh module name per (source, generation)
------------------------------------------------
``importlib.reload`` mutates a module in place: objects already captured by an
in-flight call silently change identity, and ``isinstance`` checks against
pre-reload classes start failing. Instead every load gets a name of the form::

    nexus_ext.<label>_<identity-hash>__g<generation>

where ``<identity-hash>`` is a stable digest of the source's *absolute* path.
The hash keeps two files that share a stem from colliding; the generation keeps
each load unique, so generation *N* is immutable. A call that pinned generation
*N* keeps running that module; new calls use *N+1*. Nothing here ever calls
``importlib.reload``.

Cleanup and release
-------------------
:class:`ModuleHandle` records provenance (source identity, path, generation,
sha256). :meth:`ToolLoader.release` removes an *owned* set of module names from
``sys.modules`` and returns whether a module was still importable.
:meth:`ToolLoader.discard_staged` deletes the private content-addressed staged
copy that backed a load (never a workspace source). The loader itself does not
decide *when* a generation may be dropped -- that is the manifest lease's job
(``ManifestRef.on_retire``); it only performs the removal safely and
idempotently, and it **refuses to unload a module it does not own** (one it did
not install), so a caller cannot ask it to drop ``sys``.

Boundary
--------
Manager-layer (L3). Imports ``nexus.tools.spec`` and ``nexus.ext`` contracts,
never the runtime, session, or core loop.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from ..config import Config
from ..errors import ExtensionError
from ..ext.manifest import ModuleHandle
from ..ext.quarantine import (
    ExtractedSpec,
    Quarantine,
    QuarantineCode,
    QuarantineError,
    QuarantineOutcome,
    QuarantineResult,
    StagedSource,
    sanitize_text,
)
from ..tools.spec import (
    RegisteredTool,
    ToolExecutionResult,
    ToolSpec,
    ToolSpecError,
    validate_input_schema,
    validate_tool_name,
)

__all__ = [
    "DeclaredTool",
    "LoadOutcome",
    "ModuleRecord",
    "ToolLoadError",
    "ToolLoader",
    "extract_declared_tools",
    "module_name_for",
    "validate_spec_against",
]


class ToolLoadError(ExtensionError):
    """A validated module could not be imported or registered."""


#: The prefix every loader-owned module name shares. ``release`` will never
#: touch a name outside this namespace.
MODULE_PREFIX = "nexus_ext."


def module_name_for(source_identity: str, generation: int) -> str:
    """The unique, generation-stamped module name for one source.

    ``source_identity`` is the *absolute* source identity (the original
    workspace path, not a staged copy). Two files with the same stem but
    different locations therefore get different names, and the same file at two
    generations gets two names. The label is sanitized to identifier characters
    so a path cannot inject ``.``/``..``/``/`` into the module namespace, and a
    stable digest of the full identity keeps the name collision-free even when
    the label is truncated or two identities sanitize alike.

    The name is deterministic: same identity + generation always yields the same
    name across processes, which is what makes a pinned generation reproducible.
    """
    if not isinstance(source_identity, str) or not source_identity:
        raise ToolLoadError("source_identity must be a non-empty string")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ToolLoadError("generation must be a non-negative integer")
    label = _identity_label(source_identity)
    digest = hashlib.sha256(
        source_identity.encode("utf-8", "surrogatepass")
    ).hexdigest()[:12]
    return f"{MODULE_PREFIX}{label}_{digest}__g{generation}"


def _identity_label(source_identity: str) -> str:
    """A short, identifier-safe label taken from the source's basename."""
    text = source_identity.replace("\\", "/")
    tail = text.rsplit("/", 1)[-1]
    tail = tail.removesuffix(".py")
    safe = "".join(
        ch if (ch.isalnum() or ch == "_") else "_" for ch in tail
    ).strip("_")
    return safe or "ext"


@dataclass(frozen=True)
class DeclaredTool:
    """One tool a module declared, before it is paired with a live callable."""

    spec_json: ExtractedSpec
    name: str

    def to_spec(self) -> ToolSpec:
        """Reconstruct the contract type from JSON-safe metadata."""
        return ToolSpec(
            name=self.spec_json.name,
            description=self.spec_json.description,
            input_schema=self.spec_json.input_schema,
            bundle=self.spec_json.bundle,
            mutates=self.spec_json.mutates,
            concurrency=self.spec_json.concurrency,
            timeout_s=self.spec_json.timeout_s,
            path_mode=self.spec_json.path_mode,
        )


@dataclass(frozen=True)
class ModuleRecord:
    """Everything provenance the loader keeps about one loaded module."""

    handle: ModuleHandle
    source_id: str
    declaration: str
    tool_names: tuple[str, ...]
    bundled_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.handle.name,
            "source_id": self.source_id,
            "generation": self.handle.generation,
            "sha256": self.handle.sha256,
            "origin": self.handle.origin,
            "path": self.handle.path,
            "declaration": self.declaration,
            "tools": list(self.tool_names),
            "bundled_tools": list(self.bundled_tools),
        }


@dataclass
class LoadOutcome:
    """The result of one :meth:`ToolLoader.load`: tools plus provenance."""

    outcome: QuarantineOutcome
    module: ModuleType | None = None
    record: ModuleRecord | None = None
    tools: tuple[RegisteredTool, ...] = ()
    specs: tuple[ToolSpec, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome.ok

    @property
    def code(self) -> QuarantineCode:
        return self.outcome.code

    @property
    def name(self) -> str:
        return self.outcome.name

    @property
    def handle(self) -> ModuleHandle | None:
        return self.record.handle if self.record is not None else None


# ---------------------------------------------------------------------------
# Declaration extraction (from the *imported* module, never from JSON alone)
# ---------------------------------------------------------------------------


def _mapping_from(obj: object) -> dict[str, Any] | None:
    if isinstance(obj, ToolSpec):
        return {
            "name": obj.name,
            "description": obj.description,
            "input_schema": obj.input_schema,
            "bundle": obj.bundle,
            "mutates": obj.mutates,
            "concurrency": obj.concurrency,
            "timeout_s": obj.timeout_s,
            "path_mode": obj.path_mode,
        }
    if hasattr(obj, "to_dict"):
        try:
            payload = obj.to_dict()
        except Exception:  # noqa: BLE001 - a bad to_dict is an invalid declaration
            return None
        if isinstance(payload, dict):
            return payload
    if isinstance(obj, dict):
        return obj
    return None


def _extracted_from(value: object, label: str) -> ExtractedSpec:
    payload = _mapping_from(value)
    if payload is None:
        raise ToolSpecError(f"{label} is not a tool declaration")
    try:
        extracted = ExtractedSpec.from_json(payload)
    except (KeyError, TypeError) as exc:
        raise ToolSpecError(f"{label} is missing required fields: {exc}") from exc
    # Reconstruct and validate through the contract so any tightening applies.
    return ExtractedSpec(
        name=extracted.name,
        description=extracted.description,
        input_schema=extracted.input_schema,
        bundle=extracted.bundle,
        mutates=extracted.mutates,
        concurrency=extracted.concurrency,
        timeout_s=extracted.timeout_s,
        path_mode=extracted.path_mode,
    )


def extract_declared_tools(module: ModuleType) -> tuple[list[DeclaredTool], str]:
    """Extract the declared tools from an imported module.

    Returns ``(declared_tools, declaration_kind)`` where ``declaration_kind`` is
    ``"spec"`` or ``"register"``. Every extracted spec is reconstructed through
    :class:`ToolSpec` so invalid declarations raise :class:`ToolSpecError`.

    For the ``SPEC`` form the single tool's ``run`` must be an async callable
    on the module. For the ``register()`` form the synchronous callable is
    invoked once and must return a finite iterable of
    :class:`RegisteredTool`-shaped objects.

    This is a pure inspection helper; :meth:`ToolLoader.load` uses the private
    variant that also returns the objects ``register()`` produced so it does not
    have to call ``register()`` twice in one process.
    """
    declared, kind, _items = _extract_declared(module)
    return declared, kind


def _extract_declared(
    module: ModuleType,
) -> tuple[list[DeclaredTool], str, list[Any]]:
    """Like :func:`extract_declared_tools` but also returns ``register()`` items.

    The third element is the list of live objects produced by ``register()``
    (empty for the ``SPEC`` form), so the loader can bind runnables without a
    second call to a side-effecting function.
    """
    has_spec = hasattr(module, "SPEC")
    register = getattr(module, "register", None)
    has_register = callable(register)
    if has_spec and has_register:
        raise ToolSpecError("module defines both SPEC and register(); ambiguous")
    if has_spec:
        extracted = _extracted_from(module.SPEC, "SPEC")
        _validate_spec_declaration(extracted)
        run = getattr(module, "run", None)
        if not callable(run):
            raise ToolSpecError("SPEC module must define a callable run(args, ctx)")
        if not inspect.iscoroutinefunction(run):
            raise ToolSpecError("SPEC run(args, ctx) must be an async function")
        return [DeclaredTool(spec_json=extracted, name=extracted.name)], "spec", []
    if has_register:
        produced = register()
        if inspect.isawaitable(produced):
            raise ToolSpecError("register() must be synchronous, not a coroutine")
        if produced is None:
            raise ToolSpecError("register() returned None; return a finite iterable")
        try:
            items = list(produced)
        except TypeError as exc:
            raise ToolSpecError(
                "register() must return a finite iterable of tools"
            ) from exc
        declared: list[DeclaredTool] = []
        for index, item in enumerate(items):
            declared.append(_declared_from_registered(item, index))
        if not declared:
            raise ToolSpecError("register() returned no tools")
        return declared, "register", items
    raise ToolSpecError("module defines neither SPEC nor register()")


def _declared_from_registered(item: object, index: int) -> DeclaredTool:
    if isinstance(item, RegisteredTool):
        spec = item.spec
        return DeclaredTool(spec_json=_extracted_from(spec, f"register()[{index}].spec"), name=spec.name)
    spec_obj = getattr(item, "spec", None)
    if spec_obj is None and isinstance(item, dict):
        spec_obj = item.get("spec")
    if spec_obj is None:
        raise ToolSpecError(f"register()[{index}] has no spec")
    extracted = _extracted_from(spec_obj, f"register()[{index}].spec")
    run = getattr(item, "run", None)
    if run is None and isinstance(item, dict):
        run = item.get("run")
    if run is not None and not inspect.iscoroutinefunction(run):
        raise ToolSpecError(f"register()[{index}].run must be an async function")
    return DeclaredTool(spec_json=extracted, name=extracted.name)


def _validate_spec_declaration(extracted: ExtractedSpec) -> None:
    validate_tool_name(extracted.name)
    validate_input_schema(extracted.input_schema)
    if not isinstance(extracted.description, str) or not extracted.description.strip():
        raise ToolSpecError("description must be a non-empty string")
    # Reconstructing the frozen contract validates bundle/concurrency/timeout.
    ExtractedSpec(
        name=extracted.name,
        description=extracted.description,
        input_schema=extracted.input_schema,
        bundle=extracted.bundle,
        mutates=extracted.mutates,
        concurrency=extracted.concurrency,
        timeout_s=extracted.timeout_s,
        path_mode=extracted.path_mode,
    )


def validate_spec_against(
    staged: StagedSource,
    extracted: Sequence[ExtractedSpec],
) -> None:
    """Require the extracted declaration to reconstruct into valid contracts.

    A derivation check, not an authority check: it proves that a declaration
    captured out-of-process still round-trips through the in-process
    :class:`ToolSpec` validator. The strong guarantee -- that the staged bytes
    are the *exact* bytes the isolated child parsed -- is enforced by hash in
    :meth:`ToolLoader.load`.
    """
    if not staged.sha256:
        raise ToolLoadError("staged source has no content hash")
    for spec in extracted:
        ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            bundle=spec.bundle,
            mutates=spec.mutates,
            concurrency=spec.concurrency,
            timeout_s=spec.timeout_s,
            path_mode=spec.path_mode,
        )


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


@dataclass
class _Owned:
    """The module names this loader installed and may later release."""

    names: set[str] = field(default_factory=set)


class ToolLoader:
    """Import validated extension modules under unique, generation-stamped names.

    One loader may be shared across a reload; it tracks the module names it
    installed so it can release exactly those and nothing else. The import lock
    serializes concurrent loads of the same loader, which is what keeps two
    reloads from interleaving ``sys.modules`` mutations.
    """

    def __init__(self, *, origin: str = "ext", module_prefix: str = MODULE_PREFIX) -> None:
        if not isinstance(origin, str) or not origin:
            raise ToolLoadError("origin must be a non-empty string")
        self._origin = origin
        self._module_prefix = module_prefix
        self._owned = _Owned()
        self._lock = threading.RLock()

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def owned_modules(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._owned.names))

    def owns(self, module_name: str) -> bool:
        if not isinstance(module_name, str) or not module_name.startswith(
            self._module_prefix
        ):
            return False
        with self._lock:
            return module_name in self._owned.names

    # -- import -----------------------------------------------------------

    def load(
        self,
        staged: StagedSource,
        generation: int,
        *,
        expected_sha256: str | None = None,
        extra: dict[str, Any] | None = None,
        origin: str | None = None,
    ) -> LoadOutcome:
        """Import frozen, staged bytes into generation ``generation``.

        The staged file must still hash to ``expected_sha256`` (defaulting to
        the hash recorded when it was staged) immediately before the import, so
        the bytes executed are the bytes validated. The import is version-stamped
        and never reloaded. On any failure the partially-installed module is
        removed from ``sys.modules`` before the error is returned.

        ``origin`` overrides the loader-wide origin for this one load (used to
        tag a skill's bundled ``tools/*.py`` as ``"skill"`` while the same loader
        still owns and releases every module it installs).

        ``extra`` are read-only helper modules the loader installs under stable
        names first (e.g. a shim exposing Nexus contract types under names a
        hot module can import), but only if those names are not already present.
        """
        effective_origin = self._origin if origin is None else origin
        if not isinstance(effective_origin, str) or not effective_origin:
            raise ToolLoadError("origin must be a non-empty string")
        expected = expected_sha256 if expected_sha256 is not None else staged.sha256
        module_name = module_name_for(staged.source_identity, generation)
        with self._lock:
            if expected is None or staged.sha256 != expected:
                return self._fail(
                    staged,
                    QuarantineCode.HASH_MISMATCH,
                    "staged source does not match the validated hash",
                )
            # The frozen ``StagedSource`` recorded the hash at validation time.
            # Re-hash the actual bytes now, immediately before import: this is
            # the TOCTOU close -- a file swapped, edited, or replaced by a
            # symlink after staging is refused rather than executed.
            actual = self._hash_staged(staged)
            if actual != expected:
                return self._fail(
                    staged,
                    QuarantineCode.HASH_MISMATCH,
                    "staged source changed after validation; refusing to import",
                )
            # A unique name can never collide with a live generation, but guard
            # against a re-entrant load of the same (source, generation).
            if module_name in sys.modules:
                return self._fail(
                    staged,
                    QuarantineCode.HASH_MISMATCH,
                    f"module {module_name!r} is already loaded",
                )
            self._install_extra(extra)
            spec = importlib.util.spec_from_file_location(module_name, staged.path)
            if spec is None or spec.loader is None:
                return self._fail(
                    staged,
                    QuarantineCode.IMPORT_ERROR,
                    "cannot build an import spec for the staged file",
                )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            self._owned.names.add(module_name)
            try:
                spec.loader.exec_module(module)
                declared, declaration, items = _extract_declared(module)
            except ToolSpecError as exc:
                self._discard(module_name)
                return self._fail(
                    staged, QuarantineCode.BAD_SPEC, _safe_detail(exc)
                )
            except SyntaxError as exc:
                self._discard(module_name)
                return self._fail(
                    staged, QuarantineCode.SYNTAX_ERROR, _safe_detail(exc)
                )
            except BaseException as exc:  # noqa: BLE001 - arbitrary import-time code
                self._discard(module_name)
                return self._fail(
                    staged, QuarantineCode.IMPORT_ERROR, _safe_detail(exc)
                )
            try:
                tools = self._build_tools(
                    module,
                    declared,
                    declaration,
                    items,
                    generation,
                    staged,
                    effective_origin,
                )
            except ToolSpecError as exc:
                self._discard(module_name)
                return self._fail(
                    staged, QuarantineCode.BAD_SPEC, _safe_detail(exc)
                )
            handle = ModuleHandle(
                name=module_name,
                path=str(staged.path),
                generation=generation,
                sha256=staged.sha256,
                origin=effective_origin,
                module=module,
            )
            record = ModuleRecord(
                handle=handle,
                source_id=staged.candidate.identity(),
                declaration=declaration,
                tool_names=tuple(tool.name for tool in tools),
                bundled_tools=tuple(sorted(_names_of(module))),
            )
            outcome = QuarantineOutcome(
                code=QuarantineCode.OK,
                ok=True,
                origin=effective_origin,
                path=sanitize_text(str(staged.path), limit=200),
                name=staged.candidate.identity(),
                sha256=staged.sha256,
                size=staged.size,
                declaration=declaration,
                tools=record.tool_names,
            )
            return LoadOutcome(
                outcome=outcome,
                module=module,
                record=record,
                tools=tools,
                specs=tuple(tool.spec for tool in tools),
            )

    def load_result(self, result: QuarantineResult, generation: int, **kwargs: Any) -> LoadOutcome:
        """Load a fully-quarantined :class:`QuarantineResult`."""
        staged = result.staged
        expected = staged.sha256
        try:
            self._verify_or_fail(staged, expected)
        except QuarantineError:
            # The staged copy no longer matches what was validated; drop the
            # private artifact before surfacing the refusal.
            self.discard_staged(staged)
            raise
        outcome = self.load(staged, generation, expected_sha256=expected, **kwargs)
        if outcome.ok and result.specs:
            # The staged declaration was validated out-of-process; compare the
            # in-process names/schemas against it as a final consistency check.
            declared_names = {spec.name for spec in outcome.specs}
            expected_names = {spec.name for spec in result.specs}
            if declared_names != expected_names:  # pragma: no cover - tampered stage
                self.release_module(outcome.record.handle.name if outcome.record else "")
                return self._fail(
                    staged,
                    QuarantineCode.SPEC_MISMATCH,
                    "in-process declaration does not match the quarantined one",
                )
        return outcome

    def _verify_or_fail(self, staged: StagedSource, expected: str) -> None:
        try:
            current = hashlib.sha256(staged.path.read_bytes()).hexdigest()
        except OSError:
            current = ""
        if current != expected:
            raise QuarantineError(
                "staged source changed before import; refusing to execute"
            )

    @staticmethod
    def _hash_staged(staged: StagedSource) -> str:
        """SHA-256 of the bytes currently at the staged path.

        Refuses a symlink explicitly: a staged copy is content-addressed and
        private, so a symlink there is always an attack or a mistake, never a
        legitimate layout.
        """
        try:
            if staged.path.is_symlink():
                return ""
            if not staged.path.is_file():
                return ""
            return hashlib.sha256(staged.path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def _install_extra(self, extra: dict[str, Any] | None) -> None:
        if not extra:
            return
        for name, module in extra.items():
            if not isinstance(name, str) or not isinstance(module, ModuleType):
                raise ToolLoadError("extra modules must map module names to modules")
            if name not in sys.modules:
                sys.modules[name] = module

    def _build_tools(
        self,
        module: ModuleType,
        declared: Sequence[DeclaredTool],
        declaration: str,
        items: Sequence[Any],
        generation: int,
        staged: StagedSource,
        origin: str,
    ) -> tuple[RegisteredTool, ...]:
        tools: list[RegisteredTool] = []
        if declaration == "spec":
            spec = declared[0].to_spec()
            tools.append(
                RegisteredTool(
                    spec=spec,
                    run=_bind_run(module, spec.name),
                    origin=origin,
                    source=str(staged.path),
                    generation=generation,
                )
            )
            return tuple(tools)
        # register() form: ``items`` are the live objects produced by the one
        # in-process call that extraction already made; bind them without a
        # second call to a side-effecting function.
        if len(items) != len(declared):
            raise ToolSpecError(
                "register() produced a different number of tools than it declared"
            )
        for item, declared_tool in zip(items, declared):
            spec = declared_tool.to_spec()
            run = _run_from_registered(item)
            if run is None:
                raise ToolSpecError(
                    f"register() tool {spec.name!r} has no callable run"
                )
            tools.append(
                RegisteredTool(
                    spec=spec,
                    run=run,
                    origin=origin,
                    source=str(staged.path),
                    generation=generation,
                )
            )
        return tuple(tools)

    # -- cleanup ----------------------------------------------------------

    def release_module(self, module_name: str) -> bool:
        """Remove one loader-owned module from ``sys.modules``.

        Returns ``True`` when the module was present and removed, ``False`` when
        it was already gone. A name the loader does not own (or one outside its
        namespace) is never touched -- asking to release ``sys`` is a no-op, not
        a catastrophe.
        """
        if not self.owns(module_name):
            return False
        with self._lock:
            existed = sys.modules.pop(module_name, None) is not None
            self._owned.names.discard(module_name)
            return existed

    def release(self, module_names: Iterable[str]) -> tuple[str, ...]:
        """Release several modules, returning the names actually removed."""
        removed: list[str] = []
        for name in module_names:
            if self.release_module(name):
                removed.append(name)
        return tuple(removed)

    def release_record(self, record: ModuleRecord) -> bool:
        return self.release_module(record.handle.name)

    def discard_staged(self, staged: StagedSource) -> bool:
        """Delete the private staged copy backing a load, if it was created by us.

        A staged source is content-addressed and private (``staged.private``);
        deleting it after a failure leaves no partial artifact behind. A source
        read directly from the workspace (``Quarantine.open``) is never private,
        so this is a no-op and can never delete a user's file.
        """
        if not staged.private:
            return False
        try:
            staged.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _discard(self, module_name: str) -> None:
        """Roll back a partial load so a failure leaves no ``sys.modules`` entry."""
        if module_name in sys.modules:
            sys.modules.pop(module_name, None)
        self._owned.names.discard(module_name)

    def _fail(
        self, staged: StagedSource, code: QuarantineCode, detail: str
    ) -> LoadOutcome:
        # A failed load is a partial artifact: drop the private staged copy so a
        # retry re-stages cleanly and no validated-but-unexecutable file lingers.
        self.discard_staged(staged)
        return LoadOutcome(
            outcome=QuarantineOutcome(
                code=code,
                ok=False,
                origin=self._origin,
                path=sanitize_text(str(staged.path), limit=200),
                name=staged.candidate.identity(),
                sha256=staged.sha256,
                size=staged.size,
                detail=sanitize_text(detail),
                error_type=code.name,
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bind_run(module: ModuleType, tool_name: str) -> Callable[..., Any]:
    """Return the module's ``run`` guarded to return a valid result type."""
    run = module.run

    async def _run(args: dict[str, Any], ctx: Any) -> ToolExecutionResult:
        result = await run(args, ctx)
        if not isinstance(result, ToolExecutionResult):
            raise ToolSpecError(
                f"{tool_name}: run() must return a ToolExecutionResult"
            )
        return result

    _run.__name__ = f"{tool_name}_run"
    _run.__qualname__ = f"{tool_name}_run"
    return _run


def _run_from_registered(item: object) -> Callable[..., Any] | None:
    if isinstance(item, RegisteredTool):
        return item.run
    run = getattr(item, "run", None)
    if run is None and isinstance(item, dict):
        run = item.get("run")
    return run if callable(run) else None


def _names_of(module: ModuleType) -> list[str]:
    """Public names a module exposes for provenance reporting (never values)."""
    names: list[str] = []
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if isinstance(value, (str, int, float, bool, type(None))) or callable(value):
            names.append(name)
    return names


def _safe_detail(exc: BaseException) -> str:
    """A control-free, credential-free one-liner for a load failure."""
    return sanitize_text(f"{type(exc).__name__}: {exc}")


# Re-exported convenience for callers constructing loader + quarantine together.
def build_default_quarantine(config: Config | None = None, **overrides: Any) -> Quarantine:
    """Construct a :class:`Quarantine` from the ``[ext]`` config section."""
    ext = getattr(getattr(config, "v2", None), "ext", None) if config is not None else None
    kwargs: dict[str, Any] = {
        "max_file_bytes": getattr(ext, "max_file_bytes", 262_144),
    }
    kwargs.update(overrides)
    return Quarantine(**kwargs)
