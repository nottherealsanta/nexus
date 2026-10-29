#!/usr/bin/env bash
# TUI load benchmark (TURN_SUMMARY_PLAN step 0/5): seed one long session in an
# isolated Nexus home, start its Core, run the release TUI's `--bench` and stop
# the Core (always). Never touches ~/.nexus10.
#   tools/bench.sh <root> [turns] [runs]
# Environment: NEXUS_CMD (Core CLI, default `uv run nexus`, run from
# nexus-core), NEXUS_PORT (default 4797), BENCH_SIZE (default 160x48),
# NEXUS_TUI_BIN (default target/release/nexus-tui, built if missing).
# Prints the seed summary (events, event data bytes) and one --bench report per run.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
core="$here/../../nexus-core"
root="${1:?usage: bench.sh <root> [turns] [runs]}"
turns="${2:-40}"
runs="${3:-3}"
cmd="${NEXUS_CMD:-uv run nexus}"
port="${NEXUS_PORT:-4797}"
size="${BENCH_SIZE:-160x48}"
bin="${NEXUS_TUI_BIN:-$here/../target/release/nexus-tui}"
[ -x "$bin" ] || (cd "$here/.." && cargo build --release)
mkdir -p "$root"
root="$(cd "$root" && pwd)"
home="$root/home"
case "$home" in "$HOME/.nexus10"*|"$HOME/.nexus"*) echo "bench.sh: refusing $home" >&2; exit 2;; esac
nexus() { (cd "$core" && NEXUS10_HOME="$home" NEXUS_PORT="$port" $cmd "$@"); }
trap 'nexus daemon stop >/dev/null 2>&1 || true' EXIT
if [ ! -f "$home/seed.json" ]; then
  rm -rf "$home"; mkdir -p "$home"; chmod 700 "$home"
  (cd "$core" && NEXUS10_HOME="$home" uv run python "$here/bench_seed.py" "$turns") > "$home/seed.json.tmp"
  mv "$home/seed.json.tmp" "$home/seed.json"
fi
cat "$home/seed.json"; echo
ls -l "$home"/*.db* 2>/dev/null || true
nexus daemon start >/dev/null
for i in $(seq 1 "$runs"); do
  echo "--- run $i"
  NEXUS10_HOME="$home" NEXUS_TUI_NO_START=1 NEXUS10_REDUCED_MOTION=1 "$bin" --dark --bench "$size"
done
