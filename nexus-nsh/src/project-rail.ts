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
