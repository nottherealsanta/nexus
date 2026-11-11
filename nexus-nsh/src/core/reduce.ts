// Pure client model: a cache of what Core says (port of nexus-tui/src/reduce.rs).
//  * A snapshot replaces everything and sets the watermark; events at or below `lastId` are duplicates.
//  * Id gaps are never loss. Events from another generation, or before the first snapshot, need a resync.
//  * History is messages plus turn summaries (`GET …/turn`); the event log is never loaded. The
//    selected view keeps the live events since the last `turn.summary` as scratch (deltas are
//    folded into `streaming`, never kept), and a `turn.summary` replaces that turn's scratch.
//  * Views of recently viewed sessions stay in `cached` and keep taking their sessions' events,
//    so switching back shows them at once; selecting one still reloads its history, which
//    merges into what is held.
import { Approval, CoreEvent, Message, Project, Question, Session, Snapshot, TurnSummary, UsageLedger, formatCursor, parseCursor } from "./api";
import { LivePartial } from "./livepartial";

export type Applied = "applied" | "duplicate" | { resync: string };

export interface Streaming { text: string; reasoning: string }
export interface SelectedView {
  sessionId: string;
  messages: Message[];
  messagesMore: boolean;
  /** Turn summaries, oldest first (by `started`). */
  turns: TurnSummary[];
  /** Messages below this seq have no summary (pruned turns, or older than turn records). */
  floor: { seq: number } | null;
  /** The session's spend ledger as of the last `/turn` read; `null` until loaded. */
  usage: UsageLedger | null;
  /** The newest event id applied when `usage` was read: later live `usage` events add to it. */
  usageMark: number;
  /** Live events of this session since the last `turn.summary`, without deltas or wire bodies. */
  scratch: CoreEvent[];
  loaded: boolean;
  /** The frozen starting context (`GET …/snapshot`), shown above the first turn. */
  starting: any | null;
  /** Why the turn details are incomplete (announced in the timeline); messages are complete. */
  turnsGap: string | null;
}

/** Live events never kept as scratch: deltas fold into `streaming`; raw wire traffic is the inspector's. */
const UNKEPT = new Set(["text_delta", "reasoning_delta", "tool_args_delta", "tool_call_started", "wire_event", "wire_response", "part_done"]);

/** The scratch copy of a live event: a `wire_request` keeps only its model, never the request body. */
export function scratchOf(e: CoreEvent): CoreEvent | null {
  if (UNKEPT.has(e.type)) return null;
  if (e.type === "wire_request") return { ...e, data: { body: { model: e.data?.body?.model } } };
  return e;
}

/** Insert or replace a summary by id, keeping the list ordered by start time. */
export function upsertTurn(turns: TurnSummary[], t: TurnSummary): TurnSummary[] {
  const rest = turns.filter((x) => x.id !== t.id);
  let i = rest.length;
  while (i > 0 && (rest[i - 1].started > t.started || (rest[i - 1].started === t.started && rest[i - 1].id > t.id))) i--;
  rest.splice(i, 0, t);
  return rest;
}

/** Snapshot rows are raw DB rows (archived 0/1, disabled_tools as JSON text); events carry API objects. */
export function normalizeSession(raw: any): Session {
  const s = { ...raw } as any;
  s.archived = !!raw.archived;
  if (typeof s.disabled_tools === "string") { try { s.disabled_tools = JSON.parse(s.disabled_tools); } catch { s.disabled_tools = []; } }
  return s as Session;
}

function emptyView(sessionId: string, messages: Message[] = [], messagesMore = false, loaded = false): SelectedView {
  return { sessionId, messages, messagesMore, turns: [], floor: null, usage: null, usageMark: 0, scratch: [], loaded, starting: null, turnsGap: null };
}

/** Views kept besides the selected one. */
export const VIEW_CACHE = 8;

export class Model {
  generation: string | null = null;
  lastId = 0;
  projects = new Map<string, Project>();
  sessions = new Map<string, Session>();
  running = new Map<string, string>();
  failedSessions = new Set<string>();
  unreadSessions = new Set<string>();
  approvals = new Map<string, Approval>();
  questions = new Map<string, Question>();
  streaming = new Map<string, Streaming>();
  partials = new Map<string, LivePartial>();
  selected: SelectedView | null = null;
  /** Views of recently viewed sessions, most recent first, at most `VIEW_CACHE`. */
  cached: SelectedView[] = [];
  /** Set when the selected session was deleted by an event. */
  selectedDeleted: string | null = null;

