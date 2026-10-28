#!/usr/bin/env bash
# Node-free visual review: seed two isolated Cores, drive the real nexus-tui with
# `--dump-frame --keys --svg`, convert the SVGs to PNG with headless Chromium and
# stop the Cores (always). Output: <out>/<name>.{txt,svg,png}.
#   tools/shots.sh <root> <out>
# Environment: NEXUS_CMD (Core CLI, default `uv run nexus`), NEXUS_PORT and
# NEXUS_PORT_EMPTY (the two Cores' ports, default 4799 and 4798; see
# webterm/seed.sh), NEXUS_TUI_BIN (default target/debug/nexus-tui, built if
# missing), CHROME (headless Chromium or chrome-headless-shell; default: the
# newest Playwright one in ~/Library/Caches/ms-playwright).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="${1:?usage: shots.sh <root> <out>}"
out="${2:?usage: shots.sh <root> <out>}"
bin="${NEXUS_TUI_BIN:-$here/../target/debug/nexus-tui}"
chrome="${CHROME:-$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell 2>/dev/null | tail -1)}"
[ -x "$chrome" ] || { echo "shots.sh: no headless Chromium; set CHROME" >&2; exit 2; }
[ -x "$bin" ] || (cd "$here/.." && cargo build)
mkdir -p "$root" "$out"
root="$(cd "$root" && pwd)"
out="$(cd "$out" && pwd)"

trap '"$here/webterm/seed.sh" "$root" stop >/dev/null 2>&1 || true' EXIT
"$here/webterm/seed.sh" "$root"
# shellcheck disable=SC1091
. "$root/env"   # WEBTERM_HOME, WEBTERM_HOME_EMPTY (and the ports)

# shot NAME WxH THEME HOME [dump-frame options...]; keys go in --keys.
shot() {
  local name="$1" size="$2" theme="$3" home="$4"
  shift 4
  rm -f "$home/tui.json"   # every shot starts from default preferences
  NEXUS10_HOME="$home" NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 \
    "$bin" "--$theme" --dump-frame "$size" --out "$out" --name "$name" --svg "$@" >/dev/null
  local w=${size%x*} h=${size#*x}
  # Cells to pixels (8.4 x 18 px), in integer shell arithmetic: BSD awk and
  # other awks differ in printf, and this needs no float.
  "$chrome" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --window-size="$(( w * 84 / 10 + 1 )),$(( h * 18 + 1 ))" \
    --screenshot="$out/$name.png" "file://$out/$name.svg" >/dev/null 2>&1
  echo "shot $out/$name.png"
}

open_session="ctrl+k Refactor enter"
H="$WEBTERM_HOME"
for size in 200x50 120x36 80x24; do
  for theme in dark light; do
    shot "session-$size-$theme" "$size" "$theme" "$H" --keys "$open_session"
  done
done
shot palette-120x36-dark 120x36 dark "$H" --keys "$open_session ctrl+p"
shot palette-120x36-light 120x36 light "$H" --keys "$open_session ctrl+p"
shot settings-120x36-dark 120x36 dark "$H" --keys "ctrl+,"
shot settings-80x24-light 80x24 light "$H" --keys "ctrl+,"
shot files-tab-120x36-dark 120x36 dark "$H" --keys "alt+2"
shot new-session-120x36-dark 120x36 dark "$H"
shot empty-workspace-120x36-dark 120x36 dark "$WEBTERM_HOME_EMPTY"
# Detail level high (alt+d) expands every tool.
shot tools-expanded-120x36-dark 120x36 dark "$H" --keys "$open_session alt+d"
shot confirm-delete-120x36-dark 120x36 dark "$H" \
  --keys "$open_session ctrl+p del space sess enter"
shot complete-slash-120x36-dark 120x36 dark "$H" --keys "$open_session /mo"
echo "screenshots in $out"
