"""Load Nexus's text-to-speech model and record how much memory that uses.

The load path is the ``/speak`` worker: an onnxruntime session for Paradee-8M
(int8, CPU) plus misaki's English grapheme-to-phoneme model (spaCy
``en_core_web_sm``). Hugging Face is forced offline, so a missing cache fails
instead of downloading.

Run from the repository root:

    .venv/bin/python "memory usage/text-to-speech/measure.py"

The program writes ``measurement.md`` next to itself. It does not synthesize
or play audio.
"""

from __future__ import annotations

import ctypes
import gc
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

# Set before huggingface_hub is imported. Matches a warm /speak worker: cached files only.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

HERE = Path(__file__).resolve().parent
TASK_VM_INFO = 22


class _TaskVmInfo(ctypes.Structure):
    """Prefix of macOS ``task_vm_info`` through the rev3 footprint fields.

    ``phys_footprint`` is the figure Activity Monitor shows as Memory.
    ``ledger_phys_footprint_peak`` is the high-water mark of that figure.
    """

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("region_count", ctypes.c_int32),
        ("page_size", ctypes.c_int32),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_peak", ctypes.c_uint64),
        ("device", ctypes.c_uint64),
        ("device_peak", ctypes.c_uint64),
        ("internal", ctypes.c_uint64),
        ("internal_peak", ctypes.c_uint64),
        ("external", ctypes.c_uint64),
        ("external_peak", ctypes.c_uint64),
        ("reusable", ctypes.c_uint64),
        ("reusable_peak", ctypes.c_uint64),
        ("purgeable_volatile_pmap", ctypes.c_uint64),
        ("purgeable_volatile_resident", ctypes.c_uint64),
        ("purgeable_volatile_virtual", ctypes.c_uint64),
        ("compressed", ctypes.c_uint64),
        ("compressed_peak", ctypes.c_uint64),
        ("compressed_lifetime", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
        ("min_address", ctypes.c_uint64),
        ("max_address", ctypes.c_uint64),
        ("ledger_phys_footprint_peak", ctypes.c_int64),
        ("ledger_purgeable_nonvolatile", ctypes.c_int64),
        ("ledger_purgeable_novolatile_compressed", ctypes.c_int64),
        ("ledger_purgeable_volatile", ctypes.c_int64),
        ("ledger_purgeable_volatile_compressed", ctypes.c_int64),
        ("ledger_tag_network_nonvolatile", ctypes.c_int64),
        ("ledger_tag_network_nonvolatile_compressed", ctypes.c_int64),
        ("ledger_tag_network_volatile", ctypes.c_int64),
        ("ledger_tag_network_volatile_compressed", ctypes.c_int64),
        ("ledger_tag_media_footprint", ctypes.c_int64),
        ("ledger_tag_media_footprint_compressed", ctypes.c_int64),
        ("ledger_tag_media_nofootprint", ctypes.c_int64),
        ("ledger_tag_media_nofootprint_compressed", ctypes.c_int64),
        ("ledger_tag_graphics_footprint", ctypes.c_int64),
        ("ledger_tag_graphics_footprint_compressed", ctypes.c_int64),
        ("ledger_tag_graphics_nofootprint", ctypes.c_int64),
        ("ledger_tag_graphics_nofootprint_compressed", ctypes.c_int64),
        ("ledger_tag_neural_footprint", ctypes.c_int64),
        ("ledger_tag_neural_footprint_compressed", ctypes.c_int64),
        ("ledger_tag_neural_nofootprint", ctypes.c_int64),
        ("ledger_tag_neural_nofootprint_compressed", ctypes.c_int64),
    ]


