import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/skill-picker.json";

const network = {
  "GET */api/skill*": { body: { description: scenario.description, content: scenario.content, children: scenario.children } },
};

describe("shared skill picker scenario", () => {
  test("13 skills show only the count; cards keep toggles separate from nested Markdown details", async () => {
    const app = await open("./SkillPickerFixture.tsx", { network, width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:ctx-skills-open");
    expect(app.count("id:ctx-skill-row-accessibility")).toBe(0);
    await app.press("id:ctx-skills-open");
    await app.waitFor("id:skill-card-accessibility");
    await app.press("id:skill-toggle-accessibility");
    expect(app.texts().join("\n")).toContain("Disabled");
    expect(app.count("id:skill-detail")).toBe(0);
    await app.press("id:skill-toggle-accessibility");
    await app.press("label:accessibility details");
    await app.waitFor("id:skill-detail");
    await app.waitFor("text:Supporting resources");
    for (const fact of scenario.detail_expect) expect(app.texts().join("\n")).toContain(fact);
    await app.keyboard.press("escape");
    expect(app.count("id:skill-detail")).toBe(0);
    expect(app.count("id:context-detail")).toBe(1);
    await app.keyboard.press("escape");
    expect(app.count("id:context-detail")).toBe(0);
  });

  test("12 skills retain inline controls and names still open the detail modal", async () => {
    const app = await open("./SkillPickerFixture.tsx", { network, width: 1440, height: 900, platform: "web" });
    await app.press("id:boundary");
    await app.waitFor("id:ctx-skill-row-accessibility-info");
    await app.press("id:ctx-skill-row-accessibility-info");
    await app.waitFor("id:skill-detail");
    await app.waitFor("text:Supporting resources");
    expect(app.texts().join("\n")).toContain("Accessible interfaces");
    await app.keyboard.press("escape");
    expect(app.count("id:context-detail")).toBe(1);
  });

  test("skills without supporting resources say so explicitly", async () => {
    const app = await open("./SkillPickerFixture.tsx", {
      network: { "GET */api/skill*": { body: { description: scenario.description, content: scenario.content, children: [] } } },
      width: 1440, height: 900, platform: "web",
    });
    await app.press("id:ctx-skills-open");
    await app.press("id:skill-name-accessibility");
    await app.waitFor("text:No other files or folders.");
  });
});
