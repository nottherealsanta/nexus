// The layout tree (plan 45, docs/layout.md): a split has a direction, children and ratios; a leaf is a group with
// ordered tabs, an active tab and an id. Everything here is pure and immutable: each operation returns a new
// layout (or a refusal), so the signals in layout-state.ts only swap values and tests need no UI.

export type TabKind = "session" | "file" | "diff" | "terminal" | "browser";
export type Tab = { kind: TabKind; id: string };
/** A tab's identity: kind + resource id. The same file or session in two groups is the same resource. */
export const tabKey = (t: Tab): string => `${t.kind}:${t.id}`;
export type Group = { type: "group"; id: string; tabs: Tab[]; active: string | null };
export type Split = { type: "split"; dir: "row" | "column"; children: Node[]; ratios: number[] };
export type Node = Group | Split;
export type Layout = { root: Node; focus: string; seq: number };
export type Dir = "left" | "right" | "up" | "down";
export type Side = "left" | "right" | "top" | "bottom";
export type Rect = { x: number; y: number; w: number; h: number };
export type Size = { w: number; h: number };
export type Path = number[];

/**
 * The smallest a pane may be, in points. The TUI's minimum is 20 x 5 cells; one NSH text cell is about
 * 10 x 24 points, so a pane needs 200 x 120 (its tab strip is 32 of those).
 */
export const MIN_W = 200;
export const MIN_H = 120;
/** A divider's hit area, in points. */
export const DIVIDER = 6;

export type Refusal = "too_small" | "no_neighbour" | "missing";
export type Result = { ok: true; layout: Layout; group?: string } | { ok: false; reason: Refusal };

const group = (id: string, tabs: Tab[] = [], active: string | null = null): Group => ({ type: "group", id, tabs, active: active ?? (tabs[0] ? tabKey(tabs[0]) : null) });

export function emptyLayout(): Layout {
  return { root: group("g1"), focus: "g1", seq: 1 };
}

// --- reading --------------------------------------------------------------------------------------------

export function groupsOf(node: Node): Group[] {
  return node.type === "group" ? [node] : node.children.flatMap(groupsOf);
}
export const findGroup = (l: Layout, id: string): Group | undefined => groupsOf(l.root).find((g) => g.id === id);
export const focusedGroup = (l: Layout): Group => findGroup(l, l.focus) ?? groupsOf(l.root)[0];
export const activeTab = (g: Group): Tab | undefined => g.tabs.find((t) => tabKey(t) === g.active);
export const hasTab = (g: Group, key: string) => g.tabs.some((t) => tabKey(t) === key);
export const allTabs = (l: Layout): Tab[] => groupsOf(l.root).flatMap((g) => g.tabs);
/** The group holding a tab, the focused one first. */
export function groupWith(l: Layout, key: string): Group | undefined {
  const f = focusedGroup(l);
  if (hasTab(f, key)) return f;
  return groupsOf(l.root).find((g) => hasTab(g, key));
}

function pathTo(node: Node, id: string, at: Path = []): Path | null {
  if (node.type === "group") return node.id === id ? at : null;
  for (let i = 0; i < node.children.length; i++) {
    const p = pathTo(node.children[i], id, [...at, i]);
    if (p) return p;
  }
  return null;
}
const nodeAt = (root: Node, path: Path): Node => path.reduce<Node>((n, i) => (n as Split).children[i], root);
function replaceAt(root: Node, path: Path, next: Node): Node {
  if (!path.length) return next;
  const s = root as Split;
  const [i, ...rest] = path;
  return { ...s, children: s.children.map((c, k) => (k === i ? replaceAt(c, rest, next) : c)) };
}

