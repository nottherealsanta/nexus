#!/usr/bin/env bash
# Real TUI dump-frame captures for the Ctrl+` / Ctrl+~ terminal toggle.
# Requires scripts/up.sh to have started the scratch Core and seeded ids.json.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
S=${NEXUS_TOGGLE_SCRATCH:-/tmp/nexus-terminal-toggle-2026-11-11}
OUT=${1:-$HERE/../after}
BIN=${NEXUS_TUI_BIN:-$ROOT/nexus-tui/target/debug/nexus-tui}
CHROME=${CHROME:-$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell 2>/dev/null | tail -1)}
[ -x "$BIN" ] || { echo "missing TUI binary: $BIN" >&2; exit 2; }
[ -x "$CHROME" ] || { echo "missing headless Chromium; set CHROME" >&2; exit 2; }
mkdir -p "$OUT"

IDS=$(python3 - "$S/ids.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["project_id"]); print(d["session_id"])
PY
)
PROJECT=$(printf '%s\n' "$IDS" | sed -n 1p)
SESSION=$(printf '%s\n' "$IDS" | sed -n 2p)
HOME_DIR="$S/home"
size=170x50

frame() { # name keys
  local name="$1" keys="$2"
  rm -f "$HOME_DIR/tui.json"
  NEXUS10_HOME="$HOME_DIR" NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 \
    "$BIN" --dark --dump-frame "$size" --out "$OUT" --name "$name" --svg \
    --open first --keys "$keys" >/dev/null
  "$CHROME" --headless --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --window-size="$(( 170 * 84 / 10 + 1 )),$(( 50 * 18 + 1 ))" \
    --screenshot="$OUT/$name.png" "file://$OUT/$name.svg" >/dev/null 2>&1
  echo "tui frame: $OUT/$name.png"
}

probe() { # name
  python3 "$HERE/probe.py" list --home "$HOME_DIR" --project "$PROJECT" \
    --out "$OUT/$1-terminals.json" >/dev/null
}

# 1. Empty dock opens from the session.
frame tui-01-open 'ctrl+`'
# 2. New terminal: Enter on the empty list; waits are inert spaces at the shell prompt.
frame tui-02-created 'ctrl+` enter enter enter space space space space cat space -v enter'
probe tui-02
python3 - "$OUT/tui-02-terminals.json" "$OUT/tui-terminal-id.txt" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))["terminals"]
row = next((r for r in rows if r["status"] == "running" and not r["creator"].startswith("probe")), None)
open(sys.argv[2], "w").write((row or {}).get("id", "") + "\n")
print("TUI terminal:", row["id"] if row else "NONE (create failed)")
PY
# 3. Dock focused on the existing running shell; Ctrl+` closes the panel.
frame tui-03-toggle-backtick-close 'ctrl+` space space space ctrl+`'
probe tui-03
# 4. Reopen, then Ctrl+~ closes from shell focus.
frame tui-04-toggle-tilde-close 'ctrl+` space space space ctrl+~'
probe tui-04
# 5. Reopen the same resource and type into cat -v. Leaked toggle bytes would show ^@ / ^^.
frame tui-05-reopened 'ctrl+` space space space X enter'
probe tui-05
# 6. Leave a parity marker under the prompt for the NSH capture of the same resource.
frame tui-06-parity 'ctrl+` space space ctrl+c printf space '"'"'NEXUS_TUI_TOGGLE_PARITY\n'"'"' enter'
probe tui-06

echo "TUI capture complete in $OUT"
