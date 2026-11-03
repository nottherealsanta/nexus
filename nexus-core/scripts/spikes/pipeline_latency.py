#!/usr/bin/env python3
"""M0 spike (a): one real STT -> Luna -> TTS round trip and where the time goes (MANAGER_PLAN.md section 16).

    cd nexus-core
    uv run python scripts/spikes/pipeline_latency.py --wav question.wav --copy-auth-from ~/.nexus --yes

Input: a mono 16 kHz PCM16 WAV of you asking something ("what needs me?"), at most 120 s. Record one with any tool.
For each reasoning effort it runs, `--repeat` times:

1. speech to text with the configured local model (`[voice] stt_model`, from the Hugging Face cache; nothing is
   downloaded),
2. one tool-less request to `--model` (default `copilot/gpt-6-luna`) with the Manager's system prompt and the
   transcript, streamed, noting time to the first token and to the first full sentence,
3. text to speech of that first sentence with the local model (`[speech] tts_model`), noting time to the first audio
   frame.

It prints a table (milliseconds) and the plan's target, **under 1.5 s from end of speech to first audio**, and can
write the rows as JSON with `--out`. It does not use the daemon or any session.

Spends: `len(efforts) x repeat` model requests of about 2k input tokens and a few dozen output tokens on the chosen
provider's account (Copilot by default), plus local CPU for STT and TTS. Sends the transcript of your WAV and the
Manager's prompt to that provider. Nothing else leaves the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common

TARGET_MS = 1500


async def run(args: argparse.Namespace) -> list[dict]:
    # Imported only after the scratch home is exported.
    from nexus import runtime as runtime_module
    from nexus.config import ConfigFile
    from nexus.llmhub.types import Message, Request, Text, TextDelta
    from nexus.manager.prompt import PROMPT
    from nexus.manager.turn import SentenceSplitter
    from nexus.voice.manager import VoiceManager
    from nexus.voice.speech import SpeechService

    values = ConfigFile().read()["values"]
    raw = Path(args.wav).read_bytes()
    voice = VoiceManager(lambda: values["voice"]["stt_model"])
    speech = SpeechService()
    rows = []
    for effort in args.efforts.split(","):
        for attempt in range(args.repeat):
            row = {"effort": effort, "attempt": attempt + 1}
            t0 = time.perf_counter()
            result = await voice.transcribe(raw)
            row["stt_ms"] = round((time.perf_counter() - t0) * 1000)
            row["transcript"] = result.text

            provider, model = runtime_module.provider_for(args.model)
            request = Request(model=model, instructions=PROMPT, messages=(Message("user", (Text(
                f'<user_said source="speech">\n{result.text}\n</user_said>'),)),), tools=(),
                reasoning_effort=None if effort == "none" else effort, conversation_id="spike-latency")
            splitter, first_token, first_sentence, sentence = SentenceSplitter(), None, None, ""
            t1 = time.perf_counter()
            async for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    first_token = first_token or (time.perf_counter() - t1)
                    if first_sentence is None:
                        for found in splitter.feed(event.text):
                            first_sentence, sentence = time.perf_counter() - t1, found
                            break
            if first_sentence is None:
                for found in splitter.flush():
                    first_sentence, sentence = time.perf_counter() - t1, found
                    break
            row["llm_first_token_ms"] = round((first_token or 0) * 1000)
            row["llm_first_sentence_ms"] = round((first_sentence or 0) * 1000)
            row["first_sentence"] = sentence

            t2 = time.perf_counter()
            stream = await speech.speak(sentence or "Nothing to say.")
            async for _ in stream:  # the first yielded bytes are the WAV header; the next are audio
                row["tts_first_audio_ms"] = round((time.perf_counter() - t2) * 1000)
                break
            speech.stop()
            row["end_of_speech_to_first_audio_ms"] = (
                row["stt_ms"] + row["llm_first_sentence_ms"] + row["tts_first_audio_ms"])
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k.endswith("_ms") or k in ("effort", "attempt")}))
    speech.unload()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_arguments(parser)
    parser.add_argument("--wav", required=True, help="mono 16 kHz PCM16 WAV, at most 120 seconds")
    parser.add_argument("--model", default="copilot/gpt-6-luna")
    parser.add_argument("--efforts", default="none,low,medium", help="comma-separated reasoning efforts to compare")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", help="write the rows here as JSON")
    args = parser.parse_args()
    if not Path(args.wav).is_file():
        sys.exit(f"{args.wav} does not exist")
    efforts = len(args.efforts.split(","))
    _common.prepare(args, spends=f"{efforts * args.repeat} requests to {args.model} (about 2k tokens in each) "
                                 "and local STT/TTS CPU; the transcript of your WAV is sent to that provider")
    rows = asyncio.run(run(args))
    print(f"\n{'effort':8} {'stt':>6} {'llm 1st sentence':>17} {'tts 1st audio':>14} {'total':>7}  target {TARGET_MS} ms")
    for row in rows:
        print(f"{row['effort']:8} {row['stt_ms']:>6} {row['llm_first_sentence_ms']:>17} "
              f"{row['tts_first_audio_ms']:>14} {row['end_of_speech_to_first_audio_ms']:>7}")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
