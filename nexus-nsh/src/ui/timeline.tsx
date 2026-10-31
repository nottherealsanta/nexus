import { agentColor } from "../agent-color";
import { List, Scroll, signal, Text, View } from "nsh";
import { Image } from "nsh";
import { Button, TextView } from "nsh/base";
import type { TextViewCodeBlock } from "nsh/base";
import { FOCUS, Icon, IconButton, Status, T } from "./kit";
import { DiffView } from "./diff";
import { disabledOf, foldedMeta, startingSections, StartingContext } from "./newsession";
import type { CtxSection } from "./newsession";
import { ACTIVITY_CONTROL_INSET, ACTIVITY_INDENT, CARD_INSET, TEXT_INDENT, TimelineColumn } from "./reading-column";
import { byId, copyText, jump, keyLabel, openLinked, run, toggleSessionInventory } from "../commands";
import { contextDetail, focusedTool, isNarrow, m, model, modelCatalog, scrolledUp, selectSession, timelineDetail, timelineOpen, toolOutputAll, viewed } from "../store";
import type { CoreEvent, Message, Part, TurnSummary } from "../core/api";
import { floorIndex, groupRequests, sumUsage, toolTimes, turnsIn } from "../core/turns";
import type { IconName } from "../icons";
import { roles, statusGlyphs } from "../gen/theme";
import "../../fonts/MonaspaceArgon-Regular.otf";
import "../../fonts/MonaspaceArgon-Medium.otf";
import "../../fonts/MonaspaceArgon-Bold.otf";

type Tone = "info" | "accent" | "warning";
type TurnStatus = "running" | "done" | "error" | "interrupted" | "step_limit";
type Diff = { add: number; del: number };
/** Timeline metadata uses the app's monospace face. */
const META = `${T.meta} font-mono`;

type Row =
  | { key: string; kind: "footer"; segments: { text: string; tip?: string }[]; error?: string; interrupted: boolean }
  | {
      key: string; kind: "user"; delivery?: string | null; images?: { name: string; data_url: string }[]; n: number; time: string; text: string; turnKey: string; folded: boolean;
      tools: number; duration: string; status: TurnStatus;
    }
  | {
      key: string; kind: "context"; meta: string; reconstructed?: boolean; open: boolean;
      sections: CtxSection[]; total: number; window?: number;
    }
  | { key: string; kind: "notice"; text: string; label: string; tone: Tone }
  | { key: string; kind: "handoff"; from: string; summary: string; time: string }
  | { key: string; kind: "assistant"; text: string; streaming?: boolean }
  | { key: string; kind: "reasoning"; text: string; encrypted?: boolean; open: boolean; running?: boolean; duration?: string }
  | {
      key: string; kind: "activity"; summary: string; items: (Extract<Row, { kind: "tool" }> | Extract<Row, { kind: "reasoning" }>)[];
      open: boolean; defaultOpen: boolean; hidden: number; failed: number; duration?: string; diff?: Diff;
    }
  | {
      key: string; kind: "tool"; id: string; name: string; arg: string; summary: string; tone: Tone;
      state: "running" | "done" | "error" | "waiting"; duration?: string; seconds?: number; diff?: Diff; output?: string;
      detail: { text: string; tone: "plain" | "add" | "del" | "info" | "error" | "accent" }[];
      raw: string; open: boolean; clipped: boolean; showAll: boolean;
      /** Images the tool returned (K8): artifacts Core stored, shown beside the output. */
      media: { artifact: string; mime: string; name: string }[];
    }
  | {
      key: string; kind: "child"; childId?: string; agent: string; title: string; status: string; tone: Tone;
      body: string; open: boolean; background: boolean; ack: boolean; meta: string;
    }
  | { key: string; kind: "diag"; text: string; label: string; detail?: string; tone: Tone }
  | { key: string; kind: "older"; text: string; action?: "load-older" }
  | { key: string; kind: "empty" };

export type ToolDetailLine = { text: string; tone: "plain" | "add" | "del" | "info" | "error" | "accent" };
const DETAIL_HEAD = 100;
const DETAIL_TAIL = 100;
const HIGH_HEAD = 30;
const HIGH_TAIL = 20;
const HIDDEN_MARK = "lines hidden · show all (o)";