def _task_vm_info() -> _TaskVmInfo:
    if sys.platform != "darwin":
        raise SystemExit(
            "This measurement reads the macOS task footprint. It is not implemented for other systems."
        )
    libc = ctypes.CDLL(None)
    libc.task_info.argtypes = [
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    libc.task_info.restype = ctypes.c_int
    info = _TaskVmInfo()
    count = ctypes.c_uint(ctypes.sizeof(info) // 4)
    port = ctypes.c_uint.in_dll(libc, "mach_task_self_")
    status = libc.task_info(port, TASK_VM_INFO, ctypes.byref(info), ctypes.byref(count))
    if status != 0:
        raise SystemExit(f"task_info failed with kern_return {status}")
    return info


def _mib(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):,.1f} MiB"


def _size(num_bytes: int) -> str:
    if num_bytes < 100 * 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return _mib(num_bytes)


def sample(label: str) -> dict[str, int | str]:
    """One reading after a collection, so a just-freed buffer is not still charged."""
    gc.collect()
    info = _task_vm_info()
    return {
        "label": label,
        "phys_footprint": int(info.phys_footprint),
        "resident_size": int(info.resident_size),
        "resident_size_peak": int(info.resident_size_peak),
        "footprint_peak": int(info.ledger_phys_footprint_peak),
        "graphics": int(info.ledger_tag_graphics_footprint),
        "neural": int(info.ledger_tag_neural_footprint),
        "ru_maxrss": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
    }


def _machine() -> str:
    model = subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip()
    mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    return (
        f"{model}, {mem / (1024**3):.0f} GiB RAM, "
        f"{platform.system()} {platform.release()}, Python {platform.python_version()}"
    )


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file in path.rglob("*"):
        if file.is_file() and not file.is_symlink():
            total += file.stat().st_size
    return total


def _write(text: str) -> None:
    (HERE / "measurement.md").write_text(text, encoding="utf-8")


def main() -> None:
    started = sample("Python interpreter")
    print(f"interpreter footprint: {_mib(int(started['phys_footprint']))}", flush=True)

    import numpy  # noqa: F401
    import onnxruntime
    import huggingface_hub  # noqa: F401

    ort_imported = sample("onnxruntime imported")
    print(
        f"onnxruntime footprint: {_mib(int(ort_imported['phys_footprint']))}",
        flush=True,
    )

    from nexus.host_support.speech import (
        _CONFIG_FILE,
        _MODEL_FILE,
        _REPO_ID,
        _REVISION,
        _load_engine,
        _phonemizer_dir,
    )

    # Importing misaki pulls spaCy. The English model itself loads inside _load_engine.
    import misaki  # noqa: F401
    import spacy  # noqa: F401

    libraries = sample("misaki and spaCy imported")
    print(
        f"phonemizer libraries footprint: {_mib(int(libraries['phys_footprint']))}",
        flush=True,
    )

    load_started = time.perf_counter()
    engine = _load_engine()
    load_seconds = time.perf_counter() - load_started
    loaded = sample("Paradee model loaded")
    print(f"model loaded footprint: {_mib(int(loaded['phys_footprint']))}", flush=True)

    # Resolve the cached files the loader just opened, for the disk-size line.
    from huggingface_hub import hf_hub_download

    model_path = Path(hf_hub_download(_REPO_ID, _MODEL_FILE, revision=_REVISION))
    config_path = Path(hf_hub_download(_REPO_ID, _CONFIG_FILE, revision=_REVISION))
    phonemizer = _phonemizer_dir()
    input_names = [item.name for item in engine.session.get_inputs()]
    provider = engine.session.get_providers()

    rows = [started, ort_imported, libraries, loaded]
    loaded_footprint = int(loaded["phys_footprint"])
    model_added = loaded_footprint - int(libraries["phys_footprint"])
    lines = [
        "# Text-to-speech memory",
        "",
        f"A process that has loaded the speech model uses **{_mib(loaded_footprint)}** "
        f"of physical memory. Loading Paradee and the English phonemizer, after onnxruntime, "
        f"misaki, and spaCy are already imported, adds **{_mib(model_added)}**.",
        "",
        f"Measured by `measure.py` on {time.strftime('%Y-%m-%d %H:%M %Z')} ({_machine()}).",
        "",
        "The program loads the `/speak` stack Nexus uses: Paradee-8M int8 "
        f"(`{_REPO_ID}` at `{_REVISION}`) in an onnxruntime CPU session, plus misaki "
        "and spaCy's `en_core_web_sm` phonemizer. It then reads this process's physical "
        "footprint. That is the number Activity Monitor shows as Memory. Resident size "
        "is the classic RSS figure. The two differ because the footprint counts compressed "
        "pages, while RSS counts resident pages, including some file-backed ones.",
        "",
        "## Result",
        "",
        "| Stage | Physical footprint | Resident size | Added since the previous stage |",
        "| --- | ---: | ---: | ---: |",
    ]
    previous = 0
    for row in rows:
        footprint = int(row["phys_footprint"])
        added = footprint - previous
        lines.append(
            f"| {row['label']} | {_mib(footprint)} | {_mib(int(row['resident_size']))} | {_mib(added)} |"
        )
        previous = footprint
    lines.extend(
        [
            "",
            f"Peak physical footprint during this process: {_mib(int(loaded['footprint_peak']))}.",
            f"Peak resident size: {_mib(int(loaded['resident_size_peak']))}.",
            f"`ru_maxrss`: {_mib(int(loaded['ru_maxrss']))}.",
            "",
            f"Execution providers: {', '.join(provider)}. Session inputs: {', '.join(input_names)}.",
            f"onnxruntime {onnxruntime.__version__}. Load took {load_seconds:.1f} s.",
            f"Model file: `{model_path}` ({_size(_file_bytes(model_path))}).",
            f"Vocabulary: `{config_path}` ({_size(_file_bytes(config_path))}).",
            f"Phonemizer directory: `{phonemizer}` ({_size(_tree_bytes(phonemizer))} on disk).",
            f"Loaded vocabulary entries: {len(engine.vocab)}.",
            f"Graphics ledger: {_mib(int(loaded['graphics']))}. "
            f"Neural ledger: {_mib(int(loaded['neural']))}. "
            "Those tags sit inside the physical footprint; they are not extra on top of it.",
            "",
            "## What the number is",
            "",
            "The last row is a process whose only job is this model: the Python interpreter, "
            "onnxruntime, misaki, spaCy, the English phonemizer, and the Paradee session. "
            f"The ONNX file is {_size(_file_bytes(model_path))} on disk. "
            f"Importing misaki and spaCy adds more than the ONNX runtime does, and the "
            f"{_mib(model_added)} load step is the phonemizer model plus the Paradee session. "
            "That is the warm `/speak` worker, without the Nexus daemon around it.",
            "",
            "The Nexus daemon keeps that worker in a child process and unloads it after "
            "10 idle minutes. This program does not include the daemon, and it does not "
            "synthesize a sentence. Generating audio allocates short-lived buffers beyond "
            "the loaded session.",
            "",
            "## Run again",
            "",
            "```sh",
            "cd /path/to/nexus",
            '.venv/bin/python "memory usage/text-to-speech/measure.py"',
            "```",
            "",
            "Requires the speak extra (onnxruntime, misaki, huggingface_hub) and a Paradee "
            "cache plus `~/.nexus/models/speech/python` already downloaded by `/speak`.",
            "",
        ]
    )
    # ``engine`` stays referenced through the write so the loaded session is the one just measured.
    text = "\n".join(lines)
    _write(text)
    print(text)


if __name__ == "__main__":
    main()
