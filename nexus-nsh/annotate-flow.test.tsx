// Drawing on a composer image, through the app bundle: the native module that
// flattens the marks is only registered there.
import { describe, expect, open, test } from "nsh/test";

const session = { id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Task", model: "", permission: "ask", created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]" };
const network = {
  "GET */__nexus/host": { body: { host: "test", canStart: false } },
  "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
  "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "allow" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: false } }, defaults: {}, schema: {}, errors: [] } },
  "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
  "GET */api/event*": { body: 'event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0}\n\n', headers: { "content-type": "text/event-stream" } },
  "GET */api/provider": { body: [] },
  "GET */api/model/last*": { body: { model: "scripted/m" } },
  "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
  "GET */api/agent*": { body: { agents: [], problems: [] } },
  "GET */api/skill*": { body: { skills: [], problems: [] } },
  "GET */api/mcp*": { body: { servers: [] } },
  "GET */api/session/ses_1/message*": { body: [] },
  "GET */api/session/ses_1/snapshot": { body: null },
  "GET */api/session/ses_1/context": { body: { used: 0, window: null, exact: false } },
  "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
  "GET */api/session/ses_1/worktrees": { body: { worktrees: [] } },
  "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
  "POST */api/session/ses_1/prompt": { status: 202, body: { status: "busy" } },
  "GET */api/session/*": { body: session },
};


async function openWithImage() {
  const app = await open("./ComposerFixture.tsx", { network, width: 1024, height: 768, platform: "web" });
  await app.waitFor("id:session-ses_1");
  await app.press("id:session-ses_1");
  await app.waitFor("id:composer-input");
  await app.paste("id:composer-input", { files: ["./fixtures/clipboard.png"] });
  await app.waitFor("id:image-previews");
  return app;
}

/** PNG colour type, byte 25: the fixture is RGB (2); only flattening produces RGBA (6). */
function colourType(dataUrl: string) {
  const prefix = "data:image/png;base64,";
  expect(dataUrl.startsWith(prefix)).toBe(true);
  return atob(dataUrl.slice(prefix.length, prefix.length + 44)).charCodeAt(25);
}

async function sentImages(app: Awaited<ReturnType<typeof openWithImage>>) {
  await app.submit("id:composer-input");
  await app.waitUntil(() => app.requests().some((r) => r.method === "POST" && r.url.endsWith("/prompt")));
  return JSON.parse(app.requests().find((r) => r.method === "POST" && r.url.endsWith("/prompt"))!.body!).images;
}

describe("drawing on a composer image", () => {
  test("the modal says what it does", async () => {
    const app = await openWithImage();
    await app.press("id:composer-image-1");
    await app.waitFor("id:annotate-picture");
    for (const fact of ["1-4 colour · u undo · x clear · enter done · esc cancel", "Undo", "Clear", "Done"]) expect(app.shows(`text:${fact}`)).toBe(true);
    expect(app.texts().some((text) => text.startsWith("Mark up "))).toBe(true);
    await app.screenshot("/private/tmp/nexus-annotate-open.png");
  });

  test("Done puts the marks in what is sent, in place of the original", async () => {
    const app = await openWithImage();
    await app.press("id:composer-image-1");
    await app.waitFor("id:annotate-picture");
    await app.keyboard.press("2");
    await app.drag("id:annotate-picture", { x: 120, y: 40 });
    await app.drag("id:annotate-picture", { x: -120, y: -40 });
    await app.keyboard.press("u");
    await app.screenshot("/private/tmp/nexus-annotate-drawn.png");
    await app.press("id:annotate-done");
    await app.waitUntil(() => !app.shows("id:annotate"));
    const images = await sentImages(app);
    expect(images.length).toBe(1);
    expect(colourType(images[0].data_url)).toBe(6);
  });

  test("Esc cancels and sends the original", async () => {
    const app = await openWithImage();
    await app.press("id:composer-image-1");
    await app.waitFor("id:annotate-picture");
    await app.drag("id:annotate-picture", { x: 120, y: 40 });
    await app.keyboard.press("escape");
    await app.waitUntil(() => !app.shows("id:annotate"));
    const images = await sentImages(app);
    expect(colourType(images[0].data_url)).toBe(2);
  });

  test("Done with no marks keeps the original", async () => {
    const app = await openWithImage();
    await app.press("id:composer-image-1");
    await app.waitFor("id:annotate-picture");
    await app.press("id:annotate-done");
    await app.waitUntil(() => !app.shows("id:annotate"));
    const images = await sentImages(app);
    expect(colourType(images[0].data_url)).toBe(2);
  });
});
