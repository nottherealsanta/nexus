import { describe, expect, test } from "nsh/test";
import { PHONE_TABS, compactApproval, connectProblem, desktopOnly, healthSummary, mobileSettings, parsePairing, phoneRow, phoneStatus, mobileClient, projectDot, pushSample, rememberCore, sessionSections, traceBars, transportOf, phoneSessionGroups } from "./src/mobile";
import { manifest } from "./src/gen/manifest";

describe("mobile client rules", () => {
  test("uses the cut-down client on iPhone only; iPad, desktop and web keep the desktop layout", () => {
    expect(mobileClient(true, "ios", "phone")).toBe(true);
    expect(mobileClient(true, "ios", "pad")).toBe(false);
    expect(mobileClient(true, "ios", "tablet")).toBe(false);
    expect(mobileClient(true, "macos", "desktop")).toBe(false);
    expect(mobileClient(false, "web", "phone")).toBe(false);
  });
  test("offers only its three tabs and applicable settings", () => {
    expect(PHONE_TABS.map((tab) => tab.id)).toEqual(["sessions", "manager", "settings"]);
    const sections = mobileSettings(manifest.settings.section as any);
    expect(sections.map((s: any) => s.id)).toEqual(["appearance", "providers", "dictation", "manager", "notifications", "about"]);
    expect(sections.flatMap((s: any) => (s.row ?? []).map((r: any) => r.id))).not.toContain("appearance.editor");
  });
  test("hides desktop surfaces but retains session interactions", () => {
    for (const id of ["terminal.open", "inspector.open", "file.open", "session.resources", "project.overview", "palette.open"]) expect(desktopOnly(id)).toBe(true);
    for (const id of ["session.new", "waiting.allow_once", "waiting.deny", "waiting.submit_answers"]) expect(desktopOnly(id)).toBe(false);
    expect(["user", "assistant", "older", "tool", "context", "thought"].filter((kind) => phoneRow({ kind }))).toEqual(["user", "assistant", "older"]);
    expect(phoneRow({ kind: "diag", tone: "error" })).toBe(true);
    // A fork's inherited block stays on a phone, folded with the desktop's wording.
    expect(phoneRow({ kind: "fork" })).toBe(true);
  });
  test("summarizes approvals and prioritizes waiting status", () => {
    expect(compactApproval({ tool: "bash", args: { command: "ls\npwd" } })).toBe("ls …");
    expect(compactApproval({ tool: "patch", args: { patch: "*** Update File: a.txt\n@@" } })).toBe("a.txt");
    expect(phoneStatus({ waiting: true, running: true }).word).toBe("Waiting on you");
    expect(phoneStatus({ waiting: false, running: true }).word).toBe("Working");
  });
  test("groups sessions by urgency while preserving recent order", () => {
    const sessions = [
      { id: "recent", waiting: false, running: false },
      { id: "both", waiting: true, running: true },
      { id: "running", waiting: false, running: true },
      { id: "waiting", waiting: true, running: false },
    ];
    expect(phoneSessionGroups(sessions, (s) => s).map((g) => [g.title, g.sessions.map((s) => s.id)])).toEqual([
      ["Needs you", ["both", "waiting"]], ["Running", ["running"]], ["Recent", ["recent"]],
    ]);
    expect(phoneSessionGroups([], (s: typeof sessions[number]) => s)).toEqual([]);
  });
});

describe("mobile redesign rules (plans/05 5.2)", () => {
  test("sessions fall into Needs you, Running and Recent, once each, newest first", () => {
    const list = [{ id: "a", updated: 1 }, { id: "b", updated: 5 }, { id: "c", updated: 3 }, { id: "d", updated: 4 }];
    const secs = sessionSections(list, { waiting: (id) => id === "a", running: (id) => id === "b" || id === "a" });
    expect(secs.map((s) => [s.id, s.items.map((x) => x.id)])).toEqual([["needs", ["a"]], ["running", ["b"]], ["recent", ["d", "c"]]]);
    expect(secs[0].count).toBe(1);
    expect(sessionSections([], { waiting: () => false, running: () => false })).toEqual([]);
  });
  test("a project keeps its dot colour", () => {
    expect(projectDot("prj_1")).toBe(projectDot("prj_1"));
    expect(projectDot("prj_1").startsWith("bg-")).toBe(true);
  });
  test("transport follows the address", () => {
    expect(transportOf("http://100.101.102.103:4747")).toBe("Tailscale");
    expect(transportOf("https://mac.tail1234.ts.net")).toBe("Tailscale");
    expect(transportOf("http://192.168.1.20:4747")).toBe("LAN");
    expect(transportOf("http://10.0.0.5:4747")).toBe("LAN");
    expect(transportOf("http://172.20.1.1:4747")).toBe("LAN");
    expect(transportOf("http://studio.local:4747")).toBe("LAN");
    expect(transportOf("http://127.0.0.1:4747")).toBe("This device");
    expect(transportOf("https://core.example.com")).toBe("Remote");
    expect(transportOf("http://8.8.8.8")).toBe("Remote");
  });
  test("pairing accepts a nexus link, an address or a bare host", () => {
    expect(parsePairing("nexus://pair?url=http%3A%2F%2F192.168.1.20%3A4747&token=t&name=mac")).toEqual({ url: "http://192.168.1.20:4747", token: "t", name: "mac" });
    expect(parsePairing("192.168.1.20:4747")).toEqual({ url: "http://192.168.1.20:4747" });
    expect(parsePairing(" https://core.example.com/ ")).toEqual({ url: "https://core.example.com" });
    expect(parsePairing("nexus://pair?name=x")).toBeNull();
    expect(parsePairing("ftp://x")).toBeNull();
    expect(parsePairing("two words")).toBeNull();
    expect(parsePairing("")).toBeNull();
  });
  test("recent Cores are newest first, unique and capped", () => {
    let list = [] as { url: string; at: number }[];
    for (let i = 0; i < 7; i++) list = rememberCore(list, { url: `http://h${i}:1`, at: i });
    list = rememberCore(list, { url: "http://h5:1", at: 9 });
    expect(list.length).toBe(5);
    expect(list[0].url).toBe("http://h5:1");
    expect(list.filter((c) => c.url === "http://h5:1").length).toBe(1);
  });
  test("a LAN failure names Local Network; a token and ATS failure have their own words", () => {
    expect(connectProblem("did not answer", "http://192.168.1.20:4747").body).toContain("Local Network");
    expect(connectProblem("did not answer", "https://core.example.com").body).not.toContain("Local Network");
    expect(connectProblem("This Core asks for a token.", "http://x").title).toContain("token");
    expect(connectProblem("transport security", "http://x").title).toContain("HTTP");
  });
  test("health samples keep the newest, summarize and draw bars", () => {
    let s: (number | null)[] = [];
    for (let i = 0; i < 40; i++) s = pushSample(s, i);
    expect(s.length).toBe(30);
    expect(s[0]).toBe(10);
    const h = healthSummary([40, null, 20, 60]);
    expect(h).toEqual({ now: 60, median: 40, failed: 1 });
    const bars = traceBars([50, null, 200]);
    expect(bars[1]).toEqual({ h: 1, failed: true });
    expect(bars[2].h).toBe(1);
    expect(bars[0].h).toBe(0.25);
    expect(healthSummary([])).toEqual({ now: null, median: null, failed: 0 });
  });
});
