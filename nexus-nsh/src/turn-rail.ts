// The timeline's message rail: geometry for the TUI's turn rail (`draw_turn_rail`), the track
// scrub and the previous/next-turn keys. Pure so the rules can be tested without a timeline.
export const RAIL_LIST_PAD = 12; // the list's `py-3`
export const RAIL_ROW_ESTIMATE = 72; // the list's estimatedItemSize, for rows not laid out yet
const MIN_BAND = 8; // the scroll bar thumb's minimum, so a long conversation still shows a band

export type RailRow = { key: string; kind: string; n?: number; text?: string };
export type RowBox = { row: RailRow; index: number; top: number; height: number };
export type RowGeometry = { boxes: RowBox[]; total: number };

const heightKey = (sid: string, key: string) => `${sid}\u0000${key}`;

/** Each row's start and height in content coordinates, summed as `newBelow` does. */
export function rowGeometry(input: {
  rows: readonly RailRow[];
  heights: Record<string, number>;
  sid: string;
  pad?: number;
  estimate?: number;
}): RowGeometry {
  const pad = input.pad ?? RAIL_LIST_PAD;
  const estimate = input.estimate ?? RAIL_ROW_ESTIMATE;
  const boxes: RowBox[] = [];
  let top = pad;
  input.rows.forEach((row, index) => {
    const height = input.heights[heightKey(input.sid, row.key)] ?? estimate;
    boxes.push({ row, index, top, height });
    top += height;
  });
  return { boxes, total: top };
}

export type RailMark = { key: string; n: number; text: string; top: number; seen: boolean; index: number };
export type RailLayout = { band: { top: number; height: number }; marks: RailMark[] };

/**
 * The rail's marks and viewport band, or null when it does not show: fewer than two user
 * messages (the TUI's `turns.len() < 2`) or a track too short to place a mark.
 */
export function railLayout(input: {
  rows: readonly RailRow[];
  heights: Record<string, number>;
  sid: string;
  scrollY: number;
  viewH: number;
  trackH: number;
  pad?: number;
  estimate?: number;
}): RailLayout | null {
  const { trackH } = input;
  if (trackH < 3) return null;
  const { boxes, total } = rowGeometry(input);
  const users = boxes.filter((box) => box.row.kind === "user");
  if (users.length < 2) return null;
  const span = Math.max(total, input.viewH, 1);
  const at = (line: number) => Math.min(trackH - 1, Math.max(0, Math.floor((line * trackH) / span)));
  const scroll = Math.max(0, input.scrollY);
  const viewport = Math.max(0, input.viewH);
  const bandTop = at(scroll);
  const bandEnd = Math.max(bandTop, at(scroll + Math.max(0, viewport - 1)));
  const height = Math.min(trackH, Math.max(MIN_BAND, bandEnd - bandTop + 1));
  const band = { top: Math.min(bandTop, trackH - height), height };
  const marks = users.map((box) => ({
    key: box.row.key,
    n: box.row.n ?? 0,
    text: box.row.text ?? "",
    top: at(box.top),
    seen: box.top < scroll + viewport && box.top + box.height > scroll,
    index: box.index,
  }));
  return { band, marks };
}

/** The track: put the conversation at screen row `y` at the middle of the viewport. */
export function railScrubTop(y: number, trackH: number, total: number, viewH: number): number | null {
  if (total <= viewH || trackH <= 0) return null;
  const row = Math.min(trackH - 1, Math.max(0, Math.floor(y)));
  const line = Math.min(total - 1, Math.floor((row * total) / trackH));
  const maxTop = total - viewH;
  return Math.max(0, Math.min(maxTop, line - Math.floor(viewH / 2)));
}

/** The previous or next user row from `currentKey`; with no selection, next starts at the first. */
export function adjacentUser(rows: readonly RailRow[], currentKey: string | null, delta: number): string | null {
  const users = rows.filter((row) => row.kind === "user");
  if (!users.length) return null;
  const index = currentKey === null ? -1 : users.findIndex((row) => row.key === currentKey);
  const target = index + delta;
  if (target < 0 || target >= users.length) return null;
  return users[target].key;
}

/** The least scroll that shows a row: its top when above, its end when below, its top when taller than the view. */
export function revealTop(rowTop: number, rowH: number, scrollY: number, viewH: number, total: number): number {
  const maxTop = Math.max(0, total - viewH);
  const current = Math.max(0, Math.min(scrollY, maxTop));
  let top = current;
  if (rowTop < current) top = rowTop;
  else if (rowTop + rowH > current + viewH) top = rowH <= viewH ? rowTop + rowH - viewH : rowTop;
  return Math.max(0, Math.min(maxTop, top));
}

function firstLine(text: string): string {
  return (text.split("\n").find((line) => line.trim().length > 0) ?? "").trim();
}

/** `#n · first line`, truncated to 48 characters with an ellipsis, as the TUI's tooltip. */
export function railLabel(n: number, text: string): string {
  const label = `#${n} · ${firstLine(text)}`;
  const glyphs = [...label];
  return glyphs.length <= 48 ? label : `${glyphs.slice(0, 47).join("")}…`;
}
