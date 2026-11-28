"""Opt-in local STT benchmark. Never downloads a model.

The pinned snapshot must already be in the standard Hugging Face cache (download
it through Settings or ``POST /api/voice/stt/download`` first). The benchmark
uses the same isolated, offline Photon worker as the daemon.

Run from ``nexus-core``:

    uv run --extra voice python -m nexus.voice.benchmark speech.wav --device cpu --repeat 3

For the recorder feasibility gate (plan 54 A1), run the simulated-live mode:

    uv run --extra voice python -m nexus.voice.benchmark --live-sim speech.wav --window 10 --cadence 1.0

``--live-sim`` also accepts the WAV as its value. The existing one-shot form is
unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import sys
import time
from pathlib import Path
from typing import Any

from .audio import parse_wav
from .engine import PhotonWorkerEngine
from .live_sim import BacklogSample, LiveSimConfig, simulate_wav
from .store import ModelPaths, ModelStore


def _max_rss_bytes(children: bool = False) -> int:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF)
    # macOS reports bytes; Linux reports KiB.
    return int(usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024)


def _current_rss_bytes() -> int:
    """Current RSS where the platform exposes it, otherwise the process high-water mark."""

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", encoding="utf-8") as status:
                for line in status:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    return _max_rss_bytes()


def _read_process_rss_bytes(pid: int | None) -> int | None:
    """Current RSS of another process where the platform exposes it.

    Only Linux's ``/proc/<pid>/status`` is used. Other platforms return ``None``
    so the benchmark reports worker RSS as unavailable instead of re-labelling
    the parent's or a high-water figure as a time series.
    """

    if pid is None or not sys.platform.startswith("linux"):
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _worker_pid(engine: Any) -> int | None:
    # PhotonWorkerEngine owns the isolated worker process; it has no public pid accessor.
    process = getattr(engine, "_process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def _worker_rss_bytes(engine: Any) -> int | None:
    return _read_process_rss_bytes(_worker_pid(engine))


def _worker_rss_reason(engine: Any) -> str:
    if not sys.platform.startswith("linux"):
        return "worker RSS sampling is not available on this platform"
    if _worker_pid(engine) is None:
        return "the engine did not expose a worker process id"
    return "worker RSS could not be read from /proc"


async def _load_engine(paths: ModelPaths, device: str) -> tuple[PhotonWorkerEngine, float]:
    engine = PhotonWorkerEngine(paths, device=device)
    started = time.perf_counter()
    await engine.load()
    return engine, time.perf_counter() - started


async def _run_live(
    paths: ModelPaths,
    wav: bytes,
    config: LiveSimConfig,
    device: str,
) -> dict[str, Any]:
    """Measure the combined preview + final workload through the cached model.

    Two loads are measured so cold worker startup and a warm reload are visible;
    the second engine runs the simulation. No model is downloaded and the audio
    timeline is simulated, but every inference window is a real bounded call.
    """

    cold_engine, cold_load_s = await _load_engine(paths, device)
    await cold_engine.close()
    engine, warm_load_s = await _load_engine(paths, device)
    rss_samples: list[dict[str, Any]] = []
    worker_reason = _worker_rss_reason(engine)

    def sample(sample: BacklogSample) -> None:
        rss_samples.append(
            {
                "at_s": round(sample.at_s, 3),
                "parent_rss_bytes": _current_rss_bytes(),
                "worker_rss_bytes": _worker_rss_bytes(engine),
            }
        )

    try:
        result = await simulate_wav(wav, engine, config, on_sample=sample)
    finally:
        await engine.close()
    worker_available = any(item["worker_rss_bytes"] is not None for item in rss_samples)
    payload = result.to_dict()
    payload.update(
        {
            "mode": "live-sim",
            "device": engine.device,
            "cold_load_seconds": round(cold_load_s, 3),
            "warm_load_seconds": round(warm_load_s, 3),
            "rss_samples": rss_samples,
            "worker_rss": {
                "available": worker_available,
                "unavailable_reason": None if worker_available else worker_reason,
                "source": "linux /proc/<worker pid>/status" if worker_available else None,
            },
            "rss_notes": {
                "parent_rss_bytes": "current parent process RSS; macOS falls back to the RUSAGE_SELF high-water mark",
                "worker_rss_bytes": "current Photon worker RSS where available; null when unavailable",
                "max_rss_bytes": "parent process high-water mark, not a time series",
                "worker_max_rss_bytes": "RUSAGE_CHILDREN high-water mark, not a time series",
            },
            "max_rss_bytes": _max_rss_bytes(),
            "worker_max_rss_bytes": _max_rss_bytes(children=True),
        }
    )
    return payload


async def _run(paths: ModelPaths, wav: bytes, duration_s: float, device: str, repeat: int) -> dict[str, Any]:
    engine = PhotonWorkerEngine(paths, device=device)
    started = time.perf_counter()
    await engine.load()
    load_s = time.perf_counter() - started
    texts: list[str] = []
    timings: list[float] = []
    try:
        for _ in range(repeat):
            before = time.perf_counter()
            texts.append(await engine.transcribe(wav))
            timings.append(time.perf_counter() - before)
    finally:
        await engine.close()
    best = min(timings) if timings else 0.0
    return {
        "audio_seconds": duration_s,
        "device": engine.device,
        "load_seconds": round(load_s, 3),
        "transcribe_seconds": [round(value, 3) for value in timings],
        "real_time_factor": round(duration_s / best, 2) if best else None,
        "texts": texts,
        "max_rss_bytes": _max_rss_bytes(),
        "worker_max_rss_bytes": _max_rss_bytes(children=True),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the cached moondream/parakeet-redux model offline.")
    parser.add_argument("wav", nargs="?", type=Path, help="mono 16 kHz PCM16 WAV, 0.2-120 seconds")
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--repeat", type=int, default=1, help="transcriptions after the initial load (default 1)")
    parser.add_argument(
        "--live-sim",
        nargs="?",
        const=True,
        default=False,
        metavar="WAV",
        help="run the simulated-live windowed workload (optionally taking the WAV path)",
    )
    parser.add_argument("--window", type=float, default=10.0, help="live-sim maximum inference window seconds (default 10)")
    parser.add_argument("--cadence", type=float, default=1.0, help="live-sim preview cadence seconds (default 1.0)")
    parser.add_argument("--overlap", type=float, default=0.75, help="live-sim forced-boundary overlap seconds (default 0.75)")
    parser.add_argument("--silence", type=float, default=0.8, help="live-sim VAD close silence seconds (default 0.8)")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    live = args.live_sim is not False
    wav_path = Path(args.live_sim) if isinstance(args.live_sim, str) else args.wav
    if wav_path is None:
        parser.error("a WAV path is required" if not live else "--live-sim requires a WAV path")
    try:
        config = LiveSimConfig(
            window_s=args.window,
            cadence_s=args.cadence,
            overlap_s=args.overlap,
            silence_s=args.silence,
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        wav = wav_path.read_bytes()
        info = parse_wav(wav)
    except Exception as exc:  # noqa: BLE001 - the command reports, it does not raise
        print(f"Cannot read {wav_path}: {exc}", file=sys.stderr)
        return 2
    try:
        paths = ModelStore().verified_paths()
    except FileNotFoundError:
        print("The pinned STT model is not in the Hugging Face cache; download it first.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"The cached STT model failed verification: {exc}", file=sys.stderr)
        return 2
    try:
        if live:
            result = asyncio.run(_run_live(paths, wav, config, args.device))
        else:
            result = asyncio.run(_run(paths, wav, info.duration_s, args.device, args.repeat))
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
