// Launch splash state: milestones only move forward, and live ends the splash.
import { describe, expect, test } from "nsh/test";
import { SPLASH_SLOW_MS, SPLASH_STEPS, splashSweep,splashDismiss, splashDone, splashFinish, splashReach, splashStage, splashStart } from "./src/ui/splash";

describe("launch splash", () => {
  test("the five milestones are the TUI's, and the milestones never move backwards", () => {
    splashStart();
    expect(splashStage()).toBe(0);
    expect([...SPLASH_STEPS]).toEqual(["daemon", "workspace", "providers", "session", "ready"]);
    splashReach(3);
    splashReach(2);
    expect(splashStage()).toBe(3);
    splashReach(9);
    expect(splashStage()).toBe(SPLASH_STEPS.length);
  });

  test("the light sweep is a function of the clock; the quiet line waits 5 s", () => {
    expect(splashSweep(0)).toBe(-8);
    expect(splashSweep(320)).toBe(2);
    expect(splashSweep(32 * 30)).toBe(splashSweep(0));
    expect(SPLASH_SLOW_MS).toBe(5000);
  });

  test("a live connection fades the splash out", async () => {
    splashStart();
    expect(splashDone()).toBe(false);
    splashFinish();
    await new Promise((r) => setTimeout(r, 220));
    expect(splashDone()).toBe(true);
  });

  test("a failed start dismisses it at once, and a new address shows it again", () => {
    splashStart();
    splashDismiss();
    expect(splashDone()).toBe(true);
    splashStart();
    expect(splashDone()).toBe(false);
    expect(splashStage()).toBe(0);
  });
});
