// Connection supervisor: handshake, snapshot, SSE with durable cursors, reconnect with backoff,
// resync, daemon-restart detection. Mirrors nexus-tui/src/sync.rs.
import { ApiError, Backoff, eventStream, request, sleep } from "./transport";
import { Compat, CoreEvent, Info, Snapshot, TurnPage, TurnSummary, checkCompat, parseApiVersion } from "./api";
import { Model } from "./reduce";

/** `/turn` page size while loading a session's summaries (Core's maximum). */
export const TURN_PAGE = 200;
/** Pages of summaries read per session open; older turns beyond them are announced, not loaded. */
export const TURN_PAGES_MAX = 25;

/**
 * Every turn summary of a session, newest page first and then `before=` pages to the oldest,
 * returned oldest first with the newest page's floor and ledger. A failed or malformed read
 * throws; the caller announces it.
 */
export async function loadTurns(id: string, stillWanted: () => boolean = () => true): Promise<{ page: TurnPage; capped: boolean } | null> {
  const first = await request<TurnPage>("GET", `/api/session/${id}/turn`, { query: { limit: TURN_PAGE } });
  if (!first || !Array.isArray(first.turns)) throw new Error("Core returned no turn summaries");
  let turns: TurnSummary[] = first.turns;
  let more = !!first.more_before;
  let pages = 1;
  while (more && turns.length && pages < TURN_PAGES_MAX) {
    if (!stillWanted()) return null;
    const page = await request<TurnPage>("GET", `/api/session/${id}/turn`, { query: { before: turns[0].id, limit: TURN_PAGE } });
    if (!page || !Array.isArray(page.turns)) throw new Error("Core returned no turn summaries");
    turns = [...page.turns, ...turns];
    more = !!page.more_before && page.turns.length > 0;
    pages++;
  }
  return { page: { ...first, turns, more_before: more }, capped: more };
}

export type ConnectionState =
  | { kind: "starting" }
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "reconnecting"; attempt: number; retryAt: number; reason: string }
  | { kind: "offline"; reason: string; noDaemon: boolean }
  | { kind: "auth_failed"; reason: string }
  | { kind: "incompatible"; core: string; update: "client" | "core" };

export type Banner =
  | { kind: "newer_core"; core: string }
  | { kind: "api_unreported" }
  | { kind: "daemon_restarted" }
  | { kind: "resynced"; why: string };

export interface SyncHost {
  model: Model;
  setConnection(c: ConnectionState): void;
  setInfo(i: Info, c: Compat): void;
  addBanner(b: Banner): void;
  changed(): void;
  /** Called once a fresh snapshot is applied, so views can reload selection. */
  snapshotApplied?(): void;
  canStart: boolean;
}

export class Sync {
  private abort: AbortController | null = null;
  private stopped = false;
  private wake: (() => void) | null = null;
  private backoff = new Backoff();
  private pid: number | null = null;
  private needSnapshot = true;
  private everLive = false;
  private startedOnce = false;
  private incompatibleKey = "";

  constructor(private host: SyncHost, private pollMs = 2000) {}

  start() { void this.loop(); }
  stop() { this.stopped = true; this.abort?.abort(); this.wake?.(); }
  /** Retry now (app.reconnect). */
  retryNow() { this.backoff.reset(); this.abort?.abort(); this.wake?.(); }
  forceSnapshot() { this.needSnapshot = true; this.abort?.abort(); }

  private pause(ms: number): Promise<void> {
    return new Promise((r) => { const t = setTimeout(() => { this.wake = null; r(); }, ms); this.wake = () => { clearTimeout(t); this.wake = null; r(); }; });
  }

  private async loop() {
    const h = this.host;
    h.setConnection({ kind: "connecting" });
    while (!this.stopped) {
      try {
        await this.session();
        throw new ApiError(0, "event stream closed");
      } catch (err) {
        if (this.stopped) return;
        const e = err instanceof ApiError ? err : new ApiError(0, String((err as Error)?.message ?? err));
        if (e.isCursor || (e.code === "overflow")) {
          this.needSnapshot = true;
          h.addBanner({ kind: "resynced", why: e.code ?? "cursor rejected" });
          continue; // immediate: refetch snapshot, resume after its watermark
        }
        if (e.isAuth) {
          h.setConnection({ kind: "auth_failed", reason: e.message });
          await this.pause(this.pollMs * 2);
          continue;
        }
        if ((e as any).incompatible) { await this.pause(this.pollMs * 2); continue; }
        const noDaemon = e.code === "no_daemon";
        if (noDaemon && !this.startedOnce && h.canStart && !this.everLive) {
          this.startedOnce = true;
          h.setConnection({ kind: "starting" });
          try { await fetch("/__nexus/start", { method: "POST" }); } catch {}
          continue;
        }
        const delay = this.backoff.next();
        const attempt = this.backoff.failures;
        if (noDaemon || (!this.everLive && attempt >= 2)) {
          h.setConnection({ kind: "offline", reason: noDaemon ? "no daemon is running" : e.message, noDaemon });
        } else {
          h.setConnection({ kind: "reconnecting", attempt, retryAt: Date.now() + delay * 1000, reason: e.message });
        }
        await this.pause(delay * 1000);
        if (this.stopped) return;
      }
    }
  }

