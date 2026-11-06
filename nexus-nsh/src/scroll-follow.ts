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

// The first-send pin: a session's first message sits at the top of the view, with room
// below it (a spacer) so it can even while the reply is empty. The spacer is the view
// height less what the pinned row and everything after it already take, and is gone
// once the reply fills the screen.
export function pinSpacer(below: number, viewH: number): number {
  return Math.max(0, viewH - below);
}

/** The reader has moved the view off the pin (not the pin's own settling). */
export function pinReleased(m: { y: number; target: number; sinceApplied: number; contentH?: number; viewH?: number }): boolean {
  // A view that cannot reach the pin yet (the spacer or the rows are still arriving) has not been moved.
  if (m.contentH !== undefined && m.viewH !== undefined && m.contentH < m.target + m.viewH - 2) return false;
  return m.sinceApplied > 150 && Math.abs(m.y - m.target) > 2;
}
