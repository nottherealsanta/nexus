// The recorder's real NSH surface (plan 54, UI-NSH): the header entry, the
// large modal, Recent, delete/quit confirmations, Esc and focus, against a
// mocked Core. The capture controller is the real one; a storage-gated silent
// source stands in for a microphone (no hardware), and Core's public state is
// mocked. Global event application and the shared view-model have their own
// focused tests; opening here never POSTs or opens the input.
import { describe, expect, test } from "nsh/test";
import { open } from "./open-bundle";
import { applyRecorderGlobalEvent, applyRecorderRecoveryOutcomes, recorderStartBlockReason, recorderViewNow, recoverRecorderAtLaunch, startRecorder } from "./src/ui/recorder";
import { acquireMicrophone, microphoneOwner } from "./src/audio-capture";
import { emptyTranscript } from "./src/recorder-view";
import { recorderCore, recorderFailure, recorderRecent, recorderSeenInterrupted, recorderSnapshot, recorderStartup, recorderTranscript } from "./src/store";

const recordingId = "0123456789abcdef0123456789abcdef";
const session = {
  id: "ses_1", project_id: "prj_1", directory: "/work/demo", title: "Recorder test", model: "", permission: "ask",
  created: 1, updated: 2, effort: null, agent: "build", parent_id: null, archived: 0, revision: 1, disabled_tools: "[]",
};
const connected = `event: server.connected\ndata: {"generation":"log_t","head":0,"head_cursor":"log_t:0","floor":0,"stream_id":"stream_t"}\n\n`;
const publicState = (extra: Record<string, unknown> = {}) => ({
  id: recordingId, generation: 1, revision: 1, capture_state: "recording", active: true,
  durable_offset: 0, durable_bytes: 0, chunks: 0, holes: 0, complete: false,
  transcription_state: "preparing", transcription_progress: { lag_samples: 0, pending_finals: 0, failed: 0, transcript_revision: 0 },
  transcript_revision: 0, ...extra,
});
const interrupted = publicState({
  revision: 3, capture_state: "interrupted", active: false, durable_offset: 120_000,
  final_sample_count: 120_000, complete: false, transcription_state: "complete", reason: "lease_expired",
});
const stopped = publicState({
  revision: 6, capture_state: "stopped", active: false, durable_offset: 240_000,
  final_sample_count: 240_000, complete: true, transcription_state: "complete",
  transcription_progress: { lag_samples: 0, pending_finals: 0, failed: 0, transcript_revision: 2 },
  transcript_revision: 2,
});
const created = { ...publicState(), lease_token: "lease_test", recovery_token: null, recovery: "client" };
const transcript = {
  recording_id: recordingId, generation: 1, segments: [
    { segment_id: 0, revision: 2, start: 0, end: 112_000, status: "final", text: "Hello there." },
  ],
  live: { provisional: null }, transcript_revision: 2, state: "live", complete: false,
};

function network(extra: Record<string, any> = {}) {
  return {
    "GET */__nexus/host": { body: { host: "test", canStart: false } },
    "GET */api/info": { body: { name: "nexus", version: "0.10.0.dev0", pid: 1 } },
    "GET */api/config": { body: { revision: "test", values: { defaults: { model: "", effort: "", agent: "", permission: "ask" }, tools: { disabled: [] }, ui: { detail: "medium", reduced_motion: true } }, defaults: {}, schema: {}, errors: [] } },
    "GET */api/snapshot*": { body: { watermark: { generation: "log_t", head: 0, floor: 0, head_cursor: "log_t:0" }, projects: [{ id: "prj_1", path: "/work/demo", name: "demo", created: 1, updated: 2, revision: 1 }], sessions: [session], running: [], approvals: [], questions: [], selected: null } },
    "GET */api/event*": { body: connected, headers: { "content-type": "text/event-stream" } },
    "GET */api/provider": { body: [] },
    "GET */api/model/last*": { body: { model: "scripted/m" } },
    "GET */api/model*": { body: { source: "t", models: [], model: "scripted/m" } },
    "GET */api/agent*": { body: { agents: [], problems: [] } },
    "GET */api/skill*": { body: { skills: [], problems: [] } },
    "GET */api/mcp*": { body: { servers: [] } },
    "GET */api/session/ses_1/*": { body: null },
    "GET */api/session/ses_1/turn*": { body: { turns: [], more_before: false, more_after: false, floor: null, usage: { since: null, models: {} } } },
    "GET */api/session/ses_1/input": { body: { held: false, inputs: [] } },
    "GET */api/session/*": { body: session },
    "GET */api/recordings?limit=50": { body: { recordings: [] } },
    "POST */api/recordings": { status: 201, body: created },
    "GET */api/recordings/*/transcript?after=-1&limit=200": { body: transcript },
    "GET */api/recordings/*/transcript.json": { body: { segments: transcript.segments, complete: false } },
    "GET */api/recordings/*": { body: stopped },
    "POST */api/recordings/*/stop": { delay: 300, body: stopped },
    "DELETE */api/recordings/*": { status: 204, body: null },
    ...extra,
  } as Record<string, any>;
}

