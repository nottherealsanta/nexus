import { expect, open, test } from "nsh/test";

test("terminal evidence preview is scrollable and only confirmation adds to the composer", async () => {
  const app = await open("./TerminalEvidenceFixture.tsx", {width:1024, height:768, colorScheme:"dark"});
  await app.waitFor("id:confirm-dialog");
  expect(app.texts().join("\n")).toContain("Untrusted terminal output");
  expect(app.shows("id:confirm-body-scroll")).toBe(true);
  await app.screenshot("../reports/terminal-evidence/nsh-preview.png");
  await app.press("id:confirm-ok");
  expect(app.shows("id:confirm-dialog")).toBe(false);
  await app.screenshot("../reports/terminal-evidence/nsh-draft.png");
});

test("terminal evidence keeps confirmation reachable in a short narrow viewport", async () => {
  const app = await open("./TerminalEvidenceFixture.tsx", {width:640, height:480, colorScheme:"dark"});
  await app.waitFor("id:confirm-dialog");
  await app.screenshot("../reports/terminal-evidence/nsh-preview-narrow.png");
  await app.press("id:confirm-ok");
  expect(app.shows("id:confirm-dialog")).toBe(false);
});
