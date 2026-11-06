import { describe, expect, test } from "nsh/test";
import { gaugeCells, leftPercent, reservePercent, reserveText, until } from "./src/usage-format";

describe("usage formatting", () => {
  test("gauge and left handle boundaries and unknown values", () => {
    expect(gaugeCells(24)).toEqual([8, 8, 3, 0, 0, 0, 0, 0, 0, 0]);
    expect(gaugeCells(null)).toEqual(Array(10).fill(0));
    expect(gaugeCells(100)).toEqual(Array(10).fill(8));
    expect(gaugeCells(1.875)[0]).toBe(2);
    expect(leftPercent(42)).toBe("58%");
    expect(leftPercent(0.2)).toBe("99.8%");
    expect(leftPercent(null)).toBe("—");
  });
  test("reserve and expiry use the fixed clock", () => {
    const now = 1000000, week = 604800;
    expect(reservePercent(0, now + week / 2, week, now)).toBe(50);
    expect(reservePercent(0, now + 1, week - 1, now)).toBe(null);
    expect(reservePercent(0, now, week, now)).toBe(null);
    expect(reservePercent(null, now + 1, week, now)).toBe(null);
    expect(reserveText(8.5)).toBe("+8%");
    expect(reserveText(-1.5)).toBe("-2%");
    expect(reserveText(null)).toBe("—");
    expect(until(null, now)).toBe("—");
    expect(until(now - 1, now)).toBe("now");
    expect(until(now + 59, now)).toBe("now");
    expect(until(now + 3900, now)).toBe("1h 5m");
    expect(until(now + 90000, now)).toBe("1d 1h");
  });
});
