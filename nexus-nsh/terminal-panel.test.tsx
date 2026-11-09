import { describe, expect, open, test } from "nsh/test";
import { terminalItems } from "./src/terminal-panel";
import scenario from "../interface/scenarios/terminal-dock.json";

describe("terminal panel", () => {
  test("shows terminal tabs and control actions", async () => {
    const app = await open("./TerminalFixture.tsx", {width:1200,height:800,platform:"web",colorScheme:"dark"});
    await app.waitFor("id:terminal-panel");
    const text = app.texts().join("\n");
    for (const fact of scenario.expect) expect(text).toContain(fact);
    for (const fact of scenario.absent) expect(text.includes(fact)).toBe(false);
    for (const action of scenario.actions) {
      expect(app.shows(`id:terminal-${action.id === "hide" ? "panel-close" : action.id}`)).toBe(true);
      expect(app.shows(`id:terminal-icon-${action.icon}`)).toBe(true);
    }
    expect(text).toContain("Read-only");
    expect(text).toContain("Enter open  c take control  r release  Esc close");
    await app.screenshot("../reports/terminal-evidence/nsh-dock.png");
    await app.press("id:terminal-rename");
    expect(app.texts().join("\n")).toContain("[Rename]");
    await app.press("[Cancel]");
    await app.press("id:terminal-close");
    await app.waitFor("Close running terminal?");
    expect(app.texts().join("\n")).toContain("[Close terminal]");
    expect(app.texts().join("\n")).toContain("[Cancel]");
  });
});
void terminalItems;
