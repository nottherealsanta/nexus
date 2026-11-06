# Voice transcript provenance — 2026-11-06

Both clients insert the same editable message prefix when final dictation adds
nonempty speech: `[Voice transcript — may contain errors.]`. It covers the whole
message, including text typed before or after the speech, and appears once even
after several recordings. Since it is ordinary message text, the context line
and the model receive the same warning; no separate model-only instructions or
new storage fields are needed. Clearing the draft removes the note as well.

OS keyboard dictation does not expose reliable provenance to Nexus and is not
automatically labelled. Existing messages are not retroactively changed.

## Checks

- `cargo test --test voice_transcript`: **2 passed**. Real final-transcription
  completion handling, mixed text, repeated recordings, empty results, clearing,
  composer/timeline rendering and outgoing prompt text.
- `nsh test dictation.test.tsx --target web`: **7 passed**. Shared scenario checks
  warning wording, repeated dictation, whitespace and empty results alongside
  the existing audio/state checks.
- `nsh test voice-transcript.test.tsx --target web`: **1 passed**. Completed draft
  from the real insertion helper is pasted into the actual composer; the stored
  user message renders the same note and the prompt body preserves it verbatim.
  This does not exercise browser microphone capture.
- Browser runs used Playwright's Chrome for Testing via `--browser`, because
  Chrome is not installed at the NSH CLI's default application paths.
- Full TUI suite: **14 failures outside this change**, in the Markdown library
  check, six Manager checks, six shell checks and timeline mouse selection.
  Concurrent UI changes were present; no clean baseline was reset or stashed.
- `nsh check`: known failures at `App.test.tsx:501,517` (`app.clock` nullable)
  and `src/ui/newsession.tsx:331` (`Button.onFocus` unsupported); no errors in
  the changed dictation files or new tests.

## Visual comparison

`tui-composer.png` (160×48 cells) and `nsh-composer.png` (1280×768 web) show
the same mock warning and text in both a stored user message and the draft.
Both images were read and compared: same note, placement before the content,
blank separator and editable draft text. Existing surrounding shell differences
are unchanged. No new client-specific difference.

Screenshots use fixture data, not user sessions. No Core restart, migration,
real-home change, live microphone capture or provider call.
