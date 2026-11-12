// The mobile (phone) cut-down client, as pure rules (AGENTS.md "Mobile is a cut-down client", PLAN.md section 5):
// three tabs, a session view of status, user messages and agent replies, the Settings that apply on a phone, and no
// desktop surfaces. The UI reads these; tests check them without a device.
import { manifest } from "./gen/manifest";

/** All iOS devices use the cut-down client; Mac Catalyst reports macOS. */
export function mobileClient(native: boolean, os: string, idiom: string): boolean {
  return native && (os === "ios" || idiom === "phone");
}

/** The phone's tabs, in order. A session opens full screen above the Sessions tab. */
export const PHONE_TABS = [
  { id: "sessions", label: "Sessions", icon: "sessions" },
  { id: "manager", label: "Manager", icon: "manager" },
  { id: "settings", label: "Settings", icon: "settings" },
] as const;
export type PhoneTab = (typeof PHONE_TABS)[number]["id"];
export type PhonePage = PhoneTab | "conversation";

/** Partition an already recent-first session list without losing its order. */
export function phoneSessionGroups<S>(sessions: readonly S[], status: (session: S) => { waiting: boolean; running: boolean }): { title: string; sessions: S[] }[] {
  const groups: { title: string; sessions: S[] }[] = [
    { title: "Needs you", sessions: [] }, { title: "Running", sessions: [] }, { title: "Recent", sessions: [] },
  ];
  for (const session of sessions) {
    const state = status(session);
    groups[state.waiting ? 0 : state.running ? 1 : 2].sessions.push(session);
  }
  return groups.filter((group) => group.sessions.length > 0);
}

const mobile = (manifest.settings as unknown as { mobile?: { sections?: string[]; rows?: string[] } }).mobile ?? {};
const mobileSections = new Set(mobile.sections ?? []);
const mobileRows = new Set(mobile.rows ?? []);

/** Settings sections shown on a phone, in the manifest's order; page sections keep their own content. */
export function mobileSettings<S extends { id: string; page?: boolean; row?: readonly { id: string }[] }>(sections: readonly S[]): S[] {
  return sections.filter((s) => mobileSections.has(s.id)).map((s) => (s.page ? s : { ...s, row: (s.row ?? []).filter((r) => mobileRows.has(r.id)) }));
}

/** Commands that reach a desktop surface (files, editor, Git, inspector, terminal, overview, project management, tool detail). */
const DESKTOP_PREFIXES = ["terminal.", "inspector.", "editor.", "git.", "file.", "files.", "tool.", "copy.tool_"];
const DESKTOP_IDS = new Set([
  "app.open_inspector", "session.open_inspector", "app.usage", "app.help_keys", "palette.open",
  "sidebar.left.toggle", "sidebar.right.toggle", "sidebar.tab.files", "sidebar.tab.git", "sidebar.tab.projects", "sidebar.focus",
  "session.worktrees", "session.worktree_info", "session.worktree.integrate", "session.resources", "session.change_directory", "session.wire_toggle",
  "session.tab_close", "session.tab_next", "session.tab_prev", "session.export", "session.copy_id",
  "project.overview", "project.move_up", "project.move_down", "project.status.current", "project.status.passive",
  "project.status.inactive", "project.remove", "project.add",
  "timeline.detail_cycle", "turn.fold_toggle", "turn.fold_all_but_last", "turn.unfold_all", "composer.attach", "composer.mention", "tools.configure", "skills.configure", "skills.learn",
  "mcp.configure", "manager.open_session", "manager.context", "search.start",
]);
export const PHONE_UNAVAILABLE = "Not available on a phone: use Nexus on a desktop or the web.";

/** True when a command is for the desktop and web only, so a phone neither shows nor runs it. */
export function desktopOnly(id: string): boolean {
  return DESKTOP_IDS.has(id) || DESKTOP_PREFIXES.some((p) => id.startsWith(p));
}

/**
 * The rows a phone session view keeps: the user's messages, the agent's replies (live ones too), the control that
 * loads older history, a fork's inherited block, and failures, which are the session's status. Context, thoughts, tools, children,
 * handoffs, turn footers and other notices stay on the desktop.
 */
export function phoneRow(row: { kind: string; tone?: string }): boolean {
  // A fork's inherited block stays, folded with the desktop's wording; open, its messages are filtered like any other.
  if (row.kind === "user" || row.kind === "assistant" || row.kind === "older" || row.kind === "fork") return true;
  return row.kind === "diag" && row.tone === "error";
}

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/** One line for the compact approval card: the command, path or first argument the tool wants, without the diff. */
export function compactApproval(a: { tool: string; args?: Record<string, unknown> | null }): string {
  const args = a.args ?? {};
  const pick = (...keys: string[]) => { for (const k of keys) { const v = str(args[k]).trim(); if (v) return v; } return ""; };
  let line = "";
  if (a.tool === "bash") line = pick("command", "cmd");
  else if (a.tool === "patch") line = (str(args.patch).match(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/m)?.[1] ?? "").trim() || pick("path");
  else line = pick("path", "file_path", "url", "query", "name");
  if (!line) { const first = Object.values(args)[0]; line = str(first); }
  const one = line.split("\n").filter((l) => l.trim())[0] ?? "";
  const lines = line.split("\n").filter((l) => l.trim()).length;
  return lines > 1 ? `${one} …` : one;
}

/** The phone session header's status word: what the session is doing, in the order a user must act on it. */
export function phoneStatus(s: { waiting: boolean; running: boolean; archived?: boolean }): { word: string; tone: "warning" | "info" | "muted" } {
  if (s.waiting) return { word: "Waiting on you", tone: "warning" };
  if (s.running) return { word: "Working", tone: "info" };
  return { word: s.archived ? "Archived" : "Idle", tone: "muted" };
}
