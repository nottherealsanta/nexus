// The Manager's orb (MANAGER_PLAN.md 10.2): a soft luminous sphere with a bright core and an alpha
// falloff into whatever is behind it, never a flat disc. Layered radial gradients, two or three slowly
// drifting lobes that deform the edge with the level, and ripples that follow a voice. Pure drawing:
// the caller gives it the time, the smoothed level, the state and the palette.
import type { CanvasRenderingContext2D } from "nsh";
import type { OrbMode } from "../../manager";

export type Rgb = [number, number, number];

export function hexToRgb(hex: string): Rgb {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return [128, 128, 128];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
export const mix = (a: Rgb, b: Rgb, t: number): Rgb => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
export const rgba = (c: Rgb, a: number) => `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${Math.max(0, Math.min(1, a)).toFixed(3)})`;

export interface OrbFrame {
  /** Seconds since the orb appeared. */
  t: number;
  /** The smoothed level, 0 to 1. */
  level: number;
  /** Enter and exit, 0 (gone) to 1 (fully there). */
  presence: number;
  mode: OrbMode;
  /** The state the orb is easing away from, and how far along (0 to 1). */
  from: OrbMode;
  blend: number;
  /** Error or reconnecting: still, in the idle colour. */
  still: boolean;
  light: boolean;
  colors: Record<OrbMode, string>;
}

const TAU = Math.PI * 2;
const smooth = (x: number) => x * x * (3 - 2 * x);

/** The colour an orb in `mode` wears; settling wears the idle colour. */
export function modeColor(mode: OrbMode, colors: Record<OrbMode, string>): Rgb {
  return hexToRgb(colors[mode]);
}

/** Draw the orb centred in a `width` x `height` canvas whose nominal radius is `radius`. */
export function drawOrb(cx: CanvasRenderingContext2D, width: number, height: number, radius: number, f: OrbFrame) {
  const p = smooth(f.presence);
  if (p <= 0.003) return;
  const mid = f.still ? "settling" : f.mode;
  const col = mix(modeColor(f.still ? "settling" : f.from, f.colors), modeColor(mid, f.colors), smooth(f.blend));
  const white: Rgb = [255, 255, 255];
  const core = mix(col, white, f.light ? 0.35 : 0.62);
  const x = width / 2;
  // The orb rises from the composer edge: a few points of travel, a scale and a fade.
  const y = height / 2 + (1 - p) * 16;
  const level = f.still ? 0 : f.level;
  const live = f.mode === "listening" || f.mode === "speaking";

  // The size of the sphere: swells with the voice, breathes while thinking, shrinks as it settles.
  let r = radius;
  if (f.mode === "listening" || f.mode === "speaking") r = radius * (0.8 + 0.3 * level);
  else if (f.mode === "thinking") r = radius * (0.84 + 0.05 * Math.sin((f.t * TAU) / 2.6));
  else r = radius * 0.8;
  if (f.still) r = radius * 0.7;
  r *= 0.55 + 0.45 * p;

  cx.save();
  cx.globalAlpha = p;

  // 1. The halo: a wide, faint falloff, so there is never an edge.
  const halo = cx.createRadialGradient(x, y, r * 0.2, x, y, r * 2.15);
  halo.addColorStop(0, rgba(col, f.light ? 0.2 : 0.3 + 0.12 * level));
  halo.addColorStop(0.45, rgba(col, f.light ? 0.08 : 0.11 + 0.05 * level));
  halo.addColorStop(1, rgba(col, 0));
  cx.fillStyle = halo;
  cx.beginPath();
  cx.arc(x, y, r * 2.15, 0, TAU);
  cx.fill();

  // 2. Ripples that follow a voice: soft bands of light that leave the sphere and fade, never a line.
  if (live && !f.still) {
    for (let i = 0; i < 2; i++) {
      const k = (f.t * 0.6 + i * 0.5) % 1;
      const ring = r * (1.05 + 0.85 * k);
      const band = r * 0.2;
      const a = Math.sin(Math.PI * Math.min(1, k * 1.15)) * (0.05 + 0.22 * level);
      const wave = cx.createRadialGradient(x, y, Math.max(0, ring - band), x, y, ring + band);
      wave.addColorStop(0, rgba(col, 0));
      wave.addColorStop(0.5, rgba(col, a));
      wave.addColorStop(1, rgba(col, 0));
      cx.fillStyle = wave;
      cx.beginPath();
      cx.arc(x, y, ring + band, 0, TAU);
      cx.fill();
    }
  }

  // 3. The body: a sphere lit from above-left, falling to nothing at its edge.
  const lx = x - r * 0.18, ly = y - r * 0.22;
  const body = cx.createRadialGradient(lx, ly, r * 0.06, x, y, r * 1.1);
  const solid = f.light ? 0.82 : 0.94;
  body.addColorStop(0, rgba(core, solid));
  body.addColorStop(0.2, rgba(mix(col, core, 0.45), solid * 0.94));
  body.addColorStop(0.55, rgba(col, solid * 0.58));
  body.addColorStop(0.82, rgba(col, solid * 0.2));
  body.addColorStop(1, rgba(col, 0));
  cx.fillStyle = body;
  cx.beginPath();
  cx.arc(x, y, r * 1.1, 0, TAU);
  cx.fill();

  // 4. Lobes: three slow, drifting glows that push the edge about as the level rises.
  if (!f.still) {
    const drift = f.mode === "thinking" ? 0.5 : 1;
    for (let i = 0; i < 3; i++) {
      const a = f.t * (0.55 + i * 0.23) * drift + i * 2.1;
      const d = r * (0.1 + 0.3 * level) * (i === 1 ? 1.15 : 1);
      const lobeX = x + Math.cos(a) * d, lobeY = y + Math.sin(a * 1.17) * d;
      const lr = r * (0.62 + 0.26 * level);
      const lobe = cx.createRadialGradient(lobeX, lobeY, 0, lobeX, lobeY, lr);
      lobe.addColorStop(0, rgba(mix(col, core, 0.3), 0.34 + 0.2 * level));
      lobe.addColorStop(1, rgba(col, 0));
      cx.fillStyle = lobe;
      cx.beginPath();
      cx.arc(lobeX, lobeY, lr, 0, TAU);
      cx.fill();
    }
  }

  // 5. The bright core, and a small highlight that gives the sphere its depth.
  const bright = cx.createRadialGradient(lx, ly, 0, lx, ly, r * 0.62);
  bright.addColorStop(0, rgba(white, f.light ? 0.6 : 0.74));
  bright.addColorStop(0.45, rgba(core, 0.34));
  bright.addColorStop(1, rgba(core, 0));
  cx.fillStyle = bright;
  cx.beginPath();
  cx.arc(lx, ly, r * 0.62, 0, TAU);
  cx.fill();

  cx.restore();
}

/** Reduced motion and the lower tiers: a still disc per state, softened at the edge, no animation. */
export function drawStaticOrb(cx: CanvasRenderingContext2D, width: number, height: number, radius: number, mode: OrbMode, colors: Record<OrbMode, string>, light: boolean) {
  const col = modeColor(mode, colors);
  const x = width / 2, y = height / 2;
  const r = radius * (mode === "listening" ? 0.95 : mode === "speaking" ? 0.9 : mode === "thinking" ? 0.78 : 0.62);
  const body = cx.createRadialGradient(x, y, r * 0.15, x, y, r * 1.05);
  body.addColorStop(0, rgba(col, 0.95));
  body.addColorStop(0.7, rgba(col, 0.6));
  body.addColorStop(1, rgba(col, 0));
  cx.fillStyle = body;
  cx.beginPath();
  cx.arc(x, y, r * 1.05, 0, TAU);
  cx.fill();
  if (mode === "thinking") {
    // The state also reads in shape: thinking is a ring around a smaller disc.
    cx.strokeStyle = rgba(col, light ? 0.55 : 0.7);
    cx.lineWidth = 1.5;
    cx.beginPath();
    cx.arc(x, y, r * 1.12, 0, TAU);
    cx.stroke();
  }
}