/** UI-only cap: the model's separate output clip stays untouched. */
export function capToolDetail(lines: ToolDetailLine[], showAll: boolean, head = DETAIL_HEAD, tail = DETAIL_TAIL): ToolDetailLine[] {
  if (showAll || lines.length <= head + tail + 20) return lines;
  const hidden = lines.length - head - tail;
  return [
    ...lines.slice(0, head),
    { text: `… ${hidden} ${HIDDEN_MARK}`, tone: "accent" },
    ...lines.slice(-tail),
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

function toolArg(args: string | undefined): string {
  if (!args) return "";
  try {
    const o = JSON.parse(args);
    const v = o.command ?? o.path ?? o.file ?? o.pattern ?? o.query ?? o.directory ?? o.code ?? o.url ?? Object.values(o)[0];
    return typeof v === "string" ? v.replace(/\s+/g, " ") : JSON.stringify(v ?? "");
  } catch { return args.slice(0, 120); }
}
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
  if (s < 10) return `${s.toFixed(1)}s`;
  const whole = Math.round(s);
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
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
function thoughtSeconds(turns: TurnSummary[]): Map<string, number> {
  const seen = new Map<string, number | null>();
  for (const turn of turns) for (const request of turn.requests ?? []) {
    const id = request.message_id;
    const seconds = request.reasoning_seconds;
    if (!id) continue;
    const valid = request.status === "completed" && typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0;
    seen.set(id, seen.has(id) || !valid ? null : seconds!);
  }
  const out = new Map<string, number>();
  for (const [id, seconds] of seen) if (seconds !== null) out.set(id, seconds);
  return out;
}
function text(m: Message): string {
  return m.parts.filter((p) => p.type === "text").map((p) => p.text ?? "").join("\n");
}

const DIAG_TYPES = new Set(["session.error", "retry_scheduled", "retry_started", "retry_exhausted", "turn.recovered", "session.directory_changed", "session.notified", "permission.replied", "question.replied"]);
function diag(e: CoreEvent): Extract<Row, { kind: "diag" }> | null {
  const d = e.data ?? {};
  const k = `ev-${e.id}`;
  const label = roles.diagnostic.label;
  switch (e.type) {
    case "session.error": return { key: k, kind: "diag", label, text: `${d.kind ?? "error"}: ${d.message ?? ""}`, detail: d.status ? `HTTP ${d.status}` : undefined, tone: "warning" };
    case "retry_scheduled": return { key: k, kind: "diag", label, text: `provider retry ${d.attempt ?? ""}/${d.max_attempts ?? ""} in ${d.delay ?? "?"}s`, detail: d.reason, tone: "warning" };
    case "retry_started": return { key: k, kind: "diag", label, text: `provider retry started (attempt ${d.attempt ?? ""})`, tone: "warning" };
    case "retry_exhausted": return { key: k, kind: "diag", label, text: "provider retries exhausted", detail: d.reason, tone: "warning" };
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
        out.push({ t: at, row: { key: `retry-${r.id}-end`, kind: "diag", label, text: "provider retries exhausted", detail: a.error ?? undefined, tone: "warning" } });
      }
    });
  }
  if (t.error) {
    const e = t.error;
    out.push({ t: t.finished ?? t.started, row: { key: `error-${t.id}`, kind: "diag", label, text: `${e.kind ?? "error"}: ${e.message ?? ""}`, detail: e.status ? `HTTP ${e.status}` : undefined, tone: "warning" } });
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
      const model = r.model ? bareModel(r.model) : null;
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
    const oldS = (o.old_string ?? o.old ?? "") as string;
    const newS = (o.new_string ?? o.new ?? "") as string;
    return { add: newS ? newS.split("\n").length : 0, del: oldS ? oldS.split("\n").length : 0 };
  } catch { return null; }
}
function detailLines(value: string): string[] {
  if (!value) return [];
  const lines = value.split(/\r\n|\n/);
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}
export function detailOf(name: string, args: string | undefined, output: string | undefined, raw: boolean): ToolDetailLine[] {
  if (raw) {
    const lines = detailLines(args ?? "").map((l) => ({ text: l, tone: "plain" as const }));
    return [...lines, ...detailLines(output ?? "").map((l) => ({ text: l, tone: "plain" as const }))];
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
  return detailLines(output ?? "").map((l) => ({ text: l, tone: "plain" as const }));
}

export function buildRows(): Row[] {
  m();
  const sel = model.selected;
  if (!sel) return [];
  const results = new Map<string, Part>();
  for (const msg of sel.messages) for (const p of msg.parts) if (p.type === "tool_result" && p.call_id) results.set(p.call_id, p);
  const traces = toolTimes(sel.turns, sel.scratch);
  const waiting = new Set([...model.approvals.values()].map((a) => a.call_id));
  const busy = viewed()?.status === "busy";
  const metas = turnMeta(sel.messages, sel.turns, sel.scratch, busy);
  const thoughts = thoughtSeconds(sel.turns);
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
        meta: foldedMeta(sections.length, total, true),
        reconstructed: s.origin === "reconstructed", open: open(`context:${sid}`, true),
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
          key: msg.id, kind: "user", delivery: msg.delivery, images: msg.parts.filter((p) => p.type === "image") as any, n: n + 1, time: clock(msg.created), text: body, turnKey: key, folded,
          tools: meta?.tools ?? 0, duration: meta ? fmtDuration(meta.endTime - meta.startTime) : "",
          status: meta ? statusOf(meta) : "running",
        });
        if (folded) { rows.push({ t: msg.created, r: out }); continue; }
      }
    } else if (msg.role === "assistant") {
      if (foldedTurn) { rows.push({ t: msg.created, r: out }); continue; }
      const reasoningParts = msg.parts.filter((p) => p.type === "reasoning").length;
      msg.parts.forEach((p, i) => {
        const key = `${msg.id}-${i}`;
        if (p.type === "text" && (p.text ?? "").trim()) out.push({ key, kind: "assistant", text: p.text ?? "" });
        else if (p.type === "reasoning") {
          const summary = p.summary ?? "";
          // One completed request reports one thinking duration; several parts share it, so none is attributed.
          const seconds = reasoningParts === 1 && summary.trim() ? thoughts.get(msg.id) : undefined;
          out.push({
            key, kind: "reasoning", text: summary, encrypted: !summary.trim(), open: open(`reason:${key}`, false),
            duration: seconds !== undefined ? fmtDuration(seconds) : undefined,
          });
        } else if (p.type === "tool_call") {
          const id = p.id ?? key;
          if (p.name === "session") {
            let a: any = {};
            try { a = JSON.parse(p.arguments ?? "{}"); } catch {}
            const child = [...model.sessions.values()].find((c) => c.parent_id === sid && c.parent_call_id === id);
            const res = results.get(id);
            const waits = child ? [...model.approvals.values(), ...model.questions.values()].some((w) => w.session_id === child.id) : false;
            const running = child ? model.running.has(child.id) || child.status === "busy" : false;
            const failed = !!res?.is_error || !!child && (child.status === "failed" || model.failedSessions.has(child.id));
            const background = a.wait === false;
            // A successful background result is always a start acknowledgement, not an outcome.
            const ack = background && !!res && !res.is_error;
            const status = waits ? "waiting for input" : running ? "running" : failed ? "failed" : res ? (ack ? "started" : "done") : "starting…";
            const body = res ? (res.output ?? "") : waits ? "waiting for input" : running ? "running…" : "starting…";
            const meta = childModel(child);
            const key = `child:${id}`;
            out.push({
              key, kind: "child", childId: child?.id, agent: a.agent ?? "child",
              title: a.title || child?.title || firstLine(a.prompt ?? ""), status,
              tone: waits ? "accent" : running ? "info" : failed ? "warning" : "info",
              body, open: open(key, false), background, ack, meta,
            });
          } else {
            const res = results.get(id);
            const tr = traces.get(id);
            const state: "running" | "done" | "error" | "waiting" = waiting.has(id) ? "waiting"
              : res?.is_error ? "error" : res || tr?.end ? "done" : "running";
            const counts = (p.name === "edit" || p.name === "patch") && !res?.is_error ? diffCounts(p.arguments) : null;
            let summary = "";
            if (waiting.has(id)) summary = "waiting for approval";
            else if (p.name === "edit" || p.name === "patch") summary = res?.is_error ? "failed" : counts ? `+${counts.add} −${counts.del}` : "";
            else if (p.name === "write") { try { summary = `+${detailLines(String(JSON.parse(p.arguments ?? "{}").content ?? "")).length}`; } catch {} }
            else if (res?.output) summary = `${detailLines(res.output).length} lines`;
            const media = ((res as any)?.media ?? []).filter((x: any) => /^image\//.test(x?.mime ?? ""));
            if (media.length && !waiting.has(id)) summary = [summary, `${media.length} image${media.length === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
            const seconds = tr?.start !== undefined && tr?.end !== undefined ? tr.end - tr.start : undefined;
            const key = `tool:${id}`;
            const rawOpen = open(`${key}:raw`, false);
            const showAll = toolOutputAll()[key] ?? false;
            const detailLevel = timelineDetail();
            const head = detailLevel === "high" ? HIGH_HEAD : DETAIL_HEAD;
            const tail = detailLevel === "high" ? HIGH_TAIL : DETAIL_TAIL;
            const allDetail = detailOf(p.name ?? "tool", p.arguments, res?.output, rawOpen);
            const clipped = allDetail.length > head + tail + 20;
            const detail = capToolDetail(allDetail, showAll, head, tail);
            out.push({
              key, kind: "tool", id, name: p.name ?? "tool", arg: toolArg(p.arguments), summary,
              tone: counts ? (counts.add >= 0 ? "accent" : "info") : "info",
              state, duration: p.name === "bash" && seconds !== undefined && seconds >= 1 ? fmtDuration(seconds) : undefined, seconds,
              diff: counts ?? undefined, output: res?.output,
              detail,
              raw: rawOpen ? JSON.stringify({ arguments: p.arguments ?? "", output: res?.output ?? "" }, null, 2) : "",
              open: open(key, detailLevel === "high" || media.length > 0), clipped, showAll, media,
            });
          }
        }
      });
    } // tool-role messages are shown through their call's row
    rows.push({ t: msg.created, r: out });
    // A turn's footer belongs after its last message.
    if (turnAt > 0 && (msg === sel.messages[sel.messages.length - 1] || (() => { const next = sel.messages[sel.messages.indexOf(msg) + 1]; return next?.role === "user" && next.delivery !== "steer"; })())) {
      const meta = metas[turnAt - 1];
      if (meta && !foldedTurn) {
        const v = viewed();
        const segments: { text: string; tip?: string }[] = [];
        if (meta.models.length) segments.push({ text: meta.models.join(" → ") });
        else if (v?.model && String(v.model) !== "default") segments.push({ text: String(v.model) });
        segments.push({ text: `${meta.tools} tool${meta.tools === 1 ? "" : "s"}` });
        if (meta.known > 0) {
          let tip = `input ${meta.input.toLocaleString()} · cached ${meta.cached.toLocaleString()} · output ${meta.output.toLocaleString()} · reasoning ${meta.reasoning.toLocaleString()}`;
          if (meta.input > 0) tip += ` · ${Math.round(100 * meta.cached / meta.input)}% cached${meta.unknown ? " (reported usage only)" : ""}`;
          // Unknown usage is never counted as zero: the total says it is a floor.
          if (meta.unknown) tip += ` · ${meta.unknown} request${meta.unknown === 1 ? "" : "s"} reported no usage`;
          const suffix = meta.unknown ? " + unknown" : "";
          segments.push({ text: `${fmtCount(meta.input)} in${suffix}`, tip });
          segments.push({ text: `${fmtCount(meta.output)} out${suffix}`, tip });
        } else if (meta.unknown) {
          segments.push({ text: "tokens unknown", tip: `${meta.unknown} request${meta.unknown === 1 ? "" : "s"} reported no usage` });
        }
        segments.push({ text: fmtDuration(meta.endTime - meta.startTime), tip: "Elapsed time" });
        if (!meta.running && meta.finishedTime != null) segments.push({ text: clock(meta.finishedTime), tip: "Finished time" });
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
  const st = model.streaming.get(sel.sessionId);
  if (st?.reasoning) flat.push({ key: "stream-r", kind: "reasoning", text: st.reasoning, open: true, running: true });
  if (st?.text) flat.push({ key: "stream-t", kind: "assistant", text: st.text, streaming: true });
  if (sel.messagesMore) flat.unshift({ key: "older-messages", kind: "older", text: "Older messages are not loaded", action: "load-older" });
  if (sel.turnsGap) flat.unshift({ key: "turns-gap", kind: "older", text: `${sel.turnsGap[0].toUpperCase()}${sel.turnsGap.slice(1)}.` });
  return groupActivities(flat);
}

type ActivityEntry = Extract<Row, { kind: "tool" }> | Extract<Row, { kind: "reasoning" }>;
function activitySummary(items: ActivityEntry[]): string {
  const tools = items.filter((item): item is Extract<Row, { kind: "tool" }> => item.kind === "tool");
  if (tools.some((tool) => tool.state === "running" || tool.state === "waiting")) return "Working";
  const counts = new Map<string, number>();
  for (const item of items) {
    let label: string;
    if (item.kind === "reasoning") label = "Thought";
    else {
      label = ({
        bash: "Ran command", read: "Read file", edit: "Edited file", patch: "Edited file", write: "Wrote file",
        code: "Ran cell", ask: "Asked question", web_search: "Searched time", web_fetch: "Fetched page",
      } as Record<string, string>)[item.name] ?? `Used ${item.name}`;
    }
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()].map(([label, count], index) => {
    if (label === "Thought") return "Thought";
    const [verb, noun] = label.lastIndexOf(" ") > 0 ? [label.slice(0, label.lastIndexOf(" ")), label.slice(label.lastIndexOf(" ") + 1)] : [label, ""];
    const piece = `${verb} ${count} ${noun}${count === 1 || !noun ? "" : "s"}`.trim();
    return index ? piece[0].toLowerCase() + piece.slice(1) : piece;
  }).join(", ");
}

function groupActivities(rows: Row[]): Row[] {
  const result: Row[] = [];
  let items: ActivityEntry[] = [];
  const flush = () => {
    if (!items.length) return;
    // A run of only thoughts is no activity group: each is its own `▸ Thought · preview` row (D07).
    if (items.every((item) => item.kind === "reasoning")) { result.push(...items); items = []; return; }
    const key = `group:${items[0].key}`;
    const waiting = items.some((item) => item.kind === "tool" && item.state === "waiting");
    const running = items.some((item) => item.kind === "reasoning" ? item.running : item.state === "running" || item.state === "waiting");
    const detail = timelineDetail();
    const defaultOpen = waiting || detail === "high" || (running && detail !== "low");
    const expanded = waiting || open(key, defaultOpen);
    const explicit = timelineOpen()[key] === true;
    const hidden = running && !waiting && detail !== "high" && !explicit ? Math.max(0, items.length - 5) : 0;
    const tools = items.filter((item): item is Extract<Row, { kind: "tool" }> => item.kind === "tool");
    const timed = tools.filter((tool) => tool.seconds !== undefined);
    const diffs = tools.filter((tool) => tool.diff !== undefined);
    result.push({
      key, kind: "activity", summary: running ? "Working" : activitySummary(items), items: items.slice(hidden),
      open: expanded, defaultOpen, hidden,
      failed: tools.filter((tool) => tool.state === "error").length,
      duration: timed.length ? fmtDuration(timed.reduce((sum, tool) => sum + (tool.seconds ?? 0), 0)) : undefined,
      diff: diffs.length ? diffs.reduce<Diff>((sum, tool) => ({ add: sum.add + (tool.diff?.add ?? 0), del: sum.del + (tool.diff?.del ?? 0) }), { add: 0, del: 0 }) : undefined,
    });
    items = [];
  };
  for (const row of rows) {
    if (row.kind === "tool" || row.kind === "reasoning") items.push(row);
    else { flush(); result.push(row); }
  }
  flush();
  return result;
}

const RAIL: Record<string, string> = { info: "bg-info", accent: "bg-primary", warning: "bg-warning" };
function Rail(p: { color: string }) { return <View class={`w-[3px] self-stretch rounded-full ${p.color}`} />; }

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
      <View class="w-full font-mono">
        <If when={r().kind === "context"}>
          {() => {
            const c = r() as Extract<Row, { kind: "context" }>;
            return <StartingContext sections={c.sections} total={c.total} estimated window={c.window} reconstructed={!!c.reconstructed} onInventoryToggle={(kind, name, locked) => { void toggleSessionInventory(kind, name, locked); }} />;
          }}
        </If>
        <If when={r().kind === "user"}>
          <UserBand r={r as () => Extract<Row, { kind: "user" }>} />
        </If>
        <If when={r().kind === "footer"}>
          {() => {
            const f = r() as Extract<Row, { kind: "footer" }>;
            return (
              <View class="w-full items-end gap-0.5 pt-3 pb-1" testID="turn-footer">
                <If when={!!f.error}><Text style={{ userSelect: "text" }} class="text-xs leading-4 text-error font-mono">{`✕ ${f.error}`}</Text></If>
                <View class="flex-row flex-wrap justify-end items-center">
                  <Each of={f.segments} key={(s, i) => `${i}`}>
                    {(s) => (
                      <Text class={META} tooltip={s().tip ? <Text class="text-xs leading-4 text-popover-foreground bg-popover border border-border rounded-(--radius) px-2 py-1">{s().tip!}</Text> : undefined}>
                        {`${s() === f.segments[0] ? "" : " · "}${s().text}`}
                      </Text>
                    )}
                  </Each>
                  <If when={f.interrupted}><Text class="text-xs leading-4 text-warning font-mono">{" · /continue"}</Text></If>
                </View>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "handoff"}>
          <View class="py-1 font-mono"><HandoffCard r={r as any} /></View>
        </If>
        <If when={r().kind === "notice" && (r() as any).text.startsWith("→ Background Shell Done:")}>
          <Text style={{ userSelect: "text" }} class={`text-[13px] leading-5 text-muted-foreground py-2 font-mono ${TEXT_INDENT}`} testID="background-shell-done">{(r() as any).text}</Text>
        </If>
        <If when={r().kind === "notice" && !(r() as any).text.startsWith("→ Background Shell Done:")}>
          <View class={`flex-row gap-3 rounded-(--radius-card) bg-card border border-border py-2.5 my-1 font-mono ${CARD_INSET}`}>
            <Rail color={RAIL[(r() as any).tone] ?? "bg-info"} />
            <View class="flex-1 min-w-0 gap-0.5">
              <Text class="text-xs leading-4 font-medium text-muted-foreground font-mono">{(r() as any).label}</Text>
              <Text style={{ userSelect: "text" }} class={`${T.sec} font-mono`}>{(r() as any).text}</Text>
            </View>
          </View>
        </If>
        <If when={r().kind === "assistant"}>
          <View class={`py-1.5 ${TEXT_INDENT}`} testID="msg-assistant">
            <TextView
              selectable markdown={(r() as any).text} options={{ streaming: !!(r() as any).streaming }}
              renderCodeBlock={(block) => <CodeFence block={block} />}
              classes={{ root: "font-mono text-[15px] leading-6 text-foreground", code: "font-(family-name:--font-code) text-[13px] rounded-(--radius-card) bg-card text-foreground" }}
            />
          </View>
        </If>
        <If when={r().kind === "reasoning"}>
          <View class={ACTIVITY_INDENT}><ReasoningRow x={r as () => Extract<Row, { kind: "reasoning" }>} /></View>
        </If>
        <If when={r().kind === "activity"}>
          <View class={ACTIVITY_INDENT}><ActivityRow r={r as () => Extract<Row, { kind: "activity" }>} /></View>
        </If>
        <If when={r().kind === "tool"}>
          <ToolRow r={r as () => Extract<Row, { kind: "tool" }>} />
        </If>
        <If when={r().kind === "child"}>
          {() => {
            const c = r() as Extract<Row, { kind: "child" }>;
            const kind = c.tone === "accent" ? "waiting" : c.status === "running" || c.status === "starting…" || c.status === "started" ? "running" : c.status === "failed" ? "error" : "done";
            return (
              <View class="flex-row rounded-(--radius-card) bg-card border border-border overflow-hidden my-1 font-mono">
                <Rail color={RAIL[c.tone] ?? "bg-info"} />
                <View class={`flex-1 min-w-0 gap-1 py-2 ${CARD_INSET}`}>
                  <Button onPress={() => c.childId && void selectSession(c.childId)} disabled={!c.childId}
                    class={`flex-row items-center gap-2 rounded-(--radius) px-1 -ml-1 min-h-6 hover:bg-accent ${FOCUS}`} testID="child-card">
                    <Status kind={kind} />
                    <Text class="text-xs leading-4 text-muted-foreground">child</Text>
                    <If when={c.background}><Text class="text-xs leading-4 text-muted-foreground">Background</Text></If>
                    <Text class={`text-[13px] leading-5 ${agentColor(c.agent)}`}>{c.agent}</Text>
                    <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground-secondary">{c.title ? `“${c.title}”` : ""}</Text>
                    <Text testID="child-status" class={META}>{c.status}</Text>
                  </Button>
                  <If when={!!c.meta}><Text testID="child-meta" class={`${META} pl-6`}>{c.meta}</Text></If>
                  <Button onPress={() => toggle(c.key, false)} accessibilityLabel={open(c.key, false) ? "Collapse child result" : c.ack ? "Expand background start acknowledgement" : "Expand child result"} testID="child-result-toggle" class={`items-start pl-6 ${FOCUS}`}>
                    <Text style={open(c.key, false) ? { userSelect: "text" } : { numberOfLines: 2 }} class={META}>{c.body}</Text>
                    <Text class={META}>{open(c.key, false) ? "Show less" : c.ack ? "Show start" : "Show result"}</Text>
                  </Button>
                </View>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "diag"}>
          <View class={`flex-row gap-3 rounded-(--radius-card) bg-card border border-border py-2.5 my-1 font-mono ${CARD_INSET}`} testID="msg-diag">
            <Rail color={RAIL[(r() as any).tone] ?? "bg-warning"} />
            <View class="flex-1 min-w-0 gap-0.5">
              <Text class="text-xs leading-4 font-medium text-muted-foreground font-mono">{(r() as any).label}</Text>
              <Text style={{ userSelect: "text" }} class={`${T.sec} font-mono`}>{(r() as any).text}</Text>
              <If when={(r() as any).detail}><Text class={META}>{(r() as any).detail}</Text></If>
            </View>
          </View>
        </If>
        <If when={r().kind === "older"}>
          <If when={(r() as any).action === "load-older"} else={<Text class={`${META} py-2 ${TEXT_INDENT}`} testID="timeline-older">{(r() as any).text}</Text>}>
            <View class={`flex-row items-center gap-2 py-2 ${TEXT_INDENT}`}>
              <Text class={META} testID="timeline-older">{(r() as any).text}</Text>
              <Button onPress={() => run("turn.load_older")} testID="load-older" accessibilityLabel="Load older messages" class={`rounded-(--radius) px-2 min-h-6 justify-center hover:bg-accent ${FOCUS}`}>
                <Text class="text-xs leading-4 text-foreground font-mono">Load older messages</Text>
              </Button>
            </View>
          </If>
        </If>
      </View>
    </TimelineColumn>
  );
}

/** A fenced code block (DESIGN_PLAN D08): a panel card with the language and a Copy button over the code. */
function CodeFence(p: { block: TextViewCodeBlock }) {
  const code = () => p.block.code;
  return (
    <View class="w-full my-3 rounded-[10px] bg-card overflow-hidden" testID="code-fence">
      <View class="flex-row items-center h-8 pl-4 pr-2 gap-2">
        <Text class="flex-1 min-w-0 text-xs leading-4 text-muted-foreground font-mono">{p.block.language ?? ""}</Text>
        <Button onPress={() => copyText(code(), "Copied code")} accessibilityLabel="Copy code" testID="code-copy"
          class={`flex-row items-center gap-1.5 rounded-(--radius) px-2 min-h-6 hover:bg-accent ${FOCUS}`}>
          <Icon name="copy" size={12} class="tint-muted-foreground" />
          <Text class={META}>Copy</Text>
        </Button>
      </View>
      <Scroll horizontal>
        <View class="gap-0 px-4 pb-3.5">
          {code().split("\n").map((line) => <Text style={{ userSelect: "text" }} class="font-(family-name:--font-code) text-[13px] leading-5 text-foreground">{line || " "}</Text>)}
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
          <Text class="flex-1 min-w-0 text-[14px] leading-5 font-semibold text-foreground font-mono">{`Continued from ${name()}`}</Text>
          <Text class={META}>{`handoff · ${r().time}`}</Text>
        </View>
        <Text style={{ numberOfLines: 3 }} testID="handoff-preview" class="text-[14px] leading-5 text-foreground font-mono">{lines().join("\n")}</Text>
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
      <View class={`group/band w-full rounded-(--radius-card) bg-raised py-3 relative ${CARD_INSET}`} testID="msg-user"
        onFocus={() => focusedBand.set(r().key)} onBlur={() => { if (focusedBand() === r().key) focusedBand.set(null); }}>
        {/* The TUI's band rail stays visible; focus only changes its colour. */}
        <View class={`absolute left-0 top-2 bottom-2 w-0.5 rounded-full ${focusedBand() === r().key ? "bg-primary" : "bg-border"}`} />
        <If when={!!r().images?.length}>
          <View class="flex-row flex-wrap gap-2 pb-2">
            <Each of={r().images ?? []} key={(i) => i.data_url}>
              {(i) => <View class="gap-1"><Image src={i().data_url} style={{ width: 240, height: 180, objectFit: "contain" }} accessibilityLabel={i().name} /><Text class={META}>{i().name}</Text></View>}
            </Each>
          </View>
        </If>
        <View class="flex-row items-start gap-3">
          <Text style={big() && !full() ? { userSelect: "text", numberOfLines: 6 } : { userSelect: "text" }} class="flex-1 min-w-0 font-mono text-[15px] leading-6 text-foreground">{body()}</Text>
          <View class="flex-row items-center gap-1 pt-0.5">
            {/* Hover icons: kept mounted (keyboard reachable), shown while the card is hovered or focused. */}
            <View class={`flex-row items-center ${focusedBand() === r().key ? "" : "opacity-0 group-hover/band:opacity-100"}`}>
              <IconButton icon="copy" label="Copy message" onPress={copy} testID={`copy-user-${r().n}`} />
              <IconButton icon="chevron-down" label="Fold message" onPress={fold} testID={`fold-user-${r().n}`} />
            </View>
            <Text class={`${META} pt-1`}>{meta()}</Text>
          </View>
        </View>
      </View>
      <If when={big()}><Button onPress={() => toggle(`user:${r().key}`, false)} testID={`user-full-${r().n}`} class={`items-start py-3 ${TEXT_INDENT} ${FOCUS}`}><Text class={META}>{full() ? "▾ collapse message" : "▸ show full message (sent whole)"}</Text></Button></If>
      </View>
    }>
      <Button onPress={fold} accessibilityLabel={`${meta()} folded, show`} testID={`msg-user-folded-${r().n}`}
        class={`w-full flex-row items-center gap-2 rounded-(--radius-card) bg-raised min-h-9 hover:bg-accent ${CARD_INSET} ${FOCUS}`}>
        <Icon name="chevron-right" size={14} class="tint-muted-foreground" />
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 font-mono text-[14px] leading-[22px] text-foreground">{r().text}</Text>
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
  const list = (): Band[] => {
    const h = rowHeights();
    let top = LIST_PAD;
    const out: Band[] = [];
    for (const r of p.rows()) {
      const height = h[r.key] ?? ROW_ESTIMATE;
      if (r.kind === "user") out.push({ key: r.key, top, height, n: r.n, text: firstLine(r.text), turnKey: r.turnKey });
      top += height;
    }
    return out;
  };
  const current = () => {
    const y = scrollY();
    const all = list();
    let idx = -1;
    all.forEach((b, i) => { if (b.top + b.height <= y) idx = i; });
    return idx < 0 ? null : { band: all[idx], next: all[idx + 1] as Band | undefined };
  };
  return (
    <If when={!!current()}>
      {() => {
        const c = current()!;
        const y = scrollY();
        const lines = Math.max(0, Math.floor((Math.min(y, c.next?.top ?? Infinity) - c.band.top) / STICKY_LINE));
        const offset = c.next ? Math.min(0, c.next.top - y - STICKY_BAR) : 0;
        return (
          <View class="absolute left-0 right-0 items-center" style={{ top: offset, pointerEvents: "box-none" }} testID="sticky-user">
            <TimelineColumn>
              <Button onPress={() => p.scrollTo(c.band.top)} accessibilityLabel="Scroll to this message" testID="sticky-user-button"
                class={`w-full flex-row items-center gap-3 rounded-(--radius) bg-raised border-b border-border min-h-7 hover:bg-accent ${CARD_INSET} ${FOCUS}`}>
                <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground-secondary font-mono">{c.band.text}</Text>
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
  return tone === "add" ? "text-success" : tone === "del" ? "text-error" : tone === "info" ? "text-info" : tone === "error" ? "text-error" : tone === "accent" ? "text-primary" : "text-foreground-secondary";
}

/** Member label and the verb icon (the status glyph replaces it while running, failed or waiting). */
const VERB: Record<string, string> = {
  bash: "Shell", read: "Read", edit: "Edit", patch: "Edit", write: "Write", code: "Code", ask: "Ask",
  web_search: "Web search", web_fetch: "Fetch", grep: "Search", glob: "Find",
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
  return (
    <View class="py-0.5">
      <Button onPress={() => toggle(r().key, r().defaultOpen)} testID={`activity-${r().key.replaceAll(":", "-")}`}
        class={`w-full flex-row items-center gap-2 rounded-(--radius) ${ACTIVITY_CONTROL_INSET} min-h-7 hover:bg-accent ${FOCUS}`}>
        <Icon name={r().open ? "chevron-down" : "chevron-right"} size={14} class="tint-muted-foreground" />
        <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground-secondary font-mono">{r().summary}</Text>
        <If when={!!r().diff}>
          {() => <Text class={META}><Text class="text-success">{`+${r().diff!.add}`}</Text>{" "}<Text class="text-error">{`−${r().diff!.del}`}</Text></Text>}
        </If>
        <If when={r().failed > 0}><Text class="text-xs leading-4 text-error font-mono">{`✕ ${r().failed} failed`}</Text></If>
        <If when={!!r().duration}><Text class={META}>{r().duration!}</Text></If>
      </Button>
      <If when={r().open}>
        <View class="ml-[11px]">
          <If when={r().hidden > 0}><Text class={`${META} pl-[18px]`}>{`… ${r().hidden} earlier activities hidden`}</Text></If>
          <Each of={r().items} key={(item) => item.key}>
            {(item) => <ActivityItem item={item} last={() => r().items.at(-1)?.key === item().key} />}
          </Each>
        </View>
      </If>
    </View>
  );
}

/** A member of an expanded group: a tree branch (1 px line, 12 px tick) to the left of the row. */
function ActivityItem(p: { item: () => ActivityEntry; last: () => boolean }) {
  const item = p.item;
  return (
    <View class="relative pl-[18px]">
      <If when={p.last()} else={<View class="absolute left-0 top-0 bottom-0 w-px bg-border" />}>
        {() => <View class="absolute left-0 top-0 h-3 w-px bg-border" />}
      </If>
      <View class="absolute left-0 top-3 w-3 h-px bg-border" />
      <If when={item().kind === "tool"}>
        <ToolRow r={item as () => Extract<Row, { kind: "tool" }>} />
      </If>
      <If when={item().kind === "reasoning"}>
        <ReasoningRow x={item as () => Extract<Row, { kind: "reasoning" }>} />
      </If>
    </View>
  );
}

/** Thought rows (D07): `▸ Thought · preview`; running shows the last 5 lines behind a rule. */
function ReasoningRow(p: { x: () => Extract<Row, { kind: "reasoning" }> }) {
  const x = p.x;
  const lines = () => x().text.split("\n").filter((l) => l.trim().length > 0);
  const hidden = () => Math.max(0, lines().length - 5);
  const label = () => x().duration && !x().running ? `${roles.reasoning.label} for ${x().duration}` : roles.reasoning.label;
  return (
    <View class="py-0.5">
      <If when={x().running} else={
        <Button onPress={() => toggle(`reason:${x().key}`, false)} accessibilityLabel={`${label()}, ${x().open ? "collapse" : "expand"}`} testID="reasoning"
          class={`w-full flex-row items-center gap-2 rounded-(--radius) ${ACTIVITY_CONTROL_INSET} min-h-6 hover:bg-accent ${FOCUS}`}>
          <Icon name={x().open ? "chevron-down" : "chevron-right"} size={14} class="tint-muted-foreground" />
          <Text class={`text-[13px] leading-5 font-semibold text-muted-foreground font-mono`}>{label()}</Text>
          <If when={!!x().encrypted}>
            {() => <Text class="text-[13px] leading-5 italic text-muted-foreground font-mono">encrypted, replayed to the provider</Text>}
          </If>
          <If when={!x().encrypted}>
            {() => <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 text-[13px] leading-5 italic text-muted-foreground font-mono">{firstLine(x().text)}</Text>}
          </If>
        </Button>
      }>
        <View class={`flex-row items-center gap-2 ${ACTIVITY_CONTROL_INSET} min-h-6`} testID="reasoning">
          <Text class="text-[13px] leading-5 font-semibold text-foreground-secondary font-mono">{"Thinking…"}</Text>
        </View>
      </If>
      <If when={x().running || (x().open && !x().encrypted)}>
        <View class="ml-[7px] border-l-2 border-border pl-4 py-1">
          <If when={x().running && hidden() > 0}>
            {() => <Text class={META}>{`… ${hidden()} earlier lines`}</Text>}
          </If>
          <Text style={{ userSelect: "text" }} class="text-[13px] leading-5 italic text-foreground-secondary font-mono">
            {x().running ? lines().slice(-5).join("\n") : x().text}
          </Text>
        </View>
      </If>
    </View>
  );
}

/** One line of a member's detail output (the hidden-lines marker is shown as the foot instead). */
function ToolDetail(p: { line: () => ToolDetailLine }) {
  return <Text style={{ userSelect: "text" }} class={`font-mono text-xs leading-[18px] ${ToolTone(p.line().tone)}`}>{p.line().text}</Text>;
}

function ToolRow(p: { r: () => Extract<Row, { kind: "tool" }> }) {
  const r = p.r;
  const toggleRaw = () => toggle(`${r().key}:raw`, false);
  const markFocused = () => focusedTool.set({ key: r().key, clipped: r().clipped, toggleRaw });
  const clearFocused = () => { if (focusedTool()?.key === r().key) focusedTool.set(null); };
  const open = () => r().open;
  const live = () => r().state === "running" || r().state === "waiting" || r().state === "error";
  const stateKind = () => (r().state === "running" ? "running" : r().state === "waiting" ? "waiting" : r().state === "error" ? "error" : "done");
  const body = () => r().detail.filter((x) => !x.text.includes(HIDDEN_MARK));
  const isDiff = () => !r().raw && r().state !== "error" && (r().name === "edit" || r().name === "write" || r().name === "patch");
  const marker = () => r().detail.find((x) => x.text.includes(HIDDEN_MARK));
  const hidden = () => marker()?.text.match(/(\d+) lines hidden/)?.[1] ?? "";
  return (
    <View class="py-0.5">
      <View role="button" onFocus={markFocused} onBlur={clearFocused} onPress={() => { markFocused(); toggle(r().key, false); }} accessibilityLabel={`${r().name} ${r().state}`} accessibilityState={{ selected: open() }}
        class={`flex-row items-center gap-2 rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`} testID={`tool-row-${r().id}`}>
        <If when={live()} else={<Icon name={verbIcon(r().name)} size={16} class="tint-foreground-secondary" />}>
          {() => <Status kind={stateKind()} />}
        </If>
        <Text class="min-w-[52px] text-[13px] leading-5 font-medium text-foreground font-mono">{verbLabel(r().name)}</Text>
        <Text style={{ numberOfLines: 1, textOverflow: "ellipsis-middle" }} class="flex-1 min-w-0 font-mono text-xs leading-5 text-foreground-secondary">{r().arg}</Text>
        <If when={r().state !== "waiting" && !!r().summary}>
          <Text class={META}><Text class={r().summary.startsWith("+") ? "text-success" : r().summary.startsWith("−") ? "text-error" : "text-muted-foreground"}>{r().summary}</Text></Text>
        </If>
        <If when={r().state !== "waiting" && !!r().duration}><Text class={META}>{r().duration!}</Text></If>
        <If when={r().state === "waiting"}><Text class="text-[13px] text-primary font-mono">waiting</Text></If>
      </View>
      <If when={open()}>
        <View class="ml-[18px] mt-1 mb-1 rounded-(--radius) bg-card overflow-hidden">
          <View onFocus={markFocused} onBlur={clearFocused} class="flex-row items-center gap-2 h-7 px-3">
            <Text class="text-xs leading-4 font-semibold text-foreground font-mono">{verbLabel(r().name)}</Text>
            <Text style={{ numberOfLines: 1 }} class="flex-1 min-w-0 font-mono text-xs leading-4 text-foreground-secondary">{r().arg}</Text>
            <Button onPress={() => { markFocused(); toggleRaw(); }} class={`rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`} testID={`tool-raw-${r().id}`}>
              <Text class={META}>{r().raw ? "[raw]" : "raw · r"}</Text>
            </Button>
          </View>
          <Scroll class="max-h-[320px]" contentClass="px-3 py-2 gap-0.5">
            <If when={isDiff()} else={
              <Each of={body()} key={(x, i) => `${i}`}>
                {(x) => <ToolDetail line={x} />}
              </Each>
            }>
              {() => <DiffView lines={body} />}
            </If>
          </Scroll>
          <If when={r().media.length > 0}>
            <View class="flex-row flex-wrap gap-3 px-3 pb-2" testID={`tool-media-${r().id}`}>
              <Each of={r().media} key={(x) => x.artifact}>
                {(x) => <View class="gap-1"><Image src={`/api/artifact/${x().artifact}`} style={{ width: 320, height: 240, objectFit: "contain" }} accessibilityLabel={x().name || "image"} /><Text class={META}>{x().name || x().mime}</Text></View>}
              </Each>
            </View>
          </If>
          <If when={!!marker()}>
            {() => (
              <View onFocus={markFocused} onBlur={clearFocused} class="flex-row items-center gap-2 h-7 px-3">
                <Text class={META}>{`↑ ${hidden()} earlier lines`}</Text>
                <View class="flex-1" />
                <Button onPress={() => { markFocused(); void run("tool.show_all_output"); }} class={`rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`} testID={`tool-show-all-${r().id}`}>
                  <Text class="text-[13px] leading-5 font-medium text-primary font-mono">Show all</Text>
                </Button>
              </View>
            )}
          </If>
          <If when={r().clipped && r().showAll}>
            <View onFocus={markFocused} onBlur={clearFocused} class="flex-row items-center gap-2 h-7 px-3">
              <View class="flex-1" />
              <Button onPress={() => { markFocused(); void run("tool.show_all_output"); }} class={`rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`}>
                <Text testID={`tool-show-less-${r().id}`} class={META}>show less o</Text>
              </Button>
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
  const rows = () => buildRows();
  jump.fn = () => { try { ref?.scrollToEnd({ animated: true }); } catch {} };
  return (
    <View class="flex-1 min-h-0" testID="timeline">
      <If when={rows().length > 0} else={<EmptyTimeline />}>
        <List
          ref={(n: any) => (ref = n)} each={rows()} keyExtractor={(x) => x.key} estimatedItemSize={72} class="flex-1" contentStyle={{ paddingVertical: 12 }}
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
        <View class="absolute bottom-2 left-0 right-0 items-center" style={{ pointerEvents: "box-none" }}>
          <TimelineColumn class="items-end">
            <Button onPress={() => run("session.jump_latest")} accessibilityLabel={`Jump to latest, ${newBelow(rows())} new`} testID="jump-latest" class={`flex-row items-center gap-1.5 rounded-full border border-border bg-popover px-3 min-h-8 shadow-md hover:bg-accent ${FOCUS}`}>
              <Text class="text-[13px] leading-5 text-foreground-secondary">↓</Text>
              <Text class="text-[13px] leading-5 text-popover-foreground">{newBelow(rows()) > 0 ? `${newBelow(rows())} new` : "latest"}</Text>
              <Text class={META}>{`· ${keyLabel(byId.get("session.jump_latest")!) ?? "end"}`}</Text>
            </Button>
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
