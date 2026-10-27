#!/usr/bin/env bash
cd "$(dirname "$0")/.."
S=$(cd "$1" && pwd)
for f in host-real host-fix; do [ -f "$S/$f.pid" ] && kill "$(cat "$S/$f.pid")" 2>/dev/null; done
pkill -f e2e/scripted_core.py 2>/dev/null
(cd ../nexus-core && NEXUS10_HOME="$S/home-real" NEXUS_PORT=4811 uv run nexus daemon stop >/dev/null 2>&1)
echo down
