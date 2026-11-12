// The Manager's header indicator in NSH: the same braille wave field as the TUI (`nexus-tui/src/indicator.rs`).
// Ten cells of braille text, a few dots lit, travelled by three slow sine waves. Idle, thinking, listening and
// speaking differ in speed and brightness; listening is drawn in `manager.listening`, speaking in `manager.speaking`, the
// rest in gray (`waveColor`). Pure drawing: the caller gives it the time.
import type { OrbMode } from "../../manager";
import { diagramPalettes, managerColors } from "../../gen/theme";

export const BRAILLE_WIDTH = 10;

/** Braille dot bits, [row][column]. */
const BITS = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]];
/** Vertical centre in dot rows, amplitude, frequency, drift, phase, line width. */
const WAVES: [number, number, number, number, number, number][] = [
  [1.05, 0.95, 0.55, 1.0, 0.0, 1.0],
  [2.35, 0.80, 0.43, -0.8, 1.9, 1.0],
  [1.70, 0.55, 0.80, 1.3, 3.7, 0.5],
];

/** What the field draws: the call's modes, plus two still marks. `still`: the voice is disconnected, a frozen quiet
 *  wave. `muted`: connected with the microphone muted, a frozen flat line. Neither moves or asks for animation frames. */
export type WaveMode = OrbMode | "still" | "muted";

/** Radians of drift per second (the still marks do not drift). */
export const waveSpeed = (mode: WaveMode): number =>
  mode === "speaking" ? 5.2 : mode === "listening" ? 3.4 : mode === "thinking" ? 2.6 : mode === "settling" ? 1.1 : 0;

/** How bright the dots are, 0 to 1. */
export const waveBrightness = (mode: WaveMode): number =>
  mode === "speaking" ? 1 : mode === "listening" ? 0.84 : mode === "thinking" ? 0.68 : mode === "muted" ? 0.55 : mode === "still" ? 0.4 : 0.55;

/** The braille cell for dot columns `index * 2` and `index * 2 + 1` at time `t`. */
export function brailleCell(t: number, mode: WaveMode, level: number, index: number): string {
  const time = t * waveSpeed(mode);
  const lv = Math.max(0, Math.min(1, level));
  // The speaker's waves follow the measured voice: silence is a flat line, loud is tall.
  const amp = mode === "listening" || mode === "speaking" ? 0.12 + 0.88 * lv : mode === "muted" ? 0 : 0.78;
  let bits = 0;
  for (let col = 0; col < 2; col++) {
    const dx = index * 2 + col;
    for (let row = 0; row < 4; row++) {
      for (const [centre, a, freq, drift, phase, reach] of WAVES) {
        const y = centre + a * amp * Math.sin(dx * freq - time * drift + phase);
        if (Math.abs(y - row) < 0.42 * reach) {
          bits |= BITS[row][col];
          break;
        }
      }
    }
  }
  return String.fromCharCode(0x2800 + bits);
}

/** The whole field, one character per cell. */
export function brailleField(t: number, mode: WaveMode, level: number, width = BRAILLE_WIDTH): string {
  let out = "";
  for (let i = 0; i < width; i++) out += brailleCell(t, mode, level, i);
  return out;
}

type Rgb = [number, number, number];
const rgb = (hex: string): Rgb => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
const lerp = (a: Rgb, b: Rgb, k: number): Rgb => [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
const ease = (x: number) => { const c = Math.max(0, Math.min(1, x)); return c * c * (3 - 2 * c); };

/** The colour a mode draws its waves in: the speaker's role colour while somebody talks, `text.muted` otherwise. */
export function waveRole(mode: WaveMode, scheme: "light" | "dark"): string {
  return mode === "listening" ? managerColors[scheme].listening : mode === "speaking" ? managerColors[scheme].speaking : diagramPalettes[scheme].muted;
}

/**
 * The colour (`#rrggbb`) the waves are drawn in while easing from `from` to `mode` (`blend` 0 to 1); the dot brightness
 * stays an opacity, so this is the role colour itself.
 */
export function waveColor(from: WaveMode, mode: WaveMode, blend: number, scheme: "light" | "dark"): string {
  const c = lerp(rgb(waveRole(from, scheme)), rgb(waveRole(mode, scheme)), ease(blend));
  return "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
}

/** The dot opacity while easing from `from` to `mode`. */
export function waveOpacity(from: WaveMode, mode: WaveMode, blend: number): number {
  const k = ease(blend);
  return waveBrightness(from) * (1 - k) + waveBrightness(mode) * k;
}
