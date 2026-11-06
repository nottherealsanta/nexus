import { signal } from "nsh";
import { request } from "./core/transport";
import { model, toast, viewed } from "./store";

export const inspectorViews = ["Timing", "Conversation", "Steps", "Requests", "Raw"] as const;
export interface Lane { id: string; session: string; session_id: string; kind: string; label: string; start: number; end: number; running: boolean; end_unknown?: boolean; status: string; detail: string }
export interface Inspection {
  session_id: string; view: string; page: number; more: boolean; generated: number;
  cache: { input: number | null; cached: number | null; uncached: number | null; hit_percent: number | null; write: number | null };
  notes: string[]; text: string; lanes: Lane[];
}
export const inspector = signal<{ session: string; view: string; page: number; loading: boolean; error?: string; data?: Inspection } | null>(null);
export const inspectorLane = signal<string | null>(null);
export const inspectorZoom = signal(1);
export const inspectorPan = signal(0);
export const inspectorInfo = signal(false);
let generation = 0;
export async function inspectSession(session?: string, view = "Timing", page = 0) {
  const sid = session ?? viewed()?.id ?? model.sessions.keys().next().value;
  if (!sid) { toast("info", "Select a session to inspect"); return; }
  const previous = inspector();
  if (!previous) inspectorInfo.set(false);
  const token = ++generation;
  inspector.set({ session: sid, view, page, loading: true, data: previous?.session === sid && previous.view === view ? previous.data : undefined });
  if (previous?.session !== sid || previous.view !== view || previous.page !== page) {
    inspectorLane.set(null); inspectorZoom.set(1); inspectorPan.set(0);
  }
  try {
    const data = await request<Inspection>("GET", `/api/session/${encodeURIComponent(sid)}/inspect`, { query: { view, page } });
    if (generation === token && inspector()) inspector.set({ session: sid, view, page, loading: false, data });
  } catch (e) {
    if (generation === token && inspector()) inspector.set({ session: sid, view, page, loading: false, error: String(e) });
  }
}
export function closeInspector() { ++generation; inspector.set(null); }

export function timingWindow(lanes: Lane[], zoom: number, pan: number) {
  if (!lanes.length) return { start: 0, span: 1, origin: 0 };
  const start = Math.min(...lanes.map((l) => l.start));
  const finish = Math.max(...lanes.map((l) => l.end));
  const total = Math.max(0.001, finish - start);
  const span = total / zoom;
  return { start: start + (total - span) * pan, span, origin: start };
}
