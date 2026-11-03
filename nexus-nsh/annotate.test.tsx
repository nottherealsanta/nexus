import { describe, expect, test } from "nsh/test";
import { extend, fit, normalise, penWidth, withinMessageLimit } from "./src/annotate-logic";

describe("marks on a composer image", () => {
  test("the picture fits its room, centred, and small pictures grow", () => {
    expect(fit({ width: 100, height: 50 }, { width: 400, height: 400 })).toEqual({ x: 0, y: 100, width: 400, height: 200 });
  });
  test("points are clamped to the picture", () => {
    expect(normalise(50, 25, { width: 100, height: 50 })).toEqual({ x: 0.5, y: 0.5 });
    expect(normalise(-5, 900, { width: 100, height: 50 })).toEqual({ x: 0, y: 1 });
  });
  test("a repeated point adds nothing", () => {
    const stroke = { color: "#ff3b30", points: [{ x: 0.5, y: 0.5 }] };
    expect(extend(stroke, { x: 0.5, y: 0.5 })).toBe(stroke);
    expect(extend(stroke, { x: 0.6, y: 0.5 }).points.length).toBe(2);
  });
  test("the pen matches nexus-annotate", () => {
    expect(penWidth(1000, 500)).toBe(4);
    expect(penWidth(100, 100)).toBe(2);
  });
  test("the message limit is the one paste checks", () => {
    expect(withinMessageLimit([1000])).toBe(true);
    expect(withinMessageLimit([(8 * 1024 * 1024 * 4) / 3 + 257])).toBe(false);
  });
});