/** Each group's rectangle in `rect`, by id. */
export function rects(node: Node, rect: Rect, out = new Map<string, Rect>()): Map<string, Rect> {
  if (node.type === "group") { out.set(node.id, rect); return out; }
  let at = 0;
  node.children.forEach((c, i) => {
    const share = node.ratios[i];
    const r = node.dir === "row"
      ? { x: rect.x + at * rect.w, y: rect.y, w: share * rect.w, h: rect.h }
      : { x: rect.x, y: rect.y + at * rect.h, w: rect.w, h: share * rect.h };
    at += share;
    rects(c, r, out);
  });
  return out;
}
const whole = (size: Size): Rect => ({ x: 0, y: 0, w: size.w, h: size.h });
export const groupRects = (l: Layout, size: Size) => rects(l.root, whole(size));

export type DividerBox = { path: Path; index: number; dir: "row" | "column"; rect: Rect };
/** Every divider: the line between child `index` and `index + 1` of the split at `path`. */
export function dividers(node: Node, rect: Rect, path: Path = [], out: DividerBox[] = []): DividerBox[] {
  if (node.type === "group") return out;
  let at = 0;
  node.children.forEach((c, i) => {
    const share = node.ratios[i];
    const r = node.dir === "row"
      ? { x: rect.x + at * rect.w, y: rect.y, w: share * rect.w, h: rect.h }
      : { x: rect.x, y: rect.y + at * rect.h, w: rect.w, h: share * rect.h };
    at += share;
    if (i < node.children.length - 1) {
      out.push({ path, index: i, dir: node.dir, rect: node.dir === "row" ? { x: r.x + r.w - DIVIDER / 2, y: r.y, w: DIVIDER, h: r.h } : { x: r.x, y: r.y + r.h - DIVIDER / 2, w: r.w, h: DIVIDER } });
    }
    dividers(c, r, [...path, i], out);
  });
  return out;
}

/** What a node needs along an axis so that every group in it keeps the minimum. */
function needed(node: Node, axis: "x" | "y", minW = MIN_W, minH = MIN_H): number {
  if (node.type === "group") return axis === "x" ? minW : minH;
  const along = (node.dir === "row") === (axis === "x");
  const parts = node.children.map((c) => needed(c, axis, minW, minH));
  return along ? parts.reduce((a, b) => a + b, 0) : Math.max(...parts);
}
export const requiredSize = (l: Layout, minW = MIN_W, minH = MIN_H): Size => ({ w: needed(l.root, "x", minW, minH), h: needed(l.root, "y", minW, minH) });
/** Too small to show every group at the minimum: the client shows only the focused group. */
export function collapsed(l: Layout, size: Size, minW = MIN_W, minH = MIN_H): boolean {
  if (groupsOf(l.root).length < 2) return false;
  const need = requiredSize(l, minW, minH);
  return size.w < need.w || size.h < need.h;
}

// --- geometry -------------------------------------------------------------------------------------------

const overlap = (a0: number, a1: number, b0: number, b1: number) => Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));
/** The nearest group in a direction, by geometry: touching or past the edge, sharing some of its span. */
export function neighbour(l: Layout, id: string, dir: Dir, size: Size): string | null {
  const all = groupRects(l, size);
  const me = all.get(id);
  if (!me) return null;
  let best: { id: string; gap: number; shared: number } | null = null;
  for (const [other, r] of all) {
    if (other === id) continue;
    let gap: number, shared: number;
    if (dir === "right") { gap = r.x - (me.x + me.w); shared = overlap(me.y, me.y + me.h, r.y, r.y + r.h); }
    else if (dir === "left") { gap = me.x - (r.x + r.w); shared = overlap(me.y, me.y + me.h, r.y, r.y + r.h); }
    else if (dir === "down") { gap = r.y - (me.y + me.h); shared = overlap(me.x, me.x + me.w, r.x, r.x + r.w); }
    else { gap = me.y - (r.y + r.h); shared = overlap(me.x, me.x + me.w, r.x, r.x + r.w); }
    if (gap < -1 || shared <= 1) continue;
    if (!best || gap < best.gap - 1 || (Math.abs(gap - best.gap) <= 1 && shared > best.shared)) best = { id: other, gap, shared };
  }
  return best?.id ?? null;
}

