import type { Project, Session } from "./core/api";
import { orderedProjects, projectStatus, PROJECT_STATUS } from "./project-rail";

/** What a root session is doing, most urgent first (the rail's counters use the same order). */
export type TrayState = "attention" | "finished" | "running" | "idle";
const MARK: Record<TrayState, string> = { attention: "●", finished: "◆", running: "▶", idle: "○" };
const WORD: Record<TrayState, string> = { attention: "needs attention", finished: "finished", running: "running", idle: "idle" };
export const TRAY_SESSION_LIMIT = 8;

export interface TraySession { id: string; title: string; state: TrayState }
export interface TrayProject { id: string; name: string; counts: Record<TrayState, number>; sessions: TraySession[]; more: number }
export interface TrayModel { attention: boolean; projects: TrayProject[]; tooltip: string }

/**
 * The tray's content: one entry per root session (a waiting approval or question anywhere in its tree
 * marks the root), under each project that is not inactive, most urgent and most recent first.
 */
export function trayModel(
  projects: Iterable<Project>,
  sessions: Iterable<Session>,
  approvals: Iterable<{ session_id: string }>,
  questions: Iterable<{ session_id: string }>,
  running: ReadonlyMap<string, string>,
  unread: ReadonlySet<string>,
): TrayModel {
  const all = [...sessions];
  const byId = new Map(all.map((s) => [s.id, s]));
  const rootOf = (id: string) => {
    const seen = new Set<string>();
    let current = byId.get(id);
    while (current?.parent_id && !seen.has(current.id)) { seen.add(current.id); current = byId.get(current.parent_id); }
    return current?.id;
  };
  const waiting = new Set<string>();
  for (const request of [...approvals, ...questions]) {
    const root = rootOf(request.session_id);
    if (root) waiting.add(root);
  }
  const order: TrayState[] = ["attention", "finished", "running", "idle"];
  const stateOf = (s: Session): TrayState =>
    waiting.has(s.id) ? "attention" : unread.has(s.id) ? "finished" : running.has(s.id) || s.status === "busy" ? "running" : "idle";
  const out: TrayProject[] = [];
  for (const p of orderedProjects(projects)) {
    if (projectStatus(p) === PROJECT_STATUS.inactive) continue;
    const rows: TraySession[] = all
      .filter((s) => s.project_id === p.id && !s.parent_id && !s.archived)
      .sort((a, b) => b.updated - a.updated || a.id.localeCompare(b.id))
      .map((s) => ({ id: s.id, title: s.title || "New session", state: stateOf(s) }));
    rows.sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));
    const counts = { attention: 0, finished: 0, running: 0, idle: 0 };
    for (const r of rows) counts[r.state]++;
    out.push({ id: p.id, name: p.name, counts, sessions: rows.slice(0, TRAY_SESSION_LIMIT), more: Math.max(0, rows.length - TRAY_SESSION_LIMIT) });
  }
  const attention = out.reduce((n, p) => n + p.counts.attention, 0);
  const running_ = out.reduce((n, p) => n + p.counts.running, 0);
  const tooltip = attention > 0 ? `Nexus: ${attention} ${attention === 1 ? "session needs" : "sessions need"} attention`
    : running_ > 0 ? `Nexus: ${running_} running` : "Nexus";
  return { attention: attention > 0, projects: out, tooltip };
}

/** A project's submenu title: its name and the counts that are not zero. */
export function projectTitle(p: TrayProject): string {
  const parts = (["attention", "running", "finished"] as const).filter((k) => p.counts[k] > 0).map((k) => `${p.counts[k]} ${k === "attention" ? "attention" : k}`);
  return parts.length ? `${p.name} (${parts.join(", ")})` : p.name;
}

export const sessionTitle = (s: TraySession) => `${MARK[s.state]} ${s.title} (${WORD[s.state]})`;
