// The header title marquee only moves a title wider than its tab, starts and
// ends with a readable hold, and stops at the title's end.
import { describe, expect, test } from "nsh/test";
import { marqueeShift, tabTitleOverflow, tabTitleRoomCells } from "./src/ui/header";
import { textCellPoints } from "./src/ui/geometry";

describe("hover tab title marquee", () => {
  test("a title that fits never moves", () => {
    expect(marqueeShift(0, 0)).toBe(0);
    expect(marqueeShift(90_000, 0)).toBe(0);
  });

  test("overflow uses rendered width rather than character count", () => {
    expect(tabTitleOverflow(150, 160)).toBe(0);
    expect(tabTitleOverflow(160, 160)).toBe(0);
    expect(tabTitleOverflow(175, 160)).toBe(15);
  });

  test("a moving title holds, reveals exactly its overflow, then holds the end", () => {
    expect(marqueeShift(0, 60)).toBe(0);
    expect(marqueeShift(599, 60)).toBe(0);
    expect(marqueeShift(600, 60)).toBe(0);
    expect(Math.abs(marqueeShift(700, 60) - 6) < 1e-9).toBe(true);
    expect(marqueeShift(1600, 60)).toBe(60);
    expect(marqueeShift(2100, 60)).toBe(60);
  });

  test("the title region is fixed and excludes the close slot", () => {
    expect(tabTitleRoomCells(208)).toBe(15);
    expect(tabTitleRoomCells(176)).toBe(11);
    // The title alone never claims the status, padding or fixed close slot.
    expect(tabTitleRoomCells(208) * textCellPoints()).toBeLessThan(208);
  });

  test("the cycle starts over from the title's beginning", () => {
    const travel = 60 / 60 * 1000;
    const cycle = travel + 600 * 2;
    expect(marqueeShift(cycle, 60)).toBe(0);
    expect(Math.abs(marqueeShift(cycle + 700, 60) - 6) < 1e-9).toBe(true);
  });
});
