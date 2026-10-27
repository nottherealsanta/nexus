#!/usr/bin/env bash
# Start two isolated Cores (mode-700 homes, no provider calls) for the visual
# harness: a seeded workspace and an empty one. Writes <root>/env for shots.mjs.
#   tools/webterm/seed.sh <root>            start and seed
#   tools/webterm/seed.sh <root> stop       stop both
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
core="$here/../../../nexus-core"
root="${1:?usage: seed.sh <root> [stop]}"
mkdir -p "$root"
nexus() { (cd "$core" && NEXUS10_HOME="$1" NEXUS_PORT="$2" uv run nexus "${@:3}"); }
if [ "${2:-}" = "stop" ]; then
  nexus "$root/home" 4799 daemon stop || true
  nexus "$root/home-empty" 4798 daemon stop || true
  exit 0
fi
for h in home home-empty; do rm -rf "$root/$h"; mkdir -p "$root/$h"; chmod 700 "$root/$h"; done
mkdir -p "$root/ws/nexus-demo" "$root/ws/side-project"
nexus "$root/home-empty" 4798 daemon start
nexus "$root/home" 4799 daemon start >/dev/null
token=$(python3 -c "import json,sys;print(json.load(open('$root/home/daemon.json'))['token'])")
api() { curl -sf -X POST -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -H "Idempotency-Key: $2" -d "$3" "http://127.0.0.1:4799$1"; }
field() { python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"; }
p1=$(api /api/project p1 "{\"path\":\"$root/ws/nexus-demo\",\"name\":\"nexus-demo\"}" | field)
p2=$(api /api/project p2 "{\"path\":\"$root/ws/side-project\",\"name\":\"side-project\"}" | field)
first=""
i=0
for t in "Refactor the loader" "Fix flaky test in sync" "Release notes for 0.10" "Review the CJK width handling 日本語"; do
  i=$((i+1)); id=$(api /api/session "s$i" "{\"project\":\"$p1\",\"title\":\"$t\"}" | field)
  [ -z "$first" ] && first=$id
done
api /api/session s9 "{\"project\":\"$p2\",\"title\":\"Spike: terminal harness\"}" >/dev/null
nexus "$root/home" 4799 daemon stop >/dev/null
(cd "$core" && NEXUS10_HOME="$root/home" uv run python "$here/seed_messages.py" "$first")
nexus "$root/home" 4799 daemon start
printf 'WEBTERM_HOME=%s\nWEBTERM_HOME_EMPTY=%s\nWEBTERM_PORT=4799\nWEBTERM_PORT_EMPTY=4798\n' "$root/home" "$root/home-empty" > "$root/env"
echo "seeded; env in $root/env"
