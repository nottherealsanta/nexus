import { describe, expect, test } from "nsh/test";
import { activityOffsetPercent, activityPhase } from "./src/ui/activity-motion";

describe("composer activity motion", () => {
  test("phase repeats without depending on when the component mounted", () => {
    expect(activityPhase(1234)).toBe(activityPhase(4234));
    expect(activityPhase(3000)).toBe(0);
    expect(Math.abs(activityPhase(-1) - 2999 / 3000)).toBeLessThan(1e-12);
  });

  test("cosine pulse position and velocity meet smoothly at the loop seam", () => {
    const before = activityOffsetPercent(activityPhase(2999));
    const seam = activityOffsetPercent(activityPhase(3000));
    const after = activityOffsetPercent(activityPhase(3001));
    expect(seam).toBe(0);
    expect(Math.abs(before - seam)).toBeLessThan(0.001);
    expect(Math.abs(after - seam)).toBeLessThan(0.001);
    expect(activityOffsetPercent(0.5)).toBe(80);
  });
});
