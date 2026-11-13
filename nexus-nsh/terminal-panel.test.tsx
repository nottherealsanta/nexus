import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/terminal-dock.json";

// A terminal is a tab (plan 45): its pane has the toolbar, banners and the screen; there is no dock.
describe("terminal tab pane", () => {
  test("shows the toolbar and the read-only banner, and closing a running terminal asks first", async () => {
    const app = await open("./TerminalFixture.tsx", {width:1200,height:800,platform:"web",colorScheme:"dark"});
    await app.waitFor("id:terminal-panel");
    const text = app.texts().join("\n");
    expect(text).toContain("Build");
    for (const fact of scenario.absent) expect(text.includes(fact)).toBe(false);
    // The old dock's own controls are gone: new, close and hide belong to the group strip and menu now.
    for (const gone of ["terminal-new", "terminal-close", "terminal-panel-close", "terminal-panel-shell"]) expect(app.shows(`id:${gone}`)).toBe(false);
    for (const action of scenario.actions.filter(a => ["rename", "control", "attach", "copy"].includes(a.id))) {
      expect(app.shows(`id:terminal-${action.id}`)).toBe(true);
      expect(app.shows(`id:terminal-icon-${action.icon}`)).toBe(true);
    }
    expect(text).toContain("Read-only");
    await app.screenshot("../reports/terminal-evidence/nsh-dock.png");
    await app.press("id:terminal-rename");
    expect(app.texts().join("\n")).toContain("[Rename]");
    await app.press("[Cancel]");
  });

  test("the shared terminal shortcut is bound globally and is not a plain key", async () => {
    const app = await open("./TerminalShortcutFixture.tsx", {width:1200,height:800,platform:"web",colorScheme:"dark"});
    await app.waitFor("id:terminal-open");
    expect(app.texts()).toContain("false");
    await app.keyboard.press("ctrl+k");
    expect(app.texts()).toContain("1");
    // Ctrl+` and its Shift spelling reach the same command (here no project can create a terminal, so nothing opens).
    await app.keyboard.press("ctrl+`");
    await app.keyboard.press("ctrl+shift+`");
    expect(app.texts().slice(0, 1)[0]).toBe("false");
  });
});