  /** One connected session: runs until the stream ends or errors (always throws or never returns normally). */
  private async session(): Promise<void> {
    const h = this.host, m = h.model;
    if (!this.everLive) h.setConnection({ kind: "connecting" });
    const info = await request<Info>("GET", "/api/info");
    const compat = checkCompat(parseApiVersion(info.api_version));
    h.setInfo(info, compat);
    if (compat.kind === "incompatible") {
      const key = `${info.pid}:${compat.core.major}.${compat.core.minor}`;
      this.incompatibleKey = key;
      h.setConnection({ kind: "incompatible", core: `${compat.core.major}.${compat.core.minor}`, update: compat.update });
      const err = new ApiError(0, "incompatible API");
      (err as any).incompatible = true;
      throw err;
    }
    const restarted = this.pid !== null && info.pid !== this.pid;
    this.pid = info.pid ?? null;
    if (restarted) { this.needSnapshot = true; h.addBanner({ kind: "daemon_restarted" }); }
    if (this.needSnapshot || !m.generation) {
      const sel = m.selected?.sessionId;
      let snap: Snapshot;
      try {
        snap = await request<Snapshot>("GET", "/api/snapshot", { query: { session: sel, message_limit: 200 } });
      } catch (e) {
        if (e instanceof ApiError && e.status === 404 && sel) { m.select(null); snap = await request<Snapshot>("GET", "/api/snapshot", { query: { message_limit: 200 } }); }
        else throw e;
      }
      m.applySnapshot(snap);
      this.needSnapshot = false;
      h.snapshotApplied?.();
      h.changed();
      // The snapshot's messages are bounded and carry no diagnostics: reload the full
      // history (it merges with whatever the stream applies meanwhile).
      if (m.selected) void this.loadSelected().catch(() => {});
    }
    this.abort = new AbortController();
    const signal = this.abort.signal;
    let buf: CoreEvent[] = [];
    let scheduled = false;
    const flush = () => {
      scheduled = false;
      let resync: string | null = null;
      let closed: string | null = null;
      for (const ev of buf) {
        const r = m.applyEvent(ev);
        if (typeof r === "object") { resync = r.resync; break; }
        if (r === "applied" && ev.type === "turn.summary" && ev.session_id && ev.session_id === m.selected?.sessionId) closed = ev.session_id;
      }
      if (closed) void this.refreshLedger(closed).catch(() => {});
      buf = [];
      h.changed();
      if (resync) { this.needSnapshot = true; h.addBanner({ kind: "resynced", why: resync }); this.abort?.abort(); }
    };
    try {
      for await (const f of eventStream(m.cursor(), signal)) {
        if (f.event === "server.connected") {
          this.backoff.reset(); this.everLive = true; this.startedOnce = false; // one start attempt per outage
          h.setConnection({ kind: "live" });
          continue;
        }
        if (f.event === "bus.overflow") { this.needSnapshot = true; throw new ApiError(0, "event stream overflow", "overflow"); }
        let ev: CoreEvent;
        try { ev = JSON.parse(f.data); } catch { this.needSnapshot = true; throw new ApiError(0, "undecodable event", "overflow"); }
        buf.push(ev);
        if (!scheduled) { scheduled = true; queueMicrotask(flush); }
      }
    } catch (e) {
      if (signal.aborted && this.needSnapshot) { flush(); return this.session(); }
      if (signal.aborted && !this.stopped) { flush(); throw new ApiError(0, "reconnecting"); }
      flush();
      throw e;
    }
    flush();
  }

  /**
   * Load the selected session's full history: every saved message, the frozen starting
   * context and the turn summaries with the spend ledger (`GET …/turn`). The event log is
   * not read: live events cover the running turn, summaries cover the rest.
   */
  async loadSelected(): Promise<void> {
    const m = this.host.model;
    const id = m.selected?.sessionId;
    if (!id) return;
    const wanted = () => m.selected?.sessionId === id;
    const [msgs, starting, turns] = await Promise.all([
      request<any[]>("GET", `/api/session/${id}/message`),
      request<any>("GET", `/api/session/${id}/snapshot`).catch(() => null),
      loadTurns(id, wanted).then((r) => r, (e) => ({ error: String((e as Error)?.message ?? e) })),
    ]);
    if (!wanted() || turns === null) return;
    let gap: string | null = null;
    if ("error" in turns) gap = `turn details failed to load (${turns.error}); tokens, models and timings are missing`;
    else if (turns.capped) gap = `only the newest ${TURN_PAGE * TURN_PAGES_MAX} turns have details loaded`;
    const page = "error" in turns ? null : turns.page;
    m.applyHistory({
      sessionId: id, messages: msgs, starting, turnsGap: gap,
      turns: page?.turns ?? [], floor: page?.floor ?? null, usage: page?.usage ?? null,
    });
    this.host.changed();
  }

  /** Re-read the ledger (and floor) after a turn closed; the summary itself came on the stream. */
  async refreshLedger(id: string): Promise<void> {
    const m = this.host.model;
    const page = await request<TurnPage>("GET", `/api/session/${id}/turn`, { query: { limit: 1 } });
    if (m.selected?.sessionId !== id || !page?.usage) return;
    m.applyLedger(id, page.usage, page.floor ?? null, m.lastId);
    this.host.changed();
  }
}
