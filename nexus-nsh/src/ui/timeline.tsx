import { agentColor } from "../agent-color";
import { List, Scroll, computed, effect, signal, Text, View } from "nsh";
import { Image } from "nsh";
import { Button, TextView } from "nsh/base";
import type { TextViewCodeBlock } from "nsh/base";
import { FOCUS, Icon, IconButton, PROSE, Shimmer, Status, StatusRail, T } from "./kit";
import { DiffView } from "./diff";
import { disabledOf, startingSections, StartingContext } from "./newsession";
import type { CtxSection } from "./newsession";
import { ACTIVITY_CHEVRON, CARD_INSET, TEXT_INDENT, TimelineColumn } from "./reading-column";
import { byId, copyText, jump, keyLabel, openLinked, run, toggleSessionInventory } from "../commands";
import { config, contextDetail, focusedTool, imagePreview, isNarrow, m, model, modelCatalog, scrolledUp, selectSession, streamRev, timelineDetail, timelineOpen, toolOutputAll, touch, viewed } from "../store";
import type { CoreEvent, Message, Part, TurnSummary } from "../core/api";
import { floorIndex, groupRequests, sumUsage, toolTimes, turnsIn } from "../core/turns";
import { apiUrl } from "../core/transport";
import type { IconName } from "../icons";
import { roles, statusGlyphs } from "../gen/theme";
import { pasteLabel } from "../pastes";
import "../../fonts/MonaspaceArgon-Regular.otf";
import "../../fonts/MonaspaceArgon-Medium.otf";
import "../../fonts/MonaspaceArgon-Bold.otf";

type Tone = "info" | "accent" | "warning" | "error";
type TurnStatus = "running" | "done" | "error" | "interrupted" | "step_limit";
type Diff = { add: number; del: number };
/** Timeline metadata uses the app's monospace face. */
const META = `${T.meta}`;
/** A tappable row: finger-sized on a phone, its desktop height otherwise. */
const hit = (base: string) => (isNarrow() ? "min-h-11" : base);

type Row =
  | { key: string; kind: "footer"; segments: { text: string }[]; error?: string; interrupted: boolean }
  | {
      key: string; kind: "user"; delivery?: string | null; images?: { name: string; data_url: string }[]; pastes?: { marker: string; text: string }[]; n: number; time: string; text: string; turnKey: string; folded: boolean;
      tools: number; duration: string; status: TurnStatus;
    }
  | {
      key: string; kind: "context"; reconstructed?: boolean;
      sections: CtxSection[]; total: number; window?: number;
    }
  | { key: string; kind: "notice"; text: string; label: string; tone: Tone }
  | { key: string; kind: "handoff"; from: string; summary: string; time: string }
  | { key: string; kind: "assistant"; text: string; streaming?: boolean }
  | { key: string; kind: "reasoning"; text: string; encrypted?: boolean; open: boolean; running?: boolean }
  | {
      key: string; kind: "activity"; summary: string; items: (Extract<Row, { kind: "tool" }> | Extract<Row, { kind: "reasoning" }>)[];
      open: boolean; defaultOpen: boolean; running: boolean; hidden: number; denied: number; diff?: Diff;
    }
  | {
      key: string; kind: "tool"; id: string; name: string; arg: string; summary: string; tone: Tone;
      state: "running" | "done" | "error" | "waiting" | "denied"; duration?: string; seconds?: number; diff?: Diff; output?: string;
      /** The file an edit counts toward in its group's summary (the call id without a path). */
      editFile?: string; exit?: number; errorLine?: string;
      detail: ToolDetailLine[];
      raw: string; open: boolean; clipped: boolean; showAll: boolean;
      /** Images the tool returned (K8): artifacts Core stored, shown beside the output. */
      media: { artifact: string; mime: string; name: string }[];
    }
  | {
      key: string; kind: "child"; childId?: string; agent: string; title: string; state: "running" | "done" | "idle";
      background: boolean; meta: string; outcome: string;
    }
  | { key: string; kind: "diag"; text: string; label: string; detail?: string; tone: Tone; count?: number }
  | { key: string; kind: "older"; text: string; action?: "load-older" }
  | { key: string; kind: "empty" };

export type ToolDetailLine = { text: string; tone: "plain" | "add" | "del" | "info" | "error" | "accent" | "command" | "muted" | "show-all" | "show-less" };
const DETAIL_HEAD = 100;
const DETAIL_TAIL = 100;
const HIGH_HEAD = 30;
const HIGH_TAIL = 20;
const HIDDEN_MARK = "lines hidden · show all (o)";
/** Output rows kept at the end of a tool's detail, and the command rows kept at its start (the TUI's `OUTPUT_TAIL` and `COMMAND_ROWS`). */
const OUTPUT_TAIL = 8;
const COMMAND_ROWS = 2;
/** The result Core stores when the user declines a tool call. */
const DECLINED = "the user declined this tool call";
/** How long a finished group's chevron flashes the accent (the TUI's six animation frames). */
const FLASH_MS = 600;
/** Verb and thought icons in an open group: quieter than the 16 px chrome icons. */
const MEMBER_ICON = 13;

/** UI-only cap: the model's separate output clip stays untouched. */
export function capToolDetail(lines: ToolDetailLine[], showAll: boolean, head = DETAIL_HEAD, tail = DETAIL_TAIL): ToolDetailLine[] {
  if (showAll || lines.length <= head + tail + 20) return lines;
  const hidden = lines.length - head - tail;
  return [
    ...lines.slice(0, head),
    { text: `… ${hidden} ${HIDDEN_MARK}`, tone: "show-all" },
    ...lines.slice(-tail),
  ];
}

/** Command and output detail: `head` command rows are never cut, and long output keeps its last rows. */
export function capToolOutput(lines: ToolDetailLine[], showAll: boolean, head: number): ToolDetailLine[] {
  const rows = lines.length - head;
  if (rows <= OUTPUT_TAIL + 2) return lines;
  if (showAll) return [...lines.slice(0, head), { text: `↑ showing all ${rows} lines · o show less`, tone: "show-less" }, ...lines.slice(head)];
  return [
    ...lines.slice(0, head),
    { text: `↑ ${rows - OUTPUT_TAIL} earlier lines · enter show all`, tone: "show-all" },
    ...lines.slice(-OUTPUT_TAIL),
  ];
}