async function openRecorder(extra: Record<string, any> = {}, seam: "silent" | "no-persist" | "reload" = "silent") {
  const app = await open("./App.tsx", {
    network: network(extra), width: 1280, height: 800, platform: "web",
    storage: { "nexus.recorder.test": seam },
  });
  await app.waitFor("id:splash", { state: "gone" });
  await app.waitFor("id:open-recorder");
  return app;
}

const posts = (app: any, suffix: string) => app.requests().filter((r: any) => r.method === "POST" && r.url.includes(suffix));
const recordingRequests = (app: any) => app.requests().filter((r: any) => r.url.includes("/api/recordings"));

describe("recorder header and modal", () => {
  test("the header entry sits left of Notifications and opens without creating a recording or microphone", async () => {
    let listCalls = 0;
    const app = await openRecorder({
      "GET */api/recordings?limit=50": () => ({ body: { recordings: listCalls++ === 0 ? [] : [{ ...stopped }] } }),
    });
    expect(recordingRequests(app).some((r: any) => r.method === "POST")).toBe(false);
    const recorder = app.layout("id:open-recorder");
    const manager = app.layout("id:open-manager-context");
    expect(recorder).not.toBe(null);
    expect(manager).not.toBe(null);
    expect(recorder!.x < manager!.x).toBe(true);
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitFor("text:Hello there.");
    expect(app.texts()).toContain("Recorder");
    expect(recordingRequests(app).some((r: any) => r.method === "POST")).toBe(false);
    expect(posts(app, "/api/recordings").length).toBe(0);
    expect(recordingRequests(app).map((r: any) => `${r.method} ${new URL(r.url).pathname}${new URL(r.url).search}`).slice(-3)).toEqual([
      "GET /api/recordings?limit=50",
      `GET /api/recordings/${recordingId}`,
      `GET /api/recordings/${recordingId}/transcript?after=-1&limit=200`,
    ]);
    expect(app.accessibility("id:recorder-dialog")?.role).toBe("dialog");
    expect(app.shows("id:recorder.start")).toBe(true);
    expect(app.shows("id:recorder.recent")).toBe(true);
  });

  test("app startup loads the interrupted state and opening the modal keeps the interrupted header", async () => {
    const app = await openRecorder({
      "GET */api/recordings?limit=50": { body: { recordings: [{ ...interrupted }] } },
      "GET */api/recordings/*": { body: interrupted },
    });
    await app.waitUntil(() => app.texts().some((t) => t.includes("Recording interrupted")));
    expect(recordingRequests(app).length).toBeGreaterThanOrEqual(3);
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitUntil(() => app.texts().some((t) => t.includes("Interrupted at") && t.includes("The recorder lost its connection to Core")));
    await app.waitUntil(() => app.texts().some((t) => t.includes("Recording interrupted")));
    expect(app.texts().join("\n")).not.toContain("Listening…");
  });

  test("reload discovery recovers the active spool and leaves a locked spool untouched", async () => {
    const active = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const locked = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const app = await openRecorder({
      [`GET */api/recordings/${active}`]: { body: publicState({ id: active }) },
      [`GET */api/recordings/${locked}`]: { body: publicState({ id: locked }) },
    }, "reload");
    await app.waitUntil(() => app.requests().some((r: any) => r.url.includes(`/api/recordings/${active}`)));
    expect(app.requests().some((r: any) => r.url.includes(`/api/recordings/${locked}`))).toBe(false);
    expect(recordingRequests(app).filter((r: any) => r.method === "POST").length).toBe(0);
    expect(app.texts().join("\n").includes("recovery_token")).toBe(false);
  });

  test("another client's capture shows no Listening row and no owner-only controls", async () => {
    const app = await openRecorder({
      "GET */api/recordings?limit=50": { body: { recordings: [{ ...publicState() }] } },
      "GET */api/recordings/*": { body: publicState() },
    });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitUntil(() => app.texts().some((t) => t.includes("Recording on another client")));
    expect(app.texts().join("\n")).not.toContain("Listening…");
    expect(app.shows("id:recorder.stop")).toBe(false);
  });

  test("Start is unavailable with an accurate refusal when persistent storage is denied, before Core or microphone", async () => {
    const app = await openRecorder({}, "no-persist");
    expect(recordingRequests(app).filter((r: any) => r.method === "POST").length).toBe(0);
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitFor("id:recorder-startup-notice");
    expect(app.texts().join("\n")).toContain("Persistent storage is required for recording");
    expect(app.texts().join("\n").includes("Persistent browser storage")).toBe(false);
    expect(app.texts().join("\n").includes("Unsaved audio is kept in memory only")).toBe(false);
    expect(app.enabled("id:recorder.start")).toBe(false);
    expect(recordingRequests(app).filter((r: any) => r.method === "POST").length).toBe(0);
  });

  test("initial focus is Start recording, so Enter starts without stopping by accident", async () => {
    const app = await openRecorder({ "GET */api/recordings/*": { body: publicState() } });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitFor("id:recorder.start");
    await app.waitUntil(() => app.enabled("id:recorder.start"));
    await app.keyboard.press("enter");
    await app.waitUntil(() => app.texts().some((t) => t.includes("REC 00:00")));
    expect(posts(app, "/api/recordings").length).toBe(1);
    // Enter never chose Stop: the microphone is still on and the modal is open.
    expect(app.shows("id:recorder.stop")).toBe(true);
  });

  test("opening, then Esc, closes idle and restores the header entry", async () => {
    const app = await openRecorder();
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.keyboard.press("escape");
    await app.waitFor("id:recorder-modal", { state: "gone" });
    expect(app.shows("id:open-recorder")).toBe(true);
  });

  test("Start uses the real controller, opens the modal's recording action row, and Esc minimises without stopping", async () => {
    const app = await openRecorder({ "GET */api/recordings/*": { body: publicState() } });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitUntil(() => app.enabled("id:recorder.start"));
    await app.press("id:recorder.start");
    await app.waitUntil(() => posts(app, "/api/recordings").length === 1);
    await app.waitUntil(() => app.texts().some((t) => t.includes("REC 00:00")));
    expect(app.texts().join("\n")).toContain("REC");
    expect(app.shows("id:recorder.minimize")).toBe(true);
    expect(app.shows("id:recorder.stop")).toBe(true);
    await app.keyboard.press("escape");
    await app.waitFor("id:recorder-modal", { state: "gone" });
    expect(app.texts().join("\n")).toContain("REC");
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.press("id:recorder.stop");
    await app.waitUntil(() => posts(app, "/stop").length === 1);
  });

  test("scrolling up shows Jump to live; choosing it follows the tail again", async () => {
    const segments = Array.from({ length: 24 }, (_, index) => ({
      segment_id: index, revision: 1, start: index * 16_000, end: (index + 1) * 16_000,
      status: "final", text: `Segment ${index}.`,
    }));
    const app = await openRecorder({
      "GET */api/recordings?limit=50": { body: { recordings: [{ ...stopped }] } },
      "GET */api/recordings/*/transcript?after=-1&limit=200": { body: { ...transcript, segments } },
    });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.waitFor("text:Segment 23.");
    expect(app.shows("id:jump_live")).toBe(false);
    await app.scroll("id:recorder-transcript", -240);
    await app.waitFor("id:jump_live");
    await app.keyboard.press("end");
    await app.waitFor("id:jump_live", { state: "gone" });
  });

  test("Recent lists Core's recordings, delete asks first and cancel keeps it", async () => {
    const app = await openRecorder({ "GET */api/recordings?limit=50": { body: { recordings: [{ ...stopped }] } } });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.press("id:recorder.recent");
    await app.waitFor("id:recent.0");
    expect(app.texts().join("\n")).toContain("Complete");
    await app.press("id:recorder.delete");
    await app.waitFor("id:confirm-dialog");
    expect(app.texts()).toContain("Delete this recording?");
    await app.press("id:confirm-cancel");
    await app.waitFor("id:confirm-dialog", { state: "gone" });
    expect(app.shows("id:recent.0")).toBe(true);
    expect(app.requests().some((r: any) => r.method === "DELETE")).toBe(false);
    await app.keyboard.press("escape");
    await app.waitFor("id:recorder-modal");
    expect(app.shows("id:recorder.start")).toBe(true);
  });
});

