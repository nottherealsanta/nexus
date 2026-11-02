// Launch splash state: milestones only move forward, and live ends the splash.
import { describe, expect, test } from "nsh/test";
import { SPLASH_SEGMENTS, SPLASH_STEPS, splashDismiss, splashDone, splashFinish, splashReach, splashStage, splashStart } from "./src/ui/splash";

describe("launch splash", () => {
  test("the five milestones are the TUI's, and the bar never moves backwards", () => {
    splashStart();
    expect(splashStage()).toBe(0);
    expect([...SPLASH_STEPS]).toEqual(["daemon", "workspace", "providers", "session", "ready"]);
    expect(SPLASH_SEGMENTS).toBe(30);
    splashReach(3);
    splashReach(2);
    expect(splashStage()).toBe(3);
    splashReach(9);
    expect(splashStage()).toBe(SPLASH_STEPS.length);
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
