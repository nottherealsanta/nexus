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
  "sidebar.left.toggle", "sidebar.right.toggle", "sidebar.tab.files", "sidebar.tab.git", "sidebar.focus",
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

// --- Mobile redesign (plans/05 section 5.2): sessions sections, transport, pairing, health -------------------------

export type SessionSectionId = "needs" | "running" | "recent";
export interface SectionSession { id: string; updated: number; archived?: boolean | number; status?: string }
export interface SessionSection<S> { id: SessionSectionId; title: string; count: number; items: S[] }

/**
 * The Sessions list's sections in the order a user acts on them: sessions that need an answer (with a count), then
 * running ones, then the rest by recency. Each session is in exactly one section; empty sections are left out.
 */
export function sessionSections<S extends SectionSession>(sessions: readonly S[], state: { waiting: (id: string) => boolean; running: (id: string) => boolean }): SessionSection<S>[] {
  const byRecent = [...sessions].sort((a, b) => b.updated - a.updated);
  const needs = byRecent.filter((s) => state.waiting(s.id));
  const running = byRecent.filter((s) => !state.waiting(s.id) && state.running(s.id));
  const recent = byRecent.filter((s) => !state.waiting(s.id) && !state.running(s.id));
  const out: SessionSection<S>[] = [
    { id: "needs", title: "Needs you", count: needs.length, items: needs },
    { id: "running", title: "Running", count: running.length, items: running },
    { id: "recent", title: "Recent", count: recent.length, items: recent },
  ];
  return out.filter((s) => s.items.length > 0);
}

/** The project's colour dot: a stable token per project id, so a project keeps its colour. */
export const PROJECT_DOTS = ["bg-primary", "bg-info", "bg-success", "bg-warning", "bg-multi"] as const;
export function projectDot(id: string): (typeof PROJECT_DOTS)[number] {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PROJECT_DOTS[h % PROJECT_DOTS.length];
}

/** How a phone reaches Core, from the address alone: Tailscale (100.64.0.0/10 or *.ts.net), the LAN, this device, or elsewhere. */
export type Transport = "Tailscale" | "LAN" | "This device" | "Remote";
export function transportOf(url: string): Transport {
  let host = "";
  try { host = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, ""); } catch { return "Remote"; }
  if (host === "localhost" || host === "::1" || host.startsWith("127.")) return "This device";
  if (host.endsWith(".ts.net")) return "Tailscale";
  const v4 = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if (a === 100 && b >= 64 && b <= 127) return "Tailscale";
    if (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254)) return "LAN";
    return "Remote";
  }
  if (host.endsWith(".local") || !host.includes(".")) return "LAN";
  return "Remote";
}

/** A Core address as a short host label for the connection card. */
export function hostLabel(url: string): string {
  try { return new URL(url).hostname.replace(/^\[|\]$/g, ""); } catch { return url; }
}

export interface Pairing { url: string; token?: string; name?: string }
/**
 * What the Connect screen accepts: a `nexus://pair?url=…&token=…&name=…` link (what `nexus pair` prints and encodes in
 * its QR code), an http(s) address, or a bare host[:port]. Returns null when the text is none of those.
 */
export function parsePairing(text: string): Pairing | null {
  const t = text.trim();
  if (!t) return null;
  const link = t.match(/^nexus:\/\/pair\?(.+)$/i);
  if (link) {
    const q = new URLSearchParams(link[1]);
    const url = q.get("url");
    if (!url || !/^https?:\/\//i.test(url)) return null;
    return { url: url.replace(/\/+$/, ""), token: q.get("token") || undefined, name: q.get("name") || undefined };
  }
  if (/\s/.test(t) || /^[a-z][a-z0-9+.-]*:\/\//i.test(t) && !/^https?:\/\//i.test(t)) return null;
  return { url: /^https?:\/\//i.test(t) ? t.replace(/\/+$/, "") : `http://${t}` };
}

export interface RecentCore { url: string; name?: string; at: number }
/** The recent Cores list: newest first, one per address, at most five. */
export function rememberCore(list: readonly RecentCore[], core: RecentCore, max = 5): RecentCore[] {
  return [core, ...list.filter((c) => c.url !== core.url)].slice(0, max);
}

/** The Connect screen's message for a failed connection; iOS Local Network permission is the usual cause on a LAN. */
export function connectProblem(message: string, url: string): { title: string; body: string } {
  const lan = transportOf(url) === "LAN";
  if (/asks for a token/i.test(message)) return { title: "This Core asks for a token", body: "Restart it with NEXUS_HOST=0.0.0.0 so a phone can connect, or pair again with `nexus pair`." };
  if (/transport security|secure connection/i.test(message)) return { title: "iOS blocked plain HTTP", body: "Use an https:// address, or Tailscale." };
  if (lan) return { title: "Cannot reach this Core", body: "Is Local Network allowed for Nexus? Open iOS Settings, Privacy & Security, Local Network and switch Nexus on. Also check that this phone is on the same Wi-Fi and Core runs with NEXUS_HOST=0.0.0.0." };
  return { title: "Cannot reach this Core", body: "Check the address and that Core is running and reachable from this phone (same network or Tailscale)." };
}

/** Round-trip samples for the connection card: the newest `max` readings in ms (null is a failed probe). */
export function pushSample(samples: readonly (number | null)[], sample: number | null, max = 30): (number | null)[] {
  return [...samples, sample].slice(-max);
}
/** The latest reading, the median of the successful ones and the failure count. */
export function healthSummary(samples: readonly (number | null)[]): { now: number | null; median: number | null; failed: number } {
  const ok = samples.filter((x): x is number => x !== null).sort((a, b) => a - b);
  return { now: samples.length ? samples[samples.length - 1] : null, median: ok.length ? ok[Math.floor(ok.length / 2)] : null, failed: samples.length - ok.length };
}
/** Bar heights 0..1 for the trace (failures are full-height); the scale is the slowest reading, at least 100 ms. */
export function traceBars(samples: readonly (number | null)[]): { h: number; failed: boolean }[] {
  const top = Math.max(100, ...samples.filter((x): x is number => x !== null));
  return samples.map((s) => s === null ? { h: 1, failed: true } : { h: Math.max(0.08, Math.min(1, s / top)), failed: false });
}
