// The local SearXNG behind web_search (Settings › Tools and the tour's search step). Core starts it in a container on
// its own host and answers the start request before the container is ready, so clients poll GET /api/search/service.
import { signal } from "nsh";
import { ApiError, newKey, request } from "./core/transport";
import { toast } from "./store";
import { uiText } from "./gen/manifest";

export type SearchService = { state: "running" | "starting" | "stopped" | "absent" | "unavailable" | string; url: string; runtime: string | null; detail: string | null };

export const searchService = signal<SearchService | null>(null);
export const searchServiceError = signal<string | null>(null);
/** Why the last start failed; kept across status polls until the next start or a running answer. */
export const searchStartError = signal<string | null>(null);
export const searchStarting = signal(false);
/** The tour's search step asked Core to start it once for this visit. */
export const searchOnboardingAsked = signal(false);

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

export async function refreshSearchService(): Promise<void> {
  try {
    searchService.set(await request<SearchService>("GET", "/api/search/service"));
    searchServiceError.set(null);
  } catch (error) { searchServiceError.set(errorMessage(error)); }
}

export function searchStartReason(): string | undefined {
  if (searchStarting()) return "The search service is starting.";
  const s = searchService();
  if (!s) return undefined;
  if (s.state === "running") return "The search service is already running.";
  if (s.state === "starting") return "The search service is starting.";
  if (s.state === "unavailable") return s.detail ?? "The search service cannot be started here.";
  return undefined;
}

/** State, address, detail, runtime and the last start error, as Settings and the tour show them. TUI `search_status_lines`. */
export function searchStatusLines(): string[] {
  const s = searchService();
  if (!s) return [searchServiceError() ? uiText("web_search.no_status") : uiText("web_search.loading"), ...(searchStartError() ? [searchStartError()!] : [])];
  if (searchStarting()) return [uiText("web_search.state.starting"), ...(s.url ? [s.url] : [])];
  const label = s.state === "running" ? uiText("web_search.state.running")
    : s.state === "starting" ? uiText("web_search.state.starting")
    : s.state === "stopped" ? uiText("web_search.state.stopped")
    : s.state === "absent" ? uiText("web_search.state.absent")
    : uiText("web_search.state.unavailable");
  return [
    label,
    ...(s.url ? [s.url] : []),
    ...(s.detail ? [s.detail] : []),
    ...(s.runtime ? [uiText("web_search.runtime", { runtime: s.runtime })] : []),
    ...(searchStartError() ? [searchStartError()!] : []),
  ];
}

export async function startSearchService(o: { quiet?: boolean } = {}): Promise<void> {
  await refreshSearchService();
  const reason = searchStartReason();
  if (reason) { toast("info", reason); return; }
  searchStarting.set(true);
  searchStartError.set(null);
  try {
    // The async start returns at once with `starting`; an older Core keeps only the blocking route.
    let reply: SearchService | null;
    try {
      reply = await request<SearchService | null>("POST", "/api/services/search/start", { body: {}, key: newKey() });
    } catch (error) {
      if (!(error instanceof ApiError) || (error.status !== 404 && error.status !== 405)) throw error;
      reply = await request<SearchService | null>("POST", "/api/search/service/start", { body: {}, key: newKey() });
    }
    if (reply) searchService.set(reply);
    if (reply?.state === "running") toast("success", uiText("web_search.running"));
    else {
      toast("info", uiText("web_search.starting"));
      await refreshSearchService();
    }
  } catch (error) {
    const message = errorMessage(error);
    searchStartError.set(message);
    if (!o.quiet || error instanceof ApiError) toast("error", uiText("web_search.failed", { error: message }));
    await refreshSearchService();
  } finally { searchStarting.set(false); }
}

/** Entering the tour's search step: read the state, then start it once unless it already runs or cannot start. */
export async function ensureSearchStarted(): Promise<void> {
  if (searchOnboardingAsked()) return;
  searchOnboardingAsked.set(true);
  await refreshSearchService();
  const state = searchService()?.state;
  if (state === "running" || state === "starting" || state === "unavailable") return;
  await startSearchService({ quiet: true });
}
