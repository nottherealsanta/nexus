// What the views show about turns, from turn summaries (`GET …/turn`, `turn.summary`) and, for the
// running turn only, the live scratch events. Pure: no store, no UI. Unknown usage stays unknown.
import type { CoreEvent, Message, TokenUsage, TurnSummary, UsageLedger } from "./api";

/** One provider request as the views need it. `usage: null` is unknown (or not reported yet). */
export interface RequestView { step: number | null; model: string | null; usage: TokenUsage | null; running: boolean; time: number }

const tokens = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0);

/** A live `usage` event's counts in summary form. */
export function usageOfEvent(e: CoreEvent): TokenUsage {
  const d = e.data ?? {};
  return { input: tokens(d.input_tokens), cached: tokens(d.cached_input_tokens), output: tokens(d.output_tokens), reasoning: tokens(d.reasoning_tokens) };
}

export function requestsOf(t: TurnSummary): RequestView[] {
  return (t.requests ?? []).map((r) => ({
    step: r.step ?? null, model: r.model ?? null, usage: r.usage ?? null,
    running: r.status === "running", time: r.finished ?? r.started ?? t.started,
  }));
}

/** The requests the scratch shows (`step_start`, `wire_request`, `usage`), in order. */
export function liveRequests(scratch: CoreEvent[]): RequestView[] {
  const out: RequestView[] = [];
  let cur: RequestView | null = null;
  for (const e of scratch) {
    if (e.type === "step_start") {
      cur = { step: typeof e.data?.step === "number" ? e.data.step : null, model: null, usage: null, running: true, time: e.time };
      out.push(cur);
    } else if (e.type === "wire_request" || e.type === "usage") {
      if (!cur) { cur = { step: null, model: null, usage: null, running: true, time: e.time }; out.push(cur); }
      if (e.type === "wire_request" && typeof e.data?.body?.model === "string") cur.model = e.data.body.model;
      if (e.type === "usage") { cur.usage = usageOfEvent(e); cur.running = false; cur.time = e.time; }
    }
  }
  return out;
}

/**
 * The running turn's requests: its summary (when one was loaded) completed by the live ones.
 * A live request joins the summary's request of the same step (or, without a step seen, the
 * newest one still missing usage); the summary's usage wins where both have it.
 */
export function mergeLive(summary: RequestView[], live: RequestView[]): RequestView[] {
  const out = summary.map((r) => ({ ...r }));
  for (const l of live) {
    const same = l.step !== null
      ? out.find((r) => r.step === l.step)
      : [...out].reverse().find((r) => r.usage === null);
    if (!same) { out.push({ ...l }); continue; }
    if (!same.usage && l.usage) { same.usage = l.usage; same.running = false; same.time = l.time; }
    if (!same.model && l.model) same.model = l.model;
  }
  return out;
}

export interface UsageSum extends TokenUsage {
  /** Requests whose usage is known. */
  known: number;
  /** Finished requests that reported no usage. */
  unknown: number;
}
export function sumUsage(reqs: RequestView[]): UsageSum {
  const s: UsageSum = { input: 0, cached: 0, output: 0, reasoning: 0, known: 0, unknown: 0 };
  for (const r of reqs) {
    if (r.usage) {
      s.known++;
      s.input += tokens(r.usage.input); s.cached += tokens(r.usage.cached);
      s.output += tokens(r.usage.output); s.reasoning += tokens(r.usage.reasoning);
    } else if (!r.running) s.unknown++;
  }
  return s;
}

/** The summaries whose turn starts within the messages `[fromSeq, toSeq)` (a turn's first message is `start_seq + 1`). */
export function turnsIn(turns: TurnSummary[], fromSeq: number, toSeq: number): TurnSummary[] {
  return turns.filter((t) => t.start_seq + 1 >= fromSeq && t.start_seq + 1 < toSeq);
}

