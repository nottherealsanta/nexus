#!/usr/bin/env bash
# One scratch-home Core (tokenless, wildcard listener) + seeded project/session + NSH local proxy.
# Only processes started here are recorded and later stopped by down.sh.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
S=${NEXUS_TOGGLE_SCRATCH:-/tmp/nexus-terminal-toggle-2026-11-11}
CORE_PORT=${NEXUS_TOGGLE_CORE_PORT:-47971}
NSH_PORT=${NEXUS_TOGGLE_NSH_PORT:-47972}
HOME_DIR="$S/home"
WS="$S/ws"
mkdir -p "$S" "$HOME_DIR" "$WS"
chmod 700 "$HOME_DIR"

busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
for port in "$CORE_PORT" "$NSH_PORT"; do
  if busy "$port"; then
    echo "port $port is already in use; refusing to stop another process. Set NEXUS_TOGGLE_*_PORT." >&2
    exit 2
  fi
done

if [ -f "$S/core.pid" ] && kill -0 "$(cat "$S/core.pid")" 2>/dev/null; then
  echo "Core already running (pid $(cat "$S/core.pid"))"
else
  python3 "$HERE/spawn_detached.py" --pidfile "$S/core.pid" --log "$S/core.log" --cwd "$ROOT/nexus-core" -- \
    env NEXUS10_HOME="$HOME_DIR" NEXUS_PORT="$CORE_PORT" .venv/bin/python -m nexus serve --host 0.0.0.0 >/dev/null
fi
for _ in $(seq 1 100); do [ -f "$HOME_DIR/daemon.json" ] && curl -fsS "http://127.0.0.1:$CORE_PORT/api/info" >/dev/null 2>&1 && break; sleep 0.2; done
curl -fsS "http://127.0.0.1:$CORE_PORT/api/info" >/dev/null || { echo "Core did not start; see $S/core.log" >&2; exit 1; }

python3 "$HERE/seed_terminal.py" --home "$HOME_DIR" --workspace "$WS" --out "$S/ids.json" >"$S/seed.log"
python3 - "$HOME_DIR" <<'PY'
import json, sys
from pathlib import Path
d = json.loads((Path(sys.argv[1]) / "daemon.json").read_text())
assert d["token"] == "", d
assert d["url"].startswith("http://127.0.0.1:"), d
print("daemon.json:", json.dumps(d))
PY

if [ -f "$S/host.pid" ] && kill -0 "$(cat "$S/host.pid")" 2>/dev/null; then
  echo "NSH host already running (pid $(cat "$S/host.pid"))"
else
  python3 "$HERE/spawn_detached.py" --pidfile "$S/host.pid" --log "$S/host.log" --cwd "$ROOT/nexus-nsh" -- \
    env NEXUS10_HOME="$HOME_DIR" NEXUS_NSH_NO_START=1 node scripts/serve.mjs --port "$NSH_PORT" >/dev/null
fi
for _ in $(seq 1 100); do curl -fsS "http://127.0.0.1:$NSH_PORT/__nexus/host" >/dev/null 2>&1 && break; sleep 0.2; done
curl -fsS "http://127.0.0.1:$NSH_PORT/__nexus/host" >/dev/null || { echo "NSH host did not start; see $S/host.log" >&2; exit 1; }

echo "scratch: $S"
echo "Core:    http://127.0.0.1:$CORE_PORT (tokenless wildcard, daemon.json url 127.0.0.1)"
echo "NSH:     http://127.0.0.1:$NSH_PORT"
echo "ids:     $(cat "$S/ids.json")"
