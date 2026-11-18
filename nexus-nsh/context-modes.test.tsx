import { describe, expect, open, test } from "nsh/test";
import { worktreeStatus } from "./src/ui/newsession";

const network = { "GET */api/*": { body: {} } };
const opts = { network, width: 1200, height: 900, platform: "web" as const };

describe("context header modes (plan 48)", () => {
  test("a draft switches, and its worktree row follows the total and switches the draft", async () => {
    const app = await open("./ContextModesFixture.tsx", opts);
    await app.waitFor("id:context-sections");
    expect(app.count("id:ctx-skill-row-review")).toBe(1);
    expect(app.count("id:context-worktree")).toBe(1);
    expect(app.texts().join("\n")).toContain("ask the agent to start in a new worktree");
    expect(app.texts().join("\n")).toContain("2 uncommitted changes stay in the checkout");
    expect(app.count("label:Worktree off")).toBe(1);
    await app.press("id:context-worktree-toggle");
    expect(app.count("label:Worktree on")).toBe(1);
    await app.press("id:context-worktree-toggle");
    expect(app.count("label:Worktree off")).toBe(1);
  });

  test("outside Git the worktree row is hidden, not disabled", async () => {
    const app = await open("./ContextModesFixture.tsx", opts);
    await app.press("id:no-git");
    expect(app.count("id:context-worktree")).toBe(0);
  });

  test("fixed: no card background, state marks instead of switches, read-only dialogs naming /skills", async () => {
    const app = await open("./ContextModesFixture.tsx", opts);
    await app.press("id:fix");
    await app.waitFor("id:context-sections-fixed");
    expect(app.count("id:context-sections")).toBe(0);
    expect(app.count("id:ctx-skill-row-review")).toBe(0);
    expect(app.count("id:ctx-skill-row-review-state")).toBe(1);
    expect(app.count("id:context-worktree")).toBe(0);
    expect(app.texts().join("\n")).toContain("new worktree requested · not entered");
    await app.press("id:ctx-skills-open");
    await app.waitFor("id:skill-state-review");
    expect(app.count("id:skill-toggle-review")).toBe(0);
    expect(app.texts().join("\n")).toContain("/skills switches");
  });

  test("/skills and /mcp open live switches for the draft", async () => {
    const app = await open("./ContextModesFixture.tsx", opts);
    await app.press("id:live-skills");
    await app.waitFor("id:skill-toggle-review");
    expect(app.texts().join("\n")).toContain("● Enabled");
    await app.press("id:skill-toggle-review");
    expect(app.texts().join("\n")).toContain("○ Disabled");
    await app.keyboard.press("escape");
    await app.press("id:live-mcp");
    await app.waitFor("id:mcp-toggle-docs");
    // The header row is the fixture's static "off"; the dialog follows the draft, which starts on.
    expect(app.count("label:docs off")).toBe(1);
    await app.press("id:mcp-toggle-docs");
    expect(app.count("label:docs off")).toBe(2);
  });

  test("the worktree line matches the TUI's wording", () => {
    expect(worktreeStatus({ worktree_request: { state: "requested" } })).toEqual({ text: "⌥ new worktree requested", warning: false });
    expect(worktreeStatus({ worktree_request: { state: "entered", entered_branch: "fix-cue" } })).toEqual({ text: "⌥ fix-cue · worktree entered", warning: false });
    expect(worktreeStatus({})).toBe(null);
  });
});
