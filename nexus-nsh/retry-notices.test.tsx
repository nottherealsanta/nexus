import { describe, expect, test } from "nsh/test";
import { activeRetry, retryNotices } from "./src/ui/retry-notices";

describe("provider retry notices", () => {
  test("shows live retries in order with readable delays", () => {
    const events = [
      { id: 1, type: "retry_scheduled", time: 1, data: { attempt: 1, max_attempts: 3, delay: 0.7380287708, reason: "network error" } },
      { id: 2, type: "retry_started", time: 2, data: { attempt: 2 } },
      { id: 3, type: "retry_exhausted", time: 3, data: { reason: "offline" } },
      { id: 4, type: "session.error", time: 4, data: {} },
    ] as any;
    const notices = retryNotices([], events);
    expect(notices.map((n) => n.text)).toEqual(["provider retry 1/3 in 0.7s", "provider retry started (attempt 2)", "provider retries exhausted"]);
    expect(notices[0].detail).toBe("network error");
    expect(notices[2].error).toBe(true);
  });

  test("running summaries do not duplicate the live retry", () => {
    const turn = { id: "t", status: "running", started: 1, requests: [{ id: "r", started: 1, attempts: [
      { n: 1, status: "failed", started: 1, finished: 2, error: "offline" },
      { n: 2, status: "running", started: 3, delay: 1 },
    ] }] } as any;
    const event = { id: 1, type: "retry_scheduled", time: 2, data: { attempt: 1, max_attempts: 3, delay: 1 } } as any;
    expect(retryNotices([turn], [event]).length).toBe(1);
    turn.status = "completed";
    const saved = retryNotices([turn], []);
    expect(saved.length).toBe(1);
    expect(saved[0].text).toBe("provider retry attempt 2 in 1.0s");
  });

  test("only active or unresolved exhausted retries are shown", () => {
    const scheduled = { id: 1, type: "retry_scheduled", time: 2, data: { attempt: 1, max_attempts: 3, delay: 1 } } as any;
    const failed = { id: "t", status: "failed", started: 1, requests: [{ id: "r", status: "failed", started: 1, attempts: [
      { n: 1, status: "failed", started: 1, finished: 2, error: "offline" },
      { n: 2, status: "failed", started: 3, finished: 4, delay: 1, error: "offline" },
    ] }] } as any;
    expect(activeRetry([], [scheduled], true, true)?.text).toBe("provider retry 1/3 in 1.0s");
    expect(activeRetry([], [scheduled], false, true)).toBe(null);
    expect(activeRetry([failed], [], false, false)?.text).toBe("provider retries exhausted");
    expect(activeRetry([failed], [], false, true)).toBe(null);
    expect(activeRetry([{ ...failed, status: "completed" }], [], false, false)).toBe(null);
    expect(activeRetry([failed, { ...failed, id: "later", status: "completed" }], [], false, false)).toBe(null);
  });
});
