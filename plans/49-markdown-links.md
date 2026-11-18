# Markdown links

Requested 2026-11-17: web and file links should be clickable in both desktop clients; web links open externally and file links open the workspace file.

- [x] Assistant timeline Markdown: carry destinations through wrapping and click handling in the TUI; route NSH `TextView` link presses to the browser or file tab.
- [ ] Extend the same file-link behavior to the other Markdown surfaces in both clients (file previews, context documents, Manager cards and side questions).
- [ ] Verify paired screenshots and real link clicks in both clients. Navigate to a referenced line if supported by both file views.

Phone file tabs remain excluded by the mobile client scope. The TUI test exercises timeline clicks; the NSH test checks workspace path resolution, not an actual link press.
