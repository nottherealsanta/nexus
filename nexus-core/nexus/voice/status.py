"""Voice model status. Never loads a model, imports a runtime or downloads."""

from __future__ import annotations

import importlib.util
import platform
from importlib import metadata
from typing import Any

from . import hf_cache
from .store import EXPECTED_SIZES, REPOS, REVIEWED_RUNTIME, unsupported_reason

STT = {
    "repos": [(repo.repo, repo.revision, list(repo.files)) for repo in REPOS],
    "modules": ["moondream", "kestrel", "kestrel_native", "kestrel_kernels", "torch", "numpy", "huggingface_hub"],
    "versions": REVIEWED_RUNTIME,
    "extra": "voice",
}
TTS = {
    "repos": [("gradium/phonon-31m-en", "main", ["config.json", "model.q8.gguf", "tokenizer.json",
                                               "default-voice.safetensors"])],
    "modules": ["ptts", "numpy"],
    "extra": "voice",
}


def _missing(modules: list[str]) -> list[str]:
    return [m for m in modules if importlib.util.find_spec(m) is None]


def _version_error(versions: dict[str, str]) -> str | None:
    for package, expected in versions.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            return f"missing {package}; run: nexus voice init"
        if actual != expected:
            return f"{package} {actual} is not supported (reviewed: {expected}); run: nexus voice init"
    return None


def entry(spec: dict[str, Any], *, supported: bool = True) -> dict[str, Any]:
    repo, revision, _ = spec["repos"][0]
    cached = [hf_cache.find(r, rev, files) for r, rev, files in spec["repos"]]
    total = sum(hf_cache.size(r) for r, _, _ in spec["repos"])
    if not all(cached) and not any(hf_cache.incomplete(r) for r, _, _ in spec["repos"]):
        # Report the download size for an absent pinned model; consent needs it.
        total = sum(EXPECTED_SIZES.get(r, 0) for r, _, _ in spec["repos"]) or total
    base = {"repo": repo, "revision": revision, "size": total,
            "cache_path": str(hf_cache.repo_dir(repo)), "error": None}
    if not supported:
        return base | {"state": "unsupported"}
    if any(hf_cache.incomplete(r) for r, _, _ in spec["repos"]):
        return base | {"state": "downloading"}
    if not all(cached):
        return base | {"state": "absent"}
    missing = _missing(spec["modules"])
    if missing:
        return base | {"state": "runtime_missing",
                       "error": f"missing {', '.join(missing)}; run: nexus voice init"}
    version_error = _version_error(spec.get("versions", {}))
    if version_error:
        return base | {"state": "runtime_missing", "error": version_error}
    return base | {"state": "ready"}


def _chosen(spec: dict[str, Any], repo: str) -> dict[str, Any]:
    if repo == spec["repos"][0][0]:
        return spec
    # A custom repo has no pinned revision or file list, so any snapshot counts.
    return spec | {"repos": [(repo, "main", [])]}


def _glibc_at_least(version: str, wanted: tuple[int, int]) -> bool:
    try:
        major, minor = (int(part) for part in version.split(".", 2)[:2])
    except (TypeError, ValueError):
        return False
    return (major, minor) >= wanted


def _stt_supported(system: str, machine: str, libc: str, libc_version: str, mac_version: str) -> bool:
    """Whether the pinned native runtime has wheels for this platform."""
    if system == "Darwin":
        if machine != "arm64":
            return False
        try:
            return int(mac_version.split(".", 1)[0]) >= 14
        except (TypeError, ValueError):
            return False
    if system != "Linux" or machine not in {"x86_64", "aarch64"}:
        return False
    if libc != "glibc":
        return False
    # torch 2.15 needs glibc 2.28; Kestrel's aarch64 kernel wheel needs 2.34.
    return _glibc_at_least(libc_version, (2, 34) if machine == "aarch64" else (2, 28))


def _platform_stt_supported() -> bool:
    libc, libc_version = platform.libc_ver()
    return _stt_supported(
        platform.system(),
        platform.machine().lower(),
        libc.lower(),
        libc_version,
        platform.mac_ver()[0],
    )


def status() -> dict[str, Any]:
    from ..config import ConfigFile
    values = ConfigFile().read()["values"]
    system = platform.system()
    stt_repo = values["voice"]["stt_model"]
    supported = _platform_stt_supported()
    stt = entry(_chosen(STT, stt_repo), supported=supported)
    reason = unsupported_reason(stt_repo)
    if reason is not None:
        stt.update(state="unsupported", error=reason)
    elif not supported:
        stt["error"] = "STT needs macOS 14+ on Apple silicon or glibc Linux x86-64/aarch64"
    return {
        "stt": stt,
        "tts": entry(_chosen(TTS, values["speech"]["tts_model"]), supported=system in ("Darwin", "Linux")),
    }
