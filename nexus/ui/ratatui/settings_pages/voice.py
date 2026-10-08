"""Settings → Voice & speech: local dictation and local Paradee speech on one page.

Both are local features, so every value is global and no scope control is shown. Model downloads
always ask first (a confirmation page, the one allowed drill-in) and then show progress here.
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support import speech_download as sd
from ....ui_support.speech_settings import SPEECH_CHOICES, compatible_voices, read_speech_settings, reset_speech_config, set_speech_config
from ....ui_support.voice_settings import set_voice_config

AREA = "voice"
DEVICES = ("auto", "cpu", "mps", "cuda")
LIMITS = (15, 30, 60, 90, 120)
LANGUAGES = (("English (US)", "a"),)
SPEEDS = tuple(SPEECH_CHOICES["speed"])
SPEECH_DEVICES = tuple(SPEECH_CHOICES["device"])


def _speed(value) -> str:
    return f"{float(value):g}×"


async def build(workflows) -> dict:
    client = workflows.client
    voice = await client.voice_status()
    speech_values = await read_speech_settings(client)
    speech = await client.speech_status()
    language = str(speech_values["language"])

    blocks = [sp.heading("VOICE INPUT · local dictation"),
              sp.note("Audio is transcribed on this device. Auto-send submits at once; turn it off to review the transcript in the composer.")]
    blocks.append(sp.row("voice", "Voice input", sp.toggle(voice.enabled, sp.op(AREA, "voice", name="enabled"))))
    blocks.append(sp.row("auto_send", "Send transcript automatically", sp.toggle(voice.auto_send, sp.op(AREA, "voice", name="auto_send")),
                         description="Off lets you review the transcript in the composer first."))
    blocks.append(sp.row("device", "Processing device", sp.select(voice.configured_device, [(d, d) for d in DEVICES], sp.op(AREA, "voice", name="device")),
                         description="Auto chooses an available accelerator; CPU works without one."))
    blocks.append(sp.row("max_seconds", "Recording limit", sp.select(f"{voice.max_seconds} seconds", [(f"{v} seconds", v) for v in LIMITS],
                                                                   sp.op(AREA, "voice", name="max_seconds")),
                         description="Recording stops at this limit; press any key to finish earlier."))
    blocks.append(sp.row("voice_model", "Model", sp.readout(f"{voice.state} · {voice.message or 'local transcription'}")))
    if voice.state in {"loading", "downloading"}:
        total = voice.bytes_total or 0
        blocks.append(sp.progress(voice.progress, f"{voice.bytes_done // 1_000_000}/{total // 1_000_000} MB" if total else voice.state))
        blocks.append(sp.buttons("voice-refresh", [("Refresh status", sp.op(AREA, "refresh"), "secondary")]))
    elif voice.cached or voice.state in {"ready", "transcribing"}:
        blocks.append(sp.buttons("voice-load", [("Load model", sp.op(AREA, "voice_prepare", download=False), "secondary")]))
    else:
        blocks.append(sp.buttons("voice-download", [("Download local model…", sp.op(AREA, "voice_download_ask"), "primary")]))
    blocks.append(sp.gap())

    blocks += [sp.heading("SPEECH · /speak, local Paradee"),
               sp.note("/speak reads the latest answer aloud with Paradee, a small CPU model with one voice (Kokoro's af_heart). Settings are saved in nexus.toml [speech]. Nothing downloads without your consent.")]
    blocks.append(sp.row("language", "Language", sp.select(next((l for l, v in LANGUAGES if v == language), language),
                                                            [(l, v) for l, v in LANGUAGES], sp.op(AREA, "speech", name="language"))))
    blocks.append(sp.row("speech_voice", "Voice", sp.select(str(speech_values["voice"]), [(v, v) for v in compatible_voices(language)],
                                                             sp.op(AREA, "speech", name="voice"))))
    blocks.append(sp.row("speed", "Speed", sp.select(_speed(speech_values["speed"]), [(_speed(v), v) for v in SPEEDS], sp.op(AREA, "speech", name="speed"))))
    blocks.append(sp.row("speech_device", "Device", sp.select(str(speech_values["device"]), [(d, d) for d in SPEECH_DEVICES], sp.op(AREA, "speech", name="device"))))
    state = speech.state
    if state == "downloading":
        blocks.append(sp.row("speech_model", "Model", sp.readout("downloading")))
        blocks.append(sp.progress(float(getattr(speech, "progress", 0.0) or 0.0), sd.progress_text(speech).removeprefix("Downloading the local speech model… ")))
        blocks.append(sp.buttons("speech-refresh", [("Refresh status", sp.op(AREA, "refresh"), "secondary")]))
    elif state == "ready":
        blocks.append(sp.row("speech_model", "Model", sp.readout(sd.ready_text())))
    else:
        blocks.append(sp.row("speech_model", "Model", sp.readout(speech.message or "not downloaded")))
        blocks.append(sp.buttons("speech-download", [("Download speech model…", sp.op(AREA, "speech_download_ask"), "primary")]))
    blocks.append(sp.buttons("speech-reset", [("Reset speech settings", sp.op(AREA, "speech_reset"), "ghost")]))
    return sp.page(AREA, "Voice & speech", blocks, footer="Saved in ~/.nexus/config.toml · applies at once")


async def handle(workflows, operation) -> None:
    client = workflows.client
    key = operation["key"]
    if key == "voice":
        await set_voice_config(client, **{operation["name"]: operation["value"]})
    elif key == "voice_download_ask":
        workflows.menu("Download the local voice model?", [("Cancel", {"kind": "back"}), ("Download", sp.op(AREA, "voice_prepare", download=True))],
                       ["About 179 MB. Audio is transcribed on this device and never sent to a speech service."])
    elif key == "voice_prepare":
        await client.voice_prepare(allow_download=bool(operation.get("download", True)))
        workflows.return_to_page()
    elif key == "speech":
        await set_speech_config(client, **{operation["name"]: operation["value"]})
    elif key == "speech_reset":
        await reset_speech_config(client)
        workflows.shell.flash("Speech settings reset to default", "success")
    elif key == "speech_download_ask":
        workflows.menu(f"{sd.CONSENT_TITLE}. Download it now?", [("Cancel", {"kind": "back"}), ("Download", sp.op(AREA, "speech_prepare"))], [sd.CONSENT_PROMPT])
    elif key == "speech_prepare":
        await client.speech_prepare()
        workflows.return_to_page()
    elif key == "refresh":
        pass  # the page is rebuilt after every operation
    else:
        raise ValueError(f"Unknown Voice & speech operation {key!r}")