  cursor(): string | null { return this.generation ? formatCursor(this.generation, this.lastId) : null; }

  /** Show `id`: its cached view if one is held, else an empty one. The view left behind is cached. */
  select(id: string | null) {
    this.selectedDeleted = null;
    if ((this.selected?.sessionId ?? null) === id) return;
    const old = this.selected;
    if (old) this.cached = [old, ...this.cached.filter((v) => v.sessionId !== old.sessionId)].slice(0, VIEW_CACHE);
    const at = id ? this.cached.findIndex((v) => v.sessionId === id) : -1;
    this.selected = !id ? null : at >= 0 ? this.cached.splice(at, 1)[0] : emptyView(id);
  }

  /** The selected or a cached view of `id`. */
  viewOf(id: string | null | undefined): SelectedView | null {
    if (!id) return null;
    if (this.selected?.sessionId === id) return this.selected;
    return this.cached.find((v) => v.sessionId === id) ?? null;
  }

  applySnapshot(s: Snapshot) {
    const wanted = this.selected?.sessionId ?? null;
    const prev = this.selected;
    this.generation = s.watermark.generation;
    this.lastId = s.watermark.head;
    this.projects = new Map(s.projects.map((p) => [p.id, p]));
    this.running = new Map(s.running.map((t) => [t.session_id, t.id]));
    this.failedSessions = new Set(s.sessions.filter((session) => session.status === "failed").map((session) => session.id));
    this.sessions = new Map();
    for (const raw of s.sessions) {
      const ses = normalizeSession(raw);
      if (!ses.status) ses.status = this.running.has(ses.id) ? "busy" : "idle";
      this.sessions.set(ses.id, ses);
    }
    this.approvals = new Map(s.approvals.map((a) => [a.id, a]));
    this.questions = new Map(s.questions.map((q) => [q.id, q]));
    this.streaming = new Map();
    this.partials.clear();
    // Cached views may have missed events; selecting one reloads its history. Their live
    // scratch has gaps, so it goes, as the selected view's does.
    this.cached = this.cached.filter((v) => this.sessions.has(v.sessionId));
    for (const v of this.cached) v.scratch = [];
    if (s.selected) {
      this.selected = emptyView(s.selected.session_id, s.selected.messages, s.selected.messages_more, true);
      // Older pages already read stay cached: the snapshot only carries the newest messages.
      const floor = this.selected.messages[0]?.seq;
      const old = prev?.sessionId === s.selected.session_id ? prev : null;
      const older = old && floor !== undefined ? old.messages.filter((x) => x.seq < floor) : [];
      if (old && older.length) { this.selected.messages = [...older, ...this.selected.messages]; this.selected.messagesMore = old.messagesMore; }
    } else if (wanted && this.sessions.has(wanted)) {
      this.selected = emptyView(wanted);
    } else {
      this.selected = null;
    }
  }

  /** Older messages (`turn.load_older`) go before the loaded ones; duplicates are dropped by id. */
  prependMessages(sessionId: string, older: Message[], more: boolean) {
    const v = this.viewOf(sessionId);
    if (!v) return;
    const have = new Set(v.messages.map((x) => x.id));
    v.messages = [...older.filter((x) => !have.has(x.id)), ...v.messages].sort((a, b) => a.seq - b.seq);
    v.messagesMore = more;
  }

