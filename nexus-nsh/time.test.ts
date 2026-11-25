import { describe, expect, test } from "nsh/test";
import { formatClock } from "./src/time";

describe("clock time format", () => {
  const evening = new Date(2026, 10, 25, 18, 3);
  const midnight = new Date(2026, 10, 25, 0, 5);
  test("24h is the default", () => {
    expect(formatClock(evening)).toBe("18:03");
    expect(formatClock(midnight, "24h")).toBe("00:05");
    expect(formatClock(evening, "bogus")).toBe("18:03");
  });
  test("12h shows AM and PM", () => {
    expect(formatClock(evening, "12h")).toBe("6:03 PM");
    expect(formatClock(midnight, "12h")).toBe("12:05 AM");
    expect(formatClock(new Date(2026, 10, 25, 12, 0), "12h")).toBe("12:00 PM");
  });
  test("system follows the platform hour cycle", () => {
    expect(formatClock(evening, "system", () => true)).toBe("18:03");
    expect(formatClock(evening, "system", () => false)).toBe("6:03 PM");
  });
  test("an invalid date is empty", () => {
    expect(formatClock(new Date(NaN), "12h")).toBe("");
  });
});
