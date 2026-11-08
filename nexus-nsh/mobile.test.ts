import { describe, expect, test } from "nsh/test";
import { PHONE_TABS, compactApproval, desktopOnly, mobileSettings, phoneRow, phoneStatus, mobileClient } from "./src/mobile";
import { manifest } from "./src/gen/manifest";

describe("mobile client rules", () => {
  test("uses the cut-down client on iPhone and iPad but keeps desktop and web", () => {
    expect(mobileClient(true, "ios", "phone")).toBe(true);
    expect(mobileClient(true, "ios", "tablet")).toBe(true);
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
});
