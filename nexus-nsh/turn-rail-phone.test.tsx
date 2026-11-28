// Mobile is a cut-down client: no message rail and no turn keys on a phone, as AGENTS.md says.
import { expect, test } from "nsh/test";
import { open } from "./open-bundle";

test("a phone has no message rail", async () => {
  const app = await open("./TurnRailFixture.tsx", { width: 390, height: 844, platform: "ios" });
  await app.waitFor("id:timeline-row");
  expect(app.shows("id:turn-rail")).toBe(false);
  expect(app.count("id:turn-rail-mark")).toBe(0);
});
