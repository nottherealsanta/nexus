import { describe, expect, test } from "nsh/test";
import { contextTokens, inventorySections, previewSections, startingSections } from "./src/ui/newsession";

describe("starting context presentation", () => {
  test("draft hides the agent name and inline instruction preview", () => {
    const result = previewSections({ agent: "build", prompt: "Prompt", file: { path: "/work/AGENTS.md", bytes: 40, line: "Project instructions" } });
    expect(result.sections[0].meta).toBe("");
    expect(result.sections[1].body.kind).toBe("none");
    expect(result.sections[1].meta).toBe("");
    expect(result.sections[1].detail.includes("Project instructions")).toBe(true);
  });

  test("recorded context omits the agent name but retains its prompt and instructions", () => {
    const result = startingSections({ agent: { name: "build" }, system_prompt: "First\nSecond", instruction_file: { path: "/work/AGENTS.md", content: "Instructions" } }, []);
    expect(result.sections[0].meta).toBe("");
    expect(result.sections[0].detail).toBe("First\nSecond");
    expect(result.sections[1].meta).toBe("not read · /work/AGENTS.md");
    expect(result.sections[1].detail).toBe("Instructions");
  });

  test("tool counts and disabled states remain visible without truncating the list", () => {
    const tools = Array.from({ length: 15 }, (_, i) => `tool${i}`);
    const result = startingSections({ tools, system_prompt: "Prompt" }, ["tool2"]);
    const section = result.sections[2];
    expect(section.count).toBe(15);
    expect(section.meta).toBe("1 off");
    expect(section.body.kind === "grid" && section.body.items.length).toBe(15);
    expect(inventorySections([])[0].meta).toBe("none");
    expect(contextTokens(1502)).toBe("1.5K");
    expect(contextTokens(829)).toBe("829");
  });

  test("sections keep count, tokens and meta as separate fields for the header to lay out inline", () => {
    const tools = Array.from({ length: 18 }, (_, i) => `tool${i}`);
    const section = startingSections({ tools, system_prompt: "Prompt" }, ["tool2"]).sections[2];
    expect([section.title, section.count, section.meta]).toEqual(["Tools", 18, "1 off"]);
    expect(typeof section.tokens === "number" || section.tokens === undefined).toBe(true);
  });
});
