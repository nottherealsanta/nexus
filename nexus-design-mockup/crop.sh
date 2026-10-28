#!/bin/bash
# crop.sh in.svg|png out.png x y w h [scale] — zoom into part of a shot (pixels of the 1x image).
here="$(cd "$(dirname "$0")" && pwd)"
chrome=$(ls -d ~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell | tail -1)
s=${7:-1.5}; tmp=$(mktemp -d)/v.html
w=$(python3 -c "print(int($5*$s))"); h=$(python3 -c "print(int($6*$s))")
echo "<body style='margin:0;overflow:hidden'><div style='width:${w}px;height:${h}px;overflow:hidden'><img src='file://$(cd "$(dirname "$1")" && pwd)/$(basename "$1")' style='transform-origin:0 0;transform:scale($s) translate(-$3px,-$4px)'></div>" > "$tmp"
"$chrome" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files --window-size="$w,$h" --screenshot="$2" "file://$tmp" >/dev/null 2>&1
