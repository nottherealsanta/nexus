"""Generate voiceover WAVs with Ming-3.0-tts-0.5B.

    cd promo/tts && uv run speak.py [--takes 3] [--only id,id] [--device mps|cpu] [--asr]

Reads lines.json ([{id, voice, text}]) and writes out/<id>_take<N>.wav plus the
best take as out/<id>.wav (48 kHz mono, silence trimmed). Best = ASR word match
(mlx-whisper) first, then closeness of duration to the median take.
"""
import argparse, json, os, re, subprocess, sys, tempfile, difflib
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "Ming-3.0-tts"
OUT = HERE / "out"
MODEL = "inclusionAI/Ming-3.0-tts-0.5B"

# voice label -> Ming task + controls
VOICES = {
    "narrator": dict(task="IP", controls={"ip": "UK_George"}),  # British male, formal
    "assistant": dict(task="IP", controls={"ip": "US_Emma"}),   # American female, assistant
    "user": dict(task="FREEFORM", instruction=(
        "A young man talking casually into his phone while walking outdoors: relaxed, "
        "natural, slightly hurried, conversational, not announcer-like.")),
}


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("-", " ")).split()


def ffmpeg_post(src, dst):
    """Resample to 48k mono and trim leading/trailing silence."""
    sil = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    af = f"{sil},areverse,{sil},areverse,apad=pad_dur=0.12,aresample=48000"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1",
                    "-af", af, "-ar", "48000", "-c:a", "pcm_s16le", str(dst)], check=True)


def wav_stats(p):
    import soundfile as sf, numpy as np
    d, sr = sf.read(p)
    return len(d) / sr, float(np.abs(d).max())


def transcribe(p):
    import mlx_whisper
    r = mlx_whisper.transcribe(str(p), path_or_hf_repo="mlx-community/whisper-small.en-mlx")
    return r["text"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default=str(HERE / "lines.json"))
    ap.add_argument("--takes", type=int, default=3)
    ap.add_argument("--only")
    ap.add_argument("--device", default=None)
    ap.add_argument("--no-asr", action="store_true")
    ap.add_argument("--seed", type=int, default=1895)
    a = ap.parse_args()

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch
    device = a.device or "cpu"  # MPS fails on bf16 mixed-dtype ops; CPU is fast enough (~5x realtime)
    lines = json.loads(Path(a.lines).read_text())
    if a.only:
        keep = set(a.only.split(","))
        lines = [l for l in lines if l["id"] in keep]
    OUT.mkdir(exist_ok=True)

    sys.path.insert(0, str(VENDOR))
    cwd = os.getcwd()
    os.chdir(VENDOR)  # vendor code uses relative paths
    from ming_tts import MingTTS
    model = MingTTS(MODEL, device=device, text_normalization=False)
    os.chdir(cwd)

    rp = OUT / 'report.json'
    report = json.loads(rp.read_text()) if rp.exists() else {}
    for l in lines:
        v = VOICES[l["voice"]]
        cands = []
        for t in range(1, a.takes + 1):
            seed = a.seed + t
            torch.manual_seed(seed)
            kw = dict(task=v["task"], text=l["text"], use_zero_spk_emb=True)
            if "controls" in v:
                kw["controls"] = v["controls"]
            if "instruction" in v:
                kw["instruction"] = v["instruction"]
            # temperature > 0 gives different takes; the default 0.0 is greedy
            kw["temperature"] = 0.0 if t == 1 else 0.3
            with tempfile.TemporaryDirectory() as td:
                raw = Path(td) / "raw.wav"
                kw["output_path"] = str(raw)
                with torch.autocast(device.split(':')[0], dtype=torch.bfloat16):
                    model.synthesize(**kw)
                take = OUT / f"{l['id']}_take{t}.wav"
                ffmpeg_post(raw, take)
            dur, peak = wav_stats(take)
            score, text = 0.0, ""
            if not a.no_asr:
                text = transcribe(take)
                score = difflib.SequenceMatcher(None, norm(l["text"]), norm(text)).ratio()
            print(f"{l['id']} take{t}: {dur:.2f}s peak={peak:.2f} asr={score:.2f} '{text}'", flush=True)
            cands.append((score, -dur, t, take, dur, peak, text))
        med = sorted(x[4] for x in cands)[len(cands) // 2]
        best = max(cands, key=lambda c: (round(c[0], 2), -abs(c[4] - med)))
        final = OUT / f"{l['id']}.wav"
        final.write_bytes(best[3].read_bytes())
        report[l["id"]] = dict(take=best[2], duration=round(best[4], 2), peak=round(best[5], 2), asr=best[6], match=round(best[0], 2))
        print(f"  -> {final.name} = take{best[2]}", flush=True)
    rp.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
