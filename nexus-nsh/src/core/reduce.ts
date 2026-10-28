// Pure client model: a cache of what Core says (port of nexus-tui/src/reduce.rs).
//  * A snapshot replaces everything and sets the watermark; events at or below `lastId` are duplicates.
//  * Id gaps are never loss. Events from another generation, or before the first snapshot, need a resync.
//  * Every event of the selected session is kept (wire events are never dropped by the client).
import { Approval, CoreEvent, Message, Project, Question, Session, Snapshot, formatCursor, parseCursor } from "./api";

export type Applied = "applied" | "duplicate" | { resync: string };

export interface Streaming { text: string; reasoning: string }
export interface SelectedView {
  sessionId: string;
  messages: Message[];
  messagesMore: boolean;
  events: CoreEvent[];
  loaded: boolean;
  /** The frozen starting context (`GET …/snapshot`), shown above the first turn. */
  starting: any | null;
  /** Why the loaded diagnostics are incomplete (announced in the timeline); messages are complete. */
  eventsGap: string | null;
}

/** Snapshot rows are raw DB rows (archived 0/1, disabled_tools as JSON text); events carry API objects. */
export function normalizeSession(raw: any): Session {
  const s = { ...raw } as any;
  s.archived = !!raw.archived;
  if (typeof s.disabled_tools === "string") { try { s.disabled_tools = JSON.parse(s.disabled_tools); } catch { s.disabled_tools = []; } }
  return s as Session;
}

/** Cap on retained diagnostics kept for one session (messages are never capped). */
export const HISTORY_EVENT_CAP = 20000;

export interface EventPage { events?: CoreEvent[]; floor?: number; history_truncated?: boolean }

/**
 * Fold stored event pages into the newest `cap` events and a notice saying what is
 * missing: events expired by retention, events beyond the cap, or a page that failed
 * (`failed` is the last id read and the error). Keeping the newest means the kept
 * diagnostics join the live stream without a gap. Mirrors nexus-tui `fold_event_pages`.
 */
