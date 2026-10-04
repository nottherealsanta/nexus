---
name: native-app-review
description: Launch, exercise, capture, and visually refine a real native desktop app using computer-use screenshots. Use for Rust GPUI and other native app UI work; excludes web-only mockups and generated pictures of apps.
---

# Native app visual review

Build and launch the actual application before judging its appearance. Screenshots must come from the running native window, never an HTML recreation or an image generator.

## Prepare

- Read the project's UI contract and build instructions. Preserve working host integration and the user's requested functionality while improving presentation.
- Use an isolated development workspace and recorded/scripted data for repeatable screenshots. Keep credentials and private conversations out of captures. Label fixture mode visibly, and verify real host integration separately.
- Build with the project's normal compiler. For GPUI on macOS, check that the Metal toolchain is available; do not switch the system developer directory automatically. Pin framework versions and consult that version's source for APIs.
- Capture a matrix that reflects the app: empty conversation, populated transcript, expanded tools/context, settings, model picker, approval/question, attachments, subagent, narrow window, and both themes when supported. Record omissions honestly.
- If a source binary has no application identity, use [scripts/package_macos.py](scripts/package_macos.py) to create a local `.app` wrapper: `python package_macos.py PATH_TO_BINARY OUTPUT.app --name NAME --bundle-id ID`. Launch its `Contents/MacOS` executable through the app's normal bridge when stdin carries presentation data; launching it through Finder would lose that bridge. The helper does not sign or install the app.

## Operate and capture

Use `mcp__cua_repl.js` for native UI interaction. On first use call exactly one entry point, such as `await cua.getState()`, and read its documentation. Bind the app by its bundle ID or full `.app` path with `cua.getApp`. Do not synthesize input through shell scripts, AppleScript, or platform event APIs.

1. Observe with `getAXStateAndScreenshot()`.
2. Use current accessibility indices for controls when available. Otherwise click coordinates derived from the latest screenshot. Batch deterministic actions, then get fresh AX state before choosing another action.
3. Inspect the result of paste before retrying: clipboard restoration can time out even when the text was inserted. Use select-all and replace to recover without duplicating input.
4. Use `getScreenshot()` to inspect the resulting view. Custom-rendered controls may have no AX changes; verify that an asynchronous host response has actually produced the requested page before naming a capture. Rely on the API's capture wait rather than fixed sleeps.
5. Save returned image bytes (inspect the file signature and use `.jpg` or `.png` accordingly; macOS captures in this environment returned JPEG) to the project's ignored artifact directory if the runtime supports file writes. Give captures meaningful state names and record logical window size, theme, fixture, and build revision in a review ledger.

If capture or accessibility is unavailable, report the exact capability failure and pursue other available native capture APIs only when authorized. Do not call an unviewed render "visually verified".

Use [references/review-ledger.md](references/review-ledger.md) as the capture/check record.

## Review and iterate

Judge screenshots at normal scale: hierarchy, readable contrast, line length, spacing, alignment, clipping, scroll behavior, focus, native title bar, and visible errors. Check empty and loading states as carefully as populated ones. Keep tool parameters/results and context inspectable; announce truncation. Use native controls/window chrome rather than simulated traffic lights.

Fix the largest observable issue, rebuild, recapture the affected view, and repeat until no material layout or interaction issue remains. Test actual typing, multiline paste, selection/copy, keyboard navigation, Escape, resizing, and scrolling. A screenshot verifies appearance, not functionality; pair it with focused behavior tests and an integration journey.

Deliver build/run instructions, screenshot locations, verified journeys, and remaining limitations. Do not imply platform coverage based on a single macOS run.

## macOS rebuilds

Close the previous window before replacing its executable. The packaging helper replaces the executable inode to avoid stale macOS code-signing cache after a rebuild. If multiple old windows remain, select by title and close only the review windows; verify the visible build against a known changed label. Computer-use capture may expand the window back to its review viewport even after a resize request succeeds; record narrow-layout capture as unverified in that case, and check the layout with GPUI tests separately. Screenshot coordinates use the returned image dimensions; do not assume logical pixels equal capture pixels.
