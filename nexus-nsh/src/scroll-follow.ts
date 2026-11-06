// The follow decision of the timeline's scroll handler, shared with tests.
// A reader who scrolls up leaves follow at once: content that streams in below
// must never pull the view back down. Follow returns only when the reader
// scrolls back down to the end (or a jump asks for it explicitly).
export const END_SLOP = 24; // scrolling down to within this of the end follows it again

export function followsAfterScroll(m: {
  y: number;
  lastY: number;
  gap: number;
  following: boolean;
  contentH: number;
  viewH: number;
}): boolean {
  const up = m.y < m.lastY - 0.5;
  const down = m.y > m.lastY + 0.5;
  if (up && m.gap > 0.5) return false;
  if (down && m.gap <= END_SLOP) return true;
  // Content shorter than the view is always at its end.
  if (m.contentH <= m.viewH) return true;
  return m.following;
}
