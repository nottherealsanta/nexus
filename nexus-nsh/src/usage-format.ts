const finite = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);
const clamp = (v: number) => Math.max(0, Math.min(100, v));
const even = (v: number) => {
  const floor = Math.floor(v), fraction = v - floor;
  return fraction === 0.5 ? floor + (Math.abs(floor) % 2) : Math.round(v);
};

export const gaugeCells = (used: number | null | undefined) => {
  const eighths = even((finite(used) ? clamp(used) : 0) * 0.8);
  return Array.from({ length: 10 }, (_, i) => Math.min(8, Math.max(0, eighths - 8 * i)));
};
export const leftPercent = (used: number | null | undefined) => {
  if (!finite(used)) return "—";
  const left = 100 - clamp(used);
  const digits = left % 1 !== 0 && (left < 10 || left > 90) ? 1 : 0;
  const factor = 10 ** digits;
  return `${(even(left * factor) / factor).toFixed(digits)}%`;
};
export const reservePercent = (used: number | null | undefined, reset: number | null | undefined, period: number | null | undefined, now: number) => {
  if (!finite(used) || !finite(reset) || !finite(period) || period < 604800 || reset <= now) return null;
  return 100 * (1 - (reset - now) / period) - clamp(used);
};
export const reserveText = (value: number | null | undefined) => {
  if (!finite(value)) return "—";
  const n = even(value);
  return `${n > 0 ? "+" : ""}${n === 0 ? 0 : n}%`;
};
export const until = (at: number | null | undefined, now = Date.now() / 1000) => {
  if (!finite(at)) return "—";
  const secs = Math.max(0, Math.floor(at - now));
  const d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : "now";
};