  /**
   * Fold a fully loaded history into the selected or a cached view (never mixes sessions).
   * Messages and summaries applied live after the fetch are kept, and so is the scratch (a live
   * `turn.summary` already cleared it for its turn).
   */
  applyHistory(h: { sessionId: string; messages: Message[]; messagesMore?: boolean; turns: TurnSummary[]; floor: { seq: number } | null; usage: UsageLedger | null; starting: any | null; turnsGap: string | null; usageMark?: number }) {
    const v = this.viewOf(h.sessionId);
    if (!v) return;
    // Messages held but not fetched are kept, ordered by seq: ones applied live after the
    // fetch, and older pages a cached view already read.
    const msgs = [...h.messages];
    for (const m of v.messages) {
      if (msgs.some((x) => x.id === m.id)) continue;
      let i = msgs.length;
      while (i > 0 && msgs[i - 1].seq > m.seq) i--;
      msgs.splice(i, 0, m);
    }
    v.messages = msgs;
    // Older pages cached before this fetch keep their own "more".
    const cachedOlder = h.messages.length > 0 && msgs[0].seq < h.messages[0].seq;
    v.messagesMore = cachedOlder ? v.messagesMore : h.messagesMore ?? false;
    // A summary that arrived live is at least as new as the fetched copy, unless the held one
    // is still running and the fetch read it closed (a cached view missed the summary).
    let turns = [...h.turns];
    for (const t of v.turns) {
      const fetched = turns.find((x) => x.id === t.id);
      if (fetched && t.status === "running") continue;
      turns = upsertTurn(turns, t);
    }
    v.turns = turns;
    this.seedPartials(h.sessionId, turns, msgs);
    v.floor = h.floor;
    v.usage = h.usage;
    v.usageMark = h.usageMark ?? this.lastId;
    const newest = turns.at(-1);
    // A stop the user asked for is not a failure.
    if (newest?.error && newest.error.kind !== "interrupted") this.failedSessions.add(h.sessionId);
    else if (newest) this.failedSessions.delete(h.sessionId);
    v.starting = h.starting;
    v.turnsGap = h.turnsGap;
    v.loaded = true;
  }

  /** A newer ledger (re-read after a turn closed); live usage after `mark` adds to it. */
  applyLedger(sessionId: string, usage: UsageLedger, floor: { seq: number } | null, mark: number) {
    const v = this.viewOf(sessionId);
    if (!v) return;
    v.usage = usage;
    v.floor = floor;
    v.usageMark = mark;
  }

  seedPartials(sid: string, turns: TurnSummary[], messages: Message[] = []) {
    for (const t of [...turns].reverse().filter((t) => t.status === "running")) {
      for (const r of [...t.requests].reverse()) {
        if (r.message_id || !r.partial) continue;
        if ([...messages, ...(this.viewOf(sid)?.messages ?? [])].some((m) => m.role === "assistant" && r.started != null && m.created >= r.started)) continue;
        const p = this.partials.get(sid) ?? new LivePartial();
        p.seed(r.partial); this.partials.set(sid, p); this.streaming.set(sid, p.text()); return;
      }
    }
  }

  applyEvent(e: CoreEvent): Applied {
    if (!this.generation) return { resync: "no snapshot yet" };
    const c = parseCursor(e.cursor);
    if (c && c.generation !== this.generation) return { resync: `event log changed (${this.generation} -> ${c.generation})` };
    if (e.id != null && e.id <= this.lastId) return "duplicate";
    if (e.id != null) this.lastId = e.id;
    if (e.session_id && e.data?.request_id && ["text_delta", "reasoning_delta", "tool_call_started", "tool_args_delta", "part_done"].includes(e.type)) {
      const p = this.partials.get(e.session_id) ?? new LivePartial();
      p.event(e.data); this.partials.set(e.session_id, p); this.streaming.set(e.session_id, p.text());
      return "applied";
    }
    this.reduce(e);
    const v = this.viewOf(e.session_id);
    if (v) {
      if (e.type === "turn.summary" && e.data?.id) {
        v.turns = upsertTurn(v.turns, e.data as TurnSummary);
        v.scratch = [];
      } else {
        // A live provider error goes once the turn continues or another message arrives.
        if (e.type === "step_start" || e.type === "message.added") v.scratch = v.scratch.filter((x) => x.type !== "session.error" && x.type !== "retry_exhausted");
        const kept = scratchOf(e);
        if (kept) v.scratch.push(kept);
      }
    }
    return "applied";
  }