export function foldEventPages(pages: EventPage[], failed: { after: number; error: string } | null, cap = HISTORY_EVENT_CAP): { events: CoreEvent[]; gap: string | null } {
  let events: CoreEvent[] = [];
  let floor = 0;
  for (const p of pages) {
    if (p.history_truncated) floor = Math.max(floor, p.floor ?? 0);
    events = events.concat(p.events ?? []);
  }
  const notes: string[] = [];
  if (floor > 0) notes.push(`diagnostics at or before event ${floor} expired under retention`);
  if (events.length > cap) {
    const dropped = events.length - cap;
    events = events.slice(dropped);
    notes.push(`${dropped} older diagnostics are not loaded (only the newest ${cap} are kept)`);
  }
  if (failed) notes.push(`diagnostics after event ${failed.after} failed to load (${failed.error}); some are missing`);
  return { events, gap: notes.length ? `${notes.join("; ")} (messages are complete)` : null };
}

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
  selected: SelectedView | null = null;
  /** Set when the selected session was deleted by an event. */
  selectedDeleted: string | null = null;

  cursor(): string | null { return this.generation ? formatCursor(this.generation, this.lastId) : null; }

  select(id: string | null) {
    this.selectedDeleted = null;
    this.selected = id ? { sessionId: id, messages: [], messagesMore: false, events: [], loaded: false, starting: null, eventsGap: null } : null;
  }

  applySnapshot(s: Snapshot) {
    const wanted = this.selected?.sessionId ?? null;
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
    if (s.selected) {
      this.selected = { sessionId: s.selected.session_id, messages: s.selected.messages, messagesMore: s.selected.messages_more, events: [], loaded: true, starting: null, eventsGap: null };
    } else if (wanted && this.sessions.has(wanted)) {
      this.selected = { sessionId: wanted, messages: [], messagesMore: false, events: [], loaded: false, starting: null, eventsGap: null };
    } else {
      this.selected = null;
    }
  }

  /** Fold a fully loaded history into the selected view (never mixes sessions). */
  applyHistory(h: { sessionId: string; messages: Message[]; events: CoreEvent[]; starting: any | null; eventsGap: string | null }) {
    const v = this.selected;
    if (!v || v.sessionId !== h.sessionId) return;
    // Messages applied live after the fetch are kept, ordered by seq.
    const msgs = [...h.messages];
    for (const m of v.messages) {
      if (msgs.some((x) => x.id === m.id)) continue;
      let i = msgs.length;
      while (i > 0 && msgs[i - 1].seq > m.seq) i--;
      msgs.splice(i, 0, m);
    }
    v.messages = msgs;
    v.messagesMore = false;
    const newest = h.events.length ? h.events[h.events.length - 1].id : 0;
    const live = v.events.filter((e) => e.id > newest);
    const byId = new Map<number, CoreEvent>();
    for (const e of [...h.events, ...live]) byId.set(e.id, e);
    v.events = [...byId.values()].sort((a, b) => a.id - b.id);
    for (const event of v.events) {
      if (event.type === "session.error") this.failedSessions.add(h.sessionId);
      if (event.type === "session.status" && event.data?.status === "busy") this.failedSessions.delete(h.sessionId);
    }
    v.starting = h.starting;
    v.eventsGap = h.eventsGap;
    v.loaded = true;
  }

  applyEvent(e: CoreEvent): Applied {
    if (!this.generation) return { resync: "no snapshot yet" };
    const c = parseCursor(e.cursor);
    if (c && c.generation !== this.generation) return { resync: `event log changed (${this.generation} -> ${c.generation})` };
    if (e.id <= this.lastId) return "duplicate";
    this.lastId = e.id;
    this.reduce(e);
    if (this.selected && e.session_id === this.selected.sessionId) this.selected.events.push(e);
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
      case "project.deleted":
        this.projects.delete(d.id);
        for (const s of [...this.sessions.values()]) if (s.project_id === d.id) this.dropSession(s.id);
        break;
      case "session.created":
      case "session.updated": {
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
        for (const id of d.sessions ?? []) { const s = this.sessions.get(id); if (s) s.archived = e.type === "session.archived"; }
        break;
      case "session.status": {
        if (!sid) break;
        const s = this.sessions.get(sid);
        if (s) s.status = d.status;
        if (d.status === "busy") this.failedSessions.delete(sid);
        if (d.status === "busy") { if (d.turn_id) this.running.set(sid, d.turn_id); }
        else { this.running.delete(sid); this.streaming.delete(sid); }
        break;
      }
      case "session.error": if (sid) this.failedSessions.add(sid); break;
      case "turn.recovered":
        if (sid) { this.running.delete(sid); this.streaming.delete(sid); }
        break;
      case "message.added": {
        const m = d as Message;
        const id = m.session_id || sid || "";
        if (id && this.selected?.sessionId !== id) this.unreadSessions.add(id);
        if (m.role === "assistant") this.streaming.delete(id);
        if (this.selected && this.selected.sessionId === id && !this.selected.messages.some((x) => x.id === m.id)) {
          const msgs = this.selected.messages;
          let i = msgs.length;
          while (i > 0 && msgs[i - 1].seq > m.seq) i--;
          msgs.splice(i, 0, m);
        }
        break;
      }
      case "step_start": if (sid) this.streaming.delete(sid); break;
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
      default: break; // wire/tool/diagnostic events only land in the selected view
    }
  }

  private dropSession(id: string) {
    this.sessions.delete(id); this.running.delete(id); this.streaming.delete(id);
    this.failedSessions.delete(id);
    this.unreadSessions.delete(id);
    for (const [k, a] of this.approvals) if (a.session_id === id) this.approvals.delete(k);
    for (const [k, q] of this.questions) if (q.session_id === id) this.questions.delete(k);
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
