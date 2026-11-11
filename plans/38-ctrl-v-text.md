# 38. Ctrl+V pastes text too

User request (2026-11-11): Ctrl+V should also work when the clipboard holds only text.

## Today

- **TUI** (`nexus-tui/src/app.rs` ~10618): in the composer, Ctrl+V always starts `Effect::PasteImage`, which runs
  `images::clipboard()` (`images.rs` ~181: `osascript` reading PNG/TIFF on macOS, `wl-paste --type image/png` or
  `xclip -t image/png` on Linux). With text only on the clipboard it returns `Ok(None)` and `apply_image_paste`
  (~15791) shows the toast **"No image on the clipboard; use your terminal's paste shortcut for text"**. Nothing is
  pasted. Text paste works only through the terminal's own shortcut (Cmd+V in Ghostty → bracketed paste →
  `handle_paste`).
- **NSH** (`nexus-nsh/src/ui/composer.tsx` ~444): the input's `onPaste` takes images first, then `pasteText`, so
  the platform paste key already handles both. Ctrl+V is not bound separately (on macOS the paste key is Cmd+V).

## Plan

- [ ] **TUI:** when Ctrl+V finds no image, read the clipboard **text** and insert it through the same path as a
  bracketed paste (`handle_paste`), so long pastes still collapse into the paste preview and the question lock still
  applies. Text readers: macOS `pbpaste` (or `NSPasteboardTypeString` in the same `osascript` call, so it is one
  process), Linux `wl-paste --no-newline --type text/plain` / `xclip -selection clipboard -o`. Same 5 s timeout
  and a size limit matching the composer's paste limit.
- [ ] One clipboard read decides: image if present (as today), otherwise text, otherwise the toast "The clipboard is
  empty". If the clipboard has both an image and text (e.g. copied from a browser), the image wins, as today. Ask
  the user whether to paste both. Default: image only.
- [ ] Over SSH there is no local clipboard: keep the error, reworded to "Ctrl+V reads the local clipboard; over SSH
  use your terminal's paste shortcut".
- [ ] Missing tool on Linux (`wl-paste`/`xclip` not installed): name it in the error.
- [ ] **NSH:** confirm the same result for each platform paste key: web Ctrl+V (Linux/Windows) and Cmd+V (macOS) paste
  text and images. Ctrl+V on macOS web/desktop is not a paste key in browsers. Ask the user whether NSH should also
  bind Ctrl+V on macOS for parity with the TUI. Default: yes, through the same `onPaste` path.
- [ ] Manifest: document Ctrl+V as "Paste (image or text)" for the composer; update the note that says text needs the
  terminal shortcut. `check_manifest.py`.
- [ ] Tests: TUI unit test with a fake clipboard reader (image → marker, text → inserted text, long text → collapsed
  preview, empty → toast); NSH test for text-only paste. A real check in Ghostty with text-only and image clipboards.
- [ ] Docs: `nexus-tui/README.md` and `nexus-nsh/README.md` (paste), `interface/design-language.md` if it mentions
  the toast.
