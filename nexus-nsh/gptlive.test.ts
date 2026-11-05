import { describe, expect, test } from "nsh/test";
import { CUSHION_MAX, FADE_SAMPLES, Jitter } from "./src/core/gptlive";

describe("GPT-Live jitter schedule", () => {
  test("the first packet starts 120 ms ahead with a fade-in, then packets chain", () => {
    const j = new Jitter();
    const first = j.schedule(1, 0.02);
    expect(first.fadeIn).toBe(true);
    expect(first.at).toBeCloseTo(1.12);
    const next = j.schedule(1.01, 0.02);
    expect(next.fadeIn).toBe(false);
    expect(next.at).toBeCloseTo(1.14);
    expect(FADE_SAMPLES).toBe(120);
  });
  test("running dry mid-speech grows the cushion by 60 ms, capped at 300 ms", () => {
    const j = new Jitter();
    let now = 1;
    j.schedule(now, 0.02);
    now += 0.5; // the playhead ran dry, more audio arrives soon after
    const again = j.schedule(now, 0.02);
    expect(j.cushion).toBeCloseTo(0.18);
    expect(again).toEqual({ at: now + j.cushion, fadeIn: true });
    for (let i = 0; i < 10; i++) { now += 1; j.schedule(now, 0.02); }
    expect(j.cushion).toBe(CUSHION_MAX);
  });
  test("a long silence is the end of an utterance and does not grow the cushion", () => {
    const j = new Jitter();
    j.schedule(1, 0.02);
    j.schedule(10, 0.02);
    expect(j.cushion).toBeCloseTo(0.12);
  });
  test("barge-in keeps the learned cushion without counting as an underrun; a new call starts at 120 ms", () => {
    const j = new Jitter();
    j.schedule(1, 0.02);
    j.schedule(1.5, 0.02);
    j.reset();
    expect(j.nextStart).toBe(0);
    j.schedule(3, 0.02);
    expect(j.cushion).toBeCloseTo(0.18);
    expect(new Jitter().cushion).toBeCloseTo(0.12);
  });
});
