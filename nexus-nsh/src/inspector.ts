// The native session inspector (read-only), Inspector API v2. The Rust TUI keeps the same state machine
// (`nexus-tui/src/inspector.rs`): state per (inspected session, view) so selection, expanded details, scroll,
// zoom, filters and the cursor stack survive refreshes and a trip to another view or session; every
// request carries a sequence number and a response that is not the latest is ignored.
import { Clipboard, signal } from "nsh";
import { request } from "./core/transport";
import { connection, model, toast, viewed } from "./store";

export const inspectorViews = ["Timing", "Conversation", "Steps", "Requests", "Raw", "Questions"] as const;
export const LIMIT = 50;
/** Milliseconds between cheap `/inspect/head` polls while the inspector is open. */
export const POLL_MS = 2000;
/** Rows drawn at once. */
export const WINDOW = 60;

export interface Row {
  id: string | number; kind: string; summary?: string; detail?: string; detail_bytes?: number; detail_truncated?: boolean;
  session_id?: string; links?: Record<string, string | null>; ref?: { kind: string; id: string };
  // Lanes
  session?: string; label?: string; start?: number; end?: number; running?: boolean; end_unknown?: boolean; status?: string; ttfb?: number;
}
export type Lane = Row;
export interface CacheBlock {
  scope?: string; input: number | null; cached: number | null; uncached: number | null; hit_percent: number | null; denominator?: string;
  coverage?: { requests_with_usage: number | null; requests_total: number | null; complete?: boolean };
  models?: Record<string, { requests: number; input: number | null; cached: number | null; hit_percent: number | null }>;
  write?: number | null; write_known?: boolean;
}
export interface TreeNode { id: string; title: string | null; parent: string | null; depth: number; status: string }
export interface Omitted { kind: string; count?: number; bytes?: number; reason: string }
export interface Inspection {
  version: number; session_id: string; session: string; view: string; limit: number; before: string | null; next_before: string | null; more: boolean;
  generated: number; head: number; generation: string; floor: number; filters: Record<string, unknown>; tree: TreeNode[];
  cache: CacheBlock; cache_range: CacheBlock | null; notes: string[]; omitted: Omitted[]; text: string; records: Row[]; lanes: Row[];
}
export interface Filters { q: string; kind: string; status: string; types: string }
export interface Page {
  filters: Filters; stack: string[]; selected: string | null; expanded: Record<string, true>; full: Record<string, string>;
  top: number; zoom: number; pan: number; data?: Inspection; fetched?: number;
}
export interface InspectorState {
  session: string; target: string; view: number; pages: Record<string, Page>; seq: number; loading: boolean; loadingFull: string | null;
  error: string; paused: boolean; connected: boolean; newer: boolean; info: boolean;
}
export const inspector = signal<InspectorState | null>(null);
/** The body's scroll offset as last reported (tests read it). */
export const scrollTop = signal(0);

const blank = (): Page => ({ filters: { q: "", kind: "", status: "", types: "" }, stack: [], selected: null, expanded: {}, full: {}, top: 0, zoom: 1, pan: 0 });
const keyOf = (s: InspectorState) => `${s.target}|${s.view}`;
export const pageOf = (s: InspectorState): Page => s.pages[keyOf(s)] ?? blank();
const own = (s: InspectorState): Page => (s.pages[keyOf(s)] ??= blank());
export const dataOf = (s: InspectorState | null) => s ? pageOf(s).data : undefined;
/** Mutate the open state and publish it. */
function update(fn: (s: InspectorState) => void) { const s = inspector(); if (!s) return; fn(s); inspector.set({ ...s }); }

export const kindOptions = (view: number): string[] => view === 0 || view === 2 ? ["turn", "model", "tool", "attempt"] : view === 1 ? ["user", "assistant"] : [];
export const statusOptions = (view: number): string[] => view === 0 || view === 2 ? ["running", "completed", "failed", "interrupted"] : view === 5 ? ["pending", "replied"] : [];
export const supportsType = (view: number) => view === 3 || view === 4;

