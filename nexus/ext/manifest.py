"""The immutable extension manifest and its atomic, reference-counted handle.

Plan sections 6.1-6.4. A :class:`Manifest` is one frozen snapshot of everything
the loop may see in a single iteration: config, registered tools, skills, the
reserved extension maps, on-disk system-file contents, and the loaded-module
handles that back hot-reloaded code. Nothing about a manifest mutates in place;
a reload builds a whole new generation and swaps it atomically through
:class:`ManifestRef`.

Why generations instead of ``importlib.reload``
-----------------------------------------------
Version-stamped module names mean an in-flight tool call keeps running
generation *N* while new calls get generation *N+1*. ``ManifestRef.pin()``
synchronously marks a generation as in use and returns a :class:`ManifestLease`
that captures that exact immutable snapshot. A retired generation's cleanup
callback runs only once every pin on it has been released, so ``sys.modules``
entries and other per-generation resources are never dropped underneath live
code.

Immutability
------------
msgspec freezes the struct fields, but a bare ``dict`` would still be mutable
through its reference. ``__post_init__`` therefore copies every nested mapping
into a :class:`types.MappingProxyType`, and every nested sequence into a tuple,
so a manifest is *truly* immutable all the way down and callers cannot mutate a
mapping they built the manifest from after the fact.

Boundary
--------
This module is a manager-layer (L3) contract. It deliberately does not import
``nexus.tools`` (or any concrete skill/agent/hook/MCP type): the maps are typed
by small structural protocols so ``core`` and the loop can consume them without
importing ``nexus.ext``, and so no extension type creates an import cycle here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol, Self

import msgspec

from ..config import Config
from ..errors import ManifestError, StaleGenerationError

__all__ = [
    "AgentDef",
    "CleanupFailure",
    "EqualGenerationError",
    "Fingerprintable",
    "GenerationLease",
    "HookSpec",
    "MCPServerState",
    "Manifest",
    "ManifestDiff",
    "ManifestLease",
    "ManifestRef",
    "ModuleHandle",
    "NameDelta",
    "ProviderHandle",
    "ReloadFailure",
    "ReloadReport",
    "Skill",
    "SkillToolSet",
    "SystemFile",
    "SystemFiles",
    "ToolRegistration",
    "fingerprint",
]


class EqualGenerationError(ManifestError):
    """A *distinct* manifest claiming the current generation was offered.

    A generation number identifies one exact immutable snapshot, so two
    different objects can never share it. An equal-generation swap of the
    identical object is still a legitimate no-op; anything else must either be
    rejected (this error) or routed through :meth:`ManifestRef.compare_and_swap`
    by a serialized reload orchestrator. It lives here rather than in
    ``nexus.errors`` because it is meaningful only to this contract.
    """


class CleanupFailure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A retired generation's cleanup callback raised.

    Cleanup runs *after* a swap is committed and *outside* the ref lock, so a
    failing callback can neither roll back the swap nor wedge a reader. The
    failure is retained on the ref so an operator (or test) can observe and
    drain it; the live exception is kept for inspection but excluded from
    :meth:`to_dict`.
    """

    generation: int = 0
    error_type: str = ""
    error: str = ""
    exception: Any = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ManifestError("CleanupFailure.generation must be a non-negative int")
        if not isinstance(self.error_type, str):
            raise ManifestError("CleanupFailure.error_type must be a string")
        if not isinstance(self.error, str):
            raise ManifestError("CleanupFailure.error must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "error_type": self.error_type,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Structural views of the things a manifest holds
# ---------------------------------------------------------------------------
#
# The manifest must be constructible before the concrete skills/agents/hooks/MCP
# packets land, and it must not import them (that would couple every manager and
# invite a cycle once tools/builtin/meta.py imports ext.manager). These protocols
# name the *minimum* surface the loop is allowed to rely on; the real structs
# satisfy them structurally. ``ToolRegistration`` mirrors the public surface of
# ``nexus.tools.spec.RegisteredTool``.


class ToolRegistration(Protocol):
    """The harness-visible surface of a registered tool."""

    @property
    def name(self) -> str: ...

    @property
    def bundle(self) -> str: ...

    @property
    def mutates(self) -> bool: ...

    def to_schema(self) -> Any: ...


class Skill(Protocol):
    """A discoverable skill (name + progressive-disclosure description)."""

    name: str
    description: str


class AgentDef(Protocol):
    """A subagent definition. Reserved in this packet; always empty for now."""

    name: str


class HookSpec(Protocol):
    """A lifecycle hook definition. Reserved in this packet."""

    name: str


class ProviderHandle(Protocol):
    """A configured provider entry. Reserved in this packet."""

    name: str


class MCPServerState(Protocol):
    """The live state of one MCP server. Reserved in this packet."""

    name: str


class Fingerprintable(Protocol):
    """An extension entry that can describe itself for stable diffing.

    Implementing ``fingerprint`` (a cheap, deterministic string that changes
    exactly when the entry's *meaning* changes) lets :func:`fingerprint` and
    :meth:`ManifestDiff.between` treat an equal-but-rebuilt entry as unchanged.
    The contract is structural, so a concrete manager never has to import
    ``nexus.ext`` to participate, and ``nexus.ext`` never imports a manager.
    """

    def fingerprint(self) -> str: ...


# ---------------------------------------------------------------------------
# Stable fingerprints for change detection
# ---------------------------------------------------------------------------


def fingerprint(value: object) -> str:
    """Return a stable, comparable fingerprint for one manifest entry.

    The diff uses this to tell a genuine edit from a rebuild that produced an
    equal-but-distinct object, so unchanged entries do not churn the diff. It
    never imports a concrete manager and never calls ``__eq__`` on an entry.
    In order of preference it uses:

    1. a :class:`Fingerprintable` ``fingerprint()`` returning a non-empty string;
    2. ``to_dict()`` serialized as canonical JSON;
    3. a non-empty ``sha256`` attribute;
    4. a scalar token for primitives and sequences of them;
    5. ``id:<object id>`` -- identity, the conservative fallback.

    Callers that know better may pass a per-category ``fingerprints`` callable to
    :meth:`ManifestDiff.between` to override the default for their entries.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool:true" if value else "bool:false"
    if isinstance(value, (int, float)):
        return f"num:{value!r}"
    if isinstance(value, str):
        return "str:" + value
    if isinstance(value, (bytes, bytearray)):
        return "bytes:" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return "seq[" + ",".join(fingerprint(item) for item in value) + "]"

    method = getattr(value, "fingerprint", None)
    if callable(method):
        produced = method()
        if isinstance(produced, str) and produced:
            return "fp:" + produced

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            encoded = json.dumps(
                to_dict(),
                sort_keys=True,
                default=repr,
                separators=(",", ":"),
            )
        except Exception:  # noqa: BLE001 - a bad to_dict must not break diffing
            encoded = ""
        if encoded:
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            return "dict:" + digest

    sha = getattr(value, "sha256", None)
    if isinstance(sha, str) and sha:
        return "sha:" + sha

    return f"id:{id(value)}"


# ---------------------------------------------------------------------------
# System files
# ---------------------------------------------------------------------------


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class SystemFile(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One loaded on-disk system file (``SOUL.md``, ``MEMORY.md``, ...).

    The full content is retained because context assembly reads it every
    iteration; ``sha256`` lets a reload tell "unchanged" from "changed" without
    comparing whole documents.
    """

    name: str
    content: str = ""
    path: str | None = None
    sha256: str = ""
    size: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ManifestError("SystemFile.name must be a non-empty string")
        if not isinstance(self.content, str):
            raise ManifestError("SystemFile.content must be a string")
        if "\x00" in self.content:
            raise ManifestError(f"SystemFile {self.name!r} contains a NUL byte")
        if not self.sha256:
            object.__setattr__(self, "sha256", _content_hash(self.content))
        if self.size is None:
            object.__setattr__(self, "size", len(self.content.encode("utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }

    def fingerprint(self) -> str:
        """Content-addressed identity: same path+hash+size means unchanged."""
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SystemFiles(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The immutable set of loaded system files, keyed by logical name."""

    files: Mapping[str, SystemFile] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.files, Mapping):
            raise ManifestError("SystemFiles.files must be a mapping")
        frozen: dict[str, SystemFile] = {}
        for key, value in self.files.items():
            if not isinstance(key, str):
                raise ManifestError("SystemFiles keys must be strings")
            if not isinstance(value, SystemFile):
                raise ManifestError(
                    f"SystemFiles[{key!r}] must be a SystemFile, "
                    f"got {type(value).__name__}"
                )
            frozen[key] = value
        object.__setattr__(self, "files", MappingProxyType(frozen))

    @property
    def soul(self) -> SystemFile | None:
        return self.files.get("soul")

    @property
    def memory(self) -> SystemFile | None:
        return self.files.get("memory")

    def get(self, name: str, default: SystemFile | None = None) -> SystemFile | None:
        return self.files.get(name, default)

    def names(self) -> tuple[str, ...]:
        return tuple(self.files)

    def __contains__(self, name: object) -> bool:
        return name in self.files

    def __len__(self) -> int:
        return len(self.files)

    def to_dict(self) -> dict[str, Any]:
        return {name: entry.to_dict() for name, entry in self.files.items()}


# ---------------------------------------------------------------------------
# Loaded module handles / provenance
# ---------------------------------------------------------------------------


class ModuleHandle(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Provenance for one hot-loaded module plus its live import handle.

    ``module`` is excluded from :meth:`to_dict` (and therefore from serialized
    reports) because a module object is neither JSON-safe nor a thing a UI or
    the model should ever see.
    """

    name: str
    path: str
    generation: int = 0
    sha256: str = ""
    origin: str = "ext"
    module: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ManifestError("ModuleHandle.name must be a non-empty string")
        if not isinstance(self.path, str) or not self.path:
            raise ManifestError("ModuleHandle.path must be a non-empty string")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ManifestError("ModuleHandle.generation must be a non-negative int")
        if not isinstance(self.origin, str) or not self.origin:
            raise ManifestError("ModuleHandle.origin must be a non-empty string")

    @property
    def loaded(self) -> bool:
        return self.module is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "generation": self.generation,
            "sha256": self.sha256,
            "origin": self.origin,
            "loaded": self.loaded,
        }

    def fingerprint(self) -> str:
        """Content-addressed identity, deliberately ignoring the generation.

        A module's version-stamped ``name`` and ``generation`` change on every
        reload even when the source did not, so they are excluded when a
        ``sha256`` is known. Without a hash the name is the only stable-enough
        signal and is used instead (conservatively marking it changed).
        """
        material = {
            "path": self.path,
            "origin": self.origin,
            "content": self.sha256 or f"name:{self.name}",
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Skill-scoped bundled tools
# ---------------------------------------------------------------------------


class SkillToolSet(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One skill's quarantined, loaded ``tools/*.py`` associated with a fingerprint.

    A skill may ship ``tools/*.py``. Those modules are quarantined and loaded
    during a rebuild -- exactly like a workspace extension -- but they are
    deliberately **never** placed in :attr:`Manifest.tools`: they are exposed to
    the model only while the owning skill is active for a session/turn (plan
    section 5.4). This record is the immutable association the runtime reads from
    a pinned generation:

    * ``fingerprint`` is the whole-skill fingerprint the tools were loaded
      against, so a changed skill re-loads its tools while an unchanged one is
      reused object-for-object;
    * ``generation`` is the manifest generation the modules were imported under;
    * ``tools`` are the live, immutable ``RegisteredTool`` values (typed
      ``Any`` here so ``nexus.ext`` never imports ``nexus.tools``);
    * ``modules`` are the loader-owned module names backing them. They are also
      present in :attr:`Manifest.modules`, so the generation lease retires and
      releases them exactly once no in-flight call can still use them.

    ``error`` is populated only for a *quarantined* skill tool set that was
    retained for diagnostics; the all-or-nothing reload policy means a live
    generation never carries a partial or failed set.

    The field is named ``skill_fingerprint`` (rather than ``fingerprint``) so the
    class can expose a stable :meth:`fingerprint` for diffing: the digest ignores
    the per-rebuild generation and the version-stamped module names, exactly as
    :meth:`ModuleHandle.fingerprint` does.
    """

    skill: str
    skill_fingerprint: str = ""
    generation: int = 0
    tools: tuple[Any, ...] = ()
    modules: tuple[str, ...] = ()
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.skill, str) or not self.skill.strip():
            raise ManifestError("SkillToolSet.skill must be a non-empty string")
        if not isinstance(self.skill_fingerprint, str):
            raise ManifestError("SkillToolSet.skill_fingerprint must be a string")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ManifestError("SkillToolSet.generation must be a non-negative int")
        if not isinstance(self.error, str):
            raise ManifestError("SkillToolSet.error must be a string")
        object.__setattr__(self, "tools", tuple(self.tools))
        modules = tuple(self.modules)
        if not all(isinstance(name, str) and name for name in modules):
            raise ManifestError("SkillToolSet.modules must be non-empty strings")
        object.__setattr__(self, "modules", modules)

    @property
    def tool_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for tool in self.tools:
            name = getattr(tool, "name", None)
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "fingerprint": self.skill_fingerprint,
            "generation": self.generation,
            "tools": sorted(self.tool_names),
            "modules": sorted(self.modules),
            "error": self.error,
        }

    def fingerprint(self) -> str:
        """Stable association identity, deliberately ignoring the generation.

        The whole-skill fingerprint already covers the bundled files' content, so
        hashing the skill name, that fingerprint, and the tool names is enough to
        tell a genuine change from a rebuild that reproduced equal content. The
        version-stamped module names and the generation are excluded, exactly as
        :meth:`ModuleHandle.fingerprint` excludes them.
        """
        material = {
            "skill": self.skill,
            "skill_fingerprint": self.skill_fingerprint,
            "tools": sorted(self.tool_names),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def _freeze(mapping: Mapping[str, Any], *, sequence: bool = False) -> Mapping[str, Any]:
    """Copy a mapping into an immutable proxy, validating keys (and sequences)."""
    if not isinstance(mapping, Mapping):
        raise ManifestError("manifest entries must be provided as a mapping")
    frozen: dict[str, Any] = {}
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise ManifestError(f"manifest mapping keys must be strings, got {key!r}")
        frozen[key] = tuple(value) if sequence else value
    return MappingProxyType(frozen)


class Manifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One immutable generation of the whole live extension world.

    The loop reads a manifest at most once per iteration (``ManifestRef.get()``)
    and never sees a half-built world. ``agents``/``hooks``/``providers``/``mcp``
    are reserved for later Phase 4/5/6 packets and are empty in this packet.
    """

    generation: int = 0
    config: Config = msgspec.field(default_factory=Config)
    tools: Mapping[str, ToolRegistration] = msgspec.field(default_factory=dict)
    skills: Mapping[str, Skill] = msgspec.field(default_factory=dict)
    #: Skill-scoped bundled tools, keyed by skill name. Never part of ``tools``:
    #: a skill's ``tools/*.py`` are exposed only while that skill is active.
    skill_tools: Mapping[str, SkillToolSet] = msgspec.field(default_factory=dict)
    # --- reserved: present and typed, populated by later packets ---
    agents: Mapping[str, AgentDef] = msgspec.field(default_factory=dict)
    hooks: Mapping[str, tuple[HookSpec, ...]] = msgspec.field(default_factory=dict)
    providers: Mapping[str, ProviderHandle] = msgspec.field(default_factory=dict)
    mcp: Mapping[str, MCPServerState] = msgspec.field(default_factory=dict)
    # --- loaded artefacts ---
    system_files: SystemFiles = msgspec.field(default_factory=SystemFiles)
    modules: Mapping[str, ModuleHandle] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ManifestError("Manifest.generation must be a non-negative integer")
        if not isinstance(self.config, Config):
            raise ManifestError("Manifest.config must be a Config")
        if not isinstance(self.system_files, SystemFiles):
            raise ManifestError("Manifest.system_files must be SystemFiles")
        if not isinstance(self.skill_tools, Mapping):
            raise ManifestError("Manifest.skill_tools must be a mapping")
        for name, value in self.skill_tools.items():
            if not isinstance(value, SkillToolSet):
                raise ManifestError(
                    f"Manifest.skill_tools[{name!r}] must be a SkillToolSet, "
                    f"got {type(value).__name__}"
                )
        # Freeze every nested mapping so the manifest is immutable all the way
        # down, even if the caller keeps a reference to the dict it passed in.
        object.__setattr__(self, "tools", _freeze(self.tools))
        object.__setattr__(self, "skills", _freeze(self.skills))
        object.__setattr__(self, "skill_tools", _freeze(self.skill_tools))
        object.__setattr__(self, "agents", _freeze(self.agents))
        object.__setattr__(self, "hooks", _freeze(self.hooks, sequence=True))
        object.__setattr__(self, "providers", _freeze(self.providers))
        object.__setattr__(self, "mcp", _freeze(self.mcp))
        object.__setattr__(self, "modules", _freeze(self.modules))

    def to_dict(self) -> dict[str, Any]:
        """A sanitized, JSON-serializable view: no config values, no modules.

        Config is deliberately omitted rather than serialized: it can carry
        provider credentials (``${env:...}`` references and resolved values), and
        a manifest report must never leak them. Only names, counts, hashes, and
        provenance are exposed.
        """
        return {
            "generation": self.generation,
            "tools": sorted(self.tools),
            "skills": sorted(self.skills),
            "skill_tools": {name: s.to_dict() for name, s in self.skill_tools.items()},
            "agents": sorted(self.agents),
            "hooks": sorted(self.hooks),
            "providers": sorted(self.providers),
            "mcp": sorted(self.mcp),
            "modules": {name: h.to_dict() for name, h in self.modules.items()},
            "system_files": self.system_files.to_dict(),
        }

    def fingerprint(self) -> str:
        """A stable content digest for the whole generation (config omitted).

        Built from each entry's :func:`fingerprint`, so two generations whose
        entries are equal-but-rebuilt share a digest while a genuine edit
        changes it. Config is excluded deliberately: it can carry credentials
        and its equality semantics are manager-owned.
        """
        payload = {
            "tools": {name: fingerprint(v) for name, v in self.tools.items()},
            "skills": {name: fingerprint(v) for name, v in self.skills.items()},
            "skill_tools": {
                name: fingerprint(v) for name, v in self.skill_tools.items()
            },
            "agents": {name: fingerprint(v) for name, v in self.agents.items()},
            "hooks": {name: fingerprint(v) for name, v in self.hooks.items()},
            "providers": {name: fingerprint(v) for name, v in self.providers.items()},
            "mcp": {name: fingerprint(v) for name, v in self.mcp.items()},
            "modules": {name: fingerprint(v) for name, v in self.modules.items()},
            "system_files": {
                name: fingerprint(v) for name, v in self.system_files.files.items()
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Diff and reload report
# ---------------------------------------------------------------------------


def _name_tuple(values: object, label: str) -> tuple[str, ...]:
    """Normalize a name sequence to a sorted, validated tuple of strings."""
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ManifestError(f"NameDelta.{label} must be a sequence of names")
    result = tuple(values)
    if not all(isinstance(item, str) for item in result):
        raise ManifestError(f"NameDelta.{label} must contain only strings")
    return tuple(sorted(result))


class NameDelta(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Added/removed/changed names for one category of manifest entries."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "added", _name_tuple(self.added, "added"))
        object.__setattr__(self, "removed", _name_tuple(self.removed, "removed"))
        object.__setattr__(self, "changed", _name_tuple(self.changed, "changed"))

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }


#: Per-category key callables accepted by :meth:`ManifestDiff.between`.
EntryKey = Callable[[object], object]


def _entry_changed(old_value: object, new_value: object, key: EntryKey | None) -> bool:
    """Whether an entry changed, preferring identity then a stable fingerprint.

    Identity is checked first so reusing the same immutable object never invokes
    arbitrary ``__eq__`` (or even a fingerprint method). Distinct objects are
    compared by their fingerprint, which is what stops an equal-but-rebuilt
    entry from churning the diff.
    """
    if old_value is new_value:
        return False
    older = key(old_value) if key is not None else fingerprint(old_value)
    newer = key(new_value) if key is not None else fingerprint(new_value)
    return older != newer


def _delta(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    key: EntryKey | None = None,
) -> NameDelta:
    """Name-level diff using identity plus stable fingerprints for ``changed``."""
    added = tuple(sorted(name for name in new if name not in old))
    removed = tuple(sorted(name for name in old if name not in new))
    changed = tuple(
        sorted(
            name
            for name in new
            if name in old and _entry_changed(old[name], new[name], key)
        )
    )
    return NameDelta(added=added, removed=removed, changed=changed)


class ManifestDiff(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """What changed between two manifest generations, per category.

    Change detection is identity-first: reusing an immutable entry object is
    always "unchanged" and never calls ``__eq__``. When two entries are distinct
    objects they are compared by their stable :func:`fingerprint`, so a rebuild
    that reproduces an equal entry does not churn the diff. A category may
    override that with a custom ``fingerprints`` callable passed to
    :meth:`between`, which is how a concrete manager supplies a key without
    ``nexus.ext`` importing it.
    """

    from_generation: int = 0
    to_generation: int = 0
    config_changed: bool = False
    tools: NameDelta = msgspec.field(default_factory=NameDelta)
    skills: NameDelta = msgspec.field(default_factory=NameDelta)
    skill_tools: NameDelta = msgspec.field(default_factory=NameDelta)
    agents: NameDelta = msgspec.field(default_factory=NameDelta)
    hooks: NameDelta = msgspec.field(default_factory=NameDelta)
    providers: NameDelta = msgspec.field(default_factory=NameDelta)
    mcp: NameDelta = msgspec.field(default_factory=NameDelta)
    modules: NameDelta = msgspec.field(default_factory=NameDelta)
    system_files: NameDelta = msgspec.field(default_factory=NameDelta)

    def __post_init__(self) -> None:
        for label in ("from_generation", "to_generation"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ManifestError(f"ManifestDiff.{label} must be a non-negative int")
        for label in (
            "tools",
            "skills",
            "skill_tools",
            "agents",
            "hooks",
            "providers",
            "mcp",
            "modules",
            "system_files",
        ):
            if not isinstance(getattr(self, label), NameDelta):
                raise ManifestError(f"ManifestDiff.{label} must be a NameDelta")

    @classmethod
    def between(
        cls,
        old: Manifest,
        new: Manifest,
        *,
        fingerprints: Mapping[str, EntryKey] | None = None,
    ) -> ManifestDiff:
        """Diff two generations, optionally with per-category key callables.

        ``fingerprints`` maps a category name (``"tools"``, ``"skills"``,
        ``"skill_tools"``, ``"agents"``, ``"hooks"``, ``"providers"``, ``"mcp"``,
        ``"modules"``, ``"system_files"``) to a callable that returns a stable key
        for one entry in that category. Unlisted categories use
        :func:`fingerprint`.
        """
        if not isinstance(old, Manifest) or not isinstance(new, Manifest):
            raise ManifestError("ManifestDiff.between requires two Manifest values")
        if fingerprints is not None and not isinstance(fingerprints, Mapping):
            raise ManifestError("ManifestDiff.between fingerprints must be a mapping")

        def key_for(label: str) -> EntryKey | None:
            if fingerprints is None:
                return None
            candidate = fingerprints.get(label)
            if candidate is not None and not callable(candidate):
                raise ManifestError(
                    f"ManifestDiff.between fingerprints[{label!r}] must be callable"
                )
            return candidate

        return cls(
            from_generation=old.generation,
            to_generation=new.generation,
            # Identity, like every other category: a rebuild that reuses the same
            # Config object is "unchanged"; a re-read Config is conservatively
            # "changed". Equality is avoided because Config equality deliberately
            # excludes the v2 section (which can carry credentials).
            config_changed=old.config is not new.config,
            tools=_delta(old.tools, new.tools, key=key_for("tools")),
            skills=_delta(old.skills, new.skills, key=key_for("skills")),
            skill_tools=_delta(
                old.skill_tools, new.skill_tools, key=key_for("skill_tools")
            ),
            agents=_delta(old.agents, new.agents, key=key_for("agents")),
            hooks=_delta(old.hooks, new.hooks, key=key_for("hooks")),
            providers=_delta(old.providers, new.providers, key=key_for("providers")),
            mcp=_delta(old.mcp, new.mcp, key=key_for("mcp")),
            modules=_delta(old.modules, new.modules, key=key_for("modules")),
            system_files=_delta(
                old.system_files.files,
                new.system_files.files,
                key=key_for("system_files"),
            ),
        )

    def deltas(self) -> tuple[NameDelta, ...]:
        return (
            self.tools,
            self.skills,
            self.skill_tools,
            self.agents,
            self.hooks,
            self.providers,
            self.mcp,
            self.modules,
            self.system_files,
        )

    @property
    def changed(self) -> bool:
        return self.config_changed or any(not delta.empty for delta in self.deltas())

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_generation": self.from_generation,
            "to_generation": self.to_generation,
            "config_changed": self.config_changed,
            "changed": self.changed,
            "tools": self.tools.to_dict(),
            "skills": self.skills.to_dict(),
            "skill_tools": self.skill_tools.to_dict(),
            "agents": self.agents.to_dict(),
            "hooks": self.hooks.to_dict(),
            "providers": self.providers.to_dict(),
            "mcp": self.mcp.to_dict(),
            "modules": self.modules.to_dict(),
            "system_files": self.system_files.to_dict(),
        }


class ReloadFailure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One extension that failed to load during a reload (sanitized message)."""

    kind: str
    name: str
    error: str = ""
    error_type: str = ""
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ManifestError("ReloadFailure.kind must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ManifestError("ReloadFailure.name must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "error": self.error,
            "error_type": self.error_type,
            "path": self.path,
        }


class ReloadReport(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The outcome of one rebuild: what swapped, what failed, and how long.

    This is the object the ``ReloadExtensions`` tool hands back to the model, so
    it is deliberately small, deterministic, and fully JSON-serializable.
    """

    previous_generation: int = 0
    generation: int = 0
    changed: bool = False
    diff: ManifestDiff = msgspec.field(default_factory=ManifestDiff)
    failed: tuple[ReloadFailure, ...] = ()
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        for label in ("previous_generation", "generation"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ManifestError(f"ReloadReport.{label} must be a non-negative int")
        if not isinstance(self.diff, ManifestDiff):
            raise ManifestError("ReloadReport.diff must be a ManifestDiff")
        if not isinstance(self.failed, (list, tuple)):
            raise ManifestError("ReloadReport.failed must be a sequence")
        object.__setattr__(self, "failed", tuple(self.failed))
        if not all(isinstance(item, ReloadFailure) for item in self.failed):
            raise ManifestError("ReloadReport.failed must contain ReloadFailure values")
        if isinstance(self.duration_ms, bool) or not isinstance(
            self.duration_ms, (int, float)
        ):
            raise ManifestError("ReloadReport.duration_ms must be a number")
        object.__setattr__(self, "duration_ms", float(self.duration_ms))

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def summary(self) -> str:
        """One-line, deterministic digest covering *every* diff map.

        Category order is fixed so the text is stable across runs. ``config`` is
        reported separately from the entry maps because it is identity-compared.
        """
        parts: list[str] = []
        for label, delta in (
            ("tool", self.diff.tools),
            ("skill", self.diff.skills),
            ("skill tool", self.diff.skill_tools),
            ("agent", self.diff.agents),
            ("hook", self.diff.hooks),
            ("provider", self.diff.providers),
            ("mcp", self.diff.mcp),
            ("module", self.diff.modules),
            ("system file", self.diff.system_files),
        ):
            if delta.added:
                parts.append(f"+{len(delta.added)} {label}")
            if delta.removed:
                parts.append(f"-{len(delta.removed)} {label}")
            if delta.changed:
                parts.append(f"~{len(delta.changed)} {label}")
        if self.diff.config_changed:
            parts.append("~config")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if not parts:
            parts.append("no changes")
        head = f"gen {self.previous_generation} -> {self.generation}"
        return f"{head}: {', '.join(parts)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_generation": self.previous_generation,
            "generation": self.generation,
            "changed": self.changed,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "failed": [failure.to_dict() for failure in self.failed],
            "diff": self.diff.to_dict(),
        }


# ---------------------------------------------------------------------------
# The atomic, reference-counted handle
# ---------------------------------------------------------------------------


class ManifestLease:
    """A pinned claim on one manifest generation (the generation lease).

    Acquisition is synchronous (:meth:`ManifestRef.pin`), so a caller can never
    observe a generation without having incremented its refcount first. Release
    is idempotent and works as a context manager in both sync (``with``) and
    async (``async with``) code, so an exception or task cancellation in the
    body still releases the pin.
    """

    __slots__ = ("_manifest", "_ref", "_released")

    def __init__(self, ref: ManifestRef, manifest: Manifest):
        self._ref = ref
        self._manifest = manifest
        self._released = False

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    @property
    def generation(self) -> int:
        return self._manifest.generation

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Release the pin. Idempotent: the second and later calls are no-ops."""
        if self._released:
            return
        self._released = True
        self._ref._release(self._manifest)

    #: Alias so the lease reads naturally as a resource.
    close = release

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        state = "released" if self._released else "active"
        return f"ManifestLease(gen={self.generation}, {state})"


#: Historical/alternative name for the generation lease.
GenerationLease = ManifestLease


class ManifestRef:
    """A single atomic reference to the current :class:`Manifest`.

    Readers call :meth:`get` (one attribute read, no lock) or :meth:`pin` (which
    synchronously takes a lease). Writers call :meth:`swap` or, when serializing
    reloads, :meth:`compare_and_swap`. Generations are monotonic: a swap with a
    strictly greater generation installs the new manifest, the identical object
    is a no-op, and an older generation raises :class:`StaleGenerationError`.
    A *distinct* manifest claiming the current generation raises
    :class:`EqualGenerationError` rather than silently winning or losing.

    When a pinned generation is swapped out it is retired, not dropped. Its
    ``on_retire`` callback fires only after the last lease on that generation is
    released -- the mechanism that keeps version-stamped modules alive until no
    in-flight call can still be using them. Cleanup always runs after the swap is
    committed and outside the lock; a failing callback is caught, retained as a
    :class:`CleanupFailure` (see :attr:`cleanup_failures`), and never allowed to
    roll back a swap or wedge a reader.
    """

    __slots__ = (
        "_cleanup_failures",
        "_current",
        "_lock",
        "_on_cleanup_error",
        "_on_retire",
        "_pins",
        "_retired",
    )

    def __init__(
        self,
        manifest: Manifest | None = None,
        *,
        on_retire: Callable[[Manifest], None] | None = None,
        on_cleanup_error: Callable[[Manifest, BaseException], None] | None = None,
    ):
        if manifest is not None and not isinstance(manifest, Manifest):
            raise ManifestError("ManifestRef requires a Manifest or None")
        if on_retire is not None and not callable(on_retire):
            raise ManifestError("ManifestRef.on_retire must be callable or None")
        if on_cleanup_error is not None and not callable(on_cleanup_error):
            raise ManifestError(
                "ManifestRef.on_cleanup_error must be callable or None"
            )
        self._current: Manifest = manifest if manifest is not None else Manifest()
        self._pins: dict[int, int] = {}
        self._retired: dict[int, Manifest] = {}
        self._cleanup_failures: list[CleanupFailure] = []
        self._on_retire = on_retire
        self._on_cleanup_error = on_cleanup_error
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        return self._current.generation

    def get(self) -> Manifest:
        """Return the current generation. A single atomic read; no lock held."""
        return self._current

    def pin(self) -> ManifestLease:
        """Synchronously claim the current generation and return its lease."""
        with self._lock:
            manifest = self._current
            generation = manifest.generation
            self._pins[generation] = self._pins.get(generation, 0) + 1
        return ManifestLease(self, manifest)

    #: ``pin`` spelled as a resource acquisition.
    lease = pin

    def swap(self, manifest: Manifest, *, expected: Manifest | int | None = None) -> Manifest:
        """Install ``manifest`` atomically; return the previous generation.

        With ``expected`` given this is a conditional (compare-and-swap) install:
        the current generation must still match the expected manifest or
        generation, or :class:`StaleGenerationError` is raised and nothing
        changes. See :meth:`compare_and_swap`, which reads better for that use.

        An identical-object swap is a no-op. An older generation raises
        :class:`StaleGenerationError`; a distinct manifest claiming the current
        generation raises :class:`EqualGenerationError`.
        """
        return self._install(manifest, expected)

    def compare_and_swap(
        self,
        expected: Manifest | int,
        manifest: Manifest,
    ) -> Manifest:
        """Install ``manifest`` only if the current generation is still ``expected``.

        ``expected`` is the generation the caller last observed, as either a
        :class:`Manifest` (identity compare) or an ``int`` generation. A mismatch
        means another writer advanced the ref first, so
        :class:`StaleGenerationError` is raised and nothing changes -- the seam a
        serialized :class:`ExtensionManager` uses to avoid clobbering a
        concurrent reload.
        """
        if isinstance(expected, bool) or not isinstance(expected, (Manifest, int)):
            raise ManifestError(
                "ManifestRef.compare_and_swap expected must be a Manifest or int"
            )
        return self._install(manifest, expected)

    def try_compare_and_swap(
        self,
        expected: Manifest | int,
        manifest: Manifest,
    ) -> Manifest | None:
        """Like :meth:`compare_and_swap` but return ``None`` when the expected
        generation was already superseded instead of raising."""
        try:
            return self.compare_and_swap(expected, manifest)
        except StaleGenerationError:
            return None

    def _install(
        self,
        manifest: Manifest,
        expected: Manifest | int | None,
    ) -> Manifest:
        if not isinstance(manifest, Manifest):
            raise ManifestError("ManifestRef.swap requires a Manifest")
        if expected is not None and (
            isinstance(expected, bool) or not isinstance(expected, (Manifest, int))
        ):
            raise ManifestError(
                "ManifestRef expected must be a Manifest, a generation int, or None"
            )

        retired: Manifest | None = None
        with self._lock:
            current = self._current
            if expected is not None:
                if isinstance(expected, Manifest):
                    matches = expected is current
                    shown = expected.generation
                else:
                    matches = expected == current.generation
                    shown = expected
                if not matches:
                    raise StaleGenerationError(
                        f"expected current generation {shown}, "
                        f"found {current.generation}"
                    )
            if manifest is current:
                return current
            if manifest.generation < current.generation:
                raise StaleGenerationError(
                    f"cannot swap generation {manifest.generation} over "
                    f"current generation {current.generation}"
                )
            if manifest.generation == current.generation:
                raise EqualGenerationError(
                    f"generation {manifest.generation} already identifies the "
                    "current manifest; rebuild with generation + 1 or use "
                    "compare_and_swap"
                )
            self._current = manifest
            generation = current.generation
            if self._pins.get(generation, 0) > 0:
                # Still in use: defer cleanup until the last lease releases.
                self._retired[generation] = current
            else:
                retired = current
        if retired is not None:
            self._retire(retired)
        return current

    def _release(self, manifest: Manifest) -> None:
        generation = manifest.generation
        retired: Manifest | None = None
        with self._lock:
            count = self._pins.get(generation, 0)
            if count <= 0:
                return
            if count > 1:
                self._pins[generation] = count - 1
            else:
                del self._pins[generation]
                retired = self._retired.pop(generation, None)
        if retired is not None:
            self._retire(retired)

    def _retire(self, manifest: Manifest) -> None:
        """Run the retirement callback outside the lock, isolating any failure.

        Called only after the swap is committed and the lock released, so a
        broken callback can neither roll back the generation nor deadlock a
        reader that pins inside it. The failure is recorded for
        :attr:`cleanup_failures`; ``on_cleanup_error`` (if installed) is then
        notified, and a notification failure is itself swallowed.
        """
        callback = self._on_retire
        if callback is None:
            return
        try:
            callback(manifest)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            failure = CleanupFailure(
                generation=manifest.generation,
                error_type=type(exc).__name__,
                error=str(exc),
                exception=exc,
            )
            with self._lock:
                self._cleanup_failures.append(failure)
            handler = self._on_cleanup_error
            if handler is not None:
                with contextlib.suppress(Exception):
                    handler(manifest, exc)

    @property
    def pinned_generations(self) -> Mapping[int, int]:
        """A read-only snapshot of live pin counts (diagnostics/tests)."""
        with self._lock:
            return MappingProxyType(dict(self._pins))

    @property
    def retired_generations(self) -> tuple[int, ...]:
        """Generations retired while still pinned, awaiting cleanup."""
        with self._lock:
            return tuple(sorted(self._retired))

    @property
    def cleanup_failures(self) -> tuple[CleanupFailure, ...]:
        """Failures raised by retirement cleanup, oldest first."""
        with self._lock:
            return tuple(self._cleanup_failures)

    def drain_cleanup_failures(self) -> tuple[CleanupFailure, ...]:
        """Return and clear the collected cleanup failures."""
        with self._lock:
            failures = tuple(self._cleanup_failures)
            self._cleanup_failures.clear()
        return failures

    def clear_cleanup_failures(self) -> None:
        """Discard collected cleanup failures without returning them."""
        with self._lock:
            self._cleanup_failures.clear()

    def __repr__(self) -> str:
        return (
            f"ManifestRef(gen={self.generation}, "
            f"pins={dict(self.pinned_generations)!r})"
        )
