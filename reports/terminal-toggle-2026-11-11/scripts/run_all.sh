#!/usr/bin/env bash
# Full capture run. Usage: run_all.sh [before|after] [--build]
# With --build, rebuilds the TUI binary and the NSH web host from current source first.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LABEL=${1:-after}
BUILD=${2:-}
OUT="$HERE/../$LABEL"
S=${NEXUS_TOGGLE_SCRATCH:-/tmp/nexus-terminal-toggle-2026-11-11}
CORE_PORT=${NEXUS_TOGGLE_CORE_PORT:-47971}
mkdir -p "$OUT"

if [ "$BUILD" = "--build" ]; then
  (cd "$ROOT/nexus-tui" && cargo build)
  (cd "$ROOT/nexus-nsh" && npm run build)
fi
[ -x "$ROOT/nexus-tui/target/debug/nexus-tui" ] || (cd "$ROOT/nexus-tui" && cargo build)

"$HERE/up.sh"
IDS="$S/ids.json"
PROJECT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_id"])' "$IDS")
WORKSPACE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_path"])' "$IDS")
LAN_IP=${NEXUS_TOGGLE_LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || true)}

# Direct tokenless HTTP evidence: loopback 201, non-loopback 403 (when a LAN address exists).
ARGS=(--home "$S/home" --project "$PROJECT" --workspace "$WORKSPACE" --out "$OUT/http-probe.json")
[ -n "$LAN_IP" ] && ARGS+=(--lan-ip "$LAN_IP")
python3 "$HERE/probe.py" check "${ARGS[@]}" | tee "$OUT/http-probe.log"

"$HERE/tui_capture.sh" "$OUT"
node "$HERE/nsh_capture.mjs" "$OUT"
echo "captures: $OUT"
