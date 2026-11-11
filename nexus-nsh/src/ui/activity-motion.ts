const ACTIVITY_CYCLE_MS = 3000;

/** A periodic phase in [0, 1), independent of component/effect mount time. */
export function activityPhase(nowMs: number): number {
  const elapsed = ((nowMs % ACTIVITY_CYCLE_MS) + ACTIVITY_CYCLE_MS) % ACTIVITY_CYCLE_MS;
  return elapsed / ACTIVITY_CYCLE_MS;
}

/** TUI-matched cosine ping-pong: both position and velocity meet smoothly at loop edges. */
export function activityOffsetPercent(phase: number): number {
  return 40 * (1 - Math.cos(phase * Math.PI * 2));
}