const NOTICE = /^(?:\[(Nexus notice|Nexus recovery|Notification)|→ Background Shell Done:)/;
const closedTurns = signal<Record<string, boolean>>({});
// Sticky user bar: the bands' geometry (content coordinates) and the list's scroll offset.
type Band = { key: string; top: number; height: number; n: number; text: string; turnKey: string };
const rowHeights = signal<Record<string, number>>({});
const scrollY = signal(0);
const viewH = signal(0);
const focusedBand = signal<string | null>(null);
// Sticky user bar line height (secondary text) and bar height, for the push-up geometry.
const STICKY_LINE = 20;
const STICKY_BAR = 28;
const LIST_PAD = 12; // the list's `py-3`
const ROW_ESTIMATE = 72; // the list's estimatedItemSize, for rows not laid out yet

function open(key: string, fallback: boolean): boolean {
  const v = timelineOpen()[key];
  return v === undefined ? fallback : v;
}
function toggle(key: string, fallback: boolean) {
  const now = open(key, fallback);
  timelineOpen.update((o) => ({ ...o, [key]: !now }));
}
function toggleFold(key: string) {
  closedTurns.update((o) => ({ ...o, [key]: !(o[key] ?? false) }));
}

/**
 * The target of a patch for a one-line arg: the path of the first `*** Update|Add|Delete File: <path>`
 * header (trimmed), plus ` +N file(s)` when the patch names more than one distinct path. `null` when
 * the text has no such header.
 */
export function patchArg(patch: string): string | null {
  const paths: string[] = [];
  for (const m of patch.matchAll(/^\s*\*\*\* (?:Update|Add|Delete) File:[ \t]*(.+?)[ \t]*$/gm)) {
    if (!paths.includes(m[1])) paths.push(m[1]);
  }
  if (!paths.length) return null;
  return paths.length > 1 ? `${paths[0]} +${paths.length - 1} file${paths.length === 2 ? "" : "s"}` : paths[0];
}
function preview80(line: string): string {
  const chars = [...line];
  return chars.length > 80 ? `${chars.slice(0, 80).join("")}...` : line;
}
/** A row's argument: which field names the call, by tool, as the TUI's `tool_summary`. */
function toolArg(name: string, args: string | undefined): string {
  let o: any;
  try { o = JSON.parse(args ?? "null"); } catch { return ""; }
  if (!o || typeof o !== "object") return "";
  const first = (v: unknown) => typeof v === "string" ? (v.split(/\r?\n/)[0] ?? "") : "";
  const str = (v: unknown) => typeof v === "string" ? v : "";
  switch (name) {
    case "edit": case "write": case "read": return str(o.path);
    case "patch": return typeof o.patch === "string" ? patchArg(o.patch) ?? "" : "";
    case "bash": return preview80(first(o.command));
    case "code": return first(o.code);
    case "ask": return str(o.questions?.[0]?.question);
    default: return str(o.pattern ?? o.path ?? o.query ?? o.url ?? o.name);
  }
}
/** The code from a bash result's `[exit code N]` status line (after an optional cwd line). */
function bashExit(output: string | undefined): number | undefined {
  for (const line of (output ?? "").split("\n").slice(0, 2)) {
    const m = line.match(/^\[exit code (-?\d+)\]/);
    if (m) return Number(m[1]);
  }
  return undefined;
}
const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
function firstLine(s: string): string {
  return (s.split("\n").find((l) => l.trim().length > 0) ?? "").trim();
}
function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 100_000) return `${Math.round(n / 1000)}K`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}
function bareModel(ref: string): string { return ref.split("/").at(-1) ?? ref; }
/** The catalog's name for a model, else its bare id (as the TUI does). */
function modelName(ref: string): string {
  const name = catalogModel(ref, true)?.name;
  return typeof name === "string" && name ? name : bareModel(ref);
}
function effectiveModelRef(): string {
  const s = viewed();
  return s?.model ? String(s.model) : "";
}
function catalogModel(ref: string, matchBareId = false): any | undefined {
  const models = modelCatalog();
  return models.find((entry) => entry.ref === ref)
    ?? (matchBareId ? models.find((entry) => bareModel(String(entry.ref ?? "")) === bareModel(ref)) : undefined);
}
/** Durations: `0.4s`, `4.2s`, `12s`, `1m 05s`, `1h 02m`. Never `<1 s`. */
export function fmtDuration(s: number): string {
  if (!isFinite(s) || s < 0) s = 0;
  if (s < 9.95) return `${s.toFixed(1)}s`;
  const whole = Math.round(s);
  if (s < 59.5) return `${whole}s`;
  if (s < 3599.5) return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
  return `${Math.floor(whole / 3600)}h ${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}m`;
}
function clock(sec: number): string {
  return new Date(sec * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
/** The TUI's delivery tags: the start of a queued turn and a steer inside one, never Core's raw enum. */
function deliveryTags(delivery?: string | null): string[] {
  return delivery === "steer" || delivery === "queued" ? [delivery] : [];
}
/** A child session's model and effort, as its card names them (`model/effort`). */
function childModel(child: any): string {
  const model = typeof child?.model === "string" && child.model && child.model !== "default" ? child.model : "";
  const effort = typeof child?.effort === "string" && child.effort !== "none" && child.effort !== "default" ? child.effort : "";
  return [model, effort].filter(Boolean).join("/");
}
/** Thinking seconds per assistant message: only when one completed request produced that message. */
function text(m: Message): string {
  const parts = m.parts.filter((p, i) => p.type === "text" && !(m.role === "user" && p.collapsed === true) && !(m.role === "user" && /^\[Image [A-Z]\]$/.test(p.text ?? "") && m.parts[i + 1]?.type === "image")).map((p) => p.text ?? "");
  // A user prompt's text parts are the pieces around its pastes and images, not paragraphs.
  return parts.join(m.role === "user" ? "" : "\n");
}
/** The collapsed pastes of a user message, as clickable markers. */
function pastesOf(m: Message): { marker: string; text: string }[] {
  return m.parts.filter((p) => p.type === "text" && p.collapsed === true).map((p) => ({ marker: pasteLabel(p.text ?? ""), text: p.text ?? "" }));
}

/** `kind: message`, or the message alone (first letter capitalized) when it already says the kind. */
export function errorText(kind: unknown, message: unknown): string {
  const k = String(kind ?? "error");
  const msg = String(message ?? "");
  if (!msg) return k;
  if (msg.toLowerCase().includes(k.toLowerCase())) return msg.charAt(0).toUpperCase() + msg.slice(1);
  return `${k}: ${msg}`;
}
const DIAG_TYPES = new Set(["session.error", "retry_scheduled", "retry_started", "retry_exhausted", "turn.recovered", "session.directory_changed", "session.notified", "permission.replied", "question.replied"]);
function diag(e: CoreEvent): Extract<Row, { kind: "diag" }> | null {
  const d = e.data ?? {};
  const k = `ev-${e.id}`;
  const label = roles.diagnostic.label;
  switch (e.type) {
    case "session.error": return { key: k, kind: "diag", label, text: errorText(d.kind, d.message), detail: d.status ? `HTTP ${d.status}` : undefined, tone: d.kind === "interrupted" ? "warning" : "error" };
    case "retry_scheduled": return { key: k, kind: "diag", label, text: `provider retry ${d.attempt ?? ""}/${d.max_attempts ?? ""} in ${d.delay ?? "?"}s`, detail: d.reason, tone: "warning" };
    case "retry_started": return { key: k, kind: "diag", label, text: `provider retry started (attempt ${d.attempt ?? ""})`, tone: "warning" };
    case "retry_exhausted": return { key: k, kind: "diag", label, text: "provider retries exhausted", detail: d.reason, tone: "error" };
    case "turn.recovered": return { key: k, kind: "diag", label, text: `turn ${d.outcome ?? "interrupted"}: ${d.reason ?? "daemon restarted"}`, tone: "warning" };
    case "session.directory_changed": return { key: k, kind: "diag", label, text: `directory changed: ${d.from ?? ""} → ${d.to ?? ""}`, tone: "info" };
    case "session.notified": return { key: k, kind: "diag", label, text: `child finished: ${d.from ?? "child"}`, tone: "info" };
    case "permission.replied": return d.outcome === "cancelled" ? { key: k, kind: "diag", label, text: "approval cancelled: interrupted", tone: "warning" } : null;
    case "question.replied": return d.outcome === "cancelled" ? { key: k, kind: "diag", label, text: "question cancelled: interrupted", tone: "warning" } : null;
    default: return null;
  }
}

/** A closed turn's retry, error and recovery rows, from its summary (never from stored events). */
function summaryDiags(t: TurnSummary): { t: number; row: Extract<Row, { kind: "diag" }> }[] {
  if (t.status === "running") return [];
  const label = roles.diagnostic.label;
  const out: { t: number; row: Extract<Row, { kind: "diag" }> }[] = [];
  for (const r of t.requests ?? []) {
    const attempts = r.attempts ?? [];
    attempts.forEach((a, i) => {
      const next = attempts[i + 1];
      const at = a.finished ?? a.started ?? r.started ?? t.started;
      if (next) {
        const delay = typeof next.delay === "number" ? ` in ${fmtDuration(next.delay)}` : "";
        out.push({ t: at, row: { key: `retry-${r.id}-${a.n}`, kind: "diag", label, text: `provider retry ${next.n}/${attempts.length}${delay}`, detail: a.error ?? undefined, tone: "warning" } });
      } else if (i > 0 && a.status === "failed") {
        out.push({ t: at, row: { key: `retry-${r.id}-end`, kind: "diag", label, text: "provider retries exhausted", detail: a.error ?? undefined, tone: "error" } });
      }
    });
  }
  if (t.error) {
    const e = t.error;
    out.push({ t: t.finished ?? t.started, row: { key: `error-${t.id}`, kind: "diag", label, text: errorText(e.kind, e.message), detail: e.status ? `HTTP ${e.status}` : undefined, tone: e.kind === "interrupted" ? "warning" : "error" } });
  }
  if (t.recovery === "done") {
    out.push({ t: (t.finished ?? t.started) + 0.00005, row: { key: `recovered-${t.id}`, kind: "diag", label, text: `turn ${t.status}: recovered after a daemon restart`, tone: "warning" } });
  }
  return out;
}

type TurnMeta = {
  n: number; start: number; end: number; startTime: number; endTime: number; finishedTime?: number | null;
  input: number; cached: number; output: number; reasoning: number; steps: number;
  /** Finished requests that reported no usage, and requests with usage known. */
  unknown: number; known: number;
  tools: number; models: string[]; stop?: string; error?: { kind: string; message: string };
  /** The turn's summaries: the loop ended it, Core's status, and whether any summary exists. */
  endEvent: boolean; status?: string; summarized: boolean;
  followed: boolean; running: boolean;
};
/**
 * One meta per user message (the turn header): the summaries of Core turns that start in its
 * message range (continuations and notice turns join the user turn they follow), and the live
 * scratch for the newest one.
 */
/** A real user prompt opens a turn; notices, delivered steers and handoffs belong to the turn around them (the TUI's `is_turn_start`). */
function isTurnStart(m: Message): boolean {
  return m.role === "user" && m.delivery !== "steer" && m.delivery !== "handoff" && !NOTICE.test(text(m));
}
function turnMeta(msgs: Message[], turns: TurnSummary[], scratch: CoreEvent[], busy: boolean): TurnMeta[] {
  const starts: number[] = [];
  msgs.forEach((m, i) => { if (m.role === "user" && !NOTICE.test(text(m)) && !(m.delivery === "steer" && starts.length) && m.delivery !== "handoff") starts.push(i); });
  return starts.map((start, n) => {
    const end = n + 1 < starts.length ? starts[n + 1] : msgs.length;
    const window = msgs.slice(start, end);
    const last = n + 1 === starts.length;
    const group = turnsIn(turns, msgs[start].seq, last ? Infinity : msgs[starts[n + 1]].seq);
    const reqs = groupRequests(group, last ? scratch : null);
    const sum = sumUsage(reqs);
    const newest = group.at(-1);
    const meta: TurnMeta = {
      n, start, end, startTime: msgs[start].created, endTime: window.length ? window[window.length - 1].created : msgs[start].created, finishedTime: newest?.finished,
      input: sum.input, cached: sum.cached, output: sum.output, reasoning: sum.reasoning, unknown: sum.unknown, known: sum.known,
      steps: 0, tools: 0, models: [], endEvent: !!newest?.ended, status: newest?.status, summarized: group.length > 0,
      followed: !last, running: busy && last,
    };
    for (const t of group) {
      meta.steps += Math.max(t.steps ?? 0, (t.requests ?? []).length);
      if (t.finished) meta.endTime = Math.max(meta.endTime, t.finished);
    }
    for (const r of reqs) {
      const model = r.model ? modelName(r.model) : null;
      if (model && !meta.models.includes(model)) meta.models.push(model);
      if (!r.running) meta.endTime = Math.max(meta.endTime, r.time);
    }
    if (newest?.ended?.stop) meta.stop = newest.ended.stop;
    if (newest?.error) meta.error = { kind: String(newest.error.kind ?? "error"), message: String(newest.error.message ?? "") };
    if (last) {
      // The running turn's error arrives live, before its summary.
      for (const e of scratch) if (e.type === "session.error") meta.error = { kind: e.data?.kind ?? "error", message: e.data?.message ?? "" };
    }
    const ids = new Set<string>();
    for (const m of window) for (const p of m.parts) if (p.type === "tool_call" && p.id) ids.add(p.id);
    meta.tools = ids.size;
    return meta;
  });
}
function statusOf(t: TurnMeta): TurnStatus {
  if (t.running) return "running";
  if (t.error && t.error.kind !== "interrupted") return "error";
  if (t.stop === "max_steps") return "step_limit";
  // No summary (older than the floor, or not loaded): nothing says it was cut off.
  if (t.stop || t.endEvent || t.followed || t.status === "completed" || !t.summarized) return "done";
  return "interrupted";
}
/** The status mark on a folded user band (no status word anywhere in the footer). */
function turnGlyph(s: TurnStatus): keyof typeof statusGlyphs | undefined {
  return s === "running" ? "running" : s === "done" ? "done" : s === "error" ? "error" : s === "interrupted" ? "interrupted" : undefined;
}

function diffCounts(args: string | undefined): Diff | null {
  if (!args) return null;
  try {
    const o = JSON.parse(args);
    if (typeof o.patch === "string") {
      let add = 0, del = 0;
      for (const l of o.patch.split("\n")) {
        if (l.startsWith("+") && !l.startsWith("+++")) add++;
        else if (l.startsWith("-") && !l.startsWith("---")) del++;
      }
      return { add, del };
    }
    if (typeof o.content === "string") return { add: detailLines(o.content).length, del: 0 };
    const oldS = (o.old_string ?? o.old ?? "") as string;
    const newS = (o.new_string ?? o.new ?? "") as string;
    return { add: detailLines(newS).length, del: detailLines(oldS).length };
  } catch { return null; }
}
function detailLines(value: string): string[] {
  if (!value) return [];
  const lines = value.split(/\r\n|\n/);
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}
/** A command's first rows, ahead of its output: `$ cmd`, then a continuation. */
function commandRows(name: string, args: string | undefined): ToolDetailLine[] {
  if (name !== "bash" && name !== "code") return [];
  let command: unknown;
  try { const o = JSON.parse(args ?? "{}"); command = name === "bash" ? o.command : o.code; } catch { return []; }
  if (typeof command !== "string") return [];
  const lines = detailLines(command);
  return lines.slice(0, COMMAND_ROWS).map((line, i) => ({
    text: `${i === 0 && name === "bash" ? "$ " : "  "}${line}${i + 1 === COMMAND_ROWS && lines.length > COMMAND_ROWS ? " …" : ""}`,
    tone: "command" as const,
  }));
}
export function detailOf(name: string, args: string | undefined, output: string | undefined, raw: boolean, failed = false, error?: string, finished = output !== undefined): ToolDetailLine[] {
  const outputTone = "plain" as const;
  if (raw) {
    const lines = detailLines(args ?? "").map((l) => ({ text: l, tone: "plain" as const }));
    return [...lines, ...detailLines(output ?? "").map((l) => ({ text: l, tone: outputTone }))];
  }
  if (name === "edit" || name === "patch") {
    try {
      const o = JSON.parse(args ?? "{}");
      if (typeof o.patch === "string") {
        return detailLines(o.patch).map((l: string) => ({
          text: l,
          tone: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "info" : "plain",
        }));
      }
      const out: { text: string; tone: "add" | "del" | "info" }[] = [{ text: `@@ ${o.path ?? ""}`, tone: "info" }];
      for (const l of detailLines(String(o.old_string ?? o.old ?? ""))) out.push({ text: `-${l}`, tone: "del" });
      for (const l of detailLines(String(o.new_string ?? o.new ?? ""))) out.push({ text: `+${l}`, tone: "add" });
      return out;
    } catch { /* fall through to the output */ }
  }
  if (name === "write") {
    try {
      const o = JSON.parse(args ?? "{}");
      const out: { text: string; tone: "add" | "info" }[] = [{ text: `+ ${o.path ?? ""}`, tone: "info" }];
      for (const l of detailLines(String(o.content ?? ""))) out.push({ text: `+${l}`, tone: "add" });
      return out;
    } catch { /* fall through */ }
  }
  const head = commandRows(name, args);
  if (finished) return [...head, ...detailLines(output ?? "").map((l) => ({ text: l, tone: outputTone }))];
  return [...head, error ? { text: error, tone: "plain" as const } : { text: "no output", tone: "muted" as const }];
}

function buildStableRows(): Row[] {
  m();
  const sel = model.selected;
  if (!sel) return [];
  const results = new Map<string, Part>();
  for (const msg of sel.messages) for (const p of msg.parts) if (p.type === "tool_result" && p.call_id) results.set(p.call_id, p);
  const traces = toolTimes(sel.turns, sel.scratch);
  const waiting = new Set([...model.approvals.values(), ...model.questions.values()].map((a: any) => a.call_id));
  // Approval replies name approvals, not calls, so join the live records; a stored denial is the result the model received.
  const approvalCalls = new Map<string, string>();
  const denied = new Set<string>();
  for (const e of sel.scratch) {
    const d: any = e.data;
    if (e.type === "permission.asked" && d?.id && d?.call_id) approvalCalls.set(d.id, d.call_id);
    if (e.type === "permission.replied" && d?.outcome === "denied" && approvalCalls.has(d.id)) denied.add(approvalCalls.get(d.id)!);
  }
  for (const [id, res] of results) if (res.is_error && res.output === DECLINED) denied.add(id);
  const busy = viewed()?.status === "busy";
  const metas = turnMeta(sel.messages, sel.turns, sel.scratch, busy);
  const sid = sel.sessionId;
  const rows: { t: number; r: Row[] }[] = [];

  // Starting context, before the first turn.
  if (sel.starting) {
    const s = sel.starting;
    const session = viewed();
    const { sections, total } = startingSections(s, disabledOf(session), { skills: session?.skills, mcp: session?.mcp });
    const ref = effectiveModelRef();
    const rawWindow = Number(catalogModel(ref)?.context);
    const window = Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : undefined;
    rows.push({
      t: -1,
      r: [{
        key: `context-${sid}`, kind: "context",
        reconstructed: s.origin === "reconstructed",
        sections, total, window,
      }],
    });
  }

  let turnAt = 0;
  let foldedTurn = false;
  for (const msg of sel.messages) {
    const out: Row[] = [];
    if (msg.role === "user") {
      const body = text(msg);
      if (msg.delivery === "handoff") {
        const { from, summary } = handoffParts(body);
        out.push({ key: msg.id, kind: "handoff", from, summary, time: clock(msg.created) });
      } else if (NOTICE.test(body)) {
        out.push({
          key: msg.id, kind: "notice", text: body,
          label: body.startsWith("[Nexus recovery") ? roles.diagnostic.label : roles.context_notice.label,
          tone: body.startsWith("[Nexus recovery") ? "warning" : "info",
        });
      } else {
        // A delivered steer is a message inside the running turn, not a new turn.
        const steer = msg.delivery === "steer" && turnAt > 0;
        const n = steer ? turnAt - 1 : turnAt;
        const meta = metas[n] ?? metas[metas.length - 1];
        if (!steer) turnAt += 1;
        const key = `turn:${sid}:${n}`;
        const folded = closedTurns()[key] ?? false;
        foldedTurn = folded;
        out.push({
          key: msg.id, kind: "user", delivery: msg.delivery, images: msg.parts.filter((p) => p.type === "image") as any, pastes: pastesOf(msg), n: n + 1, time: clock(msg.created), text: body, turnKey: key, folded,
          tools: meta?.tools ?? 0, duration: meta ? fmtDuration(meta.endTime - meta.startTime) : "",
          status: meta ? statusOf(meta) : "running",
        });
        if (folded) { rows.push({ t: msg.created, r: out }); continue; }
      }
    } else if (msg.role === "assistant") {
      if (foldedTurn) { rows.push({ t: msg.created, r: out }); continue; }
      msg.parts.forEach((p, i) => {
        const key = `${msg.id}-${i}`;
        if (p.type === "text" && (p.text ?? "").trim()) out.push({ key, kind: "assistant", text: p.text ?? "" });
        else if (p.type === "reasoning") {
          const summary = p.summary ?? "";
          out.push({ key, kind: "reasoning", text: summary, encrypted: !summary.trim(), open: !!summary.trim() && open(`reason:${key}`, false) });
        } else if (p.type === "tool_call") {
          const id = p.id ?? key;
          if (p.name === "session" || p.name === "message_session") {
            let a: any = {};
            try { a = JSON.parse(p.arguments ?? "{}"); } catch {}
            const follow = p.name === "message_session";
            const child = follow ? model.sessions.get(a.session) : [...model.sessions.values()].find((c) => c.parent_id === sid && c.parent_call_id === id);
            const res = results.get(id);
            const waits = child ? [...model.approvals.values(), ...model.questions.values()].some((w) => w.session_id === child.id) : false;
            const running = child ? model.running.has(child.id) || child.status === "busy" : false;
            const background = follow || a.wait === false;
            // A background start returns at once with a start notice, not the outcome: the row follows the child until it stops.
            const done = res && !(background && running && !res.is_error) ? res : undefined;
            const failure = done?.is_error ? (firstLine(done.output ?? "").split("failed:").at(-1) ?? "").trim() : "";
            const end = running ? Date.now() / 1000 : child?.updated ?? 0;
            const meta = child ? [childModel(child), child.created > 0 ? fmtDuration(end - child.created) : ""].filter(Boolean).join(" · ") : "";
            const key = `child:${id}`;
            const title = (follow ? "" : a.title || firstLine(a.prompt ?? "")) || child?.title || "";
            out.push({
              key, kind: "child", childId: child?.id, agent: follow ? child?.agent ?? a.agent ?? "child" : a.agent ?? "child",
              title: title.length > 48 ? `${title.slice(0, 47)}…` : title,
              state: running && !waits && !done ? "running" : done ? "done" : "idle", background: background && !follow, meta,
              outcome: done?.is_error ? (failure ? `failed ${failure.length > 60 ? `${failure.slice(0, 59)}…` : failure}` : "failed") : waits && !done ? "waiting for input" : "",
            });
          } else {
            const res = results.get(id);
            const tr = traces.get(id);
            const isDenied = denied.has(id);
            const state: "running" | "done" | "error" | "waiting" | "denied" = waiting.has(id) ? "waiting" : isDenied ? "denied"
              : res?.is_error || tr?.isError ? "error" : res || tr?.end || !busy ? "done" : "running";
            const name = p.name ?? "tool";
            const isEdit = name === "edit" || name === "patch" || name === "write";
            const counts = isEdit && state !== "error" && state !== "denied" ? diffCounts(p.arguments) : null;
            let summary = "";
            let args: any = {};
            try { args = JSON.parse(p.arguments ?? "{}") ?? {}; } catch {}
            if (state === "waiting" || state === "denied" || state === "error" || name === "bash") summary = "";
            else if (name === "edit" || name === "patch") summary = counts ? `+${counts.add} −${counts.del}` : "";
            else if (name === "write") summary = typeof args.content === "string" ? `+${detailLines(args.content).length}` : "";
            else if (name === "read") { if (res) { const n = detailLines(res.output ?? "").length; summary = `${n} line${n === 1 ? "" : "s"}`; } }
            else if (name === "code") summary = "cell";
            else if (name === "ask" && Array.isArray(args.questions) && args.questions.length) summary = "question";
            const media = ((res as any)?.media ?? []).filter((x: any) => /^image\//.test(x?.mime ?? ""));
            if (media.length && !waiting.has(id)) summary = [summary, `${media.length} image${media.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
            const seconds = tr?.start !== undefined && tr?.end !== undefined ? tr.end - tr.start : undefined;
            const key = `tool:${id}`;
            const rawOpen = open(`${key}:raw`, false);
            const showAll = toolOutputAll()[key] ?? false;
            const detailLevel = timelineDetail();
            const head = detailLevel === "high" ? HIGH_HEAD : DETAIL_HEAD;
            const tail = detailLevel === "high" ? HIGH_TAIL : DETAIL_TAIL;
            const failed = state === "error";
            const allDetail = detailOf(name, p.arguments, res?.output, rawOpen, failed, tr?.error, !!res);
            // Output keeps its command rows and last rows; a diff or the raw view keeps a head and a tail.
            const tailCap = !rawOpen && !isEdit;
            const commandHead = tailCap ? commandRows(name, p.arguments).length : 0;
            const clipped = tailCap ? allDetail.length - commandHead > OUTPUT_TAIL + 2 : allDetail.length > head + tail + 20;
            const capped = tailCap ? capToolOutput(allDetail, showAll, commandHead) : capToolDetail(allDetail, showAll, head, tail);
            const detail: ToolDetailLine[] = !tailCap && clipped && showAll
              ? [{ text: `↑ showing all ${allDetail.length} lines · o show less`, tone: "show-less" }, ...capped]
              : capped;
            const exitCode = name === "bash" ? bashExit(res?.output) : undefined;
            out.push({
              key, kind: "tool", id, name, arg: toolArg(name, p.arguments), summary,
              editFile: isEdit ? (typeof args.path === "string" ? args.path : id) : undefined,
              exit: exitCode !== undefined && exitCode !== 0 ? exitCode : undefined,
              errorLine: failed ? preview80(firstLine(res?.output ?? "")) : undefined,
              tone: counts ? (counts.add >= 0 ? "accent" : "info") : "info",
              state, duration: name === "bash" && seconds !== undefined && seconds >= 1 ? fmtDuration(seconds) : undefined, seconds,
              diff: counts ?? undefined, output: res?.output,
              detail,
              raw: rawOpen ? JSON.stringify({ arguments: p.arguments ?? "", output: res?.output ?? "" }, null, 2) : "",
              open: open(key, detailLevel === "high"), clipped, showAll, media,
            });
          }
        }
      });
    } // tool-role messages are shown through their call's row
    rows.push({ t: msg.created, r: out });
    // A turn's footer belongs after its last message.
    if (turnAt > 0 && (msg === sel.messages[sel.messages.length - 1] || (() => { const next = sel.messages[sel.messages.indexOf(msg) + 1]; return !!next && isTurnStart(next); })())) {
      const meta = metas[turnAt - 1];
      // A running turn has no footer: it appears only once the turn is done, as in the TUI.
      if (meta && !foldedTurn && !meta.running) {
        const v = viewed();
        const segments: { text: string }[] = [];
        if (meta.models.length) segments.push({ text: meta.models.join(" → ") });
        else if (v?.model && String(v.model) !== "default") segments.push({ text: modelName(String(v.model)) });
        segments.push({ text: `${meta.tools} tool${meta.tools === 1 ? "" : "s"}` });
        if (meta.known > 0) {
          // Unknown usage is never counted as zero: the total says it is a floor.
          segments.push({ text: `${fmtCount(meta.input)}i` });
          segments.push({ text: `${fmtCount(meta.output)}o` });
          segments.push({ text: `${fmtCount(meta.reasoning)}r` });
          if (meta.input > 0) segments.push({ text: `${Math.round(100 * meta.cached / meta.input)}%c` });
        } else if (meta.unknown) {
          segments.push({ text: "usage unknown" });
        }
        segments.push({ text: fmtDuration(meta.endTime - meta.startTime) });
        if (!meta.running && meta.finishedTime != null) segments.push({ text: clock(meta.finishedTime) });
        const status = statusOf(meta);
        const error = status === "error" ? (firstLine(meta.error?.message ?? "") || meta.error?.kind) : undefined;
        rows.push({
          t: msg.created + 0.0001,
          r: [{ key: `footer:${sid}:${meta.n}`, kind: "footer", segments, error, interrupted: status === "interrupted" }],
        });
      }
    }
  }
  // Closed turns: retries and errors from their summaries. The running turn: live diagnostics
  // (its scratch), replaced by its summary when it closes.
  for (const t of sel.turns) for (const r of summaryDiags(t)) rows.push({ t: r.t, r: [r.row] });
  for (const e of sel.scratch) {
    if (!DIAG_TYPES.has(e.type)) continue;
    const r = diag(e);
    if (r) rows.push({ t: e.time, r: [r] });
  }
  const floorAt = floorIndex(sel.messages, sel.floor);
  if (floorAt !== null) {
    const next = sel.messages[floorAt];
    const prev = sel.messages[floorAt - 1];
    rows.push({
      t: next ? next.created - 0.0002 : (prev?.created ?? 0) + 0.0002,
      r: [{ key: `floor-${sid}`, kind: "older", text: next
        ? "Turns above have no details (tokens, models, timings): their records were pruned or predate turn summaries."
        : "These turns have no details (tokens, models, timings): their records were pruned or predate turn summaries." }],
    });
  }
  rows.sort((a, b) => a.t - b.t);
  const flat = rows.flatMap((x) => x.r);
  if (sel.starting) {
    // Context is always first, even when the first turn starts immediately.
    const i = flat.findIndex((r) => r.kind === "context");
    if (i > 0) flat.unshift(...flat.splice(i, 1));
  }
  if (sel.messagesMore) flat.unshift({ key: "older-messages", kind: "older", text: "Older messages are not loaded", action: "load-older" });
  if (sel.turnsGap) flat.unshift({ key: "turns-gap", kind: "older", text: `${sel.turnsGap[0].toUpperCase()}${sel.turnsGap.slice(1)}.` });
  return mergeDiagnostics(footersLast(flat));
}

/** A turn's diagnostics (its interrupt, error, retries) come before its footer, as in the TUI: the footer closes the turn. */
function footersLast(flat: Row[]): Row[] {
  const out = [...flat];
  for (let i = 0; i < out.length; i++) {
    if (out[i].kind !== "footer") continue;
    let j = i + 1;
    while (j < out.length && out[j].kind === "diag") j++;
    if (j > i + 1) out.splice(i, j - i, ...out.slice(i + 1, j), out[i]);
  }
  return out;
}

/** Consecutive identical diagnostics are one fact: `×2` on the first card. */
function mergeDiagnostics(flat: Row[]): Row[] {
  const out: Row[] = [];
  for (const row of flat) {
    const prev = out.at(-1);
    if (row.kind === "diag" && prev?.kind === "diag" && prev.text === row.text && prev.tone === row.tone) {
      prev.count = (prev.count ?? 1) + 1;
      if (row.detail && !prev.detail) prev.detail = row.detail;
    } else out.push(row);
  }
  return out;
}

/** Append the live streaming rows without rebuilding the settled history. */
function withStreaming(flat: Row[], st?: { text: string; reasoning: string }): Row[] {
  if (!st || (!st.reasoning && !st.text)) return flat;
  const out = [...flat];
  if (st.reasoning) out.push({ key: "stream-r", kind: "reasoning", text: st.reasoning, open: open("reason:streaming", true), running: true });
  if (st.text) out.push({ key: "stream-t", kind: "assistant", text: st.text, streaming: true });
  return out;
}

/** The complete timeline, including the live stream (tests and non-memoized callers). */
export function buildRows(): Row[] {
  m();
  const sel = model.selected;
  if (!sel) return [];
  return groupActivities(withStreaming(buildStableRows(), model.streaming.get(sel.sessionId)), model.running.has(sel.sessionId));
}

type ActivityEntry = Extract<Row, { kind: "tool" }> | Extract<Row, { kind: "reasoning" }>;
function activitySummary(items: ActivityEntry[]): string {
  // Nouns in first-seen order. Edits count the files they touch, not the calls.
  const counts = new Map<string, number>();
  const editFiles = new Set<string>();
  for (const item of items) {
    let noun: string;
    if (item.kind === "reasoning") noun = "thought";
    else {
      if (item.editFile !== undefined) editFiles.add(item.editFile);
      noun = ({
        bash: "command", read: "read", edit: "edit", patch: "edit", write: "edit",
        code: "cell", ask: "question", web_search: "search", web_fetch: "page", session: "session",
      } as Record<string, string>)[item.name] ?? "tool";
    }
    counts.set(noun, (counts.get(noun) ?? 0) + 1);
  }
  if (counts.has("edit")) counts.set("edit", Math.max(editFiles.size, 1));
  // `3 thoughts, 50 commands, 8 tools, 9 reads`; a `search` takes `es`.
  return [...counts.entries()].map(([noun, count]) => `${count} ${noun}${count === 1 ? "" : noun === "search" ? "es" : "s"}`).join(", ");
}

function groupActivities(rows: Row[], busy: boolean): Row[] {
  const result: Row[] = [];
  let items: ActivityEntry[] = [];
  const flush = (trailing = false) => {
    if (!items.length) return;
    // A run of only thoughts is no activity group: each is its own `▸ Thought · preview` row (D07).
    if (items.every((item) => item.kind === "reasoning")) { result.push(...items); items = []; return; }
    const key = `group:${items[0].key}`;
    const waiting = items.some((item) => item.kind === "tool" && item.state === "waiting");
    const running = (busy && trailing) || items.some((item) => item.kind === "reasoning" ? item.running : item.state === "waiting" || (busy && item.state === "running"));
    const tools = items.filter((item): item is Extract<Row, { kind: "tool" }> => item.kind === "tool");
    const detail = timelineDetail();
    const defaultOpen = waiting || detail === "high" || (running && detail !== "low");
    const expanded = waiting || open(key, defaultOpen);
    const explicit = timelineOpen()[key] === true;
    const hidden = running && !waiting && detail !== "high" && !explicit ? Math.max(0, items.length - 5) : 0;
    const diffs = tools.filter((tool) => tool.diff !== undefined);
    result.push({
      key, kind: "activity", summary: running ? "Working" : activitySummary(items), items: items.slice(hidden),
      open: expanded, defaultOpen, running, hidden,
      denied: tools.filter((tool) => tool.state === "denied").length,
      diff: diffs.length ? diffs.reduce<Diff>((sum, tool) => ({ add: sum.add + (tool.diff?.add ?? 0), del: sum.del + (tool.diff?.del ?? 0) }), { add: 0, del: 0 }) : undefined,
    });
    items = [];
  };
  for (const row of rows) {
    if (row.kind === "tool" || row.kind === "reasoning") items.push(row);
    else { flush(); result.push(row); }
  }
  flush(true);
  return result;
}

const RAIL: Record<string, string> = { info: "bg-info", accent: "bg-primary", warning: "bg-warning" };

/** Row heights for the sticky bar: an item's `onLayout` frame is relative to its own container, so content offsets are summed from the heights. */
function reportHeight(row: Row, h: number) {
  const cur = rowHeights();
  if (cur[row.key] === h) return;
  rowHeights.set({ ...cur, [row.key]: h });
}

function RowView(p: { r: () => Row }) {
  const r = p.r;
  return (
    <TimelineColumn class="items-center" testID="timeline-row" onLayout={(b: any) => reportHeight(r(), b.bounds?.size?.height ?? b.size?.height ?? 72)}>
      <View class="w-full">
        <If when={r().kind === "context"}>
          {() => {
            const c = r() as Extract<Row, { kind: "context" }>;
            return <StartingContext sections={c.sections} total={c.total} estimated window={c.window} reconstructed={!!c.reconstructed} onInventoryToggle={(kind, name, locked) => { void toggleSessionInventory(kind, name, locked); }} />;
          }}
        </If>
        <If when={r().kind === "user"}>
          <View class="pt-4 pb-2"><UserBand r={r as () => Extract<Row, { kind: "user" }>} /></View>
        </If>
        <If when={r().kind === "footer"}>
          {() => {
            const f = r() as Extract<Row, { kind: "footer" }>;
            return (
              <View class={`w-full items-end pt-1 pr-4 ${TEXT_INDENT}`} testID="turn-footer">
                <View class="flex-row flex-wrap justify-end items-center">
                  <Each of={f.segments} key={(s, i) => `${i}`}>
                    {(s) => (
                      <Text class={META}>
                        {`${s() === f.segments[0] ? "" : " · "}${s().text}`}
                      </Text>
                    )}
                  </Each>
                  <If when={f.interrupted}><Text class="text-xs leading-4 text-warning">{" · /continue"}</Text></If>
                </View>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "handoff"}>
          <View class="py-2"><HandoffCard r={r as any} /></View>
        </If>
        <If when={r().kind === "notice"}>
          <NoticeRow text={() => (r() as any).text} />
        </If>
        <If when={r().kind === "assistant"}>
          <View class={`py-2 pr-4 ${TEXT_INDENT}`} testID="msg-assistant">
            <TextView
              selectable links="open" markdown={(r() as any).text} options={{ streaming: !!(r() as any).streaming }}
              renderCodeBlock={(block) => <CodeFence block={block} />}
              classes={PROSE}
            />
          </View>
        </If>
        <If when={r().kind === "reasoning"}>
          <ReasoningRow x={r as () => Extract<Row, { kind: "reasoning" }>} />
        </If>
        <If when={r().kind === "activity"}>
          <ActivityRow r={r as () => Extract<Row, { kind: "activity" }>} />
        </If>
        <If when={r().kind === "tool"}>
          <ToolRow r={r as () => Extract<Row, { kind: "tool" }>} />
        </If>
        <If when={r().kind === "child"}>
          {() => {
            const c = r() as Extract<Row, { kind: "child" }>;
            return (
              <Button onPress={() => c.childId && void selectSession(c.childId)} disabled={!c.childId} testID="child-card" accessibilityLabel={`${c.agent} ${c.title}`}
                class={`items-start rounded-(--radius) -ml-1 pl-1 pr-4 py-0.5 ml-3 hover:bg-accent ${FOCUS}`}>
                <View class="flex-row items-center gap-2 max-w-full">
                  <View class="w-4 shrink-0 items-center">
                    <If when={c.state === "running"}><Status kind="running" /></If>
                    <If when={c.state === "done"}><Text class={`text-[13px] leading-5 ${agentColor(c.agent)}`}>⣿</Text></If>
                  </View>
                  <Text class={`shrink-0 text-[13px] leading-5 font-medium ${agentColor(c.agent)}`}>{capitalize(c.agent)}</Text>
                  <Text style={{ numberOfLines: 1 }} class="shrink min-w-0 text-[13px] leading-5 text-foreground-secondary">{c.title}</Text>
                  <If when={c.background}><Text class="shrink-0 rounded-(--radius) bg-sidebar px-1.5 text-xs leading-4 text-foreground-secondary">Background</Text></If>
                </View>
                <If when={!!c.meta}><Text testID="child-meta" class={`${META} pl-6`}>{c.meta}</Text></If>
                <If when={!!c.outcome}><Text testID="child-outcome" class={`${META} pl-6`}>{c.outcome}</Text></If>
              </Button>
            );
          }}
        </If>
        <If when={r().kind === "diag"}>
          {() => {
            const d = r() as Extract<Row, { kind: "diag" }>;
            return (
              <NoteLine tone={d.tone} label={d.label} text={d.text} detail={d.detail} count={d.count} testID="msg-diag" />
            );
          }}
        </If>
        <If when={r().kind === "older"}>
          <If when={(r() as any).action === "load-older"} else={<Text class={`${META} py-2 ${TEXT_INDENT}`} testID="timeline-older">{(r() as any).text}</Text>}>
            <View class={`flex-row items-center py-2 ${TEXT_INDENT}`} testID="timeline-older">
              <Button onPress={() => run("turn.load_older")} testID="load-older" accessibilityLabel="Load older messages" class={`rounded-(--radius) px-2 -ml-2 ${hit("min-h-6")} justify-center hover:bg-accent ${FOCUS}`}>
                <Text class="text-[13px] leading-5 text-foreground-secondary">Load older messages</Text>
              </Button>
            </View>
          </If>
        </If>
      </View>
    </TimelineColumn>
  );
}

/** One-line diagnostics and notices, as the TUI draws them: a plain row at the text edge, a coloured glyph, muted text, the detail after a dot. */
function NoteLine(p: { tone: Tone; label: string; text: string; detail?: string; count?: number; testID: string }) {
  const glyph = p.tone === "error" ? statusGlyphs.error : p.tone === "warning" ? statusGlyphs.interrupted : null;
  const color = p.tone === "error" ? "text-error" : p.tone === "warning" ? "text-warning" : "text-info";
  return (
    <View class={`flex-row items-start gap-2 py-1 pr-4 ${TEXT_INDENT}`} testID={p.testID} accessible accessibilityLabel={p.label}>
      <Text class={`w-3.5 text-center ${T.sec} ${color}`}>{glyph?.unicode ?? "i"}</Text>
      <Text style={{ userSelect: "text" }} class={`flex-1 min-w-0 ${T.sec} ${p.tone === "error" ? "text-error" : ""}`}>
        {p.text}
        <If when={(p.count ?? 1) > 1}><Text class={META}>{` ×${p.count}`}</Text></If>
        <If when={!!p.detail && !p.text.includes(p.detail)}><Text class={META}>{` · ${p.detail}`}</Text></If>
      </Text>
    </View>
  );
}

/** `▸ context · Notification: sub agent finished · show details`: one muted row whose full text opens in a dialog. */
function NoticeRow(p: { text: () => string }) {
  const shell = () => p.text().startsWith("→ Background Shell Done:");
  const head = () => firstLine(p.text()).replace(/^\[/, "").replace(/\]$/, "");
  const label = () => shell() ? `${firstLine(p.text())} · show full output` : `context · ${head()} · show details`;
  return (
    <View class={`py-0.5 ${TEXT_INDENT}`}>
      <Button onPress={() => contextDetail.set({ title: shell() ? "Background shell" : "Context", detail: p.text() })} testID={shell() ? "background-shell-done" : "msg-notice"} accessibilityLabel={label()}
        class={`self-stretch flex-row items-center rounded-(--radius) -ml-1 pl-1 pr-4 ${hit("min-h-6")} hover:bg-accent ${FOCUS}`}>
        <View class={ACTIVITY_CHEVRON}><Icon name="chevron-right" size={14} class="tint-muted-foreground" /></View>
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-muted-foreground">{label()}</Text>
      </Button>
    </View>
  );
}

/** A fenced code block (DESIGN_PLAN D08): a panel card with the language and a Copy button over the code. */
function CodeFence(p: { block: TextViewCodeBlock }) {
  const code = () => p.block.code;
  return (
    <View class="w-full my-1 bg-card overflow-hidden" testID="code-fence">
      <View class="flex-row items-center h-8 pl-4 pr-2 gap-2">
        <Text class={`flex-1 min-w-0 ${META} font-(family-name:--font-code)`}>{p.block.language ?? ""}</Text>
        <Button onPress={() => copyText(code(), "Copied code")} accessibilityLabel="Copy code" testID="code-copy"
          class={`flex-row items-center gap-1.5 rounded-(--radius) px-2 ${hit("min-h-6")} hover:bg-accent ${FOCUS}`}>
          <Icon name="copy" size={12} class="tint-muted-foreground" />
          <Text class={META}>Copy</Text>
        </Button>
      </View>
      <Scroll horizontal>
        <View class="gap-0 px-4 pb-4">
          {code().split("\n").map((line) => <Text style={{ userSelect: "text" }} class="font-(family-name:--font-code) text-[12.5px] leading-5 text-foreground">{line || " "}</Text>)}
        </View>
      </Scroll>
    </View>
  );
}

/** `[Handoff from session ses_x]` and the summary after it. */
export function handoffParts(body: string): { from: string; summary: string } {
  const m = /^\[Handoff from session ([^\]]*)\]\s*/.exec(body);
  return m ? { from: m[1].slice(0, 12), summary: body.slice(m[0].length) } : { from: "an earlier session", summary: body };
}

/** The summary a continued session starts from: a user-message-like card of three lines; a click shows all of it. */
function HandoffCard(p: { r: () => Extract<Row, { kind: "handoff" }> }) {
  const r = p.r;
  const lines = () => r().summary.split("\n").map((l) => l.replace(/^#+\s*/, "").trim()).filter(Boolean);
  const origin = () => { m(); const id = viewed()?.continued_from; return id ? model.sessions.get(id) ?? null : null; };
  const name = () => origin()?.title ? `“${origin()!.title}”` : r().from;
  const open = () => contextDetail.set({ title: `Handoff from ${name()}`, detail: r().summary });
  return (
    <View class={`w-full rounded-(--radius-card) bg-raised py-3 gap-1 ${CARD_INSET}`}>
      <Button onPress={open} accessibilityLabel="Show the whole handoff summary" testID="msg-handoff" class={`w-full flex-col items-stretch gap-1 ${FOCUS}`}>
        <View class="flex-row items-center gap-3">
          <Text class="flex-1 min-w-0 text-[14px] leading-[22px] font-semibold text-foreground">{`Continued from ${name()}`}</Text>
          <Text class={META}>{`handoff · ${r().time}`}</Text>
        </View>
        <Text style={{ numberOfLines: 3 }} testID="handoff-preview" class="text-[14px] leading-[22px] text-foreground">{lines().join("\n")}</Text>
      </Button>
      <View class="flex-row gap-3">
        <Button onPress={open} class={`${FOCUS}`}><Text class={META}>Show all</Text></Button>
        <If when={!!origin()}>{() => <Button onPress={() => openLinked("continued_from")} testID="handoff-open-original" class={`${FOCUS}`}><Text class={`${META} underline`}>Open the original session</Text></Button>}</If>
      </View>
    </View>
  );
}

/** A user message: the reading column's raised card (folded: one line). Its header is the turn's header. */
function UserBand(p: { r: () => Extract<Row, { kind: "user" }> }) {
  const r = p.r;
  const fold = () => toggleFold(r().turnKey);
  const copy = () => copyText(r().text, "Copied message");
  const glyph = () => turnGlyph(r().status);
  const meta = () => [...deliveryTags(r().delivery), `#${r().n}`, r().time].join(" · ");
  const big = () => r().text.length > 4096 || r().text.split("\n").length > 40;
  const full = () => open(`user:${r().key}`, false);
  const body = () => big() && !full() ? r().text.split("\n").slice(0, 6).join("\n") : r().text;
  return (
    <If when={r().folded} else={
      <View>
      <View class={`group/band w-full bg-band py-3 relative ${CARD_INSET}`} testID="msg-user"
        onFocus={() => focusedBand.set(r().key)} onBlur={() => { if (focusedBand() === r().key) focusedBand.set(null); }}>
        {/* The TUI's band rail stays visible; focus only changes its colour. */}
        <View class={`absolute left-0 top-0 bottom-0 w-0.5 ${focusedBand() === r().key ? "bg-primary" : "bg-border-strong"}`} />
        <If when={!!r().images?.length}>
          <View class="flex-row flex-wrap gap-2 pb-2" testID="user-images">
            <Each of={r().images ?? []} key={(i) => i.data_url}>
              {(i, n) => <View class="gap-1 w-24 shrink-0">
                <Text style={{ numberOfLines: 1 }} class={`${T.meta} px-0.5`}>{i().name}</Text>
                <Button onPress={() => imagePreview.set({ name: i().name, src: i().data_url })}
                  accessibilityLabel={`Open ${i().name}`} testID={`user-image-${r().n}-${n()}`}
                  class={`w-24 h-24 items-center justify-center border border-border-strong overflow-hidden ${FOCUS}`}>
                  <Image class="w-24 h-24" src={i().data_url} style={{ width: 96, height: 96, objectFit: "cover" }} accessibilityLabel={i().name} />
                </Button>
              </View>}
            </Each>
          </View>
        </If>
        <If when={!!r().pastes?.length}>
          <View class="flex-row flex-wrap gap-1.5 pb-2" testID="user-pastes">
            <Each of={r().pastes ?? []} key={(p) => p.marker}>
              {(p, n) => <Button onPress={() => contextDetail.set({ title: "Pasted text", detail: p().text })}
                accessibilityLabel={`Open pasted text, ${p().marker}`} testID={`user-paste-${r().n}-${n()}`}
                class={`rounded-(--radius) bg-raised px-2 min-h-7 hover:bg-accent ${FOCUS}`}>
                <Text class="font-mono text-[12px] leading-5 text-foreground">{p().marker}</Text>
              </Button>}
            </Each>
          </View>
        </If>
        <View class="flex-row items-start gap-3">
          <View class="flex-1 min-w-0">
            <Text style={big() && !full() ? { userSelect: "text", numberOfLines: 6 } : { userSelect: "text" }} class="text-[14px] leading-[22px] text-foreground">{body()}</Text>
            {/* A phone or tablet has no hover, so the icons and the stamp move below the text. */}
            <If when={touch}>
              <View class="flex-row items-center justify-end gap-1 pt-1">
                <IconButton icon="copy" label="Copy message" onPress={copy} testID={`copy-user-${r().n}`} />
                <IconButton icon="chevron-down" label="Fold message" onPress={fold} testID={`fold-user-${r().n}`} />
                <Text class={`${META} pt-[3px]`}>{meta()}</Text>
              </View>
            </If>
          </View>
          <If when={!touch}>
            {/* Hover icons stay out of the flow: over a small backing, hiding the stamp while they show. */}
            <Text class={`${META} pt-[3px] transition-opacity ${focusedBand() === r().key ? "opacity-0" : "group-hover/band:opacity-0"}`}>{meta()}</Text>
            <View class={`absolute right-2 top-[7px] flex-row items-center gap-1 rounded-(--radius) bg-raised transition-opacity ${focusedBand() === r().key ? "opacity-100" : "opacity-0 group-hover/band:opacity-100"}`}>
              <IconButton icon="copy" label="Copy message" onPress={copy} testID={`copy-user-${r().n}`} />
              <IconButton icon="chevron-down" label="Fold message" onPress={fold} testID={`fold-user-${r().n}`} />
            </View>
          </If>
        </View>
        <If when={big()}>
          <Button onPress={() => toggle(`user:${r().key}`, false)} testID={`user-full-${r().n}`} class={`items-start pt-2 ${FOCUS}`}>
            <Text class={META}>{full() ? "▾ collapse message" : "▸ show full message (sent whole)"}</Text>
          </Button>
        </If>
      </View>
      </View>
    }>
      <Button onPress={fold} accessibilityLabel={`${meta()} folded, show`} testID={`msg-user-folded-${r().n}`}
        class={`w-full flex-row items-center gap-2 rounded-(--radius) bg-band min-h-9 hover:bg-accent ${CARD_INSET} ${FOCUS}`}>
        <Icon name="chevron-right" size={14} class="tint-muted-foreground" />
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[14px] leading-[22px] text-foreground">{r().text || (r().pastes ?? []).map((p) => p.marker).join(" ")}</Text>
        <If when={r().tools > 0 || !!r().duration}>
          {() => <Text class={META}>{[r().tools ? `${r().tools} tool${r().tools === 1 ? "" : "s"}` : "", r().duration].filter(Boolean).join(" · ")}</Text>}
        </If>
        <If when={!!glyph()}>{() => <Status kind={glyph()!} />}</If>
        <Text class={META}>{meta()}</Text>
      </Button>
    </If>
  );
}

/** The sticky copy of the current turn's user band, under the header; the next band pushes it up. */
function StickyBar(p: { rows: () => Row[]; scrollTo: (offset: number) => void }) {
  // One band scan per row/layout change, shared by the visibility check and the render.
  const list = computed((): Band[] => {
    const h = rowHeights();
    const rows = p.rows();
    const lastUser = rows.reduce((last, r, i) => r.kind === "user" ? i : last, -1);
    let top = LIST_PAD;
    const out: Band[] = [];
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      // The newest user band waits for its measured height: it is on an estimate for a
      // frame, and using that estimate makes the header jump to the wrong band and blink
      // until layout settles.
      const height = h[r.key] ?? (i === lastUser ? undefined : ROW_ESTIMATE);
      if (r.kind === "user" && height !== undefined) out.push({ key: r.key, top, height, n: r.n, text: firstLine(r.text), turnKey: r.turnKey });
      top += height ?? ROW_ESTIMATE;
    }
    return out;
  });
  const current = computed(() => {
    const y = scrollY();
    const all = list();
    let idx = -1;
    all.forEach((b, i) => { if (b.top + b.height <= y) idx = i; });
    return idx < 0 ? null : { band: all[idx], next: all[idx + 1] as Band | undefined };
  });
  return (
    <If when={!!current()}>
      {() => {
        const c = current()!;
        const y = scrollY();
        const lines = Math.max(0, Math.floor((Math.min(y, c.next?.top ?? Infinity) - c.band.top) / STICKY_LINE));
        const offset = c.next ? Math.min(0, c.next.top - y - STICKY_BAR) : 0;
        return (
          <View class="absolute left-0 right-0 items-start" style={{ top: offset, pointerEvents: "box-none" }} testID="sticky-user">
            <TimelineColumn>
              <Button onPress={() => p.scrollTo(c.band.top)} accessibilityLabel="Scroll to this message" testID="sticky-user-button"
                class={`relative w-full flex-row items-center gap-3 bg-band ${hit("min-h-7")} hover:bg-accent ${CARD_INSET} ${FOCUS}`}>
                <View class="absolute left-0 top-0 bottom-0 w-0.5 bg-border-strong" />
                <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground-secondary">{c.band.text}</Text>
                <Text class={META}>{`↑ ${lines} lines of this turn above`}</Text>
              </Button>
            </TimelineColumn>
          </View>
        );
      }}
    </If>
  );
}

function ToolTone(tone: string): string {
  return tone === "add" ? "text-success" : tone === "del" ? "text-error" : tone === "info" ? "text-info" : tone === "error" ? "text-error" : tone === "accent" ? "text-primary" : tone === "command" ? "text-foreground" : tone === "muted" ? "text-muted-foreground" : "text-foreground-secondary";
}

/** Member label and the verb icon (the status glyph replaces it while running, failed or waiting). */
const VERB: Record<string, string> = {
  bash: "Shell", read: "Read", edit: "Edit", patch: "Edit", write: "Write", code: "Code", ask: "Ask",
  web_search: "Search", web_fetch: "Fetch",
};
function verbLabel(name: string): string { return VERB[name] ?? name; }
function verbIcon(name: string): IconName {
  if (name === "read") return "read";
  if (name === "edit" || name === "patch" || name === "write") return "edit";
  if (name.startsWith("bash")) return "shell";
  if (name.startsWith("web_")) return "web";
  if (name === "grep" || name === "glob" || name === "search") return "search";
  if (name === "ask") return "ask";
  if (name === "session") return "session";
  return "code";
}

function ActivityRow(p: { r: () => Extract<Row, { kind: "activity" }> }) {
  const r = p.r;
  const reduced = () => !!config()?.values?.ui?.reduced_motion;
  // A group that was live and now is not has just finished: its chevron flashes the accent.
  const flash = signal(false);
  let wasRunning = r().running;
  effect(() => {
    const running = r().running;
    if (wasRunning && !running && !reduced()) {
      flash.set(true);
      setTimeout(() => flash.set(false), FLASH_MS);
    }
    wasRunning = running;
  });
  return (
    <View class={`py-0.5 ${TEXT_INDENT}`}>
      <Button onPress={() => toggle(r().key, r().defaultOpen)} testID={`activity-${r().key.replaceAll(":", "-")}`}
        class={`self-stretch flex-row items-center rounded-(--radius) -ml-1 pl-1 pr-4 ${hit("min-h-7")} hover:bg-accent ${FOCUS}`}>
        <View class={ACTIVITY_CHEVRON}><Icon name={r().open ? "chevron-down" : "chevron-right"} size={14} class={flash() ? "tint-primary" : "tint-muted-foreground"} /></View>
        <If when={r().running} else={<Text style={{ numberOfLines: 1 }} class="shrink min-w-0 text-[13px] leading-5 text-foreground-secondary">{r().summary}</Text>}>
          <Shimmer text={r().summary} reduced={reduced()} testID="group-working" />
        </If>
        <If when={!!r().diff}>
          {() => <Text class={`${META} ml-2 shrink-0`}><Text class="text-success">{`+${r().diff!.add}`}</Text>{" "}<Text class="text-error">{`−${r().diff!.del}`}</Text></Text>}
        </If>
        <If when={r().denied > 0}><Text class={`${META} shrink-0`}>{` · ${r().denied} denied`}</Text></If>
      </Button>
      <If when={r().open}>
        <View>
          <If when={r().hidden > 0}><Button onPress={() => timelineOpen.update((o) => ({ ...o, [r().key]: true }))} accessibilityLabel={`Show ${r().hidden} earlier activities`} class={`self-start pl-4 ${FOCUS}`}><Text class={META}>{`… ${r().hidden} earlier`}</Text></Button></If>
          <Each of={r().items} key={(item) => item.key}>
            {(item) => <ActivityItem item={item} last={() => r().items.at(-1)?.key === item().key} />}
          </Each>
        </View>
      </If>
    </View>
  );
}

/** A member of an expanded group, on the group's tree: `├` for each member and `└` for the last, unless its box stays open. */
function ActivityItem(p: { item: () => ActivityEntry; last: () => boolean }) {
  const item = p.item;
  const ends = () => p.last() && !item().open;
  return (
    <View class="relative pl-4" testID={p.last() ? "activity-tree-end" : "activity-tree-mid"}>
      <View class={`absolute left-[7px] top-0 w-px bg-border-strong ${ends() ? "h-[14px]" : "bottom-0"}`} />
      <View class="absolute left-[7px] top-[14px] w-[7px] h-px bg-border-strong" />
      <If when={item().kind === "tool"}>
        <ToolRow r={item as () => Extract<Row, { kind: "tool" }>} />
      </If>
      <If when={item().kind === "reasoning"}>
        <ReasoningRow x={item as () => Extract<Row, { kind: "reasoning" }>} nested />
      </If>
    </View>
  );
}

/**
 * Thought rows (D07): `▸ Thought · preview`; open shows the text behind a rail. A thought still arriving is open by
 * default and keeps its last 10 lines. Nested in an activity group it reads `◇ Thought · preview`.
 */
function ReasoningRow(p: { x: () => Extract<Row, { kind: "reasoning" }>; nested?: boolean }) {
  const x = p.x;
  // A thought still arriving keeps its last 10 wrapped lines; the rest is announced.
  const rows = signal(0);
  const hidden = () => x().running ? Math.max(0, rows() - 10) : 0;
  const key = () => x().running ? "reason:streaming" : `reason:${x().key}`;
  const preview = () => x().encrypted ? "encrypted, replayed to the provider" : firstLine(x().text);
  return (
    <View class={`py-0.5 ${p.nested ? "" : TEXT_INDENT}`}>
      <Button onPress={() => { if (!x().encrypted) toggle(key(), !!x().running); }} disabled={!!x().encrypted} accessibilityLabel={`${roles.reasoning.label}, ${x().open ? "collapse" : "expand"}`} testID="reasoning"
        class={`self-stretch flex-row items-center rounded-(--radius) -ml-1 pl-1 pr-4 ${hit("min-h-6")} hover:bg-accent ${FOCUS} ${p.nested ? "gap-2" : ""}`}>
        <If when={!!p.nested} else={<View class={ACTIVITY_CHEVRON}><Icon name={x().open ? "chevron-down" : "chevron-right"} size={14} class="tint-muted-foreground" /></View>}>
          <Icon name="diamond" size={MEMBER_ICON} class="tint-muted-foreground" />
        </If>
        <View class="flex-row flex-1 min-w-0 items-center gap-1.5">
          <Text class="text-[13px] leading-5 font-medium text-muted-foreground">{roles.reasoning.label}</Text>
          <If when={!x().open && !!preview()}>
            {() => <>
              <Text class="text-[13px] leading-5 text-muted-foreground">·</Text>
              <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 italic text-muted-foreground">{preview()}</Text>
            </>}
          </If>
        </View>
      </Button>
      <If when={x().open && !x().encrypted}>
        <View class="relative pl-4 pr-4 py-1">
          <View class="absolute left-[7px] top-0 bottom-0 w-px bg-border-strong" />
          <If when={hidden() > 0}>
            {() => <Text class={META}>{`… ${hidden()} earlier lines`}</Text>}
          </If>
          <View class={x().running ? "max-h-[200px] overflow-hidden justify-end" : ""}>
            <Text style={{ userSelect: "text" }} onLayout={(b: any) => rows.set(Math.round((b.bounds?.size?.height ?? b.size?.height ?? 0) / 20))} class="shrink-0 text-[13px] leading-5 italic text-muted-foreground">
              {x().text}
            </Text>
          </View>
        </View>
      </If>
    </View>
  );
}

/** One line of a member's detail output (the hidden-lines marker is shown as the foot instead). */
function ToolDetail(p: { line: () => ToolDetailLine }) {
  return <Text style={{ userSelect: "text" }} class={`font-(family-name:--font-code) text-[12.5px] leading-5 ${ToolTone(p.line().tone)}`}>{p.line().text}</Text>;
}

function ToolRow(p: { r: () => Extract<Row, { kind: "tool" }> }) {
  const r = p.r;
  const toggleRaw = () => toggle(`${r().key}:raw`, false);
  const markFocused = () => focusedTool.set({ key: r().key, clipped: r().clipped, toggleRaw });
  const clearFocused = () => { if (focusedTool()?.key === r().key) focusedTool.set(null); };
  const open = () => r().open;
  const live = () => r().state !== "done";
  const stateKind = () => r().state === "done" ? "done" : r().state;
  const isMark = (x: ToolDetailLine) => x.tone === "show-all" || x.tone === "show-less";
  const isRaw = () => !!r().raw;
  const isDiff = () => !isRaw() && r().state !== "error" && (r().name === "edit" || r().name === "write" || r().name === "patch");
  const body = () => isDiff() ? r().detail.filter((x) => !isMark(x)) : r().detail;
  const mark = () => r().detail.find(isMark);
  const showAll = () => { markFocused(); void run("tool.show_all_output"); };
  const MarkRow = (q: { line: () => ToolDetailLine }) => (
    <Button onPress={showAll} class={`self-start rounded-(--radius) ${hit("min-h-6")} hover:bg-accent ${FOCUS}`}
      testID={q.line().tone === "show-all" ? `tool-show-all-${r().id}` : `tool-show-less-${r().id}`}>
      <Text class={`font-(family-name:--font-code) text-[12.5px] leading-5 ${q.line().text.includes(HIDDEN_MARK) ? "text-primary" : "text-muted-foreground"}`}>{q.line().text}</Text>
    </Button>
  );
  return (
    <View class="py-0.5">
      <View role="button" onFocus={markFocused} onBlur={clearFocused} onPress={() => {
        markFocused();
        // Enter or a press on an open, clipped row brings the rest of its output back.
        if (open() && r().clipped && !r().showAll) showAll();
        else toggle(r().key, timelineDetail() === "high");
      }} accessibilityLabel={`${r().name} ${r().state}`} accessibilityState={{ selected: open() }}
        class={`flex-row items-center gap-2 rounded-(--radius) -ml-1 pl-1 pr-4 ${hit("min-h-6")} hover:bg-accent ${FOCUS}`} testID={`tool-row-${r().id}`}>
        <If when={live()} else={<Icon name={verbIcon(r().name)} size={MEMBER_ICON} class="tint-muted-foreground" />}>
          {() => <View class="w-[13px] shrink-0 items-center">
            <If when={r().state === "error"} else={<Status kind={stateKind()} />}>
              {/* A failed call is part of the flow: marked, never red. */}
              <Text class="text-center text-[12px] leading-5 text-muted-foreground" accessibilityLabel="failed">{statusGlyphs.error.unicode}</Text>
            </If>
          </View>}
        </If>
        <Text class="shrink-0 text-[13px] leading-5 font-medium text-foreground">{verbLabel(r().name)}</Text>
        <If when={!!r().arg}><Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class={`shrink min-w-0 ${T.mono}`}>{r().arg}</Text></If>
        <If when={!!r().errorLine}><Text style={{ numberOfLines: 1 }} class={`shrink min-w-0 ${T.mono}`}>{r().errorLine!}</Text></If>
        <If when={!!r().summary}>
          <Text class={`${META} shrink-0`}><Text class={r().summary.startsWith("+") ? "text-success" : "text-muted-foreground"}>{r().summary.split(" −")[0]}</Text><If when={r().summary.includes(" −")}><Text class="text-error">{` −${r().summary.split(" −")[1]}`}</Text></If></Text>
        </If>
        <View class="flex-1" />
        <If when={open()}>
          <Button onPress={() => { markFocused(); toggleRaw(); }} class={`rounded-(--radius) px-1 ${hit("min-h-6")} hover:bg-accent ${FOCUS}`} testID={`tool-raw-${r().id}`}>
            <Text class={`text-xs leading-4 ${isRaw() ? "text-primary font-medium" : "text-muted-foreground"}`}>raw</Text>
          </Button>
        </If>
        <If when={r().exit !== undefined}><Text class="text-xs leading-4 text-muted-foreground shrink-0">{`exit ${r().exit}`}</Text></If>
        <If when={r().exit !== undefined && (r().state !== "done" || !!r().duration)}><Text class={META}>·</Text></If>
        <If when={r().state === "waiting"}><Text class="text-xs leading-4 text-primary shrink-0">waiting</Text></If>
        <If when={r().state === "denied"}><Text class="text-xs leading-4 text-error shrink-0">denied</Text></If>
        <If when={r().state === "error"}><Text class="text-xs leading-4 text-muted-foreground shrink-0">failed</Text></If>
        <If when={(r().state === "done" || r().state === "running") && !!r().duration}><Text class={`${META} shrink-0`}>{r().duration!}</Text></If>
      </View>
      <If when={open()}>
        <View class="pl-4 pr-4 pb-1" onFocus={markFocused} onBlur={clearFocused}>
          <If when={isDiff()} else={
            <Each of={body()} key={(x, i) => `${i}`}>
              {(x) => <If when={isMark(x())} else={<ToolDetail line={x} />}><MarkRow line={x} /></If>}
            </Each>
          }>
            {() => (
              <>
                <If when={mark()?.tone === "show-less"}>{() => <MarkRow line={() => mark()!} />}</If>
                <DiffView lines={body} />
                <If when={mark()?.tone === "show-all"}>{() => <MarkRow line={() => mark()!} />}</If>
              </>
            )}
          </If>
          <If when={r().media.length > 0}>
            <View class="flex-row flex-wrap gap-4 pt-2" testID={`tool-media-${r().id}`}>
              <Each of={r().media} key={(x) => x.artifact}>
                {(x) => <View class="gap-1"><Image src={apiUrl(`/api/artifact/${x().artifact}`)} style={{ width: 320, height: 240, objectFit: "contain" }} accessibilityLabel={x().name || "image"} /><Text class={META}>{x().name}</Text></View>}
              </Each>
            </View>
          </If>
        </View>
      </If>
    </View>
  );
}

/** Rows wholly below the viewport (the jump pill's `N new`). */
function newBelow(rows: Row[]): number {
  const h = rowHeights();
  const bottom = scrollY() + viewH();
  let top = LIST_PAD, n = 0;
  for (const r of rows) {
    if (top >= bottom) n++;
    top += h[r.key] ?? ROW_ESTIMATE;
  }
  return n;
}

export function Timeline() {
  let ref: any;
  // Settled history is rebuilt only on structural changes; a text/reasoning delta just
  // appends the two live rows (and regroups), so a long session does not re-walk every
  // message and tool detail on every token. `computed` also stops the five or six JSX
  // accesses below from rebuilding the same timeline within one frame.
  const stable = computed(() => buildStableRows());
  const rows = computed(() => {
    streamRev();
    const sel = model.selected;
    if (!sel) return [];
    return groupActivities(withStreaming(stable(), model.streaming.get(sel.sessionId)), model.running.has(sel.sessionId));
  });
  const below = computed(() => newBelow(rows()));
  jump.fn = () => { try { ref?.scrollToEnd({ animated: true }); } catch {} };
  // A session opens at its latest message, as in the TUI; `maintainVisibleContentPosition` then follows new content while at the end.
  let openedSession: string | undefined;
  effect(() => {
    const sel = model.selected;
    const id = sel?.loaded && rows().length ? sel.sessionId : undefined;
    if (!id || id === openedSession) return;
    openedSession = id;
    scrolledUp.set(false);
    setTimeout(() => { try { ref?.scrollToEnd({ animated: false }); } catch {} }, 0);
  });
  return (
    <View class="flex-1 min-h-0" testID="timeline">
      <If when={rows().length > 0} else={<EmptyTimeline />}>
        <List
          ref={(n: any) => (ref = n)} each={rows()} keyExtractor={(x) => x.key} estimatedItemSize={72} class="flex-1" contentStyle={{ paddingVertical: 12 }}
          maintainVisibleContentPosition={{ autoscrollToBottomThreshold: 160 }}
          onScroll={(e) => {
            scrollY.set(e.offset.y);
            viewH.set(e.size.height);
            scrolledUp.set(e.contentSize.height - (e.offset.y + e.size.height) > 160);
          }}
          scrollEventThrottle={64}
        >
          {(r) => <RowView r={r} />}
        </List>
      </If>
      <StickyBar rows={rows} scrollTo={(offset) => { try { ref?.scrollToOffset({ offset, animated: true }); } catch {} }} />
      <If when={scrolledUp()}>
        <View class="absolute bottom-3 left-0 right-0 items-center" style={{ pointerEvents: "box-none" }}>
          <TimelineColumn>
            <View class="w-full items-end pr-4">
              <Button onPress={() => run("session.jump_latest")} accessibilityLabel={`Jump to latest, ${below()} new`} testID="jump-latest" class={`flex-row items-center gap-2 rounded-full bg-popover px-3 ${isNarrow() ? "min-h-11" : "min-h-8"} shadow-md hover:bg-accent ${FOCUS}`}>
                <Text class="text-[13px] leading-5 text-foreground-secondary">↓</Text>
                <Text class="text-[13px] leading-5 text-popover-foreground">{below() > 0 ? `${below()} new` : "latest"}</Text>
                <Text class={META}>{`· ${keyLabel(byId.get("session.jump_latest")!) ?? "end"}`}</Text>
              </Button>
            </View>
          </TimelineColumn>
        </View>
      </If>
    </View>
  );
}

function EmptyTimeline() {
  return (
    <View class="flex-1 items-center justify-center gap-1 p-6">
      <If when={viewed() && model.selected?.loaded} else={<Text class={T.sec}>{viewed() ? "Loading…" : "Select a session"}</Text>}>
        <Text class="text-[14px] leading-[22px] font-medium text-foreground">No messages yet</Text>
        <Text class={T.sec}>Send a message below to start this session.</Text>
      </If>
    </View>
  );
}
