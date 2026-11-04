# Skill picker verification — 2026-11-04

Both clients use `interface/scenarios/skill-picker.json`: 13 named skills, a long description,
complete Markdown with YAML frontmatter, headings, emphasis, lists, code and a link,
and supporting files/folders. The same scenario is sliced to 12 to check the boundary.

User review: picker cards are borderless, using a dark-gray fill and a lighter gray
for the focused card in both clients. The shared `skill_card` / `skill_card_focus`
theme roles select the fills. Focused names are underlined; NSH also underlines the
switch when it has keyboard focus, without introducing a box outline.

## Compared screenshots

TUI: real `App`/Ratatui renderer, 170×50, SVG rasterized in Chrome. NSH: real web renderer,
1440×900, using `SkillPickerFixture.tsx`. Both use dark mode. The surrounding shell is
intentionally absent from the isolated NSH fixture; the modal content, order and interaction
are the comparison targets. Pixel sizes and visible row counts differ with the medium.

| State | TUI | NSH |
| --- | --- | --- |
| 12 skills, inline | [image](tui-inline-12.png) | [image](nsh-inline-12.png) |
| 13 skills, count only | [image](tui-inline-13.png) | [image](nsh-inline-13.png) |
| Two-column picker | [image](tui-picker-13.png) | [image](nsh-picker-13.png) |
| Full description and supporting resources | [image](tui-detail-13.png) | [image](nsh-detail-13.png) |
| Scrolled Markdown, YAML and code | [image](tui-detail-markdown-13.png) | [image](nsh-detail-markdown.png) |
| Scrolled picker before details | [image](tui-scrolled-13.png) | [image](nsh-scrolled-13.png) |
| Picker restored after details | [image](tui-return-13.png) | [image](nsh-restored-scroll.png) |

Visual review caught and fixed clipped long TUI paragraphs. Both clients now show the full
wrapped description and render the file's frontmatter as YAML, with its body as Markdown.

## Checks

- TUI `tests/skill_picker.rs`: **2 passed**, including 12/13, inline-name details, separate
  Space toggle/Enter detail actions, long-description wrapping, Markdown/resource content,
  scrolling and nested Esc restoration to cursor 5. Optional screenshot output:
  `NEXUS_SKILL_SHOTS=/tmp/skill-shots cargo test --test skill_picker` from `nexus-tui`.
- NSH `skill-picker.test.tsx`: **3 passed** in an isolated browser build: 12/13, toggles,
  nested details from both cards and inline names, Markdown/resources, no-resources message,
  and two-step Escape. Existing `context-header.test.tsx`: **5 passed**.
- Additional actual-browser checks: focused a card name and pressed Enter to open details;
  scrolled the detail body; pressed Escape; picker scroll offset stayed **600 → 600** and
  focus returned to **`skill-name-debugging`**; focused its switch and pressed Enter,
  obtaining **Disabled** without opening details. Screenshots were taken after transitions settled.
- Core `tests/test_skills.py`: **17 passed**; complete file contents, sorted folder/file
  names, directories suffixed `/`, and empty supporting-resource listings covered.
- Targeted Core Ruff and shared manifest/theme checks: **pass**.

## Broader-suite limitations

The working tree includes concurrent, unrelated work. Full TUI has eight failures: the seven
already documented in PLAN.md (Markdown, five shell expectations, timeline text selection)
and `manager::palette_context_modal_closes_before_the_dropdown` in the concurrently changed
Manager tests. Skill tests pass; no unrelated changes were reverted.

Normal NSH checking/building fails resolving native `nsh-core`'s unavailable `declarations`
feature. Browser verification used a disposable copy without its native Rust crate and with
unused microphone capture stubbed; production sources were not changed to bypass it. The
isolated check additionally reports existing nullable `app.clock` assertions and missing
native Mermaid/Annotate types. The three existing full-App shared scenarios fail startup
with HTTP 404 in that isolated harness, before reaching their scenario content; they are
not counted as passing or claimed to be new skill regressions. Native-client and live-Core
end-to-end verification remain unperformed.
