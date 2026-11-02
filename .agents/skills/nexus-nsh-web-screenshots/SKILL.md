---
name: nexus-nsh-web-screenshots
description: Quickly capture and inspect Nexus NSH web screenshots while iterating on UI, including seeded sessions, viewport and theme variations, and the hot-reloading development host. Use for NSH web visual checks, screenshots, layout debugging, or before-and-after comparisons.
---

# NSH web screenshots

Use the existing visual harness in `nexus-nsh/e2e/` for repeatable screenshots of the actual NSH web renderer. Run commands from `nexus-nsh`. See `nexus-nsh/README.md` for the host and harness details. For real iPhone rendering, use `nexus-ios-device` instead; a narrow web viewport is not a native device.

## Fast seeded capture

1. Install dependencies if needed (`npm ci`), generate and build (`npm run build`). Playwright needs its Chromium browser; if it is missing, run `npx playwright install chromium`.
2. Check ports 4811, 4812, 4820 and 4821 are free (`lsof -nP -iTCP -sTCP:LISTEN`). Do not stop another person's services. The harness uses these fixed ports.
3. Use a **new, disposable** directory: `up.sh` deletes its `home-real`, `home-fix` and `ws` subdirectories. Keep output elsewhere and never point it at a real Nexus home or workspace.

```sh
S=$(mktemp -d /tmp/nsh-visual.XXXXXX)
OUT=$(mktemp -d /tmp/nsh-shots.XXXXXX)
e2e/up.sh "$S"
node e2e/shots.mjs --out "$OUT" --scripted http://127.0.0.1:4820 \
  --real http://127.0.0.1:4821 --ids "$S/ids.json" --only 1440x900-dark
printf 'Scratch: %s\nScreenshots: %s\n' "$S" "$OUT"
```

Read the relevant PNG with the image tool. Files are named like `1440x900-dark-03-session-messages.png`; the script prints each filename. The seed includes a populated conversation, activity, approval, settings and other states. Remove `--only` for 390×844, 1024×768 and 1440×900 in both themes, or change it to e.g. `390x844-light`. `--only` filters viewport/theme tags, not individual states. The capture script navigates and interacts with the seeded instance; do not substitute a real user's host.

Leave the hosts running while iterating. For the static host, run `npm run build` after a source change, then rerun the `node e2e/shots.mjs` command and inspect the new PNGs. Use a different output directory when preserving a before/after comparison; matching filenames are overwritten. The screenshot script without `--kill-pid` does not capture the reconnecting state.

## Faster source iteration with hot reload

With the seeded Core from `up.sh` still running, start a separate development host and proxy on free ports (e.g. 8790 and 4822):

```sh
nsh dev App.tsx --target web --port 8790
# In another terminal, with S set to the same scratch directory:
NEXUS10_HOME="$S/home-fix" NEXUS_NSH_NO_START=1 npm run serve -- --port 4822 --upstream http://127.0.0.1:8790
```

Open `http://127.0.0.1:4822` in the browser, navigate to the desired state and take a screenshot using the browser screenshot tool. Open the proxy, not port 8790 directly: it supplies Core authentication. Alternatively point `--scripted` at port 4822 in the capture command above (keep `--real` at 4821). Source changes reload through the dev host; wait for the UI and data to settle before capturing. Browser tools may capture the current viewport at its existing size; use the Playwright harness for exact viewport/theme settings.

## Cleanup and troubleshooting

Stop the extra dev host and proxy you started. The checked-in `e2e/down.sh` uses a broad `pkill -f e2e/scripted_core.py`, so do **not** run it, or `e2e/run.sh` (which calls it), if another scripted Core may be running. When safe, `e2e/down.sh "$S"` stops the harness. Preserve screenshots as needed; do not delete an existing directory without checking its contents.

- A blank or stale app: rebuild for the static host; check `host-fix.log`, `fix.log` and browser errors in the scratch directory. Confirm the proxy port and that the header loaded.
- Port conflicts or failed startup: inspect listeners and logs; choose an isolated environment rather than killing unrelated processes. Startup can leave partial processes behind.
- `npm run build` requires the NSH tooling documented in `nexus-nsh/README.md`; the capture requires Playwright Chromium. The harness runs Core via `uv` in `nexus-core`.
- Screenshots can contain session text and paths. Use the seeded scratch data for shareable images; treat live-session captures as private.