// --- formatting shared word for word with the TUI -------------------------------------------------
export const num = (n: number | null | undefined) => n == null ? "unknown" : Math.trunc(n).toLocaleString("en-US");
export const pct = (n: number | null | undefined) => n == null ? "unknown" : `${Number(n.toFixed(1))}%`;
export function age(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
}
export function bytesText(n: number): string {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`;
}
export const idOf = (r: Row) => String(r.id);
export const rowsOf = (data: Inspection | undefined, view: number): Row[] => (view === 0 ? data?.lanes : data?.records) ?? [];
export function windowRange(index: number, count: number): [number, number] {
  if (count <= WINDOW) return [0, count];
  const start = Math.min(Math.max(0, index - Math.floor(WINDOW / 2)), count - WINDOW);
  return [start, start + WINDOW];
}
export function cacheLines(data: Inspection | undefined): string[] {
  const cache = data?.cache;
  const out: string[] = [];
  const write = cache?.write_known ? num(cache.write) : "unknown";
  out.push(`Session lifetime · Input ${num(cache?.input)} · Cached ${num(cache?.cached)} · Uncached ${num(cache?.uncached)}`);
  out.push(`  Hit ${pct(cache?.hit_percent)} of ${cache?.denominator ?? "input tokens reported by provider"} · Writes ${write}`);
  const cov = cache?.coverage;
  if (cov) out.push(`  ${num(cov.requests_with_usage)} of ${num(cov.requests_total)} requests reported usage${cov.complete ? "" : " (partial)"}`);
  for (const [name, m] of Object.entries(cache?.models ?? {})) out.push(`  ${name} · ${num(m.requests)} requests · input ${num(m.input)} · cached ${num(m.cached)} · hit ${pct(m.hit_percent)}`);
  const range = data?.cache_range;
  if (range) {
    out.push(`Displayed page · Input ${num(range.input)} · Cached ${num(range.cached)} · Uncached ${num(range.uncached)}`);
    out.push(`  Hit ${pct(range.hit_percent)} · ${num(range.coverage?.requests_with_usage)} of ${num(range.coverage?.requests_total)} requests reported usage`);
    for (const [name, m] of Object.entries(range.models ?? {})) out.push(`  ${name} · ${num(m.requests)} requests · input ${num(m.input)} · cached ${num(m.cached)} · hit ${pct(m.hit_percent)}`);
  }
  return out;
}
export function omittedLines(data: Inspection | undefined): string[] {
  return (data?.omitted ?? []).map((o) => `Omitted · ${o.kind}${o.count != null ? ` ×${o.count}` : ""}${o.bytes != null ? ` · ${bytesText(o.bytes)}` : ""} · ${o.reason}`);
}
export type LiveTone = "live" | "paused" | "offline";
export function statusLine(s: InspectorState, now = Date.now() / 1000): { text: string; tone: LiveTone } {
  const page = pageOf(s);
  const updated = page.fetched == null ? "never" : age(now - page.fetched);
  let text: string; let tone: LiveTone = "live";
  if (!s.connected) { text = `○ Disconnected · data from ${updated} · not current · r retry`; tone = "offline"; }
  else if (s.paused) { text = `‖ Paused · updated ${updated} · w resume`; tone = "paused"; }
  else if (page.stack.length) { text = `‖ Paused · viewing older records · updated ${updated} · p newer`; tone = "paused"; }
  else if (page.fetched == null) text = "● Live · waiting for first data";
  else text = `● Live · updated ${updated}`;
  if (s.newer) text += " · newer records available";
  if (s.loading) text += " · refreshing…";
  return { text, tone };
}
export function filtersLine(s: InspectorState): string {
  const f = pageOf(s).filters;
  let t = `Filters · search ${f.q ? `"${f.q}"` : "none"}`;
  if (kindOptions(s.view).length) t += ` · kind ${f.kind || "any"}`;
  if (statusOptions(s.view).length) t += ` · status ${f.status || "any"}`;
  if (supportsType(s.view)) t += ` · type ${f.types || "any"}`;
  return t;
}
/** The tree selector's visible entries: a window of six around the target. */
export function treeWindow(tree: TreeNode[], target: string) {
  const at = Math.max(0, tree.findIndex((n) => n.id === target));
  const start = Math.min(Math.max(0, at - 2), Math.max(0, tree.length - 6));
  const end = Math.min(tree.length, start + 6);
  return { nodes: tree.slice(start, end), before: start, after: tree.length - end };
}

// --- axis ----------------------------------------------------------------------------------------------
export function axis(lanes: Row[]) {
  if (!lanes.length) return { origin: 0, total: 1 };
  const origin = Math.min(...lanes.map((l) => l.start ?? Infinity));
  const end = Math.max(origin, ...lanes.map((l) => l.end ?? origin));
  return { origin, total: Math.max(0.001, end - origin) };
}
export function timingWindow(lanes: Row[], zoom: number, pan: number) {
  const { origin, total } = axis(lanes);
  const span = total / Math.max(1, zoom);
  return { start: origin + (total - span) * pan / 1000, span, origin, total };
}

// --- requests --------------------------------------------------------------------------------------------
let counter = 0;
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
let headInflight = false;
const stamp = () => Date.now() / 1000;

function query(s: InspectorState): Record<string, string | number | undefined> {
  const p = pageOf(s);
  return {
    view: inspectorViews[s.view], limit: LIMIT, before: p.stack[p.stack.length - 1], session: s.target !== s.session ? s.target : undefined,
    q: p.filters.q || undefined, kind: p.filters.kind || undefined, status: p.filters.status || undefined, type: p.filters.types || undefined,
  };
}
/** Fetch the current page. Any response older than this request is ignored; its request is aborted. */
export async function refreshInspector() {
  const s = inspector();
  if (!s) return;
  controller?.abort();
  controller = new AbortController();
  const token = s.seq = ++counter;
  s.loading = true; s.error = "";
  inspector.set({ ...s });
  const signal = controller.signal;
  const at = { target: s.target, view: s.view };
  try {
    const data = await request<Inspection>("GET", `/api/session/${encodeURIComponent(s.session)}/inspect`, { query: query(s), signal });
    const now = inspector();
    if (!now || now.seq !== token) return;
    const page = now.pages[`${at.target}|${at.view}`] ?? (now.pages[`${at.target}|${at.view}`] = blank());
    page.data = data; page.fetched = stamp();
    now.loading = false; now.connected = true; now.error = ""; now.newer = false;
    const gone = data.tree.length > 0 && !data.tree.some((n) => n.id === now.target);
    if (gone && now.target !== now.session) { now.target = now.session; inspector.set({ ...now }); void refreshInspector(); return; }
    inspector.set({ ...now });
  } catch (e) {
    const now = inspector();
    if (!now || now.seq !== token || signal.aborted) return;
    now.loading = false; now.error = String(e); now.connected = false;
    inspector.set({ ...now });
  }
}
/** The cheap change check: refetch only when the head or the log generation moved. */
async function pollHead() {
  const s = inspector();
  if (!s || s.paused || s.loading || headInflight) return;
  headInflight = true;
  const token = s.seq;
  try {
    const head = await request<{ head: number; generation: string; floor: number }>("GET", `/api/session/${encodeURIComponent(s.session)}/inspect/head`, { query: { session: s.target !== s.session ? s.target : undefined } });
    const now = inspector();
    if (!now || now.seq !== token) return;
    const refetch = applyHead(now, head);
    inspector.set({ ...now });
    if (refetch) void refreshInspector();
  } catch {
    const now = inspector();
    if (now && now.seq === token) { now.connected = false; inspector.set({ ...now }); }
  } finally { headInflight = false; }
}
/** Apply a head response. True when the newest page must be refetched. */
export function applyHead(s: InspectorState, head: { head: number; generation: string; floor: number }): boolean {
  s.connected = true;
  const data = pageOf(s).data;
  if (!data) return !s.loading;
  if (head.generation !== data.generation || (head.floor ?? 0) > (data.floor ?? 0)) {
    for (const [k, p] of Object.entries(s.pages)) if (k.startsWith(`${s.target}|`)) { p.stack = []; p.selected = null; p.top = 0; }
    return true;
  }
  const moved = head.head !== data.head;
  if (moved && pageOf(s).stack.length) { s.newer = true; return false; }
  if (!moved) s.error = "";
  return moved;
}
/** One poll at a time: the next is scheduled when the last one has finished. */
function startTimer() {
  stopTimer();
  const next = () => { timer = setTimeout(() => { void pollHead().finally(() => { if (inspector() && timer !== undefined) next(); }); }, POLL_MS); };
  next();
}
function stopTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined; }

export function inspectSession(session?: string, view = 0) {
  const sid = session ?? viewed()?.id ?? model.sessions.keys().next().value;
  if (!sid) { toast("info", "Select a session to inspect"); return; }
  headInflight = false;
  inspector.set({ session: sid, target: sid, view, pages: {}, seq: 0, loading: true, loadingFull: null, error: "", paused: false, connected: true, newer: false, info: false });
  startTimer();
  void refreshInspector();
}
/** No requests or polling continue once closed; a response in flight is aborted and ignored. */
export function closeInspector() { ++counter; controller?.abort(); controller = null; stopTimer(); headInflight = false; inspector.set(null); }
export const connectionLost = () => connection().kind !== "live";

// --- operations (the same ones, in the same words, as the TUI) ----------------------------------------
export function setView(view: number) { update((s) => { s.view = view; s.newer = false; }); void refreshInspector(); }
export function setTarget(target: string) { update((s) => { s.target = target; s.newer = false; }); void refreshInspector(); }
export function stepSession(delta: number) {
  const s = inspector(); const tree = dataOf(s)?.tree ?? [];
  if (!s || !tree.length) return;
  const at = Math.max(0, tree.findIndex((n) => n.id === s.target));
  const next = Math.min(Math.max(0, at + delta), tree.length - 1);
  if (next !== at) setTarget(tree[next]!.id);
}
export function older() {
  const s = inspector(); const cursor = dataOf(s)?.next_before;
  if (!s || !cursor) return;
  update((x) => { const p = own(x); p.stack.push(cursor); p.top = 0; });
  void refreshInspector();
}
export function newer() {
  const s = inspector();
  if (!s || !pageOf(s).stack.length) return;
  update((x) => { const p = own(x); p.stack.pop(); p.top = 0; x.newer = false; });
  void refreshInspector();
}
function resetPage(p: Page) { p.stack = []; p.selected = null; p.top = 0; }
export function cycleFilter(field: "kind" | "status") {
  const s = inspector(); if (!s) return;
  const options = field === "kind" ? kindOptions(s.view) : statusOptions(s.view);
  if (!options.length) return;
  update((x) => {
    const p = own(x); const at = options.indexOf(p.filters[field]);
    p.filters[field] = at < 0 ? options[0]! : at + 1 < options.length ? options[at + 1]! : "";
    resetPage(p);
  });
  void refreshInspector();
}
export function setFilter(field: "q" | "types", value: string) {
  const s = inspector(); if (!s) return;
  const text = value.trim();
  if (pageOf(s).filters[field] === text) return;
  update((x) => { const p = own(x); p.filters[field] = text; resetPage(p); });
  void refreshInspector();
}
export function clearFilters() {
  const s = inspector(); if (!s) return;
  const f = pageOf(s).filters;
  if (!(f.q || f.kind || f.status || f.types)) return;
  update((x) => { const p = own(x); p.filters = { q: "", kind: "", status: "", types: "" }; resetPage(p); });
  void refreshInspector();
}
export function toggleLive() {
  const s = inspector(); if (!s) return;
  update((x) => { x.paused = !x.paused; });
  if (!inspector()!.paused) { void refreshInspector(); }
}
export function indexOf(s: InspectorState): number {
  const rows = rowsOf(dataOf(s), s.view); const id = pageOf(s).selected;
  const at = id == null ? -1 : rows.findIndex((r) => idOf(r) === id);
  return at < 0 ? 0 : at;
}
export function select(id: string | null) { update((s) => { own(s).selected = id; }); }
/** Row buttons by record id, so a keyboard move can bring the selected row into view. */
export const rowNodes = new Map<string, any>();
/** The scrolling body, so a keyboard move can keep the selected row in view. */
export const scrollNode: { current: any } = { current: null };
async function reveal(id: string) {
  try {
    const row = rowNodes.get(id); const box = scrollNode.current;
    if (!row || !box) return;
    const [r, b] = await Promise.all([row.measure(), box.measure()]);
    if (!r || !b) return;
    const top = r.pagePosition.y - b.pagePosition.y; const bottom = top + r.bounds.size.height;
    const view = b.bounds.size.height; const now = Math.round(inspector() ? pageOf(inspector()!).top : 0);
    if (top < 0) box.scrollTo({ y: Math.max(0, now + top), animated: false });
    else if (bottom > view) box.scrollTo({ y: now + bottom - view, animated: false });
  } catch { /* not measurable yet */ }
}
export function move(delta: number) {
  const s = inspector(); if (!s) return;
  const rows = rowsOf(dataOf(s), s.view); if (!rows.length) return;
  const id = idOf(rows[Math.min(Math.max(0, indexOf(s) + delta), rows.length - 1)]!);
  select(id);
  // The row may be redrawn first when the window moves, so measure after a beat.
  setTimeout(() => void reveal(id), 0);
  setTimeout(() => void reveal(id), 80);
}
export function toggleExpand(id?: string) {
  const s = inspector(); if (!s) return;
  const rows = rowsOf(dataOf(s), s.view); const row = id != null ? rows.find((r) => idOf(r) === id) : rows[indexOf(s)];
  if (!row) return;
  update((x) => { const p = own(x); const k = idOf(row); p.selected = k; if (p.expanded[k]) delete p.expanded[k]; else p.expanded[k] = true; });
}
export function zoomBy(up: boolean) { update((s) => { const p = own(s); p.zoom = up ? Math.min(64, p.zoom * 2) : Math.max(1, Math.floor(p.zoom / 2)); }); }
export function panBy(right: boolean) { update((s) => { const p = own(s); p.pan = right ? Math.min(1000, p.pan + 100) : Math.max(0, p.pan - 100); }); }
/** Fit the selected operation, or the whole range when it is already fitted. */
export function fit() {
  const s = inspector(); if (!s || s.view !== 0) return;
  const lanes = rowsOf(dataOf(s), 0); if (!lanes.length) return;
  const { origin, total } = axis(lanes); const lane = lanes[indexOf(s)];
  update((x) => {
    const p = own(x); const a = lane?.start ?? 0; const b = lane?.end ?? a;
    if (p.zoom === 1 && b - a > 0 && total / (b - a) >= 2) {
      const zoom = Math.min(64, Math.max(1, Math.floor(total / ((b - a) * 1.2))));
      const span = total / zoom;
      const start = Math.min(Math.max((a + b) / 2 - span / 2, origin), origin + total - span);
      p.zoom = zoom; p.pan = total > span ? Math.round(((start - origin) / (total - span)) * 1000) : 0;
    } else { p.zoom = 1; p.pan = 0; }
  });
}
/** Load the full record of the selected row when its detail was clipped. */
export async function loadFull() {
  const s = inspector(); if (!s) return;
  const row = rowsOf(dataOf(s), s.view)[indexOf(s)];
  if (!row || !row.detail_truncated || pageOf(s).full[idOf(row)]) return;
  const kind = s.view === 0 ? row.ref?.kind ?? "" : row.kind; const id = s.view === 0 ? row.ref?.id ?? "" : idOf(row);
  if (!["message", "event", "turn", "request", "tool", "attempt"].includes(kind)) return;
  const at = keyOf(s); const rowId = idOf(row);
  update((x) => { x.loadingFull = rowId; });
  try {
    const body = await request<{ record: unknown }>("GET", `/api/session/${encodeURIComponent(s.session)}/inspect/record`, { query: { kind, id } });
    const now = inspector(); if (!now) return;
    const page = now.pages[at]; if (page) page.full[rowId] = JSON.stringify(body.record, null, 2);
    if (now.loadingFull === rowId) now.loadingFull = null;
    inspector.set({ ...now });
  } catch (e) {
    update((x) => { x.loadingFull = null; x.error = `Full record failed to load: ${String(e)}`; });
  }
}
export function copyText(): string {
  const s = inspector(); if (!s) return "";
  const row = rowsOf(dataOf(s), s.view)[indexOf(s)];
  if (row) { const full = pageOf(s).full[idOf(row)]; if (full) return full; if (row.detail) return row.detail; }
  return s.view === 0 ? JSON.stringify(dataOf(s), null, 2) : dataOf(s)?.text ?? "";
}
export function toggleInfo() { update((s) => { s.info = !s.info; }); }

/** One `inspector.*` manifest command. */
export function inspectorCommand(id: string) {
  switch (id) {
    case "inspector.view_timing": return setView(0);
    case "inspector.view_conversation": return setView(1);
    case "inspector.view_steps": return setView(2);
    case "inspector.view_requests": return setView(3);
    case "inspector.view_raw": return setView(4);
    case "inspector.view_questions": return setView(5);
    case "inspector.refresh": return void refreshInspector();
    case "inspector.live_toggle": return toggleLive();
    case "inspector.session_next": return stepSession(1);
    case "inspector.session_prev": return stepSession(-1);
    case "inspector.search": return focusers.q?.();
    case "inspector.filter_kind": return cycleFilter("kind");
    case "inspector.filter_status": return cycleFilter("status");
    case "inspector.filter_type": return focusers.types?.();
    case "inspector.filter_clear": return clearFilters();
    case "inspector.select_next": return move(1);
    case "inspector.select_prev": return move(-1);
    case "inspector.expand": return toggleExpand();
    case "inspector.load_full": return void loadFull();
    case "inspector.older": return older();
    case "inspector.newer": return newer();
    case "inspector.copy": return void Clipboard.writeText(copyText());
    case "inspector.cache_info": return toggleInfo();
    case "inspector.zoom_in": return zoomBy(true);
    case "inspector.zoom_out": return zoomBy(false);
    case "inspector.pan_left": return panBy(false);
    case "inspector.pan_right": return panBy(true);
    case "inspector.fit": return fit();
  }
}
/** The filter inputs register here so `/` and `t` can focus them. */
export const focusers: { q?: () => void; types?: () => void } = {};
