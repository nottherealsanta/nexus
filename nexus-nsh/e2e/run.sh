#!/usr/bin/env bash
# up + shots + down. e2e/run.sh <scratch-dir> <shots-out-dir> [extra shots.mjs args, e.g. --only 1440x900-dark]
set -euo pipefail
cd "$(dirname "$0")/.."
S=$(mkdir -p "$1" && cd "$1" && pwd)
trap 'e2e/down.sh "$S"' EXIT
e2e/up.sh "$S"
KILL=$(python3 -c "import json;print(json.load(open('$S/home-fix/daemon.json'))['pid'])")
node e2e/shots.mjs --out "$2" --scripted http://127.0.0.1:4820 --real http://127.0.0.1:4821 --ids "$S/ids.json" --kill-pid "$KILL" "${@:3}"
