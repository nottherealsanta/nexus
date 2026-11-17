// Test helper: `open` for `nsh/test` that answers `GET /api/session/{id}/open` (API 1.6) from the same
// fake network's older per-session routes (`/message`, `/turn`, `/snapshot` and the session row), so a
// scenario describes a session once. A test that sets its own `/open` route keeps it.
import { open as baseOpen } from "nsh/test";

type Entry = unknown;
const resolve = (entry: Entry, args: unknown[]): any => (typeof entry === "function" ? (entry as any)(...args) : entry);

export function withOpenRoutes(network: Record<string, Entry>): Record<string, Entry> {
  const ids = new Set<string>();
  for (const key of Object.keys(network)) {
    const m = key.match(/^GET \*\/api\/session\/([^/*]+)\/(message|turn|snapshot)/);
    if (m) ids.add(m[1]);
  }
  const routes: Record<string, Entry> = {};
  for (const id of ids) {
    const find = (suffix: string) => network[`GET */api/session/${id}${suffix}`] ?? (suffix ? network[`GET */api/session/${id}${suffix}*`] : undefined) ?? (suffix ? Object.entries(network).find(([k]) => k.startsWith(`GET */api/session/${id}${suffix}`))?.[1] : undefined);
    routes[`GET */api/session/${id}/open*`] = (...args: unknown[]) => {
      const msgs = (resolve(find("/message"), args))?.body ?? [];
      const turns = (resolve(find("/turn"), args))?.body ?? { turns: [], floor: null, usage: { since: null, models: {} } };
      const starting = (resolve(find("/snapshot"), args))?.body ?? null;
      const session = (resolve(find("") ?? network["GET */api/session/*"], args))?.body ?? { id };
      const list = Array.isArray(msgs) ? msgs : msgs.messages ?? [];
      return {
        body: {
          session, starting, messages: list, messages_more: !Array.isArray(msgs) && !!msgs.more, oldest_seq: list[0]?.seq ?? null,
          turns: turns.turns ?? [], turns_more_before: !!turns.more_before, floor: turns.floor ?? null,
          usage: turns.usage ?? { since: null, models: {} }, approvals: [], questions: [],
        },
      };
    };
  }
  return { ...routes, ...network };
}

export const open: typeof baseOpen = ((entry: any, options: any) =>
  baseOpen(entry, options && options.network && typeof options.network === "object" ? { ...options, network: withOpenRoutes(options.network) } : options)) as any;