/** Requests of a group of turns, with the scratch folded into its running (or not yet summarised) turn. */
export function groupRequests(group: TurnSummary[], scratch: CoreEvent[] | null): RequestView[] {
  const closed = group.filter((t) => t.status !== "running").flatMap(requestsOf);
  if (!scratch) return [...closed, ...group.filter((t) => t.status === "running").flatMap(requestsOf)];
  const running = group.filter((t) => t.status === "running").flatMap(requestsOf);
  return [...closed, ...mergeLive(running, liveRequests(scratch))];
}

/** The newest request with known usage: the context occupancy (`usage.input`) and its model. */
export function latestUsage(turns: TurnSummary[], scratch: CoreEvent[]): { input: number; model: string | null } | null {
  const live = liveRequests(scratch).filter((r) => r.usage);
  const last = live.at(-1);
  if (last?.usage) {
    const model = last.model ?? [...turns].reverse().flatMap((t) => [...(t.requests ?? [])].reverse()).find((r) => r.model)?.model ?? null;
    return { input: last.usage.input, model };
  }
  for (let i = turns.length - 1; i >= 0; i--) {
    const reqs = turns[i].requests ?? [];
    for (let j = reqs.length - 1; j >= 0; j--) if (reqs[j].usage) return { input: reqs[j].usage!.input, model: reqs[j].model ?? null };
  }
  return null;
}

/** Per-model totals: the ledger plus live `usage` events it has not counted yet (id after `mark`). */
export function ledgerTotals(ledger: UsageLedger | null, scratch: CoreEvent[], mark: number, fallbackModel: string): Map<string, TokenUsage & { requests: number }> {
  const out = new Map<string, TokenUsage & { requests: number }>();
  for (const [model, row] of Object.entries(ledger?.models ?? {})) {
    out.set(model, { requests: tokens(row.requests), input: tokens(row.input), cached: tokens(row.cached), output: tokens(row.output), reasoning: tokens(row.reasoning) });
  }
  let model = fallbackModel;
  for (const e of scratch) {
    if (e.type === "wire_request" && typeof e.data?.body?.model === "string") model = e.data.body.model;
    if (e.type !== "usage" || e.id <= mark) continue;
    const u = usageOfEvent(e);
    const row = out.get(model) ?? { requests: 0, input: 0, cached: 0, output: 0, reasoning: 0 };
    row.requests++; row.input += u.input; row.cached += u.cached; row.output += u.output; row.reasoning += u.reasoning;
    out.set(model, row);
  }
  return out;
}

/** Finished requests in the loaded summaries that reported no usage. */
export function unknownRequests(turns: TurnSummary[]): number {
  let n = 0;
  for (const t of turns) for (const r of t.requests ?? []) if (!r.usage && r.status !== "running") n++;
  return n;
}

/** Tool timing by call id: summaries, then live `tool_start`/`tool_end` of the running turn. */
export function toolTimes(turns: TurnSummary[], scratch: CoreEvent[]): Map<string, { start?: number; end?: number; isError?: boolean }> {
  const out = new Map<string, { start?: number; end?: number; isError?: boolean }>();
  for (const t of turns) for (const tool of t.tools ?? []) {
    out.set(tool.call_id, { start: tool.started ?? undefined, end: tool.finished ?? undefined, isError: tool.is_error });
  }
  for (const e of scratch) {
    const id = e.data?.call?.id;
    if (!id) continue;
    if (e.type === "tool_start") out.set(id, { ...(out.get(id) ?? {}), start: e.time });
    else if (e.type === "tool_end") out.set(id, { ...(out.get(id) ?? {}), end: e.time });
  }
  return out;
}

/** Where the floor row goes: before the first message the summaries cover, or after all of them. */
export function floorIndex(messages: Message[], floor: { seq: number } | null): number | null {
  if (!floor || !messages.some((m) => m.seq < floor.seq)) return null;
  const i = messages.findIndex((m) => m.seq >= floor.seq);
  return i < 0 ? messages.length : i;
}