  private reduce(e: CoreEvent) {
    const sid = e.session_id;
    const d = e.data ?? {};
    switch (e.type) {
      case "project.created":
      case "project.updated": {
        const old = this.projects.get(d.id);
        if (!old || (d.revision ?? 0) >= old.revision) this.projects.set(d.id, d as Project);
        break;
      }
      case "project.reordered":
        // Renumber in place; a project this client has not seen yet keeps its own position.
        (d.order as string[] | undefined)?.forEach((id, position) => {
          const p = this.projects.get(id);
          if (p) this.projects.set(id, { ...p, position });
        });
        break;
      case "project.deleted":
        this.projects.delete(d.id);
        for (const s of [...this.sessions.values()]) if (s.project_id === d.id) this.dropSession(s.id);
        break;
      case "session.created":
      case "session.updated": {
        if (d.kind === "btw") break;
        const old = this.sessions.get(d.id);
        if (old && (d.revision ?? 0) < old.revision) break;
        const ses = normalizeSession(d);
        if (!ses.status) ses.status = old?.status ?? "idle";
        this.sessions.set(ses.id, ses);
        break;
      }
      case "session.deleted": this.dropSession(d.id ?? sid); break;
      case "session.archived":
      case "session.unarchived":
        for (const id of d.sessions ?? []) {
          const s = this.sessions.get(id);
          if (!s) continue;
          s.archived = e.type === "session.archived";
          // The transition bumps revisions; keep them current so the next If-Match write is not stale.
          const rev = d.revisions?.[id];
          if (typeof rev === "number" && rev > s.revision) s.revision = rev;
        }
        break;
      case "session.status": {
        if (!sid) break;
        const s = this.sessions.get(sid);
        if (s) s.status = d.status;
        if (d.status === "busy") this.failedSessions.delete(sid);
        if (d.turn_finished) { this.running.delete(sid); this.streaming.delete(sid); this.partials.delete(sid); }
        else if (d.status === "busy") { if (d.turn_id) this.running.set(sid, d.turn_id); }
        else { this.running.delete(sid); this.streaming.delete(sid); this.partials.delete(sid); }
        break;
      }
      case "session.error": if (sid && d.kind !== "interrupted") this.failedSessions.add(sid); break;
      case "turn.recovered":
        if (sid) { this.running.delete(sid); this.streaming.delete(sid); this.partials.delete(sid); }
        break;
      case "message.added": {
        const m = d as Message;
        const id = m.session_id || sid || "";
        if (id && this.selected?.sessionId !== id) this.unreadSessions.add(id);
        if (m.role === "assistant") { this.streaming.delete(id); this.partials.delete(id); }
        const v = this.viewOf(id);
        if (v && !v.messages.some((x) => x.id === m.id)) {
          const msgs = v.messages;
          let i = msgs.length;
          while (i > 0 && msgs[i - 1].seq > m.seq) i--;
          msgs.splice(i, 0, m);
        }
        break;
      }
      case "step_start": if (sid) { this.streaming.delete(sid); this.partials.delete(sid); } break;
      case "text_delta":
      case "reasoning_delta":
        if (sid && typeof d.text === "string") {
          const s = this.streaming.get(sid) ?? { text: "", reasoning: "" };
          if (e.type === "text_delta") s.text += d.text; else s.reasoning += d.text;
          this.streaming.set(sid, s);
        }
        break;
      case "permission.asked": this.approvals.set(d.id, d as Approval); break;
      case "permission.replied": this.approvals.delete(d.id); break;
      case "question.asked": this.questions.set(d.id, d as Question); break;
      case "question.replied": this.questions.delete(d.id); break;
      default: break; // wire/tool/diagnostic events only land in the session's views
    }
  }

  private dropSession(id: string) {
    this.sessions.delete(id); this.running.delete(id); this.streaming.delete(id); this.partials.delete(id);
    this.failedSessions.delete(id);
    this.unreadSessions.delete(id);
    for (const [k, a] of this.approvals) if (a.session_id === id) this.approvals.delete(k);
    for (const [k, q] of this.questions) if (q.session_id === id) this.questions.delete(k);
    this.cached = this.cached.filter((v) => v.sessionId !== id);
    if (this.selected?.sessionId === id) { this.selected = null; this.selectedDeleted = id; }
  }

  sessionsRecent(): Session[] {
    return [...this.sessions.values()].sort((a, b) => b.updated - a.updated || (a.id < b.id ? -1 : 1));
  }
  projectsRecent(): Project[] {
    return [...this.projects.values()].sort((a, b) => b.updated - a.updated || (a.id < b.id ? -1 : 1));
  }
  /** Ids of a session and all its descendants. */
  tree(id: string): string[] {
    const out = [id];
    for (const s of this.sessions.values()) if (s.parent_id && out.includes(s.parent_id) && !out.includes(s.id)) out.push(s.id);
    // second pass for deeper levels given unsorted map order
    let grew = true;
    while (grew) {
      grew = false;
      for (const s of this.sessions.values()) if (s.parent_id && out.includes(s.parent_id) && !out.includes(s.id)) { out.push(s.id); grew = true; }
    }
    return out;
  }
}
