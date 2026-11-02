---
name: nexus-tui-screenshots
description: Capture and inspect screenshots of the real Nexus TUI, from seeded isolated Cores, at chosen sizes, themes and key presses. Use for TUI visual checks, layout debugging, before-and-after comparisons, or comparing the TUI with NSH, even if the request only says "look at the TUI" or "see how it renders".
---

# TUI screenshots

`nexus-tui --dump-frame` runs the real shell against a real Core, presses the given keys, and writes the frame as text and SVG. Run commands from `nexus-tui`. All of this uses isolated homes and makes no provider calls. Never point `NEXUS10_HOME` at a real home.

## Standard set

```sh
cargo build
ROOT=$(mktemp -d /tmp/tui-shots.XXXXXX)
tools/shots.sh "$ROOT/root" "$ROOT/out"
```

This seeds two Cores (a populated workspace and an empty one), writes `<name>.{txt,svg,png}` to the output directory, and stops both Cores on exit, even after a failure. It covers session, palette, settings, files, new-session, empty-workspace, confirm-delete and slash-completion frames at 200x50, 120x36 and 80x24.

It uses ports 4799 and 4798. Check they're free with `lsof -nP -iTCP -sTCP:LISTEN`; if not, set `NEXUS_PORT` and `NEXUS_PORT_EMPTY`. Don't stop services you didn't start.

Read the PNGs with the image tool. The `.txt` frame is quicker for checking wording and order.

## One state

With a Core already running on an isolated home:

```sh
NEXUS10_HOME="$HOME_DIR" NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 \
  target/debug/nexus-tui --dark --dump-frame 170x50 --out "$OUT" --name my-state --svg \
  --keys "ctrl+k tab Failed enter"
```

- **Clean state:** `tui.json` in the home holds preferences. Delete it first so each capture starts from the defaults.
- **Text only:** leave out `--out`, `--name` and `--svg` to print the frame. A tall size such as `120x130` shows a whole session without scrolling.
- **Keys:** `--keys` takes space-separated tokens, either key names or literal text, and each token gets time for Core to reply. `ctrl+k tab <title> enter` opens a session from any project, because `tab` switches the search scope. See `nexus-tui --help` for the other options, such as `--light`, `--256`, `--16`, `--mono` and `--ascii`.
- **Detail level:** one `alt+d` from the default goes to high detail, which opens every tool.
- **Most recent session:** `--open first` opens it without any keys.

To convert an SVG to a PNG, use headless Chromium at 8.4 × 18 pixels per cell, as the `shot` function in `tools/shots.sh` does.

## Live and waiting states

`tools/shots.sh` seeds settled history only. For a running turn, an approval or a question, use the scripted Core, whose prompts choose the behaviour (see `nexus-nsh/e2e/scripted_core.py`):

```sh
HOME_DIR=$(mktemp -d /tmp/tui-core.XXXXXX); chmod 700 "$HOME_DIR"
WS=$(mktemp -d /tmp/tui-ws.XXXXXX); mkdir -p "$WS/a" "$WS/b"
(cd ../nexus-core && NEXUS10_HOME="$HOME_DIR" NEXUS_PORT=47932 nohup uv run python ../nexus-nsh/e2e/scripted_core.py >"$HOME_DIR/core.log" 2>&1 & echo $! >"$HOME_DIR/core.pid")
until [ -f "$HOME_DIR/daemon.json" ]; do sleep 0.3; done
(cd ../nexus-nsh && node e2e/seed.mjs "$HOME_DIR" "$WS/a" "$WS/b" >"$HOME_DIR/ids.json")
```

Pick a free port. Seeded session titles include `All scenarios`, `Failed tool`, `Thinking now`, `Waiting for approval` and `Pick the migration database`. `Thinking now` finishes after about a minute. To see one live, create a session and prompt it with `think...` over the API, using the token in `daemon.json`, and capture it straight away. When you're done, run `kill "$(cat "$HOME_DIR/core.pid")"`.

## Comparing with NSH

Serve the same scripted Core to NSH, so both clients show identical data:

```sh
(cd ../nexus-nsh && NEXUS10_HOME="$HOME_DIR" NEXUS_NSH_NO_START=1 node scripts/serve.mjs --port 47933)
```

Open `http://127.0.0.1:47933` and capture the same session with Playwright. 170x50 cells is close to a 1440x900 window. For the full parity workflow, see `.agents/skills/nexus-ui-parity/SKILL.md`.

## Troubleshooting

- **`Core did not become live`:** `daemon.json` is missing from that home, or the Core failed to bind its port. Check the Core's log.
- **The search finds nothing:** the session is in another project. Include `tab` to widen the scope.
- **Stale frames:** rerun `cargo build` after changing the source. `--dump-frame` uses the binary as built.
- **Private data:** frames can contain session text and paths. Use seeded data for anything shared.
