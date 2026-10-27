# nexus-tui

The Ratatui terminal client (Rust). Not started.

It will speak only the Nexus Core HTTP API (`/api/*` plus the `/api/event`
stream); see `../docs/api.md`. It never reads Nexus state files directly. Old
Nexus's client in `~/repos/nexus/rust/tui` is reference.
