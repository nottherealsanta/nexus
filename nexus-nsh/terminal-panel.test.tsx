import { describe, expect, open, test } from "nsh/test";
import { terminalItems } from "./src/terminal-panel";

describe("terminal panel", () => {
  test("shows the resource, location, status and control actions", async () => {
    const app = await open("./TerminalFixture.tsx", {width:1200,height:800,platform:"web"});
    await app.waitFor("id:terminal-panel");
    const text = app.texts().join("\n");
    expect(text).toContain("Terminal");
    expect(text).toContain("New terminal");
    expect(text).toContain("Shell");
    expect(text).toContain("/work/demo");
    expect(text).toContain("running · No controller");
    expect(text).toContain("Take control");
    await app.press("Close");
    await app.waitFor("Close running terminal?");
    expect(app.texts().join("\n")).toContain("Close terminal");
    expect(app.texts().join("\n")).toContain("Cancel");
  });
});
void terminalItems;
