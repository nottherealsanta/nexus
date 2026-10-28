#!/bin/bash
# Render every mock-up: TUI shots (SVG/TXT via cargo, PNG via headless Chromium),
# NSH pages (PNG, dark and light), then the gallery. Usage: ./shoot.sh [tui|nsh|gallery] [prefix]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
chrome=$(ls -d ~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell 2>/dev/null | tail -1)
[ -x "$chrome" ] || chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
snap() { "$chrome" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files --force-device-scale-factor=1 --window-size="$3" --screenshot="$2" "$1" >/dev/null 2>&1; }
what="${1:-all}"; only="${2:-}"
if [ "$what" = all ] || [ "$what" = tui ]; then
  (cd "$here/tui" && cargo run -q -- css >/dev/null && cargo run -q -- shoot ${only:+--only "$only"})
  while IFS=$'\t' read -r id w h cap; do
    [ -n "$only" ] && [[ "$id" != "$only"* ]] && continue
    for f in "$here"/shots/tui/"$id"-*.svg; do
      snap "file://$f" "${f%.svg}.png" "$(( w * 84 / 10 )),$(( h * 18 ))"
    done
  done < "$here/shots/tui/manifest.tsv"
fi
if [ "$what" = all ] || [ "$what" = nsh ]; then
  mkdir -p "$here/shots/nsh"
  for f in "$here"/nsh/d*.html; do
    stem=$(basename "${f%.html}"); [ -n "$only" ] && [[ "$stem" != "$only"* ]] && continue
    size=$(grep -o 'data-size="[0-9]*,[0-9]*"' "$f" | head -1 | cut -d'"' -f2); size=${size:-1440,900}
    for th in dark light; do snap "file://$f?theme=$th" "$here/shots/nsh/$stem-$th.png" "$size"; done
  done
fi
if [ "$what" = all ] || [ "$what" = gallery ]; then python3 "$here/gallery.py"; fi
