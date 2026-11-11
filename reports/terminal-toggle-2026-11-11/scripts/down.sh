#!/usr/bin/env bash
# Stop only the Core and NSH host recorded in this scratch directory.
set -euo pipefail
S=${NEXUS_TOGGLE_SCRATCH:-/tmp/nexus-terminal-toggle-2026-11-11}
for name in host core; do
  pidfile="$S/$name.pid"
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
done
echo "stopped recorded scratch processes in $S"