describe("recorder quit and close", () => {
  test("quitting while this client records asks Stop and exit / Keep recording; Keep recording cancels", async () => {
    const app = await openRecorder({ "GET */api/recordings/*": { body: publicState() } });
    await app.press("id:open-recorder");
    await app.waitFor("id:recorder-modal");
    await app.press("id:recorder.start");
    await app.waitUntil(() => posts(app, "/api/recordings").length === 1);
    await app.waitUntil(() => app.texts().some((t) => t.includes("REC 00:00")));
    await app.keyboard.press("mod+p");
    await app.waitFor("id:palette");
    await app.type("id:palette-input", "Quit");
    await app.keyboard.press("enter");
    await app.waitFor("id:recorder-quit-dialog");
    expect(app.texts()).toContain("Stop recording and quit?");
    expect(app.shows("id:stop_and_exit")).toBe(true);
    expect(app.shows("id:keep_recording")).toBe(true);
    await app.press("id:keep_recording");
    await app.waitFor("id:recorder-quit-dialog", { state: "gone" });
    expect(app.texts().join("\n")).toContain("REC");
    expect(posts(app, "/stop").length).toBe(0);
  });
});

describe("global recorder events and reconnect state", () => {
  const reset = () => {
    recorderSnapshot.set(null);
    recorderCore.set(null);
    recorderTranscript.set(emptyTranscript());
    recorderRecent.set([]);
    recorderSeenInterrupted.set(false);
    recorderFailure.set(null);
    recorderStartup.set({ ready: false, recovering: true, restartRequired: false, error: null, issues: [], storagePersistent: null, storageReason: null });
  };

  test("another client's capture becomes a header pill with the modal closed", () => {
    reset();
    applyRecorderGlobalEvent({
      type: "recorder.state",
      data: { ...publicState(), id: "r_elsewhere", revision: 3, durable_offset: 160_000 },
    });
    const view = recorderViewNow();
    expect(["REC elsewhere", "REC"].includes(view.header)).toBe(true);
    expect(view.a11y).toBe("Recorder, recording on another client");
    expect(view.capture).toBe("Recording on another client · 00:10. This client's microphone is off.");
  });

  test("a newer state revision wins; an older one is ignored", () => {
    reset();
    applyRecorderGlobalEvent({ type: "recorder.state", data: { ...publicState(), id: "r_events", revision: 7 } });
    applyRecorderGlobalEvent({ type: "recorder.state", data: { ...publicState(), id: "r_events", revision: 9, capture_state: "stopped", active: false, complete: true } });
    expect(recorderCore()?.revision).toBe(9);
    applyRecorderGlobalEvent({ type: "recorder.state", data: { ...publicState(), id: "r_events", revision: 8 } });
    expect(recorderCore()?.revision).toBe(9);
  });

  test("live segments build the transcript and a deletion clears the shown recording", () => {
    reset();
    applyRecorderGlobalEvent({ type: "recorder.state", data: { ...publicState(), id: "r_seg", revision: 1 } });
    applyRecorderGlobalEvent({ type: "recorder.segment", data: { id: "r_seg", generation: 1, segment_id: 0, revision: 1, start: 0, end: 8000, status: "final", text: "One." } });
    applyRecorderGlobalEvent({ type: "recorder.segment", data: { id: "r_seg", generation: 1, segment_id: 1, revision: 1, start: 8000, status: "provisional", text: "Two" } });
    expect(recorderViewNow().rows.map((row) => row.text)).toEqual(["One.", "Two…"]);
    applyRecorderGlobalEvent({ type: "recorder.segment", data: { id: "r_seg", generation: 1, segment_id: 0, revision: 0, start: 0, end: 8000, status: "final", text: "Old." } });
    expect(recorderViewNow().rows[0].text).toBe("One.");
    applyRecorderGlobalEvent({ type: "recorder.deleted", data: { id: "r_seg", generation: 1 } });
    expect(recorderCore()).toBe(null);
    expect(recorderTranscript().segments).toEqual([]);
    expect(recorderViewNow().header).toBe("Record");
  });
});

