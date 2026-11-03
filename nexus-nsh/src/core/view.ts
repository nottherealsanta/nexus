// What this NSH instance is showing, reported to Core's ephemeral view registry.
// Ids and a panel label only: no paths, screen contents, scroll position or drafts.
// The stream id from `server.connected` is a capability; it is never logged,
// shown, placed in a URL or persisted.
import { platform } from "nsh";
import { ApiError, request } from "./transport";

export interface ClientView {
  project: string | null;
  session: string | null;
  panel: "files" | "file" | null;
}

/** Same ids and panel: the report has nothing to do. */
export const sameClientView = (a: ClientView | null, b: ClientView | null): boolean =>
  !!a && !!b && a.project === b.project && a.session === b.session && a.panel === b.panel;

/** One report may take this long; the event stream never waits for it. */
const REPORT_TIMEOUT_MS = 3000;
/** Bounded delays between failed reports for the same stream and view. */
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 16000, 30000];

/** A stable self-asserted label for this app process, valid as a Core view id. */
function clientLabel(): string {
  const os = (platform.os || "client").replace(/[^a-z0-9-]/gi, "");
  return `nsh-${os}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function viewKey(streamId: string, view: ClientView): string {
  return `${streamId}\n${view.project}\n${view.session}\n${view.panel}`;
}

export interface ViewReporterOptions {
  /** This stream belongs to another client: open a new event stream and report again. */
  onMismatch?: () => void;
}

/**
 * Reports view changes through the stream that issued the current `stream_id`.
 *
 * Core keys the view by the stream, drops it when the stream closes, and gives
 * every reconnect a new id, so a connection reports the latest view again.
 * A view is a hint, not authority: a failed report retries on a timer with a
 * capped backoff, never on the event stream. Only `client_mismatch` needs a new
 * stream.
 */
export class ViewReporter {
  readonly clientId = clientLabel();
  private streamId: string | null = null;
  private latest: ClientView | null = null;
  private reported: ClientView | null = null;
  private inFlight = false;
  private abort: AbortController | null = null;
  /** Bumped when the stream changes: a late answer from the old stream is ignored. */
  private epoch = 0;
  /** Timer and attempt state for bounded retries; all are cleared by a new view or stream. */
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryToken = 0;
  private retryKey: string | null = null;
  private retryAttempt = 0;
  /** The stream and view waiting for its retry timer. */
  private waitKey: string | null = null;

  constructor(private options: ViewReporterOptions = {}) {}

  /** A new stream's `server.connected`: a reconnect reports the latest view again. */
  connected(streamId: string | null) {
    this.cancelRetry();
    this.streamId = streamId;
    this.reported = null;
    this.epoch++;
    this.abort?.abort();
    this.pump();
  }

  /** The stream that issued `streamId` ended; a newer stream's id is left alone. */
  closed(streamId: string) {
    if (this.streamId !== streamId) return;
    this.cancelRetry();
    this.streamId = null;
    this.reported = null;
    this.epoch++;
    this.abort?.abort();
  }

  /** The user's view changed; report only when a stream is connected and the view is new. */
  setView(view: ClientView) {
    if (sameClientView(this.latest, view)) return;
    this.cancelRetry();
    this.latest = view;
    this.pump();
  }

  /** Resolves once the latest view is reported (or `ms` pass): the Manager answers a move after it. */
  async settled(ms = 1500): Promise<void> {
    const until = Date.now() + ms;
    while (Date.now() < until && this.streamId && !sameClientView(this.reported, this.latest)) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }

  private cancelRetry() {
    this.retryToken++;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryKey = null;
    this.retryAttempt = 0;
    this.waitKey = null;
  }

  private pump() {
    const streamId = this.streamId, view = this.latest;
    if (this.inFlight || !streamId || !view || sameClientView(this.reported, view)) return;
    const key = viewKey(streamId, view);
    if (this.waitKey === key) return;
    this.inFlight = true;
    void this.send(streamId, view, key);
  }

  /**
   * Retry the same view after a failure, at most every 30 s. The timer is
   * cancelled by any view or stream change, and its callback re-checks the
   * stream, epoch, latest view and in-flight state before reporting anything.
   */
  private scheduleRetry(key: string, streamId: string, view: ClientView, epoch: number) {
    if (this.retryKey !== key) {
      this.retryKey = key;
      this.retryAttempt = 0;
    }
    const delay = RETRY_DELAYS_MS[Math.min(this.retryAttempt, RETRY_DELAYS_MS.length - 1)];
    this.retryAttempt++;
    this.waitKey = key;
    const token = ++this.retryToken;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (token !== this.retryToken) return;
      if (epoch !== this.epoch || this.streamId !== streamId) return;
      if (!sameClientView(this.latest, view)) return;
      if (this.waitKey !== key) return;
      this.waitKey = null;
      this.pump();
    }, delay);
  }

  private async send(streamId: string, view: ClientView, key: string) {
    const epoch = this.epoch;
    const abort = new AbortController();
    this.abort = abort;
    const timer = setTimeout(() => abort.abort(), REPORT_TIMEOUT_MS);
    try {
      await request("POST", "/api/client/view", {
        signal: abort.signal,
        body: {
          stream_id: streamId,
          client_id: this.clientId,
          project: view.project,
          session: view.session,
          panel: view.panel,
        },
      });
      if (epoch === this.epoch && this.streamId === streamId) {
        this.reported = view;
        this.cancelRetry();
      }
    } catch (e) {
      if (epoch !== this.epoch || this.streamId !== streamId) return;
      if (e instanceof ApiError && e.status === 409 && e.code === "stream_closed") {
        this.streamId = null;
        this.reported = null;
        this.cancelRetry();
        return;
      }
      if (e instanceof ApiError && e.status === 409 && e.code === "client_mismatch") {
        this.streamId = null;
        this.reported = null;
        this.cancelRetry();
        this.options.onMismatch?.();
        return;
      }
      // A newer view owns reporting now; the finally pass sends it.
      if (!sameClientView(this.latest, view)) return;
      // The daemon validates ids, but this is only a hint: retry on a capped
      // backoff so a transient failure cannot leave the view stale for good.
      this.scheduleRetry(key, streamId, view, epoch);
    } finally {
      clearTimeout(timer);
      if (this.abort === abort) this.abort = null;
      this.inFlight = false;
      this.pump();
    }
  }
}
