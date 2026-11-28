// V-NSH: the pure recorder view-model against the shared fixture. No UI, store
// or network is involved. Text assertions use the generated manifest strings
// when D0 has regenerated them; until then the same cases still pin the tone,
// actions, focus, Esc, widths and every state transition, and the missing
// string keys are reported as a generated-manifest dependency.
import { describe, expect, test } from "nsh/test";
import fixture from "../interface/scenarios/recorder-view.json";
import { manifest } from "./src/gen/manifest";
import {
  applyTranscriptEvent,
  applyTranscriptPage,
  emptyTranscript,
  formatElapsed,
  formatRecentDate,
  missingRecorderStrings,
  recorderText,
  recorderView,
  RECORDER_VIEW_STRINGS,
} from "./src/recorder-view";
import type {
  RecorderSegmentEvent,
  RecorderText,
  RecorderViewInput,
} from "./src/recorder-view";

const cases = fixture.cases as Array<{
  name: string;
  input: Record<string, unknown>;
  expect: Record<string, unknown>;
}>;

const sharedStrings = manifest.strings as unknown as Record<string, string | undefined>;
const stringsReady = Object.prototype.hasOwnProperty.call(sharedStrings, "recorder.header.idle");

/** A text provider that exposes the selected key and substituted values. */
const tokenText: RecorderText = (key, args = {}) => {
  const values = Object.keys(args).sort().map((name) => `${name}=${args[name]}`).join(",");
  return values ? `${key}(${values})` : key;
};

type ViewInput = RecorderViewInput | Record<string, unknown>;

function inputOf(input: ViewInput): RecorderViewInput {
  return input as unknown as RecorderViewInput;
}

function view(input: ViewInput): ReturnType<typeof recorderView> {
  return recorderView(inputOf(input), tokenText);
}

function withText(input: ViewInput): ReturnType<typeof recorderView> {
  return recorderView(inputOf(input));
}

describe("shared recorder view fixture", () => {
  for (const scenario of cases) {
    test(scenario.name, () => {
      const expected = scenario.expect;
      const actual = stringsReady ? withText(scenario.input) : view(scenario.input);

      expect(actual.header_tone).toBe(expected.header_tone);
      expect(actual.button_ids).toEqual(expected.buttons);
      expect(Object.keys(actual.disabled).sort()).toEqual(Object.keys(expected.disabled as object).sort());
      expect(actual.focus).toBe(expected.focus);
      expect(actual.esc).toBe(expected.esc);

      const recent = expected.recent as { empty: string | null; notice: string | null; rows: string[] } | undefined;
      if (recent) {
        expect(actual.recent).not.toBeNull();
        expect(actual.recent?.empty === null).toBe(recent.empty === null);
        expect(actual.recent?.notice === null).toBe(recent.notice === null);
        expect((actual.recent?.rows.length ?? 0) >= recent.rows.length).toBe(true);
      } else {
        expect(actual.recent).toBeNull();
      }

      if (stringsReady) {
        expect(actual.disabled).toEqual(expected.disabled);
        expect(actual.header).toBe(expected.header);
        expect(actual.a11y).toBe(expected.a11y);
        if (expected.capture === null) expect(actual.capture).toBeNull();
        else expect(actual.capture).toBe(expected.capture);
        if (expected.audio === null) expect(actual.audio).toBeNull();
        else expect(actual.audio).toBe(expected.audio);
        if (expected.transcript_status === null) expect(actual.transcript_status).toBeNull();
        else expect(actual.transcript_status).toBe(expected.transcript_status);
        if (expected.audio_warning === null) expect(actual.audio_warning).toBeNull();
        else expect(actual.audio_warning).toBe(expected.audio_warning);
        if (expected.audio_detail !== undefined) expect(actual.audio_detail).toBe(expected.audio_detail);
        expect(actual.hint).toBe(expected.hint);
        if (recent) {
          expect(actual.recent?.empty).toBe(recent.empty);
          expect(actual.recent?.notice).toBe(recent.notice);
          expect(actual.recent?.rows.slice(0, recent.rows.length).map((row) => row.label)).toEqual(recent.rows);
        }
      }
    });
  }
});

