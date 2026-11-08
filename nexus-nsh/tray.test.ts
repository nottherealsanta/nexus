import { describe, expect, test } from "nsh/test";
import { projectTitle, sessionTitle, TRAY_SESSION_LIMIT, trayModel } from "./src/tray-logic";

const project = (id: string, extra: object = {}) => ({ id, path: `/${id}`, name: id.toUpperCase(), created: 1, updated: 1, revision: 1, ...extra }) as any;
const session = (id: string, p: string, extra: object = {}) => ({ id, project_id: p, directory: "/", title: id, agent: "a", parent_id: null, revision: 1, created: 1, updated: 1, ...extra }) as any;

describe("tray model", () => {
  test("a waiting request in a subagent marks the root and the icon", () => {
    const m = trayModel([project("a")], [session("s1", "a"), session("s2", "a", { parent_id: "s1" })], [{ session_id: "s2" }], [], new Map(), new Set());
    expect(m.attention).toBe(true);
    expect(m.projects[0].sessions.map((s) => [s.id, s.state])).toEqual([["s1", "attention"]]);
    expect(m.tooltip).toBe("Nexus: 1 session needs attention");
  });

  test("no attention without a waiting request; running and unread show", () => {
    const m = trayModel([project("a")], [session("s1", "a"), session("s2", "a", { status: "busy" }), session("s3", "a")], [], [], new Map(), new Set(["s3"]));
    expect(m.attention).toBe(false);
    expect(m.projects[0].counts).toEqual({ attention: 0, finished: 1, running: 1, idle: 1 });
    expect(m.projects[0].sessions.map((s) => s.state)).toEqual(["finished", "running", "idle"]);
    expect(m.tooltip).toBe("Nexus: 1 running");
  });

  test("inactive projects and archived sessions are left out; long lists are clipped", () => {
    const sessions = [session("x", "b"), session("old", "a", { archived: true }), ...Array.from({ length: 11 }, (_, i) => session(`s${i}`, "a"))];
    const m = trayModel([project("a"), project("b", { status: "inactive" })], sessions, [], [{ session_id: "x" }], new Map(), new Set());
    expect(m.projects.map((p) => p.id)).toEqual(["a"]);
    expect(m.projects[0].sessions).toHaveLength(TRAY_SESSION_LIMIT);
    expect(m.projects[0].more).toBe(11 - TRAY_SESSION_LIMIT);
    expect(m.attention).toBe(false);
  });

  test("titles", () => {
    expect(projectTitle({ id: "a", name: "A", counts: { attention: 1, finished: 0, running: 2, idle: 0 }, sessions: [], more: 0 })).toBe("A (1 attention, 2 running)");
    expect(sessionTitle({ id: "s", title: "Fix", state: "attention" })).toBe("● Fix (needs attention)");
  });
});
