#!/usr/bin/env bash
# Build or run the NSH (Native Shell) client in rust/app. See docs/nsh-client.md.
#   nsh-app.sh package web     build the web bundle into nexus/ui/nsh/ (git-ignored)
#   nsh-app.sh run web         run the app's web dev server
#   nsh-app.sh run macos       mint a launch ticket from `nexus web --no-browser`,
#                              then run the native app with NEXUS_URL/NEXUS_TICKET
#   nsh-app.sh package macos   build the .app with `nsh package --target macos`
#                              (output: rust/app/.nsh/out/)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app="$root/rust/app"
nsh="${NSH:-nsh}"
if ! command -v "$nsh" >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/nsh" ]; then
    nsh="$HOME/.local/bin/nsh"
  else
    echo "nsh not found: install it or set NSH=/path/to/nsh" >&2
    exit 1
  fi
fi

usage() { echo "usage: $0 package web|macos | run web|macos" >&2; exit 2; }
[ "$#" -eq 2 ] || usage

# Prints `export NEXUS_URL=... NEXUS_TICKET=...` words derived from `nexus web`.
launch_env() {
  local nexus="${NEXUS:-$root/.venv/bin/nexus}" url
  [ -x "$nexus" ] || nexus="nexus"
  url="$("$nexus" web --no-browser | tail -n 1)"
  case "$url" in
    *"/app/?ticket="*) ;;
    *) echo "unexpected output from nexus web: $url" >&2; exit 1 ;;
  esac
  echo "NEXUS_URL=${url%%app/?ticket=*}" "NEXUS_TICKET=${url##*ticket=}"
}

case "$1:$2" in
  package:web)
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    (cd "$app" && "$nsh" package --target web --out "$tmp")
    # `nsh package` writes <out>/<app name>/ for web; accept a flat output too.
    src="$tmp"
    [ -f "$src/index.html" ] || src="$(dirname "$(find "$tmp" -mindepth 2 -maxdepth 2 -name index.html | head -n 1)")"
    [ -f "$src/index.html" ] || { echo "nsh produced no index.html" >&2; exit 1; }
    dest="$root/nexus/ui/nsh"
    rm -rf "$dest"
    mkdir -p "$dest"
    cp -R "$src"/. "$dest"/
    echo "NSH web bundle written to $dest"
    ;;
  package:macos)
    cd "$app" && exec "$nsh" package --target macos
    ;;
  run:web)
    cd "$app" && exec "$nsh" run --target web
    ;;
  run:macos)
    # Warm the build first: the launch ticket lives only 60 s.
    (cd "$app" && cargo build --quiet)
    # shellcheck disable=SC2046
    env $(launch_env) bash -c 'cd "$1" && exec "$2" run --target macos' _ "$app" "$nsh"
    ;;
  *) usage ;;
esac
