import { describe, expect, test } from "nsh/test";
import scenario from "../interface/scenarios/live-partial.json";
import { LivePartial } from "./src/core/livepartial";
import { Model } from "./src/core/reduce";

describe("request partial checkpoints", () => {
  test("shared join checkpoint race and completed part", () => {
    const p = new LivePartial();
    for (const d of scenario.queued) p.event(d);
    p.seed(scenario.checkpoint);
    expect(p.text().text).toBe("Hello world");
    p.event(scenario.final); p.event(scenario.final);
    expect(p.text()).toEqual(scenario.expected);
    p.seed(scenario.checkpoint);
    expect(p.text()).toEqual(scenario.expected);
  });
  test("a reconnect gap waits for a checkpoint", () => {
    const p = new LivePartial();
    p.event({ request_id: "r", seq: 1, part_index: 0, type: "text_delta", text: "a" });
    p.event({ request_id: "r", seq: 3, part_index: 0, type: "text_delta", text: "c" });
    p.seed({ request_id: "r", seq: 2, parts: [{ type: "text", text: "ab" }] });
    expect(p.text().text).toBe("abc");
  });
  test("new request clears old streaming", () => {
    const p = new LivePartial();
    p.event({ request_id: "a", seq: 1, part_index: 0, type: "text_delta", text: "old" });
    p.event({ request_id: "b", seq: 1, part_index: 0, type: "text_delta", text: "new" });
    expect(p.text().text).toBe("new");
  });
  test("id-less events keep the durable cursor and message save clears scratch", () => {
    const m = new Model(); m.generation = "log_t"; m.lastId = 10;
    const e = { id: null, time: 1, session_id: "s", type: "text_delta", data: { request_id: "r", seq: 1, part_index: 0, type: "text_delta", text: "hello" } };
    m.applyEvent(e); m.applyEvent(e);
    expect(m.streaming.get("s")?.text).toBe("hello");
    expect(m.cursor()).toBe("log_t:10");
    m.applyEvent({ id: 11, time: 2, session_id: "s", type: "message.added", data: { id: "msg", session_id: "s", role: "assistant", parts: [] } });
    expect(m.streaming.has("s")).toBe(false);
  });
});
