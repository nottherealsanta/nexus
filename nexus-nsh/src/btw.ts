import { signal } from "nsh";
import type { CoreEvent } from "./core/api";
import { ApiError, newKey, request } from "./core/transport";

export type Btw = { parent: string; question: string; request_id: string; session?: string; inherited: number; inherited_context: string; inherited_open: boolean; context_open: boolean; calls: number; tools: Record<string, string>; messages: any[]; answer: string; running: boolean; head: number; partials: Record<string, number> };
export const btw = signal<Btw | null>(null);
let timer: ReturnType<typeof setTimeout> | undefined;
export function closeBtw() {
  const run = btw();
  btw.set(null);
  clearTimeout(timer);
  if (run?.running && run.session) void request("POST", `/api/session/${run.session}/interrupt`).catch(() => {});
}
export async function startBtw(parent: string, question: string) {
  closeBtw();
  const request_id = newKey();
  btw.set({ parent, question, request_id, inherited: 0, inherited_context: "", inherited_open: false, context_open: false, calls: 0, tools: {}, messages: [], answer: "", running: true, head: 0, partials: {} });
  try {
    const r = await request<{ session: string; inherited: number; inherited_context: string }>("POST", `/api/session/${parent}/btw`, { body: { question, request_id } });
    if (btw()?.request_id !== request_id) { void request("POST", `/api/session/${r.session}/interrupt`).catch(() => {}); return; }
    btw.set({ ...btw()!, session: r.session, inherited: r.inherited, inherited_context: r.inherited_context });
    void refreshBtw(request_id);
  } catch (e) {
    if (btw()?.request_id === request_id) btw.set({ ...btw()!, running: false, answer: String(e) });
  }
}
function toolEvent(run: Btw, type: string, d: any) {
  if (type === "code.call") run.tools[`${d.parent_call_id}:${d.cell}:${d.index}`] = `- \`${d.tool ?? "tool"}\` · ${d.error ? "refused or failed" : "done"} (nested)`;
  else if (d.call?.id) run.tools[d.call.id] = `- \`${d.call.name ?? "tool"}\` · ${type === "tool_start" ? "running" : d.result?.is_error ? "refused or failed" : "done"}`;
}
export function receiveBtw(e: CoreEvent) {
  const run = btw(); if (!run) return;
  const d = e.data ?? {};
  if (e.type === "btw.started" && e.session_id === run.parent && d.request_id === run.request_id) {
    btw.set({ ...run, session: d.session, inherited: d.inherited }); return;
  }
  if (!run.session || e.session_id !== run.session || (e.id != null && e.id > 0 && e.id <= run.head)) return;
  const next = { ...run, tools: { ...run.tools }, partials: { ...run.partials }, messages: [...run.messages] };
  if (e.id != null && e.id > 0) next.head = e.id;
  if (d.request_id && typeof d.seq === "number") {
    if (d.seq <= (next.partials[d.request_id] ?? 0)) return;
    next.partials[d.request_id] = d.seq;
  }
  switch (e.type) {
    case "btw.budget": next.calls = d.calls; break;
    case "step_start": next.answer = ""; break;
    case "tool_start": case "tool_end": case "code.call": toolEvent(next, e.type, d); break;
    case "text_delta": next.answer += d.text ?? ""; break;
    case "message.added": if (!next.messages.some((m) => m.id === d.id)) next.messages.push(d); if (d.role === "assistant") next.answer = ""; break;
    case "session.error": next.answer = d.message ?? "Side question failed"; break;
    case "session.status": if (d.status !== "idle") return; next.running = false; break;
    default: return;
  }
  btw.set(next);
}
export function applyBtwState(run: Btw, state: any): Btw {
  if (state.head < run.head) return run;
  const p = state.partial;
  if (p && (run.partials[p.request_id] ?? 0) > p.seq) return run;
  const next = { ...run, head: state.head, inherited: state.inherited, calls: state.calls, running: state.running, tools: {}, messages: state.messages, partials: { ...run.partials }, answer: state.error ?? (p?.parts ?? []).filter((p: any) => p.type === "text").map((p: any) => p.text).join("\n") };
  if (p) next.partials[p.request_id] = p.seq;
  for (const e of state.events ?? []) toolEvent(next, e.type, e.data);
  return next;
}
async function refreshBtw(request_id: string) {
  const run = btw(); if (!run?.session || run.request_id !== request_id) return;
  let finished = false;
  try {
    const state = await request<any>("GET", `/api/session/${run.session}/btw`);
    if (btw()?.request_id !== request_id) return;
    const next = applyBtwState(btw()!, state);
    btw.set(next); finished = !next.running;
  } catch (e) {
    if (btw()?.request_id !== request_id) return;
    if (e instanceof ApiError && e.status === 404) { btw.set({ ...btw()!, running: false, answer: "Side question is no longer available." }); finished = true; }
  }
  if (!finished && btw()?.request_id === request_id) timer = setTimeout(() => void refreshBtw(request_id), 1000);
}
export function toggleBtwContext() { if (btw()) btw.set({ ...btw()!, inherited_open: !btw()!.inherited_open }); }
export function toggleBtwTranscript() { if (btw()) btw.set({ ...btw()!, context_open: !btw()!.context_open }); }
export function btwParts(run: Btw) {
  const transcript = run.messages.map((m) => `\n\n**${m.role}**\n\n${(m.parts ?? []).map((p: any) => p.type === "text" ? p.text : `\n\`\`\`json\n${JSON.stringify(p, null, 2)}\n\`\`\`\n`).join("")}`).join("");
  const answers = run.messages.filter((m) => m.role === "assistant").flatMap((m) => m.parts ?? []).filter((p: any) => p.type === "text").map((p: any) => p.text).join("\n\n");
  return {
    inheritedLine: `Inherited context · ${run.inherited} messages · [i] ${run.inherited_open ? "collapse" : "expand"}`,
    inheritedBody: run.inherited_open ? `\`\`\`json\n${run.inherited_context}\n\`\`\`` : "",
    middle: `Read-only side question · ${run.calls}/10 tool calls · ${run.running ? "Running…" : "Finished"}\n\n${Object.values(run.tools).join("\n")}\n\n${answers}\n\n${run.answer}`,
    contextLine: `Context · [c] ${run.context_open ? "collapse" : "expand"}`,
    contextBody: run.context_open ? transcript.trim() : "",
    tail: "Not added to the session's context.",
  };
}
export function btwDetail(run: Btw): string {
  const p = btwParts(run);
  return `${p.inheritedLine}${p.inheritedBody ? `\n\n${p.inheritedBody}` : ""}\n\n${p.middle}\n\n${p.contextLine}${p.contextBody ? `\n\n${p.contextBody}` : ""}\n\n${p.tail}`;
}
