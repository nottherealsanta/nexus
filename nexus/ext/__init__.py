"""Hot extension management (plan sections 6.1-6.4).

Phase 4 is delivered in packets. This package currently exposes the immutable
manifest contract and its atomic, reference-counted handle:

* :class:`~nexus.ext.manifest.Manifest` -- one frozen generation of the live
  extension world (config, tools, skills, reserved maps, system files, loaded
  modules);
* :class:`~nexus.ext.manifest.ManifestRef` -- the single atomic reference the
  loop reads once per iteration, with monotonic generations and reference-counted
  :class:`~nexus.ext.manifest.ManifestLease` pins;
* :class:`~nexus.ext.manifest.ManifestDiff` / :class:`ReloadReport` -- sanitized,
  JSON-serializable descriptions of what changed.

The reload orchestrator (:class:`~nexus.ext.manager.ExtensionManager`) and
validation (:mod:`nexus.ext.quarantine`) complete the package. ``ExtensionManager``
is exposed lazily (PEP 562) so importing ``nexus.ext.manifest`` -- which
``nexus.tools.loader`` does -- never triggers the manager's import of the tool
loader and forms no cycle. ``nexus.ext`` is a manager-layer package and is never
imported by ``core``.
"""

from __future__ import annotations

from .manifest import (
    AgentDef,
    CleanupFailure,
    EqualGenerationError,
    Fingerprintable,
    GenerationLease,
    HookSpec,
    Manifest,
    ManifestDiff,
    ManifestLease,
    ManifestRef,
    MCPServerState,
    ModuleHandle,
    NameDelta,
    ProviderHandle,
    ReloadFailure,
    ReloadReport,
    Skill,
    SkillToolSet,
    SystemFile,
    SystemFiles,
    ToolRegistration,
    fingerprint,
)

__all__ = [
    "AgentDef",
    "CleanupFailure",
    "EqualGenerationError",
    "ExtensionManager",
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


def __getattr__(name: str):
    """Lazily expose :class:`ExtensionManager` without an import cycle.

    ``nexus.tools.loader`` imports ``nexus.ext.manifest``; eagerly importing the
    manager here would make ``nexus.tools.loader`` import a half-initialized
    loader. Resolving it on first access keeps ``from nexus.ext import
    ExtensionManager`` working while leaving the manifest contract dependency-free.
    """
    if name == "ExtensionManager":
        from .manager import ExtensionManager

        return ExtensionManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