describe("shared text consumption", () => {
  test("the view-model names every recorder string it needs", () => {
    expect(RECORDER_VIEW_STRINGS.length).toBeGreaterThan(50);
    expect(new Set(RECORDER_VIEW_STRINGS).size).toBe(RECORDER_VIEW_STRINGS.length);
  });

  test("a missing generated string fails loudly instead of inventing wording", () => {
    const missing = missingRecorderStrings(["recorder.title"]);
    if (missing.length > 0) {
      expect(() => recorderText("recorder.title")).toThrow();
    } else {
      expect(recorderText("recorder.title")).toBe(sharedStrings["recorder.title"]);
    }
  });

  test("header selection, placeholders and the one-hour format come from shared keys", () => {
    const hour = view({
      owner: { phase: "recording", recording_id: "r1", captured_samples: 59_568_000, pending_samples: 0, spool: "persistent", recoverable: false, reason: null, level: 0.1 },
      core: null, transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(hour.header).toBe(tokenText("recorder.header.recording", { t: "1:02:03" }));
    expect(hour.a11y).toBe(tokenText("recorder.a11y.recording", { t: "1:02:03" }));
    expect(hour.capture).toBe(tokenText("recorder.capture.recording", { t: "1:02:03" }));

    const compact = view({
      owner: { phase: "starting", recording_id: null, captured_samples: 0, pending_samples: 0, spool: "persistent", recoverable: false, reason: null, level: 0 },
      core: null, transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "compact",
    });
    expect(compact.header).toBe(tokenText("recorder.header.starting_compact"));
    expect(compact.hint).toBe(tokenText("recorder.hint.capturing"));

    const recent = view({
      owner: null, core: null, transcript: null,
      recent: { limit: 50, recordings: [] },
      view: { modal_open: true, page: "recent", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(recent.header).toBe(tokenText("recorder.title"));
    expect(recent.hint).toBe(tokenText("recorder.hint.recent"));
    expect(recent.recent?.empty).toBe(tokenText("recorder.recent.empty"));
  });

  test("interrupted reasons and unsaved-audio detail use shared keys, not literals", () => {
    const viewer = view({
      owner: null,
      core: {
        id: "r1", capture_state: "interrupted", active: false, durable_offset: 11_840_000,
        final_sample_count: null, complete: false, holes: 2, missing_samples: null,
        transcription_state: "live", transcription_progress: { failed: 0 }, reason: "stream_detached",
      },
      transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(viewer.capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "12:20", reason: tokenText("recorder.reason.stream_detached"),
    }));

    const recoverable = view({
      owner: { phase: "interrupted", recording_id: "r1", captured_samples: 12_032_000, pending_samples: 192_000, spool: "persistent", recoverable: true, reason: null, level: 0 },
      core: {
        id: "r1", capture_state: "interrupted", active: false, durable_offset: 11_840_000,
        final_sample_count: null, complete: false, holes: 0, missing_samples: null,
        transcription_state: "live", transcription_progress: { failed: 0 }, reason: "lease_expired",
      },
      transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(recoverable.audio).toBe(tokenText("recorder.audio.unsaved", { s: 12 }));
    expect(recoverable.audio_detail).toBe(tokenText("recorder.audio.saved_detail", { t: "12:20" }));
    expect(recoverable.capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "12:32", reason: tokenText("recorder.reason.lease_expired"),
    }));
  });

  test("memory-only, gaps and unavailable transcription use their shared keys", () => {
    const memory = view({
      owner: { phase: "recording", recording_id: "r1", captured_samples: 16_000, pending_samples: 16_000, spool: "memory", recoverable: false, reason: null, level: 0.2 },
      core: null, transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(memory.audio_warning).toBe(tokenText("recorder.audio.memory_only"));

    const unavailable = view({
      owner: null,
      core: {
        id: "r1", capture_state: "stopped", active: false, durable_offset: 16_000,
        final_sample_count: 16_000, complete: true, holes: 0, missing_samples: 0,
        transcription_state: "unavailable", transcription_progress: { failed: 0 },
      },
      transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    });
    expect(unavailable.transcript_status).toBe(tokenText("recorder.transcript.unavailable"));
  });
});

describe("reason fallback parity with the TUI", () => {
  const base = {
    owner: null, transcript: null,
    view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
    width: "wide",
  };

  const interrupted = (reason: string | null) => view({
    ...base,
    core: {
      id: "r1", capture_state: "interrupted", active: false, durable_offset: 16_000,
      final_sample_count: null, complete: false, holes: 0, missing_samples: null,
      transcription_state: "live", transcription_progress: { failed: 0 }, reason,
    },
  });

  test("known Core codes use the shared reason keys", () => {
    expect(interrupted("stream_detached").capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "00:01", reason: tokenText("recorder.reason.stream_detached"),
    }));
    expect(interrupted("capture_failed").capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "00:01", reason: tokenText("recorder.reason.microphone"),
    }));
  });

  test("an unknown code and a missing code follow the TUI: spaces, then no invented sentence", () => {
    expect(interrupted("some_new_code").capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "00:01", reason: "some new code",
    }));
    expect(interrupted(null).capture).toBe(tokenText("recorder.capture.interrupted_no_reason", {
      t: "00:01",
    }));
  });

  test("no reason omits the separator instead of rendering a trailing colon", () => {
    const displayed = recorderView({
      ...base,
      core: {
        id: "r1", capture_state: "interrupted", active: false, durable_offset: 16_000,
        final_sample_count: null, complete: false, holes: 0, missing_samples: null,
        transcription_state: "live", transcription_progress: { failed: 0 }, reason: null,
      },
    } as unknown as RecorderViewInput, (key, args = {}) => {
      if (key === "recorder.capture.interrupted_no_reason") return `Interrupted at ${args.t}`;
      if (key === "recorder.capture.interrupted") return `Interrupted at ${args.t}: ${args.reason}`;
      return tokenText(key, args);
    });
    expect(displayed.capture).toBe("Interrupted at 00:01");
    expect(displayed.capture?.includes(": ")).toBe(false);
  });

  test("a before-create failure uses its display reason, else the kind with underscores as spaces", () => {
    expect(view({ ...base, core: null, failure: { kind: "mic_busy", reason: "The microphone is in use by dictation." } }).capture)
      .toBe(tokenText("recorder.capture.failed", { reason: "The microphone is in use by dictation." }));
    expect(view({ ...base, core: null, failure: { kind: "mic_busy" } }).capture)
      .toBe(tokenText("recorder.capture.failed", { reason: "mic busy" }));
    expect(view({ ...base, core: null, failure: { kind: "other_client" } }).capture)
      .toBe(tokenText("recorder.capture.failed", { reason: "other client" }));
    expect(view({ ...base, core: null, failure: {} }).capture)
      .toBe(tokenText("recorder.capture.failed", { reason: "" }));
  });

  test("an owner display reason wins over the Core code", () => {
    const owned = view({
      ...base,
      owner: { phase: "interrupted", recording_id: "r1", captured_samples: 16_000, pending_samples: 0, spool: "persistent", recoverable: false, reason: "Microphone disconnected", level: 0 },
      core: {
        id: "r1", capture_state: "interrupted", active: false, durable_offset: 16_000,
        final_sample_count: null, complete: false, holes: 0, missing_samples: null,
        transcription_state: "live", transcription_progress: { failed: 0 }, reason: "lease_expired",
      },
    });
    expect(owned.capture).toBe(tokenText("recorder.capture.interrupted", {
      t: "00:01", reason: "Microphone disconnected",
    }));
  });
});

describe("ellipsis formatting", () => {
  test("uses integer samples and switches to hours at one hour", () => {
    expect(formatElapsed(0)).toBe("00:00");
    expect(formatElapsed(15_999)).toBe("00:00");
    expect(formatElapsed(16_000)).toBe("00:01");
    expect(formatElapsed(112_000)).toBe("00:07");
    expect(formatElapsed(12_064_000)).toBe("12:34");
    expect(formatElapsed(57_600_000)).toBe("1:00:00");
    expect(formatElapsed(59_568_000)).toBe("1:02:03");
  });

  test("formats recent dates without a time-zone shift", () => {
    expect(formatRecentDate("2026-11-26T14:02:00")).toBe("26 Nov 14:02");
    expect(formatRecentDate("2026-11-26T09:40:00")).toBe("26 Nov 09:40");
  });
});

describe("transcript reducer", () => {
  const segment = (id: number, revision: number, status: "final" | "provisional" | "skipped" = "final", text = `s${id}`) => ({
    segment_id: id, revision, start: id * 16_000, end: status === "provisional" ? null : (id + 1) * 16_000, status, text,
  });
  const event = (data: Record<string, unknown>) => ({ type: "recorder.segment", data }) as RecorderSegmentEvent;

  test("keeps the newer revision of a segment and ignores an older one", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 2) }));
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 1) }));
    expect(state.segments).toHaveLength(1);
    expect(state.segments[0].revision).toBe(2);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 3) }));
    expect(state.segments[0].revision).toBe(3);
  });

  test("ignores another recording or generation", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r2", generation: 1, ...segment(0, 1) }));
    expect(state.segments).toHaveLength(0);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 2, ...segment(0, 1) }));
    expect(state.segments).toHaveLength(0);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 1) }));
    expect(state.segments).toHaveLength(1);
  });

  test("always has one provisional tail; a newer final clears it", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 1, "provisional", "one") }));
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 2, "provisional", "two") }));
    expect(state.provisional?.text).toBe("two");
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 3) }));
    expect(state.provisional).toBeNull();
    expect(state.segments[0].status).toBe("final");
  });

  test("orders provisional tails by (segment_id, revision) and never downgrades a final", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(5, 4, "provisional", "five") }));
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(6, 1, "provisional", "six") }));
    expect(state.provisional?.segment_id).toBe(6);
    expect(state.provisional?.text).toBe("six");
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(6, 2) }));
    expect(state.provisional).toBeNull();
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(6, 1, "provisional", "old") }));
    expect(state.provisional).toBeNull();
  });

  test("a page for another recording starts from an empty transcript", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 1) }));
    state = applyTranscriptPage(state, {
      recording_id: "r2", generation: 1, segments: [segment(0, 1)], live: { provisional: null },
    });
    expect(state.recordingId).toBe("r2");
    expect(state.segments).toHaveLength(1);
  });

  test("reconnect replaces the local provisional row from the page", () => {
    let state = emptyTranscript("r1", 1);
    state = applyTranscriptEvent(state, event({ id: "r1", generation: 1, ...segment(0, 4, "provisional", "local") }));
    state = applyTranscriptPage(state, {
      recording_id: "r1", generation: 1, segments: [segment(0, 3)], live: { provisional: null }, transcript_revision: 7,
    });
    expect(state.provisional).toBeNull();
    expect(state.segments[0].revision).toBe(3);
    expect(state.revision).toBe(7);
  });

  test("caps at 5,000 durable segments and reports the clipped prefix", () => {
    const segments = Array.from({ length: 5_001 }, (_, id) => segment(id, 1));
    const state = applyTranscriptPage(emptyTranscript("r1", 1), {
      recording_id: "r1", generation: 1, segments, live: { provisional: null },
    });
    expect(state.segments).toHaveLength(5_000);
    expect(state.segments[0].segment_id).toBe(1);
    expect(state.dropped).toBe(1);
  });

  test("rows skip skipped segments, keep one provisional tail and expose Jump to live", () => {
    const input = {
      owner: null,
      core: {
        id: "r1", capture_state: "stopped", active: false, durable_offset: 64_000,
        final_sample_count: 64_000, complete: true, holes: 0, missing_samples: 0,
        transcription_state: "complete", transcription_progress: { failed: 0 },
      },
      transcript: {
        segments: [
          { segment_id: 0, revision: 1, start: 0, end: 16_000, status: "final", text: "hello" },
          { segment_id: 1, revision: 1, start: 16_000, end: 32_000, status: "skipped" },
          { segment_id: 2, revision: 1, start: 32_000, end: 48_000, status: "error" },
        ],
        provisional: { segment_id: 3, revision: 1, start: 48_000, status: "provisional", text: "and" },
        dropped: 2,
      },
      view: { modal_open: true, page: "recorder", transcript_follow: false, seen_interrupted: false },
      width: "wide",
    };
    const actual = view(input);
    expect(actual.rows.map((row) => row.kind)).toEqual(["earlier", "final", "error", "provisional"]);
    expect(actual.rows[3].text.endsWith("…")).toBe(true);
    expect(actual.rows[3].italic).toBe(true);
    expect(actual.jump_live).toBe(true);
    expect(actual.earlier).toBe(true);
  });

  test("text stays absent, not invented, while the generated manifest is behind", () => {
    const missing = missingRecorderStrings(["recorder.header.idle"]);
    const idleInput = {
      owner: null, core: null, transcript: null,
      view: { modal_open: true, page: "recorder", transcript_follow: true, seen_interrupted: false },
      width: "wide",
    } as unknown as RecorderViewInput;
    if (missing.length === 0) {
      expect(withText(idleInput).header).toBe(sharedStrings["recorder.header.idle"]);
    } else {
      expect(() => recorderView(idleInput)).toThrow();
    }
  });
});
