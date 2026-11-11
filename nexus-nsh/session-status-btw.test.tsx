import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/session-status-btw.json";
import { manifest } from "./src/gen/manifest";

const network = {
  "POST */api/session/ses_parent/btw": { body: { session: "ses_btw", inherited: 1, inherited_context: "inherited prefix proof" } },
  "GET */api/session/ses_btw/btw": { body: { head: 1, inherited: 1, calls: 0, running: true, messages: [], events: [], partial: null } },
  "POST */api/session/ses_btw/interrupt": { status: 204 },
  "POST */api/session/ses_parent/interrupt": { status: 204 },
};

describe("sections 21–23", () => {
  test("outcomes sit above the composer and clear on busy", async () => {
    const app = await open("./SessionStatusBtwFixture.tsx", { network, width: 1280, height: 900, platform: "web" });
    await app.waitFor("id:session-status");
    for (const fact of scenario.status_expect) expect(app.texts().join("\n")).toContain(fact);
    await app.press("id:busy");
    await app.waitUntil(() => !app.texts().join("\n").includes("Interrupted by the user"));
    for (const fact of scenario.timeline_absent) expect(app.texts().join("\n").includes(fact)).toBe(false);
    await app.press("id:busy");
    await app.waitFor("id:session-status");
    expect(app.texts().join("\n")).toContain("Interrupted by the user");
  });

  test("side-question modal streams, expands context and never changes the parent", async () => {
    const app = await open("./SessionStatusBtwFixture.tsx", { network, width: 1280, height: 900, platform: "web" });
    await app.press("id:start-btw");
    await app.waitFor("id:btw-modal");
    expect(app.texts().join("\n")).toContain("Running");
    expect(app.texts().join("\n")).toContain("↑↓ scroll");
    expect(app.texts().join("\n")).toContain("Inherited context");
    expect(app.texts().join("\n").includes("Expand inherited context")).toBe(false);
    expect(app.texts().join("\n").includes("inherited prefix proof")).toBe(false);
    await app.press("id:btw-inherited");
    await app.waitUntil(() => app.texts().join("\n").includes("inherited prefix proof"));
    await app.keyboard.press("i");
    await app.keyboard.press("f8");
    for (const fact of scenario.btw_expect) expect(app.texts().join("\n")).toContain(fact);
    await app.keyboard.press("escape");
    await app.waitFor("id:session-status");
    expect(app.texts().join("\n").includes("4747")).toBe(false);
  });

  test("Esc cancels the side run, not a busy parent", async () => {
    const app = await open("./SessionStatusBtwFixture.tsx", { network, width: 1024, height: 768, platform: "web" });
    await app.press("id:busy");
    await app.press("id:start-btw");
    await app.waitFor("id:btw-modal");
    await app.waitUntil(() => app.requests().some((r) => r.method === "GET" && r.url.endsWith("ses_btw/btw")));
    await app.keyboard.press("escape");
    await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("ses_btw/interrupt")));
    expect(app.requests().some((r) => r.method === "POST" && r.url.endsWith("ses_parent/interrupt"))).toBe(false);
  });

  test("only continuation navigation is removed", () => {
    expect(manifest.command.some((c) => String(c.id) === "session.open_continuation")).toBe(false);
    expect(manifest.command.some((c) => c.id === "session.continue")).toBe(true);
    expect(manifest.command.some((c) => c.id === "session.open_origin")).toBe(true);
  });
});
