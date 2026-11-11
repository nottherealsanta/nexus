#!/usr/bin/env bash
# Regenerate the TUI screenshots for plans 29-32 in this directory.
# The frames come from nexus-tui's own renderer against the shared fixture and
# scenario data, so no Core or provider call is made. Writes tui-*.{svg,png}.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
chrome="${CHROME:-$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell 2>/dev/null | tail -1)}"
[ -x "$chrome" ] || { echo "no headless Chromium; set CHROME" >&2; exit 2; }

cd "$repo/nexus-tui"
NEXUS_SESSION_CONTROLS_SHOTS="$here" cargo test --test session_controls screenshots

cd "$here"
for svg in tui-*.svg; do
  "$chrome" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --window-size=$((120 * 84 / 10 + 1)),$((36 * 18 + 1)) \
    --screenshot="${svg%.svg}.png" "file://$here/$svg" >/dev/null 2>&1
  echo "wrote $here/${svg%.svg}.png"
done