export type Zone = "left" | "right" | "top" | "bottom" | "centre";
/** The drop zone under a point of a group body: the outer quarter of an edge splits, the middle moves. */
export function dropZone(r: Rect, x: number, y: number): Zone {
  const fx = (x - r.x) / Math.max(1, r.w), fy = (y - r.y) / Math.max(1, r.h);
  const edges: [Zone, number][] = [["left", fx], ["right", 1 - fx], ["top", fy], ["bottom", 1 - fy]];
  const [zone, d] = edges.reduce((a, b) => (b[1] < a[1] ? b : a));
  return d < 0.25 ? zone : "centre";
}
/** The part of a body a zone highlights. */
export function zoneRect(r: Rect, zone: Zone): Rect {
  if (zone === "left") return { x: r.x, y: r.y, w: r.w / 2, h: r.h };
  if (zone === "right") return { x: r.x + r.w / 2, y: r.y, w: r.w / 2, h: r.h };
  if (zone === "top") return { x: r.x, y: r.y, w: r.w, h: r.h / 2 };
  if (zone === "bottom") return { x: r.x, y: r.y + r.h / 2, w: r.w, h: r.h / 2 };
  return r;
}

// --- editing --------------------------------------------------------------------------------------------

const nextId = (l: Layout) => ({ id: `g${l.seq + 1}`, seq: l.seq + 1 });

/** Collapse one-child splits, merge a split into a parent of its direction, and repair ratios. */
export function normalize(node: Node): Node {
  if (node.type === "group") return node;
  const kids: Node[] = [];
  const ratios: number[] = [];
  node.children.forEach((c0, i) => {
    const c = normalize(c0);
    const share = node.ratios[i] ?? 1 / node.children.length;
    if (c.type === "split" && c.dir === node.dir) c.children.forEach((g, j) => { kids.push(g); ratios.push(share * c.ratios[j]); });
    else { kids.push(c); ratios.push(share); }
  });
  if (kids.length === 1) return kids[0];
  const total = ratios.reduce((a, b) => a + b, 0);
  const ok = total > 0 && ratios.every((r) => r > 0 && Number.isFinite(r));
  return { type: "split", dir: node.dir, children: kids, ratios: ok ? ratios.map((r) => r / total) : kids.map(() => 1 / kids.length) };
}
const withRoot = (l: Layout, root: Node, extra: Partial<Layout> = {}): Layout => ({ ...l, ...extra, root: normalize(root) });
const setGroup = (l: Layout, g: Group): Layout => ({ ...l, root: replaceAt(l.root, pathTo(l.root, g.id)!, g) });

/** Focus a group. */
export function focus(l: Layout, id: string): Layout {
  return findGroup(l, id) && l.focus !== id ? { ...l, focus: id } : l;
}
/** Make a tab the group's active one, and focus the group. */
export function activate(l: Layout, id: string, key: string): Layout {
  const g = findGroup(l, id);
  if (!g || !hasTab(g, key)) return l;
  return focus(setGroup(l, { ...g, active: key }), id);
}

/** Open a tab in a group (the focused one by default): an existing one is activated, a new one appended. */
export function openTab(l: Layout, tab: Tab, id = l.focus, at?: number): Layout {
  const g = findGroup(l, id) ?? focusedGroup(l);
  const key = tabKey(tab);
  if (hasTab(g, key)) return activate(l, g.id, key);
  const tabs = [...g.tabs];
  tabs.splice(at ?? tabs.length, 0, tab);
  return focus(setGroup(l, { ...g, tabs, active: key }), g.id);
}

