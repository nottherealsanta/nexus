# TODO: Ratatui UI polish

- [ ] 1. **Polish the top bar.** Keep the tabs, separate them with a straight divider, and move the workspace path and branch name onto the next line.
   - Context: the tabs stay as they are; only the divider and the placement of workspace/branch info change, so the top row is less crowded.
- [ ] 2. **Fix sidebar-button rendering.** Investigate stale refreshes that leave stray characters on the Sessions and sidebar buttons, including after closing the sidebar.
   - Context: leftover glyphs remain after redraws. The cause is not yet known (possibly incomplete clearing of the button area), so investigate first.
- [ ] 3. **Improve the context header.** Add spacing between items; show counts after Skills, Tools, and MCPs; show token counts without a `~` prefix.
   - Context: the header should be easier to scan. Counts sit next to each label, and token values appear as plain numbers rather than approximations.
- [ ] 4. **Polish GPT Live.** Improve the visual design while keeping real-time transcription as it is.
   - Context: this is a visual-only change; the live transcription behavior is already as wanted and should not change.
   - **Fix long requests.** Prevent user requests from being truncated or omitted when GPT Live sends them to the session. Avoid imposing a request-length limit, while preserving necessary safe handling of large inputs.
     - Context: long spoken requests currently can be cut off or dropped on their way to the session.
- [ ] 5. **Simplify tool-group headings.** Do not include failed-tool counts or thought counts. Use a lighter font, and remove group-level spinners/animation. Remove the thinking animation; keep any remaining activity animation limited to an individual tool.
   - Context: group headings should be quiet summaries; any motion belongs on the specific tool that is running, not on the group or on thinking.
- [ ] 6. **Improve individual tool rows.** Show concise, useful summaries—for example, Read should show the file, offset, and limit; Bash should show only the first command line followed by an ellipsis. Let users expand/click to see the full details, and apply the same principle to other tools.
   - Context: rows should be short by default, with the full input/output available on demand. The per-tool summary for tools other than Read and Bash is still to be decided.
- [ ] 7. **Simplify sub-agent cards.** Show the agent name and task, then the model and a compact highlighted tier abbreviation (for example, `MS` or `H`) in the agent's color. Add a tool-call count and token count; do not show the latest tool in real time.
   - Context: cards should show stable summary info (name, task, model/tier, counts) instead of a live feed of the latest tool.
- [ ] 8. **Fix and simplify Models settings navigation.** Fix the up/down-arrow bug that loops through entries/tier options, and reduce unnecessary information in the settings UI.
   - Context: arrow-key navigation currently cycles through entries or tier options incorrectly. Which information counts as unnecessary is not specified yet.
- [ ] 9. **Redesign the Sessions page.** Make it a compact, settings-like page rather than full-screen; show each session on one line with an icon, name, and last-used time. The requested icon distinction was only partly captured in the recording (it begins “whether it's idle, or…”), so confirm the remaining states before specifying them.
   - Context: the icon is meant to show session status, but only the idle state is known from the recording; do not assume the other states.
- [ ] 10. **Add space above the composer.** Restore one blank line between the activity/status row (spinner, waiting-for-response text, live token counter, and Stop button) and the composer so the two areas do not touch.
   - Context: that one-line gap has disappeared and should be present again.

*Organized from the voice request and its attached transcript. The recording cuts off during the Sessions-page icon description.*
