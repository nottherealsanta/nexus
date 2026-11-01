import type { Project, Session } from "./core/api";

export function railProjects(projects: Iterable<Project>): Project[] {
  return [...projects].sort((a, b) => a.created - b.created || a.id.localeCompare(b.id));
}

export function projectLabels(projects: Iterable<Project>): Map<string, string> {
  const labels = new Map<string, string>();
  const used = new Set<string>();
  for (const p of railProjects(projects)) {
    const chars = Array.from(p.name.trim().toUpperCase());
    const first = chars[0] || "?";
    let label = first;
    if (used.has(label)) {
      label = chars.slice(1).filter((c) => /\p{L}/u.test(c) && !/[AEIOU]/.test(c))
        .map((c) => first + c).find((candidate) => !used.has(candidate)) || "";
      if (!label) {
        let n = 2;
        while (used.has(first + n)) n++;
        label = first + n;
      }
    }
    labels.set(p.id, label);
    used.add(label);
  }
  return labels;
}

export function projectActivity(id: string, sessions: Iterable<Session>, running: ReadonlyMap<string, string>, now: number) {
  let count = 0, latest = 0;
  for (const s of sessions) {
    if (s.project_id !== id) continue;
    if (running.has(s.id) || s.status === "busy") count++;
    latest = Math.max(latest, s.updated);
  }
  return { running: count, latest, active: count > 0 || (latest > 0 && now - latest * 1000 < 3_600_000) };
}

/** Counts under a rail project: needs attention, finished (unread), running, idle. */
export type ProjectCounts = [attention: number, finished: number, running: number, idle: number];

/**
 * The TUI's rail counters: one per root session of the project, in this order.
 * A waiting approval or question anywhere in a session's tree marks the root;
 * archived sessions are left out, and a failed session counts as idle.
 */
export function projectCounts(
  id: string,
  sessions: Iterable<Session>,
  approvals: Iterable<{ session_id: string }>,
  questions: Iterable<{ session_id: string }>,
  running: ReadonlyMap<string, string>,
  unread: ReadonlySet<string>,
): ProjectCounts {
  const all = [...sessions];
  const byId = new Map(all.map((s) => [s.id, s]));
  const rootOf = (sessionId: string) => {
    const seen = new Set<string>();
    let current = byId.get(sessionId);
    while (current?.parent_id && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.parent_id);
    }
    return current?.id;
  };
  const waiting = new Set<string>();
  for (const request of [...approvals, ...questions]) {
    const root = rootOf(request.session_id);
    if (root) waiting.add(root);
  }
  const counts: ProjectCounts = [0, 0, 0, 0];
  for (const s of all) {
    if (s.project_id !== id || s.parent_id || s.archived) continue;
    if (waiting.has(s.id)) counts[0]++;
    else if (unread.has(s.id)) counts[1]++;
    else if (running.has(s.id) || s.status === "busy") counts[2]++;
    else counts[3]++;
  }
  return counts;
}

/** A rail counter past nine is an asterisk, so it still fits the rail. */
export const railCount = (n: number) => n > 9 ? "*" : String(n);
