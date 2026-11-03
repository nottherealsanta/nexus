// The seam between the store/sync (which see every event and stream) and the Manager's controller
// (core/manager.ts, which imports the store). The controller fills these in when the app mounts;
// until then they do nothing, so a client that never starts a call pays nothing.
import type { CoreEvent } from "./api";

export const managerHooks: {
  event?: (e: CoreEvent) => void;
  connected?: (streamId: string | null) => void;
  closed?: (streamId: string) => void;
} = {};
