"""Voice model status. Never loads a model, imports a runtime or downloads."""

from __future__ import annotations

import importlib.util
import platform
from typing import Any

from . import hf_cache
from .store import REPOS

STT = {
    "repos": [(repo.repo, repo.revision, list(repo.files)) for repo in REPOS],
    "modules": ["numpy", "torch", "transformers", "constriction", "librosa", "huggingface_hub"],
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


def entry(spec: dict[str, Any], *, supported: bool = True) -> dict[str, Any]:
    repo, revision, _ = spec["repos"][0]
    cached = [hf_cache.find(r, rev, files) for r, rev, files in spec["repos"]]
    total = sum(hf_cache.size(r) for r, _, _ in spec["repos"])
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
    return base | {"state": "ready"}


def _chosen(spec: dict[str, Any], repo: str) -> dict[str, Any]:
    if repo == spec["repos"][0][0]:
        return spec
    # A custom repo has no pinned revision or file list, so any snapshot counts.
    return spec | {"repos": [(repo, "main", [])]}


def status() -> dict[str, Any]:
    from ..config import ConfigFile
    values = ConfigFile().read()["values"]
    system, machine = platform.system(), platform.machine().lower()
    libc = platform.libc_ver()[0].lower()
    stt_supported = (system == "Darwin" and machine == "arm64") or (
        system == "Linux" and machine == "x86_64" and libc == "glibc"
    )
    stt_repo = values["voice"]["stt_model"]
    stt = entry(_chosen(STT, stt_repo), supported=stt_supported)
    if stt_repo != STT["repos"][0][0]:
        stt.update(state="unsupported", error="Only the pinned Bark STT model is supported")
    return {
        "stt": stt,
        "tts": entry(_chosen(TTS, values["speech"]["tts_model"]), supported=system in ("Darwin", "Linux")),
    }
