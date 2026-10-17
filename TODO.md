# TODO: Ratatui UI polish

- [x] 1. **Polish the top bar.** Keep the tabs, separate them with vertical dividers, and move the workspace path and branch name onto the next line.
   - Status: implemented in current WIP; Rust chrome tests cover the three-row bar with a deliberately blank row between tabs and workspace/branch, and Python tests cover branch refresh.
   - Context: the visual sequence is tabs, blank row, workspace/branch. Branch changes are discovered on the background polling cycle (about every 3 seconds), not immediately; a shell command that might change branch refreshes it directly.
- [ ] 2. **Fix sidebar-button rendering.** Investigate stale refreshes that leave stray characters on the Sessions and sidebar buttons, including after closing the sidebar.
   - Status: not reliably reproduced or visually verified in a native terminal. Existing tests cover redraw/layout invalidation logic only.
   - Context: leftover glyphs remain after redraws. Need an actual reproduction before concluding the fix works; do not treat Models navigation as fully verified on the strength of the unit tests either.
- [x] 3. **Improve the context header.** Add spacing between items; show counts after Skills, Tools, and MCPs; show token counts without a `~` prefix.
   - Status: implemented in current WIP and covered by focused render tests; the left-side vertical rail has been removed.
   - Context: items have a blank gutter instead of a drawn rail. Counts sit next to each label, and token values appear as plain numbers rather than approximations.
- [ ] 4. **Polish GPT Live.** Improve the visual design while keeping real-time transcription as it is.
   - Status: live-card metadata styling and input/output meter coloring are implemented and test-covered, but only terminal-cell output is verified, not the visual result; overall polish remains open.
   - Context: this is a visual-only change; the live transcription behavior is already as wanted and should not change.
   - **Long requests:** Live delegation requests are capped at 200,000 characters including the composed voice note and transcript. If clipped, an exact-count omission notice and durable error row announce the lost tail. The overall visual-polish item remains open.
- [x] 5. **Simplify tool-group headings.** Do not include failed-tool counts or thought counts. Use a lighter font, and remove group-level spinners/animation. Remove the thinking animation; keep any remaining activity animation limited to an individual tool.
   - Status: implemented in existing WIP and covered by focused transcript tests.
   - Context: group headings should be quiet summaries; any motion belongs on the specific tool that is running, not on the group or on thinking.
- [x] 6. **Improve individual tool rows.** Show concise, useful summaries—for example, Read should show the file, offset, and limit; Bash should show only the first command line followed by an ellipsis. Let users expand/click to see the full details, and apply the same principle to other tools.
   - Status: completed in existing WIP; Read/Bash summaries and full-detail disclosure covered by Rust tests.
   - Context: rows should be short by default, with the full input/output available on demand. The per-tool summary for tools other than Read and Bash is still to be decided.
- [x] 7. **Simplify sub-agent cards.** Show the agent name and task, then the model and a compact highlighted tier abbreviation (`L`, `M`, or `H`) in the agent's color. Add a tool-call count and token count (show unknown usage as unknown); do not show the latest tool in real time.
   - Status: done on the strength of tests establishing the stable card structure (name, task, model/tier, tool-call and token counts, no latest-tool text); preexisting, not newly implemented. No screenshot visual approval was made.
   - Context: cards should show stable summary info (name, task, model/tier, counts) instead of a live feed of the latest tool.
- [x] 8. **Fix and simplify Models settings navigation.** Verify the up/down-arrow behavior that was reported to loop through entries/tier options; existing Rust tests now cover walking tier lists once. Reduce unnecessary information only where it is clearly redundant.
   - Status: focused Rust settings-page tests and Python Models settings tests pass (parent-verified: cargo test/build exit 0; broad targeted Python 224 passed). A native e2e keyboard traversal test (`test_models_down_and_up_visit_each_stop_once_and_clamp_at_both_ends`) passed (parent-verified: `pytest -k models_down` => 1 passed, 5 deselected). Marked done on automated native keyboard traversal only; visual/manual interactive review was not performed.
   - Context: the arrow navigation regression may already be addressed in current WIP, so verify in the running UI before changing its stops.
- [x] 9. **Redesign the Sessions page.** Make it a compact, settings-like page rather than full-screen; show each session on one line with an icon, name, and last-used time. Use the existing session status icons.
   - Status: implemented and covered by native PTY session tests; the current report is 1 passing session check (rerun pending).
   - Context: the compact row keeps recognizable status and last-used information without the full-screen dashboard layout.
- [x] 10. **Add space above the composer.** Restore one blank line between the activity/status row (spinner, waiting-for-response text, live token counter, and Stop button) and the composer so the two areas do not touch.
   - Context: that one-line gap has disappeared and should be present again.

*Organized from the voice request and its attached transcript.*
