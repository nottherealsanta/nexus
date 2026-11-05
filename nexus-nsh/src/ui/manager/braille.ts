// The Manager's header indicator in NSH: the same braille wave field as the TUI (`nexus-tui/src/indicator.rs`).
// Ten cells of braille text, a few dots lit, travelled by three slow sine waves. Idle, thinking, listening and
// speaking differ in speed and brightness; every state stays gray. Pure drawing: the caller gives it the time.
import type { OrbMode } from "../../manager";

export const BRAILLE_WIDTH = 10;

/** Braille dot bits, [row][column]. */
const BITS = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]];
/** Vertical centre in dot rows, amplitude, frequency, drift, phase, line width. */
const WAVES: [number, number, number, number, number, number][] = [
  [1.05, 0.95, 0.55, 1.0, 0.0, 1.0],
  [2.35, 0.80, 0.43, -0.8, 1.9, 1.0],
  [1.70, 0.55, 0.80, 1.3, 3.7, 0.5],
];

/** Radians of drift per second. */
export const waveSpeed = (mode: OrbMode): number =>
  mode === "speaking" ? 5.2 : mode === "listening" ? 3.4 : mode === "thinking" ? 2.6 : 1.1;

/** How bright the dots are, 0 to 1. */
export const waveBrightness = (mode: OrbMode): number =>
  mode === "speaking" ? 1 : mode === "listening" ? 0.84 : mode === "thinking" ? 0.68 : 0.55;

/** The braille cell for dot columns `index * 2` and `index * 2 + 1` at time `t`. */
export function brailleCell(t: number, mode: OrbMode, level: number, index: number): string {
  const time = t * waveSpeed(mode);
  const amp = 0.78 + 0.22 * Math.max(0, Math.min(1, level));
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
export function brailleField(t: number, mode: OrbMode, level: number, width = BRAILLE_WIDTH): string {
  let out = "";
  for (let i = 0; i < width; i++) out += brailleCell(t, mode, level, i);
  return out;
}