/** Open in the group to the right, creating it beside the focused one when there is none. */
export function openToSide(l: Layout, tab: Tab, size: Size, minW = MIN_W, minH = MIN_H): Result {
  const right = neighbour(l, l.focus, "right", size);
  if (right) return { ok: true, layout: openTab(l, tab, right), group: right };
  return splitWith(l, l.focus, "right", [tab], size, minW, minH);
}

/** Show tabs in a new group on one side of `id`. A pane under the minimum is refused. */
export function splitWith(l: Layout, id: string, side: Side, tabs: Tab[], size: Size, minW = MIN_W, minH = MIN_H, active?: string): Result {
  const path = pathTo(l.root, id);
  const r = groupRects(l, size).get(id);
  if (!path || !r) return { ok: false, reason: "missing" };
  const horizontal = side === "left" || side === "right";
  if ((horizontal ? r.w / 2 < minW : r.h / 2 < minH)) return { ok: false, reason: "too_small" };
  const { id: gid, seq } = nextId(l);
  const fresh = group(gid, tabs, active);
  const before = side === "left" || side === "top";
  const dir = horizontal ? "row" : "column";
  const old = nodeAt(l.root, path) as Group;
  const node: Split = { type: "split", dir, children: before ? [fresh, old] : [old, fresh], ratios: [0.5, 0.5] };
  return { ok: true, layout: { ...withRoot(l, replaceAt(l.root, path, node), { seq }), focus: gid }, group: gid };
}

/** Split right / down: the active tab (or the given tab) shown again in a new group beside the focused one. */
export function split(l: Layout, side: "right" | "down", tab: Tab, size: Size, minW = MIN_W, minH = MIN_H): Result {
  return splitWith(l, l.focus, side === "right" ? "right" : "bottom", [tab], size, minW, minH);
}

/** Remove a group; its space goes to the adjacent sibling, and a focused group's focus follows the space. */
export function closeGroup(l: Layout, id: string): Layout {
  const all = groupsOf(l.root);
  if (all.length < 2) return l;
  const path = pathTo(l.root, id);
  if (!path) return l;
  const idx = path[path.length - 1];
  const parent = nodeAt(l.root, path.slice(0, -1)) as Split;
  const takes = idx > 0 ? idx - 1 : idx + 1;
  const children = parent.children.filter((_, i) => i !== idx);
  const ratios = parent.ratios.map((r, i) => (i === takes ? r + parent.ratios[idx] : r)).filter((_, i) => i !== idx);
  const root = replaceAt(l.root, path.slice(0, -1), { ...parent, children, ratios });
  const heir = groupsOf(parent.children[takes]);
  const next = takes < idx ? heir[heir.length - 1] : heir[0];
  return withRoot(l, root, { focus: l.focus === id ? next.id : l.focus });
}

/** Close one tab. The group stays (empty) when it is the last one, else it goes with its last tab. */
export function closeTab(l: Layout, id: string, key: string): Layout {
  const g = findGroup(l, id);
  if (!g || !hasTab(g, key)) return l;
  const at = g.tabs.findIndex((t) => tabKey(t) === key);
  const tabs = g.tabs.filter((t) => tabKey(t) !== key);
  if (!tabs.length && groupsOf(l.root).length > 1) return closeGroup(l, id);
  const active = g.active === key ? (tabs[Math.min(at, tabs.length - 1)] ? tabKey(tabs[Math.min(at, tabs.length - 1)]) : null) : g.active;
  return setGroup(l, { ...g, tabs, active });
}
/** Drop a tab everywhere (its resource is gone). */
export function dropTab(l: Layout, key: string): Layout {
  return groupsOf(l.root).filter((g) => hasTab(g, key)).reduce((acc, g) => closeTab(acc, g.id, key), l);
}

/**
 * Move a tab to another group at `index` (the end by default), or reorder it inside its own group. The target
 * is focused and the tab active; an emptied source group is removed. `copy` leaves the source tab in place.
 */
