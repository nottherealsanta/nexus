## Nexus 0.12.0

### Highlights

- Integrated real-PTY terminal docks in the Rust TUI and NSH desktop/web, with reconnect/replay, explicit control ownership, safer lifecycle handling and bounded output. Selected terminal output can be previewed and attached to a draft; it enters model context only when sent.
- Read-only `fork` subagents inherit the parent's context for minor investigations, with a 20-step limit, recursion guards, parent tool rows and an expandable inherited-context block in both clients.
- File create/rename/delete actions, Git backend and paired desktop Git surfaces, and editable source with find/replace, save-conflict protection and undo/redo coverage.
- Shared provider usage tables, inspector terminal diagnostics and event-driven refresh, clearer question cards, conservative multi-edit matching, and step-budget notices.
- Mobile capability guards preserve the Sessions, Manager and Settings-only client. Apple native microphone/bootstrap services are included; physical-device/audio and remote update verification remain open.
- Installer download retries and daemon restart handling, plus terminal shutdown, editor shortcut, recovery and regression-test fixes.

### Important compatibility and security notes

- The maintained terminal client is the Rust `nexus-tui`. The experimental `nexus nsh tui` target is retired; its code remains archived.
- **Core binds to `0.0.0.0` without a token by default for LAN/Tailscale access. Run it only on a trusted network behind appropriate firewall rules.** Set `NEXUS_HOST=127.0.0.1` to opt into loopback with token protection. Native bootstrap honors an explicit `NEXUS10_HOME` rather than falling back to a personal daemon.
- Published artifacts include the four macOS/Linux TUI binaries, Core wheel/sdist, installer and checksums. The existing release workflow excludes the NSH web bundle; build the web/native clients from source.
- GPUI remains an experimental spike. Native/device/audio checks, the wider platform runtime matrix and remaining editor/Git visual-parity gaps are not claimed complete. See `PLAN.md` and `reports/release-0.12.0-validation.md` for verification scope.
