import { config } from "./store";

export type TimeFormat = "24h" | "12h" | "system";

/** The OS/browser clock preference (`Intl` hour cycle); 24h when it is unknown. */
export function systemUses24h(): boolean {
  try {
    const cycle = new Intl.DateTimeFormat(undefined, { hour: "numeric" }).resolvedOptions().hourCycle;
    return cycle !== "h11" && cycle !== "h12";
  } catch {
    return true;
  }
}

/** `18:03` (24h) or `6:03 PM` (12h) for a date, per `ui.time_format` (`24h` default, `12h`, `system`). */
export function formatClock(date: Date, format: TimeFormat | string | null | undefined = "24h", system24h: () => boolean = systemUses24h): string {
  if (Number.isNaN(date.getTime())) return "";
  const h = date.getHours();
  const m = String(date.getMinutes()).padStart(2, "0");
  const h24 = format === "12h" ? false : format === "system" ? system24h() : true;
  if (h24) return `${String(h).padStart(2, "0")}:${m}`;
  return `${h % 12 || 12}:${m} ${h < 12 ? "AM" : "PM"}`;
}

/** The configured time format; reads Core's config, so a reactive caller follows the setting. */
export const timeFormat = (): TimeFormat => {
  const v = config()?.values?.ui?.time_format;
  return v === "12h" || v === "system" ? v : "24h";
};

/** A clock time in the configured format; the single place NSH draws a clock. */
export const clockTime = (date: Date): string => formatClock(date, timeFormat());
