"""Load Nexus's speech-to-text model and record how much memory that uses.

The load path is the dictation runtime: Kestrel's ``ParakeetTdtRuntime`` on the
verified local checkpoint (``moondream/parakeet-redux``). Device selection matches
Nexus (``auto``: Metal on this Mac, otherwise CUDA, otherwise CPU).

Run from the repository root:

    .venv/bin/python "memory usage/speech-to-text/measure.py"

The program writes ``measurement.md`` next to itself. It only loads weights that
are already cached under ``~/.nexus/models/voice/``. It does not download, and it
does not transcribe audio.
"""

from __future__ import annotations

import ctypes
import gc
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

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


def _dir_bytes(path: Path) -> int:
    total = 0
    for file in path.iterdir():
        if file.is_file() and not file.is_symlink():
            total += file.stat().st_size
    return total


def _tensor_bytes(model: object) -> int | None:
    parameters = getattr(model, "parameters", None)
    buffers = getattr(model, "buffers", None)
    if not callable(parameters) or not callable(buffers):
        return None
    total = 0
    for tensor in list(parameters()) + list(buffers()):
        total += int(tensor.numel()) * int(tensor.element_size())
    return total


def _write(text: str) -> None:
    (HERE / "measurement.md").write_text(text, encoding="utf-8")


def main() -> None:
    started = sample("Python interpreter")
    print(f"interpreter footprint: {_mib(int(started['phys_footprint']))}", flush=True)

    from nexus.voice.engine import MODEL_ID, _build_runtime
    from nexus.voice.store import REVISION

    model_dir = (
        Path.home() / ".nexus" / "models" / "voice" / "parakeet-redux" / REVISION
    )
    if not model_dir.is_dir():
        raise SystemExit(f"Speech-to-text checkpoint is not cached: {model_dir}")

    import torch
    from kestrel.models.parakeet_tdt import ParakeetTdtRuntime  # noqa: F401  (imports the runtime Nexus loads)

    imported = sample("Torch and Kestrel imported")
    print(
        f"runtime imported footprint: {_mib(int(imported['phys_footprint']))}",
        flush=True,
    )

    load_started = time.perf_counter()
    runtime, device = _build_runtime(model_dir, "auto")
    load_seconds = time.perf_counter() - load_started
    # Keep the runtime alive until the reading below is taken.
    loaded = sample("Parakeet model loaded")
    weights = _tensor_bytes(runtime.model)
    metal = ""
    if device == "mps":
        torch.mps.synchronize()
        metal = (
            f"Metal allocator: {_mib(torch.mps.current_allocated_memory())} allocated, "
            f"{_mib(torch.mps.driver_allocated_memory())} reserved by the driver."
        )
    print(
        f"model loaded footprint: {_mib(int(loaded['phys_footprint']))} on {device}",
        flush=True,
    )

    rows = [started, imported, loaded]
    loaded_footprint = int(loaded["phys_footprint"])
    model_added = loaded_footprint - int(imported["phys_footprint"])
    lines = [
        "# Speech-to-text memory",
        "",
        f"A process that has loaded the dictation model uses **{_mib(loaded_footprint)}** "
        f"of physical memory. Loading the checkpoint, after Torch and Kestrel are already "
        f"imported, adds **{_mib(model_added)}**.",
        "",
        f"Measured by `measure.py` on {time.strftime('%Y-%m-%d %H:%M %Z')} ({_machine()}).",
        "",
        "The program loads the dictation checkpoint Nexus uses "
        f"(`{MODEL_ID}`, revision `{REVISION}`) through Kestrel's `ParakeetTdtRuntime`, "
        "then reads this process's physical footprint. That is the number Activity Monitor "
        "shows as Memory. Resident size is the classic RSS figure. The two differ because "
        "the footprint counts compressed pages and Metal allocations, while RSS counts "
        "resident pages, including some file-backed ones.",
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
            f"Resolved device: `{device}`. Load took {load_seconds:.1f} s.",
            f"Checkpoint on disk: `{model_dir}` ({_size(_dir_bytes(model_dir))}).",
        ]
    )
    if weights is not None:
        lines.append(
            f"Parameter and buffer tensors report {_mib(weights)}. "
            "That is the packed ternary storage Torch can see. It is smaller than the "
            f"{_mib(model_added)} the process grew by, because the load also builds the "
            "Metal copy, the Kestrel decode graph, and allocator scratch."
        )
    if metal:
        lines.append(metal)
    lines.extend(
        [
            f"Graphics ledger: {_mib(int(loaded['graphics']))}. "
            f"Neural ledger: {_mib(int(loaded['neural']))}. "
            "Those tags sit inside the physical footprint; they are not extra on top of it.",
            "",
            "## What the number is",
            "",
            "The last row is a process whose only job is this model: the Python interpreter, "
            "Torch, Kestrel, and the loaded Parakeet weights. The whole dictation stack is "
            'the last row. The model itself is the jump from "Torch and Kestrel imported" '
            'to "Parakeet model loaded".',
            "",
            "Inside the Nexus daemon those same bytes sit on top of the daemon itself. "
            "This program does not include the daemon, a recording, or a transcription. "
            "A live dictation pass allocates temporary activation memory beyond the loaded weights.",
            "",
            "## Run again",
            "",
            "```sh",
            "cd /path/to/nexus",
            '.venv/bin/python "memory usage/speech-to-text/measure.py"',
            "```",
            "",
            "Requires the voice extra (`kestrel`, Torch) and a checkpoint already downloaded "
            "by `nexus voice download`.",
            "",
        ]
    )
    text = "\n".join(lines)
    _write(text)
    print(text)


if __name__ == "__main__":
    main()
