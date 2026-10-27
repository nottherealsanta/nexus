#!/usr/bin/env bash
# Start the real Core processes and NSH hosts for visual/interaction runs; leave them running.
#   e2e/up.sh <scratch-dir>     (state files: <scratch>/{ids.json,pids,fix.pid,*.log}); stop with e2e/down.sh <scratch-dir>
# Real daemon (`nexus daemon start`, no projects) on 4811 behind host 4821; scripted Core (seeded) on 4812 behind host 4820.
set -euo pipefail
cd "$(dirname "$0")/.."
S=$(mkdir -p "$1" && cd "$1" && pwd)
rm -rf "$S/home-real" "$S/home-fix" "$S/ws"; mkdir -p "$S/home-real" "$S/home-fix" "$S/ws"; chmod 700 "$S/home-real" "$S/home-fix"
(cd ../nexus-core && NEXUS10_HOME="$S/home-real" NEXUS_PORT=4811 uv run nexus daemon start)
(cd ../nexus-core && NEXUS10_HOME="$S/home-fix" NEXUS_PORT=4812 nohup uv run python ../nexus-nsh/e2e/scripted_core.py >"$S/fix.log" 2>&1 & echo $! >"$S/fix.pid")
for i in $(seq 1 60); do [ -f "$S/home-fix/daemon.json" ] && curl -s -o /dev/null localhost:4812 && break; sleep 0.3; done
node e2e/seed.mjs "$S/home-fix" "$S/ws/nexus-demo" "$S/ws/docs-site" >"$S/ids.json"
NEXUS10_HOME="$S/home-real" NEXUS_NSH_NO_START=1 nohup node scripts/serve.mjs --port 4821 >"$S/host-real.log" 2>&1 & echo $! >"$S/host-real.pid"
NEXUS10_HOME="$S/home-fix" NEXUS_NSH_NO_START=1 nohup node scripts/serve.mjs --port 4820 >"$S/host-fix.log" 2>&1 & echo $! >"$S/host-fix.pid"
sleep 1; echo "up: scripted host http://127.0.0.1:4820, real-daemon host http://127.0.0.1:4821"
