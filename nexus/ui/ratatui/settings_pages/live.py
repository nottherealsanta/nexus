"""Settings → GPT Live: the ``[live]`` voice-call options. Local config only, so every value is global.

Numbers are steppers (any valid configured value is kept; ←/→ move by a step), the voice is a select, and
the instructions path is an edited text field. The model is pinned and shown read-only. Nothing here starts
a call or touches consent: changes apply to the next call.
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.live_settings import (
    LIVE_DEFAULT_VOICE, LIVE_MODEL, LIVE_RANGES, LIVE_VOICES, read_live_settings, reset_live_config, set_live_config,
)

AREA = "live"
#: Stepper increments; the schema allows every integer in ``LIVE_RANGES``.
STEPS = {"max_minutes": 5, "carry_turns": 1, "pause_seconds": 30}
#: The bridge accepts at most this many characters from a text field (``settings_page._control_rule``).
MAX_EDITABLE_PATH = 400


def _voice_label(voice: str) -> str:
    return f"Default ({LIVE_DEFAULT_VOICE})" if voice == "default" else voice


def _stepper(values: dict, key: str, unit: str) -> dict:
    low, high = LIVE_RANGES[key]
    value = int(values[key])
    return sp.stepper(value, sp.op(AREA, "live", name=key), display=f"{value} {unit}", minimum=low, maximum=high, step=STEPS[key])


async def build(workflows) -> dict:
    values = await read_live_settings(workflows.client)
    voice = str(values["voice"])
    blocks = [sp.heading("GPT Live · voice calls"),
              sp.note("A live call speaks with the agent through GPT Live. Changes apply to the next call; a call in progress keeps the values it started with. "
                      "Nothing on this page starts a call.")]
    blocks.append(sp.row("enabled", "GPT Live", sp.toggle(values["enabled"], sp.op(AREA, "live", name="enabled")),
                         description="Off disables GPT Live calls; the NEXUS_LIVE=off environment variable overrides this."))
    blocks.append(sp.row("model", "Model", sp.readout(LIVE_MODEL),
                         description="Pinned by Nexus; not configurable."))
    blocks.append(sp.row("voice", "Voice", sp.select(_voice_label(voice), [(_voice_label(v), v) for v in ("default", *LIVE_VOICES)],
                                                     sp.op(AREA, "live", name="voice")),
                         description=f"The voice you hear. Default resolves to {LIVE_DEFAULT_VOICE}."))
    blocks.append(sp.gap())
    blocks.append(sp.heading("Limits"))
    blocks.append(sp.row("max_minutes", "Longest call", _stepper(values, "max_minutes", "minutes"),
                         description="A call ends after this long (1-240 minutes); GPT Live bills open session time."))
    blocks.append(sp.row("pause_seconds", "Paused call limit", _stepper(values, "pause_seconds", "seconds"),
                         description="A paused call stays open this long, then ends (5-3600 seconds); it is billed while open."))
    blocks.append(sp.row("carry_turns", "Carried conversation", _stepper(values, "carry_turns", "turns"),
                         description="Recent messages from the current session told to the voice at the start (0-100); 0 starts fresh."))
    blocks.append(sp.gap())
    blocks.append(sp.heading("Behaviour"))
    blocks.append(sp.row("progress", "Spoken progress", sp.toggle(values["progress"], sp.op(AREA, "live", name="progress")),
                         description="Lets the voice mention what the agent is doing while it works (at most every 5 seconds)."))
    blocks.append(sp.row("instructions", "Instructions file",
                         sp.text(str(values["instructions"]), sp.op(AREA, "live", name="instructions"), placeholder="bundled prompt"),
                         description=f"Path to a file that replaces the bundled voice prompt (workspace-relative or absolute, up to {MAX_EDITABLE_PATH} characters here); empty uses the bundled one."))
    blocks.append(sp.row("log_raw", "Raw event log (developer)", sp.toggle(values["log_raw"], sp.op(AREA, "live", name="log_raw")),
                         description="Writes a bounded, redacted sideband log under ~/.nexus/logs/live/. For debugging only; leave off."))
    blocks.append(sp.buttons("live-reset", [("Reset GPT Live settings", sp.op(AREA, "live_reset"), "ghost")]))
    return sp.page(AREA, "GPT Live", blocks, footer="Saved in ~/.nexus/config.toml [live] · applies to the next call")


async def handle(workflows, operation) -> None:
    client = workflows.client
    key = operation["key"]
    if key == "live":
        await set_live_config(client, **{operation["name"]: operation["value"]})
    elif key == "live_reset":
        await reset_live_config(client)
        workflows.shell.flash("GPT Live settings reset to default", "success")
    else:
        raise ValueError(f"Unknown GPT Live operation {key!r}")