describe("real controller level and shared microphone busy reason", () => {
  test("the view-model reads the controller's level from the snapshot", () => {
    recorderFailure.set(null);
    recorderCore.set(null);
    recorderTranscript.set(emptyTranscript());
    recorderSnapshot.set({
      phase: "recording", failure: null, recordingId: "r_level", capturedSamples: 0, pendingSamples: 0,
      spoolGuarantee: "persistent", recoverable: false, lastError: null, level: 0.625,
    } as any);
    const view = recorderViewNow();
    expect(view.state.ownership).toBe("owner");
    expect(view.state.level).toBe(0.625);
  });

  test("the controller's browser storage refusal is shown with the shared platform-neutral wording", () => {
    recorderFailure.set(null);
    recorderCore.set(null);
    recorderTranscript.set(emptyTranscript());
    recorderSnapshot.set({
      phase: "failed", failure: "storage", recordingId: null, capturedSamples: 0, pendingSamples: 0,
      spoolGuarantee: "memory", recoverable: false,
      lastError: "Persistent browser storage is required for the recorder", level: 0,
    } as any);
    expect(recorderViewNow().capture).toBe("Failed: Persistent storage is required for recording");
  });

  test("a shared microphone holder becomes recorder.busy.mic with the same owner words", async () => {
    recorderStartup.set({ ready: true, recovering: false, restartRequired: false, error: null, issues: [], storagePersistent: true, storageReason: null });
    const cases = [
      ["dictation", "The microphone is in use by dictation."],
      ["manager", "The microphone is in use by the Manager's voice."],
      ["recorder", "The microphone is in use by the recorder."],
    ] as const;
    for (const [holder, reason] of cases) {
      recorderSnapshot.set(null);
      recorderFailure.set(null);
      const lease = acquireMicrophone(holder);
      try {
        await startRecorder();
        expect(recorderFailure()?.reason).toBe(reason);
        expect(recorderViewNow().capture).toBe(`Failed: ${reason}`);
      } finally {
        lease.release();
      }
    }
  });
});

