#!/usr/bin/env bash
# Install Nexus 0.10 from this checkout: the Core `nexus` command (uv tool),
# the TUI binary and the NSH web bundle.
#
#   ./install.sh               install or reinstall
#   ./install.sh --uninstall   remove them; $NEXUS10_HOME is never touched
#
# PREFIX (default ~/.local) holds bin/nexus-tui and share/nexus/web.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
WEB="$PREFIX/share/nexus/web"

die() { printf 'install: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH${2:+ ($2)}"; }

case "${1:-}" in
  --uninstall)
    command -v uv >/dev/null 2>&1 && uv tool uninstall nexus10 2>/dev/null || true
    rm -f "$BIN/nexus-tui"
    rm -rf "$WEB"
    printf 'Removed the nexus10 tool, %s/nexus-tui and %s.\nYour data in %s is untouched.\n' \
      "$BIN" "$WEB" "${NEXUS10_HOME:-$HOME/.nexus10}"
    exit 0 ;;
  "") ;;
  *) die "unknown argument $1" ;;
esac

need uv "https://docs.astral.sh/uv/"
need cargo "https://rustup.rs"
need npm
need nsh "npm install -g nsh"

printf '› Core (uv tool)\n'
uv tool install --force --reinstall "$ROOT/nexus-core"

printf '› TUI\n'
(cd "$ROOT/nexus-tui" && cargo build --release)
mkdir -p "$BIN"
install -m 755 "$ROOT/nexus-tui/target/release/nexus-tui" "$BIN/nexus-tui"

printf '› NSH web bundle\n'
(cd "$ROOT/nexus-nsh" && { [ -d node_modules ] || npm install; } && npm run build)
rm -rf "$WEB"
mkdir -p "$WEB"
cp -R "$ROOT/nexus-nsh/dist/." "$WEB/"

printf '\nInstalled. Run `nexus doctor` to check the install.\n'
case ":$PATH:" in *":$BIN:"*) ;; *) printf 'Add %s to PATH.\n' "$BIN" ;; esac
