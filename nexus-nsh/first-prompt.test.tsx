import { describe, expect, open, test } from "nsh/test";
import scenario from "../interface/scenarios/first-prompt.json";
import { sendPrompt } from "./src/commands";
import { centre, currentProject, draft, firstPrompts, model, pendingFirstPrompt, sessionDraft } from "./src/store";
import { setApiBase } from "./src/core/transport";

const session = { id: "ses_instant", project_id: "prj_instant", directory: "/work/demo", title: "New session", agent: scenario.agent, model: scenario.model, created: 1, updated: 1, revision: 1 };
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const tick = () => new Promise((r) => setTimeout(r, 30));

function setup() {
  model.select(null);
  model.sessions.clear();
  model.projects.set("prj_instant", { id: "prj_instant", path: "/work/demo", name: "demo", created: 1, updated: 1, revision: 1 });
  currentProject.set("prj_instant");
  centre.set("new_session");
  firstPrompts.set({});
  sessionDraft.set({ agent: scenario.agent, model: scenario.model });
  draft.set({ "new:prj_instant": scenario.text });
  setApiBase("http://nexus.test");
}

describe("shared first prompt scenario", () => {
  test("pending text appears synchronously; posting does not wait for history", async () => {
    setup();
    const original = globalThis.fetch;
    let create!: (r: Response) => void;
    let history!: (r: Response) => void;
    let posted = false;
    globalThis.fetch = async (url, init) => {
      const path = String(url).split("?")[0];
      if (init?.method === "POST" && path.endsWith("/api/session")) return new Promise<Response>((r) => { create = r; });
      if (path.endsWith("/prompt")) { posted = true; return reply({}); }
      if (path.endsWith("/message")) return new Promise<Response>((r) => { history = r; });
      if (path.endsWith("/ses_instant")) return reply(session);
      if (path.endsWith("/turn")) return reply({ turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } });
      return reply({});
    };
    try {
      const sending = sendPrompt(scenario.text);
      expect(pendingFirstPrompt()?.text).toBe(scenario.text);
      expect(draft()["new:prj_instant"]).toBe("");
      expect(sessionDraft().agent).toBe(scenario.agent);
      expect(model.selected).toBe(null);
      await tick();
      create(reply(session));
      await tick();
      expect(posted).toBe(true);
      expect(pendingFirstPrompt()?.text).toBe(scenario.text);
      expect(model.selected!.messages.length).toBe(0);
      history(reply([{ id: "msg_first", session_id: session.id, seq: 1, role: "user", parts: [{ type: "text", text: scenario.text }], created: 1 }]));
      expect(await sending).toBe(true);
      await tick();
      expect(pendingFirstPrompt()).toBe(null);
      expect(model.selected!.messages.length).toBe(1);
      expect(sessionDraft().agent).toBe(undefined);
    } finally { globalThis.fetch = original; }
  });

  test("failed creation restores text and selected settings", async () => {
    setup();
    const original = globalThis.fetch;
    globalThis.fetch = async () => reply({ error: "Cannot create session" }, 400);
    try {
      expect(await sendPrompt(scenario.text)).toBe(false);
      expect(pendingFirstPrompt()).toBe(null);
      expect(draft()["new:prj_instant"]).toBe(scenario.text);
      expect(sessionDraft().agent).toBe(scenario.agent);
    } finally { globalThis.fetch = original; }
  });

  test("pending submission keeps the context card and avoids a loading placeholder", async () => {
    const app = await open("./FirstPromptFixture.tsx", { width: 1440, height: 900, platform: "web" });
    await app.waitFor("id:first-prompt-sending");
    for (const fact of scenario.expect) expect(app.texts().join("\n")).toContain(fact);
    for (const fact of scenario.absent) expect(app.texts().join("\n").includes(fact)).toBe(false);
  });

  test("failed prompt after creation restores into the created session", async () => {
    setup();
    const original = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      const path = String(url).split("?")[0];
      if (path.endsWith("/prompt")) return reply({ error: "Cannot send prompt" }, 400);
      if (init?.method === "POST" || path.endsWith("/ses_instant")) return reply(session);
      if (path.endsWith("/message")) return reply([]);
      if (path.endsWith("/turn")) return reply({ turns: [], more_before: false, more_after: false, floor: null });
      return reply({});
    };
    try {
      expect(await sendPrompt(scenario.text)).toBe(false);
      expect(model.selected!.sessionId).toBe(session.id);
      expect(pendingFirstPrompt()).toBe(null);
      expect(draft()[session.id]).toBe(scenario.text);
      expect(draft()["new:prj_instant"]).toBe("");
      await tick();
    } finally { globalThis.fetch = original; }
  });
});
