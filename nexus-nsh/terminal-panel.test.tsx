import { describe, expect, open, test } from "nsh/test";
import { terminalItems } from "./src/terminal-panel";

describe("terminal panel", () => {
  test("shows terminal tabs and control actions", async () => {
    const app = await open("./TerminalFixture.tsx", {width:1200,height:800,platform:"web"});
    await app.waitFor("id:terminal-panel");
    const text = app.texts().join("\n");
    expect(text).toContain("Terminals (1)");
    expect(text).toContain("+");
    expect(text).toContain("● Shell");
    expect(text).toContain("Read-only");
    expect(text).toContain("Take control");
    expect(text).toContain("Enter open  c take control  r release  Esc close");
    await app.press("Close");
    await app.waitFor("Close running terminal?");
    expect(app.texts().join("\n")).toContain("Close terminal");
    expect(app.texts().join("\n")).toContain("Cancel");
  });
});
void terminalItems;
