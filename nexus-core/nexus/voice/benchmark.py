"""Opt-in local STT benchmark. Never downloads a model.

The pinned snapshot must already be in the standard Hugging Face cache (download
it through Settings or ``POST /api/voice/stt/download`` first). The benchmark
uses the same isolated, offline Photon worker as the daemon.

Run from ``nexus-core``:

    uv run --extra voice python -m nexus.voice.benchmark speech.wav --device cpu --repeat 3
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
from .store import ModelPaths, ModelStore


def _max_rss_bytes(children: bool = False) -> int:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF)
    # macOS reports bytes; Linux reports KiB.
    return int(usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024)


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
    parser.add_argument("wav", type=Path, help="mono 16 kHz PCM16 WAV, 0.2-120 seconds")
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--repeat", type=int, default=1, help="transcriptions after the initial load (default 1)")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    try:
        wav = args.wav.read_bytes()
        info = parse_wav(wav)
    except Exception as exc:  # noqa: BLE001 - the command reports, it does not raise
        print(f"Cannot read {args.wav}: {exc}", file=sys.stderr)
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
        result = asyncio.run(_run(paths, wav, info.duration_s, args.device, args.repeat))
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