export function moveTab(l: Layout, from: string, key: string, to: string, index?: number, copy = false): Layout {
  const src = findGroup(l, from), dst = findGroup(l, to);
  if (!src || !dst || !hasTab(src, key)) return l;
  const tab = src.tabs.find((t) => tabKey(t) === key)!;
  if (from === to) {
    const tabs = src.tabs.filter((t) => tabKey(t) !== key);
    const i = Math.max(0, Math.min(tabs.length, index ?? tabs.length));
    tabs.splice(i, 0, tab);
    return focus(setGroup(l, { ...src, tabs, active: key }), from);
  }
  let out = hasTab(dst, key) ? activate(l, to, key) : openTab(l, tab, to, index);
  if (!copy) out = closeTab(out, from, key);
  return focus(out, to);
}
/** Move the focused group's active tab into the neighbour in a direction. */
export function moveToward(l: Layout, dir: Dir, size: Size): Result {
  const g = focusedGroup(l);
  const target = neighbour(l, g.id, dir, size);
  if (!target || !g.active) return { ok: false, reason: "no_neighbour" };
  return { ok: true, layout: moveTab(l, g.id, g.active, target), group: target };
}

/** Join: the focused group's tabs go to the end of its neighbour that way, the moved active tab stays active, the split closes. */
export function join(l: Layout, dir: "right" | "down", size: Size, id = l.focus): Result {
  const src = findGroup(l, id);
  const target = src && neighbour(l, id, dir, size);
  if (!src || !target) return { ok: false, reason: "no_neighbour" };
  const dst = findGroup(l, target)!;
  const tabs = [...dst.tabs, ...src.tabs.filter((t) => !hasTab(dst, tabKey(t)))];
  const active = src.active ?? dst.active;
  const merged = setGroup(l, { ...dst, tabs, active });
  const emptied = closeGroup(merged, id);
  return { ok: true, layout: focus(emptied, target), group: target };
}

/** Drag a divider by `delta` points. Each side keeps at least its minimum; a move that cannot is clamped. */
export function resize(l: Layout, path: Path, index: number, delta: number, size: Size, minW = MIN_W, minH = MIN_H): Layout {
  const node = nodeAt(l.root, path);
  if (node.type !== "split") return l;
  const box = nodeRect(l, path, size);
  const total = node.dir === "row" ? box.w : box.h;
  if (total <= 0) return l;
  const axis = node.dir === "row" ? "x" : "y";
  const pair = node.ratios[index] + node.ratios[index + 1];
  const lo = needed(node.children[index], axis, minW, minH) / total;
  const hi = pair - needed(node.children[index + 1], axis, minW, minH) / total;
  if (lo > hi) return l;
  const first = Math.max(lo, Math.min(hi, node.ratios[index] + delta / total));
  const ratios = node.ratios.slice();
  ratios[index] = first;
  ratios[index + 1] = pair - first;
  return { ...l, root: replaceAt(l.root, path, { ...node, ratios }) };
}
function nodeRect(l: Layout, path: Path, size: Size): Rect {
  let node = l.root, r = whole(size);
  for (const i of path) {
    const s = node as Split;
    let at = 0;
    for (let k = 0; k < i; k++) at += s.ratios[k];
    const share = s.ratios[i];
    r = s.dir === "row" ? { x: r.x + at * r.w, y: r.y, w: share * r.w, h: r.h } : { x: r.x, y: r.y + at * r.h, w: r.w, h: share * r.h };
    node = s.children[i];
  }
  return r;
}

// --- persistence ----------------------------------------------------------------------------------------

export const serialize = (l: Layout): string => JSON.stringify({ v: 1, ...l });

