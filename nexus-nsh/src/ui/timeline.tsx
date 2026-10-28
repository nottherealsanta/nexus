import { agentColor } from "../agent-color";
import { List, Text, View } from "nsh";
import { Button, TextView } from "nsh/base";
import { Btn, FOCUS, Icon, Status, T } from "./kit";
import { jump, run } from "../commands";
import { agentCatalog, defaultModel, isNarrow, m, model, modelCatalog, scrolledUp, selectSession, viewed } from "../store";
import type { CoreEvent, Message, Part } from "../core/api";
import { roles } from "../gen/theme";
import { signal } from "nsh";

type Tone = "info" | "accent" | "warning";
type TurnStatus = "running" | "done" | "error" | "interrupted" | "step_limit";

type Row =
  | { key: string; kind: "turn"; n: number; title: string; meta: string; status: TurnStatus; open: boolean }
  | { key: string; kind: "footer"; text: string; status: TurnStatus }
  | {
      key: string; kind: "context"; meta: string; reconstructed?: boolean; open: boolean;
      items: { key: string; name: string; chip?: string; tokens?: string; details: string[] }[];
      total: string; share?: string;
    }
  | { key: string; kind: "user"; text: string }
  | { key: string; kind: "notice"; text: string; label: string; tone: Tone }
  | { key: string; kind: "assistant"; text: string; streaming?: boolean }
  | { key: string; kind: "reasoning"; text: string; encrypted?: boolean; open: boolean }
  | {
      key: string; kind: "tool"; id: string; name: string; arg: string; summary: string; tone: Tone;
      state: "running" | "done" | "error" | "waiting"; duration?: string; output?: string;
      detail: { text: string; tone: "plain" | "add" | "del" | "info" | "error" }[];
      raw: string; open: boolean;
    }
  | { key: string; kind: "child"; childId?: string; agent: string; title: string; status: string; tone: Tone; body: string; open: boolean }
  | { key: string; kind: "diag"; text: string; label: string; detail?: string; tone: Tone }
  | { key: string; kind: "older"; text: string }
  | { key: string; kind: "empty" };