describe("launch recovery of pending create intents", () => {
  test("recovery runs before Start and opens no microphone", async () => {
    recorderSnapshot.set(null);
    recorderFailure.set(null);
    recorderStartup.set({ ready: false, recovering: true, restartRequired: false, error: null, issues: [], storagePersistent: null, storageReason: null });
    expect(recorderStartBlockReason()).toBe("Recovering recordings…");
    const before = microphoneOwner();
    await recoverRecorderAtLaunch();
    expect(recorderStartup().recovering).toBe(false);
    expect(recorderStartBlockReason()).not.toBe("Recovering recordings…");
    expect(microphoneOwner()).toBe(before);
  });

  test("a full Core create guard keeps Start unavailable with the TUI wording", async () => {
    recorderSnapshot.set(null);
    applyRecorderRecoveryOutcomes([
      { key: "pending-guard", outcome: "kept", code: "create_guard_full", restartRequired: true },
    ]);
    expect(recorderStartBlockReason()).toBe("Core's recorder create guard is full; restart Core before creating another recording");
    const before = microphoneOwner();
    await startRecorder();
    expect(microphoneOwner()).toBe(before);
    expect(recorderSnapshot()).toBe(null);
  });

  test("busy and locked spools are quiet; spool failures surface", () => {
    applyRecorderRecoveryOutcomes([], [
      { recordingId: "active", outcome: "skipped_active" },
      { recordingId: "locked", outcome: "skipped_locked", code: "locks_unavailable" },
    ]);
    expect(recorderStartup().issues).toEqual([]);
    expect(recorderStartup().error).toBe(null);
    applyRecorderRecoveryOutcomes([], [{ recordingId: "failed", outcome: "failed", code: "io_error" }]);
    expect(recorderStartup().issues).toEqual(["io_error"]);
    expect(recorderStartup().error).toContain("io_error");
  });

  test("pending and discovered-spool recovery issues are surfaced", () => {
    applyRecorderRecoveryOutcomes(
      [{ key: "pending-failed", outcome: "failed", code: "invalid_recovery" }],
      [{ recordingId: "spool-failed", outcome: "failed", code: "recovery_impossible" }],
    );
    expect(recorderStartup().error).toContain("invalid_recovery");
    expect(recorderStartup().error).toContain("recovery_impossible");
    expect(recorderStartup().issues).toEqual(["invalid_recovery", "recovery_impossible"]);
  });
});

describe("transcription updates do not rewrite a capture interruption", () => {
  test("a later recorder.state reason=transcription keeps the capture reason", () => {
    recorderSnapshot.set(null);
    recorderCore.set(null);
    recorderTranscript.set(emptyTranscript());
    recorderRecent.set([]);
    recorderSeenInterrupted.set(false);
    applyRecorderGlobalEvent({ type: "recorder.state", data: { ...publicState(), id: "r_reason", revision: 1 } });
    applyRecorderGlobalEvent({
      type: "recorder.state",
      data: { ...publicState(), id: "r_reason", revision: 2, capture_state: "interrupted", active: false, reason: "lease_expired" },
    });
    applyRecorderGlobalEvent({
      type: "recorder.state",
      data: {
        ...publicState(), id: "r_reason", revision: 3, capture_state: "interrupted", active: false, reason: "transcription",
        transcription_state: "complete",
      },
    });
    expect(recorderCore()?.reason).toBe("lease_expired");
    expect(recorderViewNow().capture).toContain("The recorder lost its connection to Core");
  });
});