const KINDS = new Set<string>(["session", "file", "diff", "terminal", "browser"]);
function parseNode(x: any): Node | null {
  if (!x || typeof x !== "object") return null;
  if (x.type === "group") {
    if (typeof x.id !== "string" || !Array.isArray(x.tabs)) return null;
    const tabs: Tab[] = x.tabs.filter((t: any) => t && KINDS.has(t.kind) && typeof t.id === "string").map((t: any) => ({ kind: t.kind, id: t.id }));
    return group(x.id, tabs, typeof x.active === "string" && tabs.some((t) => tabKey(t) === x.active) ? x.active : null);
  }
  if (x.type === "split" && (x.dir === "row" || x.dir === "column") && Array.isArray(x.children) && x.children.length) {
    const children = x.children.map(parseNode);
    if (children.some((c: Node | null) => !c)) return null;
    const ratios = Array.isArray(x.ratios) && x.ratios.length === children.length ? x.ratios.map(Number) : children.map(() => 1 / children.length);
    return { type: "split", dir: x.dir, children, ratios };
  }
  return null;
}
/** Read a saved layout; anything malformed is null (the client starts fresh). */
export function parse(text: string | null | undefined): Layout | null {
  if (!text) return null;
  try {
    const x = JSON.parse(text);
    if (!x || x.v !== 1) return null;
    const root = parseNode(x.root);
    if (!root) return null;
    const ids = groupsOf(root).map((g) => g.id);
    if (new Set(ids).size !== ids.length) return null;
    const seq = Math.max(1, ...ids.map((i) => Number(i.replace(/^g/, "")) || 0), Number(x.seq) || 0);
    return { root: normalize(root), focus: ids.includes(x.focus) ? x.focus : ids[0], seq };
  } catch { return null; }
}

/** Rebuild a saved layout: drop tabs whose resource is gone, repair actives, collapse empty groups (one always stays). */
export function restore(l: Layout, exists: (t: Tab) => boolean): Layout {
  const prune = (n: Node): Node | null => {
    if (n.type === "group") {
      const tabs = n.tabs.filter(exists);
      return tabs.length ? group(n.id, tabs, tabs.some((t) => tabKey(t) === n.active) ? n.active : null) : null;
    }
    const kids: Node[] = [], ratios: number[] = [];
    n.children.forEach((c, i) => { const p = prune(c); if (p) { kids.push(p); ratios.push(n.ratios[i]); } });
    return kids.length ? { ...n, children: kids, ratios } : null;
  };
  const root = prune(l.root);
  if (!root) return { root: group(groupsOf(l.root)[0]?.id ?? "g1"), focus: groupsOf(l.root)[0]?.id ?? "g1", seq: l.seq };
  const n = normalize(root);
  const ids = groupsOf(n).map((g) => g.id);
  return { root: n, focus: ids.includes(l.focus) ? l.focus : ids[0], seq: l.seq };
}

/** The tab to show after a group's active tab changes by `step` (wrapping), for next / previous tab. */
export function stepTab(l: Layout, step: number, id = l.focus): Layout {
  const g = findGroup(l, id);
  if (!g || g.tabs.length < 2) return l;
  const at = g.tabs.findIndex((t) => tabKey(t) === g.active);
  const next = g.tabs[(at + step + g.tabs.length) % g.tabs.length];
  return activate(l, id, tabKey(next));
}

/** Would a split of this group on this side leave both halves at the minimum? */
export function splitFits(l: Layout, id: string, side: Side, size: Size, minW = MIN_W, minH = MIN_H): boolean {
  const r = groupRects(l, size).get(id);
  if (!r) return false;
  return side === "left" || side === "right" ? r.w / 2 >= minW : r.h / 2 >= minH;
}

/** Add a tab to a group without activating it or moving focus (a session tab that appeared elsewhere). */
export function addTab(l: Layout, tab: Tab, id = l.focus): Layout {
  const g = findGroup(l, id) ?? focusedGroup(l);
  if (allTabs(l).some((t) => tabKey(t) === tabKey(tab)) && hasTab(g, tabKey(tab))) return l;
  return setGroup(l, { ...g, tabs: [...g.tabs, tab], active: g.active ?? tabKey(tab) });
}
