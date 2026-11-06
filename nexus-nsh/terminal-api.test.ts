import { describe, expect, test } from "nsh/test";
import { terminals } from "./src/core/terminal";
import { SseParser } from "./src/core/transport";

describe("terminal transport", () => {
  test("input carries the attachment and caller-supplied sequence without a generic retry key", async () => {
    const original = globalThis.fetch;
    let seen: any;
    globalThis.fetch = async (url, init) => {
      seen = { url: String(url), init };
      return new Response(JSON.stringify({ input_seq: 1 }), { status: 200 });
    };
    try {
      expect(await terminals.input("term/1", "att", 1, "Yw==")).toEqual({ input_seq: 1 });
      expect(seen.url.endsWith("/api/terminal/term%2F1/input")).toBe(true);
      expect(JSON.parse(seen.init.body)).toEqual({ attachment_id: "att", input_seq: 1, data: "Yw==" });
      expect(seen.init.headers["idempotency-key"]).toBe(undefined);
    } finally { globalThis.fetch = original; }
  });
  test("rename sends the name to the Core resource endpoint", async () => {
    const original = globalThis.fetch;
    let seen: any;
    globalThis.fetch = async (url, init) => {
      seen = { url: String(url), init };
      return new Response(JSON.stringify({ name: "build" }), { status: 200 });
    };
    try {
      expect((await terminals.rename("term/1", "build")).name).toBe("build");
      expect(seen.url.endsWith("/api/terminal/term%2F1")).toBe(true);
      expect(seen.init.method).toBe("PATCH");
      expect(JSON.parse(seen.init.body)).toEqual({ name: "build" });
    } finally { globalThis.fetch = original; }
  });
  test("SSE parser retains terminal frames split across chunks", () => {
    const parser = new SseParser();
    expect(parser.feed('id: 7\nevent: output\ndata: {"seq":')).toEqual([]);
    expect(parser.feed('7}\n\n')).toEqual([{ event: "output", id: "7", data: '{"seq":7}' }]);
  });
});
