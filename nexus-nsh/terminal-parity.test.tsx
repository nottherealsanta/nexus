// The terminal backend is a third NSH medium: it must read like the Rust TUI (same glyphs and key
// spellings) rather than like the desktop app. Shared visuals come from interface/theme.toml.
import { describe, expect, open, test } from "nsh/test";

const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Session", model: "",
  permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null,
  archived: 0, revision: 1, disabled_tools: "[]",
};
const project = { id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 };
const connected = 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n';

const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/snapshot*": {
    body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [project], sessions: [session], running: [], approvals: [], questions: [], selected: null },
  },
  "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/*": { body: session },
};

describe("terminal parity", () => {
  test("the sidebar reads with the Rust client's search glyph and key spelling", async () => {
    const app = await open("./App.tsx", { network, width: 1280, height: 768 });
    await app.waitFor("id:header");
    const shown = app.texts();
    // The shared control glyph, not the web-search verb glyph `*`.
    expect(shown.includes("⌕")).toBe(true);
    expect(shown.includes("*")).toBe(false);
    if (shown.includes("⌘K")) {
      // A browser or desktop build keeps the platform's command key.
      expect(shown.includes("ctrl+k")).toBe(false);
    } else {
      // The terminal target reaches the TUI's keys, so it shows their spelling.
      expect(shown.includes("ctrl+k")).toBe(true);
      expect(shown.includes("⌃K")).toBe(false);
      expect(shown.includes("ctrl+,")).toBe(true);
    }
    // The footer is the TUI's text row; a decorative gear is not part of it.
    expect(shown.includes("⚙")).toBe(false);
    // The key the hint names must be the key that works: the terminal reaches the TUI's keys.
    if (!shown.includes("⌘K")) {
      await app.keyboard.press("ctrl+k");
      await app.waitFor("id:session-switcher");
      await app.keyboard.press("escape");
      await app.keyboard.press("ctrl+,");
      await app.waitFor("id:settings");
    }
  });
});
