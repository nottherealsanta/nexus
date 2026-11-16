# nexus-pixel (POC)

The NSH web app rendered inside a terminal pane with
[PixelGUI](https://github.com/zenbu-labs/terminal-browser/tree/main/pixel) (`@zenbu-labs/pixel`,
pinned 0.2.24). PixelGUI runs `main.cjs` in its patched Electron runtime: the page renders in an
offscreen Chromium WebView and reaches the terminal as kitty graphics frames, with mouse and keys
forwarded back. It is the same web build `nexus web` serves, so there is no new UI here and nothing
to keep in parity.

```sh
./scripts/nexus pixel            # NSH host on 127.0.0.1:4821 (next free port if taken), then the app
./scripts/nexus pixel --port 4900 # this port or fail
```

- Needs a terminal with the kitty graphics protocol (Ghostty, kitty). In tmux it works but slower.
- Ctrl-Q quits (Ctrl-C goes to the page). The NSH host the launcher started stops with it.
- Web storage persists in the `persist:nexus-pixel` partition.
- PixelGUI's stderr log: `$XDG_STATE_HOME/pixel/logs/`. The NSH host log:
  `$NEXUS10_HOME/logs/nexus-pixel-host.log`.
- `npm install` here downloads the Electron runtime (~330 MB in `node_modules`).

Status: experimental proof of concept, handled like `nexus-gpui`: it does not ship. It is not part of
`nexus build`, `nexus check`, `install.sh`, the release artifacts or release-please, and nothing
that ships may depend on it. See plans/decisions.md (Terminal pixel host POC).
