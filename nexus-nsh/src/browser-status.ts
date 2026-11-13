import { signal } from "nsh";
import { request } from "./core/transport";

/** Core's `/api/browser`: what browser_exec and browser_screenshot need on Core's machine (Settings › Browser). */
export type BrowserStatus = {
  ok: boolean; mode: "own" | "chrome"; uvx: string | null;
  browser: { name: string; path: string } | null; chrome: { name: string; path: string } | null;
  problems: string[]; note: string;
};

export const browserStatus = signal<BrowserStatus | null>(null);
export const browserStatusError = signal<string | null>(null);

export async function refreshBrowserStatus(): Promise<void> {
  try {
    browserStatus.set(await request<BrowserStatus>("GET", "/api/browser"));
    browserStatusError.set(null);
  } catch (error) { browserStatusError.set(error instanceof Error ? error.message : String(error)); }
}

/** Ready or not, the browser and profile, each gap with its fix, the note. The TUI's `browser_status_lines` says the same. */
export function browserStatusLines(): string[] {
  const s = browserStatus();
  if (!s) return [browserStatusError() ? "status unavailable" : "loading status…"];
  const mine = s.mode === "chrome";
  const found = mine && s.chrome ? s.chrome : s.browser;
  return [
    s.ok ? "ready" : "not ready",
    ...(found ? [`${found.name} · ${mine ? "your Chrome profile" : "own profile"}`] : []),
    ...s.problems,
    ...(s.note ? [s.note] : []),
  ];
}
