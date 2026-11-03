// HTTP + SSE transport. The web build is same-origin: the host (scripts/serve.mjs) adds
// the bearer token, so nothing here sees credentials. A native build (macOS/iOS) points
// `apiBase()` at a Core on the network, which serves without a token; on macOS a loopback
// Core is authenticated from ~/.nexus/daemon.json, the same file the TUI reads. Mirrors
// nexus-tui/src/transport.rs.
import { platform } from "nsh";
import { exists, readFile } from "fs/promises";
import { homedir } from "os";

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string, public body?: any) { super(message); }
  get isAuth() { return this.status === 401; }
  get isConflict() { return this.status === 409 && this.code === "revision_conflict"; }
  get isCursor() { return ["invalid_cursor", "cursor_generation", "cursor_ahead", "history_unavailable"].includes(this.code ?? ""); }
  /** The host could not reach Core at all. */
  get isUnreachable() { return this.status === 502 || this.status === 0; }
}

let base = "";
/** The Core origin requests go to: "" on the web (the host proxies), an absolute URL natively. */
export const apiBase = () => base;
export function setApiBase(url: string) { base = url.trim().replace(/\/+$/, ""); }

/** A loopback target: the only place a native app sends the token from daemon.json. */
function loopbackBase(): boolean {
  try {
    return loopbackName(new URL(base).hostname);
  } catch { return false; }
}

let daemon: { url: string; token: string } | null = null;
/** Read the local daemon's address and token on macOS (0600, the same file the TUI reads). */
export async function localCore(): Promise<{ url: string; token: string } | null> {
  if (platform.os !== "macos") return null;
  const home = homedir();
  // `~/.nexus` is 0.10's once `nexus migrate` marked it; before then `~/.nexus10` is.
  let migrated = false;
  try { migrated = await exists(`${home}/.nexus/.nexus10-home`); } catch {}
  for (const dir of migrated ? [".nexus", ".nexus10"] : [".nexus10", ".nexus"]) {
    try {
      const raw = await readFile(`${home}/${dir}/daemon.json`, "utf8");
      const data = JSON.parse(raw);
      if (typeof data?.url === "string" && typeof data?.token === "string") {
        daemon = { url: data.url.replace(/\/+$/, ""), token: data.token };
        return daemon;
      }
    } catch {}
  }
  return null;
}

async function ensureAuth(refresh = false) {
  if (!loopbackBase()) return;
  if (daemon && !refresh) return;
  await localCore();
}

const loopbackName = (h: string) => h === "127.0.0.1" || h === "::1" || h === "[::1]" || h === "localhost";
/** The token goes only to the daemon URL daemon.json names, never to another Core. */
function authHeaders(headers: Record<string, string>) {
  if (!daemon || !loopbackBase()) return headers;
  try {
    const target = new URL(base), source = new URL(daemon.url);
    const same = target.protocol === source.protocol && target.port === source.port
      && (target.hostname === source.hostname || (loopbackName(target.hostname) && loopbackName(source.hostname)));
    if (same) headers.authorization = `Bearer ${daemon.token}`;
  } catch {}
  return headers;
}
/**
 * A WebSocket URL on Core for `path`. On the web the host proxy adds the daemon's token; natively the token from
 * daemon.json goes only to a loopback Core, as for requests.
 */
export async function socketUrl(path: string, query: Record<string, string> = {}): Promise<string> {
  await ensureAuth();
  const origin = base || (globalThis as unknown as { location?: { origin: string } }).location?.origin || "";
  const params = new URLSearchParams(query);
  if (base && daemon && loopbackBase() && authHeaders({}).authorization) params.set("token", daemon.token);
  return `${origin.replace(/^http/, "ws")}${path}?${params}`;
}
/** A Core URL outside `request`/`eventStream` (an image the platform fetches itself). */
export const apiUrl = (path: string) => base + path;

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
  let url = apiUrl(path);
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
  let authenticated = false;
  for (let i = 0; i < attempts; i++) {
    await ensureAuth();
    try {
      const res = await fetch(url, { method, headers: authHeaders({ ...headers }), body: o.body === undefined ? undefined : JSON.stringify(o.body), signal: o.signal });
      const text = await res.text();
      if (!res.ok) throw parseError(res.status, text);
      return (text ? JSON.parse(text) : null) as T;
    } catch (e) {
      // A restarted Core has a new token in daemon.json: re-read it once and repeat the request.
      if (e instanceof ApiError && e.status === 401 && !authenticated && platform.os === "macos" && loopbackBase()) {
        authenticated = true;
        await ensureAuth(true);
        i--;
        continue;
      }
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
  const get = () => fetch(apiUrl(`/api/event${q}`), { headers: authHeaders({ accept: "text/event-stream" }), signal });
  await ensureAuth();
  let res: Response;
  try {
    res = await get();
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error)?.message ?? e}`);
  }
  if (res.status === 401 && platform.os === "macos" && loopbackBase()) {
    await ensureAuth(true);
    res = await get();
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
