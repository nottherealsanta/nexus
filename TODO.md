# TODO: Ratatui UI polish

- [x] 1. **Polish the top bar.** Keep the tabs, separate them with vertical dividers, and move the workspace path and branch name onto the next line.
   - Status: implemented in existing WIP and covered by focused chrome tests.
   - Context: the tabs stay as they are; only the divider and the placement of workspace/branch info change, so the top row is less crowded.
- [ ] 2. **Fix sidebar-button rendering.** Investigate stale refreshes that leave stray characters on the Sessions and sidebar buttons, including after closing the sidebar.
   - Status: not reliably reproduced or verified; investigate in a native terminal run.
   - Context: leftover glyphs remain after redraws. The cause is not yet known, so investigate before changing invalidation.
- [x] 3. **Improve the context header.** Add spacing between items; show counts after Skills, Tools, and MCPs; show token counts without a `~` prefix.
   - Status: implemented in existing WIP and covered by focused render tests.
   - Context: the header should be easier to scan. Counts sit next to each label, and token values appear as plain numbers rather than approximations.
- [ ] 4. **Polish GPT Live.** Improve the visual design while keeping real-time transcription as it is.
   - Status: visual redesign remains open; long-request handling was checked and appears to be in the existing WIP.
   - Context: this is a visual-only change; the live transcription behavior is already as wanted and should not change.
   - **Long requests:** Preserve the explicit 200,000-character cap, and ensure any omitted tail is clearly announced rather than silently dropped.
- [x] 5. **Simplify tool-group headings.** Do not include failed-tool counts or thought counts. Use a lighter font, and remove group-level spinners/animation. Remove the thinking animation; keep any remaining activity animation limited to an individual tool.
   - Status: implemented in existing WIP and covered by focused transcript tests.
   - Context: group headings should be quiet summaries; any motion belongs on the specific tool that is running, not on the group or on thinking.
- [x] 6. **Improve individual tool rows.** Show concise, useful summaries—for example, Read should show the file, offset, and limit; Bash should show only the first command line followed by an ellipsis. Let users expand/click to see the full details, and apply the same principle to other tools.
   - Status: completed in existing WIP; Read/Bash summaries and full-detail disclosure covered by Rust tests.
   - Context: rows should be short by default, with the full input/output available on demand. The per-tool summary for tools other than Read and Bash is still to be decided.
- [ ] 7. **Simplify sub-agent cards.** Show the agent name and task, then the model and a compact highlighted tier abbreviation (`L`, `M`, or `H`) in the agent's color. Add a tool-call count and token count (show unknown usage as unknown); do not show the latest tool in real time.
   - Context: cards should show stable summary info (name, task, model/tier, counts) instead of a live feed of the latest tool.
- [ ] 8. **Fix and simplify Models settings navigation.** Verify the up/down-arrow behavior that was reported to loop through entries/tier options; existing Rust tests now cover walking tier lists once. Reduce unnecessary information only where it is clearly redundant.
   - Status: focused Rust settings-page tests and Python Models settings tests pass; interactive bug reproduction remains unverified.
   - Context: the arrow navigation regression may already be addressed in current WIP, so verify in the running UI before changing its stops.
- [x] 9. **Redesign the Sessions page.** Make it a compact, settings-like page rather than full-screen; show each session on one line with an icon, name, and last-used time. Use the existing session status icons.
   - Context: the compact row keeps recognizable status and last-used information without the full-screen dashboard layout.
- [x] 10. **Add space above the composer.** Restore one blank line between the activity/status row (spinner, waiting-for-response text, live token counter, and Stop button) and the composer so the two areas do not touch.
   - Context: that one-line gap has disappeared and should be present again.

*Organized from the voice request and its attached transcript.*
