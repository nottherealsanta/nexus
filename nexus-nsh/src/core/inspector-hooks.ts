import type { CoreEvent } from "./api";
/** Avoid a store/inspector import cycle while forwarding the shared SSE stream. */
export const inspectorHooks: { event?: (event: CoreEvent) => void; invalidate?: () => void } = {};
