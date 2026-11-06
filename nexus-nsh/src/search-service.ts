import { signal } from "nsh";
import { ApiError, newKey, request } from "./core/transport";
import { toast } from "./store";

export type SearchService = { state: "running" | "stopped" | "absent" | "unavailable" | string; url: string; runtime: string | null; detail: string | null };

export const searchService = signal<SearchService | null>(null);
export const searchServiceError = signal<string | null>(null);
export const searchStarting = signal(false);

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
  if (s.state === "unavailable") return s.detail ?? "The search service cannot be started here.";
  return undefined;
}

export function searchStatusLines(): string[] {
  const s = searchService();
  if (!s) return [searchServiceError() ? "status unavailable" : "loading status…"];
  if (searchStarting()) return ["starting…", s.url];
  const label = s.state === "running" ? "running" : s.state === "stopped" ? "stopped" : s.state === "absent" ? "not set up" : "unavailable";
  return [label, s.url, ...(s.detail ? [s.detail] : []), ...(s.runtime ? [`runtime ${s.runtime}`] : [])];
}

export async function startSearchService(): Promise<void> {
  await refreshSearchService();
  const reason = searchStartReason();
  if (reason) { toast("info", reason); return; }
  searchStarting.set(true);
  try {
    searchService.set(await request<SearchService>("POST", "/api/search/service/start", { key: newKey() }));
    toast("success", "Web search is running");
  } catch (error) {
    toast("error", error instanceof ApiError || error instanceof Error ? `Web search did not start: ${error.message}` : String(error));
    await refreshSearchService();
  } finally { searchStarting.set(false); }
}
