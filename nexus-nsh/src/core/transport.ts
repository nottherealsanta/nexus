// HTTP + SSE transport. Same-origin: the host (scripts/serve.mjs) adds the bearer token,
// so nothing here sees credentials. Mirrors nexus-tui/src/transport.rs.

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string, public body?: any) { super(message); }
  get isAuth() { return this.status === 401; }
  get isConflict() { return this.status === 409 && this.code === "revision_conflict"; }
  get isCursor() { return ["invalid_cursor", "cursor_generation", "cursor_ahead", "history_unavailable"].includes(this.code ?? ""); }
  /** The host could not reach Core at all. */
  get isUnreachable() { return this.status === 502 || this.status === 0; }
}

export const API_BASE = ""; // same origin; desktop sets an absolute Core/host URL (see README gaps)

function parseError(status: number, text: string): ApiError {
  let body: any = null;
  try { body = JSON.parse(text); } catch {}
  return new ApiError(status, body?.error ?? (text.slice(0, 200) || `HTTP ${status}`), body?.code, body);
}

let counter = 0;
/** One key per user action: reused only for transport retries of that action. */
export const newKey = () => `nsh-${Date.now().toString(36)}-${(counter++).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

export interface ReqOpts { body?: unknown; key?: string; ifMatch?: number; signal?: AbortSignal; query?: Record<string, string | number | undefined> }

export async function request<T = any>(method: string, path: string, o: ReqOpts = {}): Promise<T> {
  let url = API_BASE + path;
  if (o.query) {
    const q = Object.entries(o.query).filter(([, v]) => v !== undefined).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&");
    if (q) url += (url.includes("?") ? "&" : "?") + q;
  }
  const headers: Record<string, string> = { accept: "application/json" };
  if (o.body !== undefined) headers["content-type"] = "application/json";
  if (o.key) headers["idempotency-key"] = o.key;
  if (o.ifMatch !== undefined) headers["if-match"] = String(o.ifMatch);
  // Keyed mutations: up to 3 attempts on transport failure (same key). HTTP errors never retry.
  const attempts = o.key ? 3 : 1;
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url, { method, headers, body: o.body === undefined ? undefined : JSON.stringify(o.body), signal: o.signal });
      const text = await res.text();
      if (!res.ok) throw parseError(res.status, text);
      return (text ? JSON.parse(text) : null) as T;
    } catch (e) {
      if (e instanceof ApiError && !e.isUnreachable) throw e;
      last = e;
      if (o.signal?.aborted) break;
      if (i + 1 < attempts) await sleep(300 * (i + 1));
    }
  }
  if (last instanceof ApiError) throw last;
  throw new ApiError(0, `network error: ${(last as Error)?.message ?? last}`);
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export interface SseFrame { event: string; id?: string; data: string }

/** Incremental SSE parser (spec subset: event, id, data, comments). */
export class SseParser {
  private buf = "";
  private ev = "message"; private id: string | undefined; private data: string[] = [];
  feed(chunk: string): SseFrame[] {
    this.buf += chunk;
    const out: SseFrame[] = [];
    for (;;) {
      const nl = this.buf.search(/\r\n|\n|\r/);
      if (nl < 0) break;
      const m = /^(\r\n|\n|\r)/.exec(this.buf.slice(nl))!;
      const line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + m[0].length);
      if (line === "") {
        if (this.data.length) out.push({ event: this.ev, id: this.id, data: this.data.join("\n") });
        this.ev = "message"; this.id = undefined; this.data = [];
      } else if (line.startsWith(":")) {
        continue;
      } else {
        const c = line.indexOf(":");
        const field = c < 0 ? line : line.slice(0, c);
        let value = c < 0 ? "" : line.slice(c + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") this.ev = value;
        else if (field === "id") this.id = value;
        else if (field === "data") this.data.push(value);
      }
    }
    return out;
  }
}

/** Streams frames from GET /api/event (authenticated by the host). Throws ApiError for non-200. */
export async function* eventStream(cursor: string | null, signal: AbortSignal): AsyncGenerator<SseFrame> {
  const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/event${q}`, { headers: { accept: "text/event-stream" }, signal });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error)?.message ?? e}`);
  }
  if (!res.ok) throw parseError(res.status, await res.text());
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  const parser = new SseParser();
  try {
    for (;;) {
      let r: ReadableStreamReadResult<Uint8Array>;
      try { r = await reader.read(); } catch (e) { throw new ApiError(0, `stream broke: ${(e as Error)?.message ?? e}`); }
      if (r.done) return;
      for (const f of parser.feed(dec.decode(r.value, { stream: true }))) yield f;
    }
  } finally {
    try { await reader.cancel(); } catch {}
  }
}

export class Backoff {
  private n = 0;
  constructor(private steps = [0.5, 1, 2, 4, 8, 16, 30]) {}
  next(): number { const s = this.steps[Math.min(this.n, this.steps.length - 1)]; this.n++; return s; }
  reset() { this.n = 0; }
  get failures() { return this.n; }
}