const NOTICE = /^\[(Nexus notice|Nexus recovery|Notification)/;
const closedTurns = signal<Record<string, boolean>>({});
const openBlocks = signal<Record<string, boolean>>({});
function open(key: string, fallback: boolean): boolean {
  const v = openBlocks()[key];
  return v === undefined ? fallback : v;
}
function toggle(key: string, fallback: boolean) {
  const now = open(key, fallback);
  openBlocks.update((o) => ({ ...o, [key]: !now }));
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
function estimate(chars: string): number { return Math.ceil(Array.from(chars).length / 4); }
function shareOf(tokens: number, window: number): string {
  const percent = tokens / window * 100;
  return percent > 0 && percent < 0.1 ? "<0.1%" : `${percent.toFixed(1)}%`;
}
function bareModel(ref: string): string { return ref.split("/").at(-1) ?? ref; }
function effectiveModelRef(): string {
  const s = viewed();
  if (s?.model) return String(s.model);
  const agent = agentCatalog().agents.find((a) => a.name === (s?.agent ?? "build"));
  return agent?.model || defaultModel();
}
function catalogModel(ref: string): any | undefined {
  const models = modelCatalog();
  return models.find((entry) => entry.ref === ref)
    ?? models.find((entry) => bareModel(String(entry.ref ?? "")) === bareModel(ref));
}
function turnCost(t: TurnMeta): string | undefined {
  const used = t.models.at(-1);
  if (!used) return undefined;
  const entry = catalogModel(used);
  if (!entry) return undefined;
  const input = entry.input_cost, output = entry.output_cost;
  if (typeof input !== "number" || !Number.isFinite(input) || typeof output !== "number" || !Number.isFinite(output)) return undefined;
  const cache = typeof entry.cache_read_cost === "number" && Number.isFinite(entry.cache_read_cost) ? entry.cache_read_cost : input;
  const fresh = Math.max(0, t.input - t.cached);
  const usd = (fresh * input + t.cached * cache + t.output * output) / 1_000_000;
  return usd < 0.01 ? "<$0.01" : `$${usd.toFixed(2)}`;
}
function toolNameLines(names: string[], maxChars: number): string[] {
  const rows = [""];
  let shown = 0;
  for (const raw of names) {
    const name = raw.length > maxChars ? `${raw.slice(0, Math.max(1, maxChars - 1))}…` : raw;
    const last = rows.length - 1;
    const addition = `${rows[last] ? "  " : ""}${name}`;
    if (rows[last].length + addition.length > maxChars) {
      if (rows.length === 2) break;
      rows.push(name);
    } else rows[last] += addition;
    shown++;
  }
  if (shown < names.length) {
    const more = `+${names.length - shown} more`;
    const last = rows.length - 1;
    const suffix = `${rows[last] ? "  " : ""}${more}`;
    if (rows[last].length + suffix.length > maxChars) {
      const keep = Math.max(0, maxChars - more.length - 2);
      rows[last] = `${rows[last].slice(0, keep).trimEnd()}  ${more}`;
    } else rows[last] += suffix;
  }
  return rows.filter(Boolean);
}
function fmtDuration(s: number): string {
  if (!isFinite(s) || s < 0.95) return "<1 s";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
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

type TurnMeta = {
  n: number; start: number; end: number; startTime: number; endTime: number;
  input: number; cached: number; output: number; reasoning: number; steps: number;
  tools: number; models: string[]; stop?: string; error?: { kind: string; message: string };
  endEvent: boolean; followed: boolean; running: boolean;
};
function turnMeta(msgs: Message[], events: CoreEvent[], busy: boolean): TurnMeta[] {
  const starts: number[] = [];
  msgs.forEach((m, i) => { if (m.role === "user" && !NOTICE.test(text(m))) starts.push(i); });
  return starts.map((start, n) => {
    const end = n + 1 < starts.length ? starts[n + 1] : msgs.length;
    const window = msgs.slice(start, end);
    const meta: TurnMeta = {
      n, start, end, startTime: msgs[start].created, endTime: window.length ? window[window.length - 1].created : msgs[start].created,
      input: 0, cached: 0, output: 0, reasoning: 0, steps: 0, tools: 0, models: [], endEvent: false,
      followed: n + 1 < starts.length, running: busy && n + 1 === starts.length,
    };
    const nextStart = n + 1 < starts.length ? msgs[starts[n + 1]].created : Infinity;
    for (const e of events) {
      if (e.time < meta.startTime || e.time > nextStart) continue;
      const d = e.data ?? {};
      switch (e.type) {
        case "usage":
          meta.input += d.input_tokens ?? 0; meta.cached += d.cached_input_tokens ?? 0;
          meta.output += d.output_tokens ?? 0; meta.reasoning += d.reasoning_tokens ?? 0;
          meta.endTime = Math.max(meta.endTime, e.time); break;
        case "step_start": meta.steps += 1; break;
        case "turn_end":
          meta.stop = d.stop; meta.endEvent = true;
          if (typeof d.steps === "number") meta.steps = Math.max(meta.steps, d.steps);
          meta.endTime = Math.max(meta.endTime, e.time); break;
        case "wire_request": {
          const model = d.body?.model;
          if (typeof model === "string" && !meta.models.includes(model)) meta.models.push(model);
          break;
        }
        case "session.error":
          meta.error = { kind: d.kind ?? "error", message: d.message ?? "" };
          meta.endTime = Math.max(meta.endTime, e.time); break;
        default: break;
      }
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
  if (t.stop || t.endEvent || t.followed) return "done";
  return "interrupted";
}
const STATUS_LABEL: Record<TurnStatus, string> = {
  running: "running", done: "done", error: "error", interrupted: "interrupted", step_limit: "step limit",
};

function diffCounts(args: string | undefined): { add: number; del: number } | null {
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
function detailOf(name: string, args: string | undefined, output: string | undefined, raw: boolean): { text: string; tone: "plain" | "add" | "del" | "info" | "error" }[] {
  if (raw) {
    const lines = (args ?? "").split("\n").slice(0, 60).map((l) => ({ text: l, tone: "plain" as const }));
    return [...lines, ...(output ?? "").split("\n").slice(0, 60).map((l) => ({ text: l, tone: "plain" as const }))];
  }
  if (name === "edit" || name === "patch") {
    try {
      const o = JSON.parse(args ?? "{}");
      if (typeof o.patch === "string") {
        return o.patch.split("\n").slice(0, 200).map((l: string) => ({
          text: l,
          tone: l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "info" : "plain",
        }));
      }
      const out: { text: string; tone: "add" | "del" | "info" }[] = [{ text: `@@ ${o.path ?? ""}`, tone: "info" }];
      for (const l of String(o.old_string ?? o.old ?? "").split("\n").slice(0, 100)) out.push({ text: `- ${l}`, tone: "del" });
      for (const l of String(o.new_string ?? o.new ?? "").split("\n").slice(0, 100)) out.push({ text: `+ ${l}`, tone: "add" });
      return out;
    } catch { /* fall through to the output */ }
  }
  if (name === "write") {
    try {
      const o = JSON.parse(args ?? "{}");
      const out: { text: string; tone: "add" | "info" }[] = [{ text: `+ ${o.path ?? ""}`, tone: "info" }];
      for (const l of String(o.content ?? "").split("\n").slice(0, 200)) out.push({ text: `+ ${l}`, tone: "add" });
      return out;
    } catch { /* fall through */ }
  }
  const lines = (output ?? "").split("\n");
  const out = lines.slice(0, 200).map((l) => ({ text: l, tone: "plain" as const }));
  if (lines.length > 200) out.push({ text: `… ${lines.length - 200} lines hidden · show all`, tone: "plain" as const });
  return out;
}

export function buildRows(): Row[] {
  m();
  const sel = model.selected;
  if (!sel) return [];
  const results = new Map<string, Part>();
  for (const msg of sel.messages) for (const p of msg.parts) if (p.type === "tool_result" && p.call_id) results.set(p.call_id, p);
  const traces = new Map<string, { start?: number; end?: number }>();
  for (const e of sel.events) {
    if (e.type === "tool_start") traces.set(e.data?.call?.id, { ...(traces.get(e.data?.call?.id) ?? {}), start: e.time });
    else if (e.type === "tool_end") traces.set(e.data?.call?.id, { ...(traces.get(e.data?.call?.id) ?? {}), end: e.time });
  }
  const waiting = new Set([...model.approvals.values()].map((a) => a.call_id));
  const busy = viewed()?.status === "busy";
  const metas = turnMeta(sel.messages, sel.events, busy);
  const sid = sel.sessionId;
  const rows: { t: number; r: Row[] }[] = [];

  // Starting context, before the first turn.
  if (sel.starting) {
    const s = sel.starting;
    const sys = String(s.system_prompt ?? "");
    const file = s.instruction_file ?? {};
    const agent = s.agent ?? {};
    const names: string[] = Array.isArray(s.tools) ? s.tools.filter((x: unknown): x is string => typeof x === "string") : [];
    const schemas = s.tool_definitions && typeof s.tool_definitions === "object" ? s.tool_definitions : {};
    const schemaText = Object.keys(schemas).length ? JSON.stringify(schemas) : "";
    const systemTokens = estimate(sys);
    const toolTokens = schemaText ? estimate(schemaText) : 0;
    const fileBytes = typeof file.bytes === "number" ? file.bytes : undefined;
    const fileTokens = fileBytes === undefined ? undefined : Math.ceil(fileBytes / 4);
    const total = systemTokens + toolTokens;
    const ref = effectiveModelRef();
    const rawWindow = Number(catalogModel(ref)?.context);
    const window = Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : undefined;
    const promptDetail = sys.split("\n").map((line: string) => line.trim()).find((line: string) => line && !line.startsWith("#")) ?? "";
    const fileStatus = file.status === "loaded" ? "loaded" : file.status === "missing" ? "not found" : String(file.status ?? "not read").replaceAll("_", " ");
    const children: any[] = Array.isArray(s.child_agents) ? s.child_agents : [];
    const childNames = children.map((c) => c?.name).filter((x): x is string => typeof x === "string");
    const items: Extract<Row, { kind: "context" }>["items"] = [
      { key: "system", name: "System prompt", tokens: `~${fmtCount(systemTokens)}`, details: promptDetail ? [promptDetail] : [] },
      {
        key: "tools", name: "Tools", chip: `[${names.length}]`, tokens: toolTokens ? `~${fmtCount(toolTokens)}` : undefined,
        details: toolNameLines(names, isNarrow() ? 38 : 92),
      },
      {
        key: "instructions", name: "Instructions", chip: `AGENTS.md ${fileStatus}`,
        tokens: fileTokens === undefined ? undefined : `~${fmtCount(fileTokens)}`,
        details: [
          `${file.path ?? ""}${fileBytes === undefined ? "" : `${file.path ? " · " : ""}${fileBytes} B, part of the system prompt`}`,
        ].filter(Boolean),
      },
      {
        key: "agent", name: "Agent", chip: String(agent.name ?? "?"),
        details: [`${agent.source ?? "?"}${agent.description ? ` · ${agent.description}` : ""}`],
      },
    ];
    if (children.length) items.push({ key: "children", name: "Child agents", chip: `[${children.length}]`, details: childNames.length ? [childNames.join("  ")] : [] });
    if (s.workspace?.active) items.push({ key: "workspace", name: "Workspace tools", chip: "active", details: [] });
    const share = window ? shareOf(total, window) : undefined;
    rows.push({
      t: -1,
      r: [{
        key: `context-${sid}`, kind: "context",
        meta: `${items.length} items · ~${fmtCount(total)} tokens${share ? ` · ${share} of window` : ""}`,
        reconstructed: s.origin === "reconstructed", open: open(`context:${sid}`, metas.length === 0),
        items, total: `~${fmtCount(total)}`, share,
      }],
    });
  }

  let turnAt = 0;
  let foldedTurn = false;
  for (const msg of sel.messages) {
    const out: Row[] = [];
    if (msg.role === "user") {
      const body = text(msg);
      if (NOTICE.test(body)) {
        out.push({
          key: msg.id, kind: "notice", text: body,
          label: body.startsWith("[Nexus recovery") ? roles.diagnostic.label : roles.context_notice.label,
          tone: body.startsWith("[Nexus recovery") ? "warning" : "info",
        });
      } else {
        const meta = metas[turnAt] ?? metas[metas.length - 1];
        const n = turnAt;
        turnAt += 1;
        const key = `turn:${sid}:${n}`;
        const folded = closedTurns()[key] ?? false;
        foldedTurn = folded;
        const status = meta ? statusOf(meta) : "running";
        const bits: string[] = [];
        if (meta?.tools) bits.push(`${meta.tools} tool${meta.tools === 1 ? "" : "s"}`);
        if (meta) bits.push(fmtDuration(meta.endTime - meta.startTime));
        out.push({ key: `${key}-head`, kind: "turn", n: n + 1, title: firstLine(body), meta: bits.join(" · "), status, open: !folded });
        if (folded) { rows.push({ t: msg.created, r: out }); continue; }
        out.push({ key: msg.id, kind: "user", text: body });
      }
    } else if (msg.role === "assistant") {
      if (foldedTurn) { rows.push({ t: msg.created, r: out }); continue; }
      msg.parts.forEach((p, i) => {
        const key = `${msg.id}-${i}`;
        if (p.type === "text" && (p.text ?? "").trim()) out.push({ key, kind: "assistant", text: p.text ?? "" });
        else if (p.type === "reasoning") {
          out.push({ key, kind: "reasoning", text: p.summary ?? "", encrypted: !(p.summary ?? "").trim(), open: open(`reason:${key}`, false) });
        } else if (p.type === "tool_call") {
          const id = p.id ?? key;
          if (p.name === "session") {
            let a: any = {};
            try { a = JSON.parse(p.arguments ?? "{}"); } catch {}
            const child = [...model.sessions.values()].find((c) => c.parent_id === sid && c.parent_call_id === id);
            const res = results.get(id);
            const waits = child ? [...model.approvals.values(), ...model.questions.values()].some((w) => w.session_id === child.id) : false;
            const running = child ? model.running.has(child.id) : false;
            const status = waits ? "waiting for input" : running ? "running" : res?.is_error ? "failed" : res ? "done" : "starting…";
            const body = res ? firstLine(res.output ?? "") : waits ? "waiting for input" : running ? "running…" : "starting…";
            const key = `child:${id}`;
            out.push({
              key, kind: "child", childId: child?.id, agent: a.agent ?? "child",
              title: a.title ?? firstLine(a.prompt ?? "") ?? child?.title ?? "", status,
              tone: waits ? "accent" : running ? "info" : res?.is_error ? "warning" : "info",
              body, open: open(key, false),
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
            else if (p.name === "write") { try { summary = `+${String(JSON.parse(p.arguments ?? "{}").content ?? "").split("\n").length}`; } catch {} }
            else if (res?.output) summary = `${(res.output.match(/\n/g)?.length ?? 0) + 1} lines`;
            const dur = tr?.start !== undefined && tr?.end !== undefined ? fmtDuration(tr.end - tr.start) : undefined;
            const key = `tool:${id}`;
            const rawOpen = open(`${key}:raw`, false);
            out.push({
              key, kind: "tool", id, name: p.name ?? "tool", arg: toolArg(p.arguments), summary,
              tone: counts ? (counts.add >= 0 ? "accent" : "info") : "info",
              state, duration: dur, output: res?.output,
              detail: detailOf(p.name ?? "tool", p.arguments, res?.output, rawOpen),
              raw: rawOpen ? JSON.stringify({ arguments: p.arguments ?? "", output: res?.output ?? "" }, null, 2) : "",
              open: open(key, false),
            });
          }
        }
      });
    } // tool-role messages are shown through their call's row
    rows.push({ t: msg.created, r: out });
    // A turn's footer belongs after its last message.
    if (turnAt > 0 && (msg === sel.messages[sel.messages.length - 1] || sel.messages[sel.messages.indexOf(msg) + 1]?.role === "user")) {
      const meta = metas[turnAt - 1];
      if (meta && !foldedTurn) {
        const v = viewed();
        const bits: string[] = [];
        if (meta.models.length) bits.push(meta.models.join(" / "));
        else if (v?.model && String(v.model) !== "default") bits.push(String(v.model));
        if (v?.effort) bits.push(String(v.effort));
        bits.push(fmtDuration(meta.endTime - meta.startTime));
        if (meta.steps) bits.push(`${meta.steps} call${meta.steps === 1 ? "" : "s"}`);
        if (meta.tools) bits.push(`${meta.tools} tool${meta.tools === 1 ? "" : "s"}`);
        if (meta.input + meta.cached + meta.output + meta.reasoning > 0) {
          let tok = `${fmtCount(meta.input)} in`;
          if (meta.cached) tok += ` ${fmtCount(meta.cached)} cached`;
          tok += ` ${fmtCount(meta.output)} out`;
          if (meta.reasoning) tok += ` ${fmtCount(meta.reasoning)} reasoning`;
          bits.push(tok);
          const cost = turnCost(meta);
          if (cost) bits.push(cost);
        }
        rows.push({ t: msg.created + 0.0001, r: [{ key: `footer:${sid}:${meta.n}`, kind: "footer", text: bits.join(" · "), status: statusOf(meta) }] });
      }
    }
  }
  for (const e of sel.events) {
    if (!DIAG_TYPES.has(e.type)) continue;
    const r = diag(e);
    if (r) rows.push({ t: e.time, r: [r] });
  }
  rows.sort((a, b) => a.t - b.t);
  const flat = rows.flatMap((x) => x.r);
  if (sel.starting) {
    // Context is always first, even when the first turn starts immediately.
    const i = flat.findIndex((r) => r.kind === "context");
    if (i > 0) flat.unshift(...flat.splice(i, 1));
  }
  const st = model.streaming.get(sel.sessionId);
  if (st?.reasoning) flat.push({ key: "stream-r", kind: "reasoning", text: st.reasoning, open: true });
  if (st?.text) flat.push({ key: "stream-t", kind: "assistant", text: st.text, streaming: true });
  if (sel.eventsGap) flat.unshift({ key: "older-events", kind: "older", text: `${sel.eventsGap[0].toUpperCase()}${sel.eventsGap.slice(1)}.` });
  return flat;
}

const RAIL: Record<string, string> = { info: "bg-info", accent: "bg-primary", warning: "bg-warning" };
function Rail(p: { color: string }) { return <View class={`w-[3px] self-stretch rounded-full ${p.color}`} />; }

function StatusPill(p: { status: TurnStatus }) {
  const kind = p.status === "running" ? "running" : p.status === "done" ? "done" : p.status === "error" ? "error" : "interrupted";
  return <Status kind={kind} word />;
}

function RowView(p: { r: () => Row }) {
  const r = p.r;
  return (
    <View class="w-full items-center px-4">
      <View class="w-full max-w-[760px]">
        <If when={r().kind === "context"}>
          {() => {
            const c = r() as Extract<Row, { kind: "context" }>;
            return (
              <View class="py-2" testID="context-section">
                <Button onPress={() => toggle(`context:${model.selected?.sessionId}`, model.selected?.messages.length === 0)}
                  class={`w-full flex-row items-center gap-2 rounded-(--radius) px-1 min-h-7 hover:bg-accent ${FOCUS}`} testID="context-header">
                  <Text class={T.meta}>{c.open ? "▾" : "▸"}</Text>
                  <Text class="text-[13px] leading-5 font-medium text-foreground-secondary">Context</Text>
                  <Text class={`flex-1 ${T.meta}`}>{c.meta}</Text>
                  <If when={!!c.reconstructed}><Text class="text-xs text-warning">reconstructed</Text></If>
                </Button>
                <If when={c.open}>
                  <View class="flex-row gap-3 pl-3 pt-2">
                    <View class="w-px self-stretch bg-border" />
                    <View class="flex-1 min-w-0 gap-2">
                      <Each of={c.items} key={(x) => x.key}>
                        {(x) => <ContextItem item={x} />}
                      </Each>
                      <View class="flex-row items-center gap-2">
                        <Text class="text-[13px] leading-5 text-muted-foreground">total</Text>
                        <Text class={`font-medium ${T.sec}`}>{`${c.total} tokens`}</Text>
                        <If when={!!c.share}><Text class={`ml-auto ${T.meta}`}>{`${c.share} of the window`}</Text></If>
                      </View>
                    </View>
                  </View>
                </If>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "turn"}>
          {() => {
            const t = r() as Extract<Row, { kind: "turn" }>;
            const key = `turn:${model.selected?.sessionId}:${t.n - 1}`;
            return (
              <Button onPress={() => closedTurns.update((o) => ({ ...o, [key]: !(o[key] ?? false) }))}
                class={`w-full flex-row items-center gap-2 rounded-(--radius) px-1 min-h-7 mt-3 hover:bg-accent ${FOCUS}`} testID="turn-header">
                <Text class={T.meta}>{t.open ? "▾" : "▸"}</Text>
                <Text class={T.meta}>{`#${t.n}`}</Text>
                <Text numberOfLines={1} class="flex-1 min-w-0 text-[13px] leading-5 font-medium text-foreground">{t.title}</Text>
                <If when={!!t.meta}><Text class={T.meta}>{t.meta}</Text></If>
                <StatusPill status={t.status} />
              </Button>
            );
          }}
        </If>
        <If when={r().kind === "footer"}>
          {() => {
            const f = r() as Extract<Row, { kind: "footer" }>;
            return (
              <View class="flex-row items-center gap-2 pt-2 pb-1 flex-wrap" testID="turn-footer">
                <View class="w-full h-px bg-border" />
                <Text class={`flex-1 min-w-0 ${T.meta}`}>{f.text}</Text>
                <StatusPill status={f.status} />
              </View>
            );
          }}
        </If>
        <If when={r().kind === "user"}>
          <View class="flex-row gap-3 rounded-(--radius-card) bg-raised border border-border px-3.5 py-3" testID="msg-user">
            <Rail color="bg-primary" />
            <Text selectable class={`flex-1 min-w-0 ${T.body}`}>{(r() as any).text}</Text>
          </View>
        </If>
        <If when={r().kind === "notice"}>
          <View class="flex-row gap-3 rounded-(--radius-card) bg-card border border-border px-3.5 py-2.5 my-1">
            <Rail color={RAIL[(r() as any).tone] ?? "bg-info"} />
            <View class="flex-1 min-w-0 gap-0.5">
              <Text class="text-xs leading-4 font-medium text-muted-foreground">{(r() as any).label}</Text>
              <Text selectable class={T.sec}>{(r() as any).text}</Text>
            </View>
          </View>
        </If>
        <If when={r().kind === "assistant"}>
          <View class="py-1.5" testID="msg-assistant">
            <TextView selectable markdown={(r() as any).text} options={{ streaming: !!(r() as any).streaming }} classes={{ root: "text-[14px] leading-[22px] text-foreground", code: "font-mono text-[13px] bg-card", codeBlock: "rounded-(--radius) border border-border bg-card" }} />
          </View>
        </If>
        <If when={r().kind === "reasoning"}>
          {() => {
            const x = r() as Extract<Row, { kind: "reasoning" }>;
            return (
              <View class="py-1.5">
                <Button onPress={() => toggle(`reason:${x.key}`, false)} class={`flex-row items-center gap-2 rounded-(--radius) px-1 min-h-7 hover:bg-accent ${FOCUS}`} testID="reasoning">
                  <Text class={T.meta}>{x.open ? "▾" : "▸"}</Text>
                  <Text class="text-xs leading-4 italic text-muted-foreground">{roles.reasoning.label}{x.encrypted ? " · encrypted (replayed, not readable)" : ""}</Text>
                </Button>
                <If when={x.open && !x.encrypted}>
                  <View class="pl-5 pt-1"><Text selectable class="text-[13px] leading-5 italic text-muted-foreground">{x.text}</Text></View>
                </If>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "tool"}>
          <ToolRow r={r as () => Extract<Row, { kind: "tool" }>} />
        </If>
        <If when={r().kind === "child"}>
          {() => {
            const c = r() as Extract<Row, { kind: "child" }>;
            return (
              <View class="flex-row rounded-(--radius-card) bg-card border border-border overflow-hidden my-1">
                <Rail color={RAIL[c.tone] ?? "bg-info"} />
                <View class="flex-1 min-w-0 gap-1 px-3 py-2">
                  <Button onPress={() => c.childId && void selectSession(c.childId)} disabled={!c.childId}
                    class={`flex-row items-center gap-2 rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`} testID="child-card">
                    <Status kind={c.tone === "accent" ? "waiting" : c.status === "running" ? "running" : "done"} />
                    <Text class="text-xs leading-4 text-muted-foreground">child</Text>
                    <Text class={`text-[13px] leading-5 ${agentColor(c.agent)}`}>{c.agent}</Text>
                    <Text numberOfLines={1} class="flex-1 min-w-0 text-[13px] leading-5 text-foreground-secondary">{c.title ? `“${c.title}”` : ""}</Text>
                    <Text class={T.meta}>{c.status}</Text>
                  </Button>
                  <Text numberOfLines={2} class={`${T.meta} pl-6`}>{c.body}</Text>
                </View>
              </View>
            );
          }}
        </If>
        <If when={r().kind === "diag"}>
          <View class="flex-row gap-3 rounded-(--radius-card) bg-card border border-border px-3.5 py-2.5 my-1" testID="msg-diag">
            <Rail color={RAIL[(r() as any).tone] ?? "bg-warning"} />
            <View class="flex-1 min-w-0 gap-0.5">
              <Text class="text-xs leading-4 font-medium text-muted-foreground">{(r() as any).label}</Text>
              <Text selectable class={T.sec}>{(r() as any).text}</Text>
              <If when={(r() as any).detail}><Text class={T.meta}>{(r() as any).detail}</Text></If>
            </View>
          </View>
        </If>
        <If when={r().kind === "older"}><Text class={`${T.meta} py-2`}>{(r() as any).text}</Text></If>
      </View>
    </View>
  );
}

function ContextItem(p: { item: () => Extract<Row, { kind: "context" }>["items"][number] }) {
  const item = p.item;
  return (
    <View class="min-w-0 gap-0.5">
        <View class="flex-row items-center gap-1.5">
          <Text class="text-[13px] leading-5 font-semibold text-foreground-secondary">{item().name}</Text>
          <If when={!!item().chip}><Text class={T.meta}>{item().chip!}</Text></If>
          <View class="flex-1" />
          <If when={!!item().tokens}><Text class={T.meta}>{item().tokens!}</Text></If>
        </View>
        <Each of={item().details} key={(x, i) => `${item().key}-${i}`}>
          {(detail) => <Text class={T.meta} numberOfLines={2}>{detail()}</Text>}
        </Each>
    </View>
  );
}

function ToolTone(tone: string): string {
  return tone === "add" ? "text-success" : tone === "del" ? "text-error" : tone === "info" ? "text-info" : tone === "error" ? "text-error" : "text-foreground-secondary";
}

function ToolRow(p: { r: () => Extract<Row, { kind: "tool" }> }) {
  const r = p.r;
  const open = () => r().open;
  const stateKind = () => (r().state === "running" ? "running" : r().state === "waiting" ? "waiting" : r().state === "error" ? "error" : "done");
  return (
    <View class="py-0.5">
      <Button onPress={() => toggle(r().key, false)} accessibilityLabel={`${r().name} ${r().state}`} selected={open()}
        class={`flex-row items-center gap-1.5 rounded-(--radius) px-1 min-h-7 hover:bg-accent ${FOCUS}`} testID="tool-row">
        <Status kind={stateKind()} />
        <Text class="w-[52px] text-[13px] leading-5 font-medium text-foreground">{r().name}</Text>
        <Text numberOfLines={1} ellipsizeMode="middle" class="flex-1 min-w-0 font-mono text-xs leading-5 text-foreground-secondary">{r().arg}</Text>
        <If when={r().state !== "waiting" && !!r().summary}>
          <Text class={T.meta}><Text class={r().summary.startsWith("+") ? "text-success" : r().summary.startsWith("−") ? "text-error" : "text-muted-foreground"}>{r().summary}</Text></Text>
        </If>
        <If when={r().state !== "waiting" && !!r().duration}><Text class={T.meta}>{r().duration}</Text></If>
        <If when={r().state === "waiting"}><Text class="text-[13px] text-primary">waiting</Text></If>
      </Button>
      <If when={open()}>
        <View class="ml-6 mt-1 mb-1 rounded-(--radius) border border-border bg-card px-3 py-2 gap-0.5">
          <Each of={r().detail} key={(x, i) => `${i}`}>
            {(x) => <Text selectable class={`font-mono text-xs leading-[18px] ${ToolTone(x().tone)}`}>{x().text}</Text>}
          </Each>
          <Button onPress={() => toggle(`${r().key}:raw`, false)} class={`self-start mt-1 rounded-(--radius) px-1 min-h-6 hover:bg-accent ${FOCUS}`}>
            <Text class={T.meta}>{r().raw ? "[raw]" : "raw · r"}</Text>
          </Button>
        </View>
      </If>
    </View>
  );
}

export function Timeline() {
  let ref: any;
  const rows = () => buildRows();
  jump.fn = () => { try { ref?.scrollToEnd({ animated: true }); } catch {} };
  return (
    <View class="flex-1 min-h-0" testID="timeline">
      <If when={rows().length > 0} else={<EmptyTimeline />}>
        <List
          ref={(n: any) => (ref = n)} each={rows()} keyExtractor={(x) => x.key} estimatedItemSize={72} class="flex-1" contentClass="py-3"
          maintainVisibleContentPosition={{ autoscrollToBottomThreshold: 96 }}
          onScroll={(e) => scrolledUp.set(e.contentSize.height - (e.offset.y + e.size.height) > 160)}
          scrollEventThrottle={64}
        >
          {(r) => <RowView r={r} />}
        </List>
      </If>
      <If when={scrolledUp()}>
        <View class="absolute bottom-3 left-0 right-0 items-center" pointerEvents="box-none">
          <Button onPress={() => run("session.jump_latest")} accessibilityLabel="Jump to latest" testID="jump-latest" class={`flex-row items-center gap-1.5 rounded-full border border-border bg-popover px-3 min-h-8 shadow-md hover:bg-accent ${FOCUS}`}>
            <Icon name="arrow-up" size={14} class="tint-foreground-secondary rotate-180" />
            <Text class="text-[13px] leading-5 text-popover-foreground">Jump to latest</Text>
          </Button>
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
void Btn; void isNarrow;
