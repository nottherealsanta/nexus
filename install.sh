#!/bin/sh
# Install Nexus 0.10: the Core `nexus` command (a uv tool), the TUI binary and, when the release has one, the web bundle.
#
#   curl -LsSf https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh | sh
#   ./install.sh                  from a checkout: build everything here (needs uv, cargo, npm and nsh)
#   ./install.sh --release        from a checkout, but install the latest release instead
#   sh install.sh --release v0.10.0     one release
#   ./install.sh --source         build from source even when run some other way
#   ./install.sh --uninstall      remove them; $NEXUS10_HOME is never touched
#
# With no checkout beside this file the default is the latest GitHub release: the files are downloaded, checked against the
# release's SHA256SUMS, and installed. PREFIX (default ~/.local) holds bin/nexus-tui and share/nexus/web.
# NEXUS_RELEASE_BASE overrides https://github.com/nottherealsanta/nexus/releases (a mirror, or file:// for tests).
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
WEB="$PREFIX/share/nexus/web"
BASE="${NEXUS_RELEASE_BASE:-https://github.com/nottherealsanta/nexus/releases}"

die() { printf 'install: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH${2:+ ($2)}"; }
step() { printf '› %s\n' "$*"; }

# The checkout this script sits in, if any. Piped from curl, $0 is just "sh".
ROOT=""
if [ -f "$0" ]; then
  ROOT="$(cd "$(dirname "$0")" && pwd)"
  [ -d "$ROOT/nexus-core" ] || ROOT=""
fi

MODE=""
TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      command -v uv >/dev/null 2>&1 && uv tool uninstall nexus10 >/dev/null 2>&1 || true
      rm -f "$BIN/nexus-tui"
      rm -rf "$WEB"
      printf 'Removed the nexus10 tool, %s/nexus-tui and %s.\nYour data in %s is untouched.\n' \
        "$BIN" "$WEB" "${NEXUS10_HOME:-$HOME/.nexus}"
      exit 0 ;;
    --source) MODE=source ;;
    --release) MODE=release
      case "${2:-}" in v[0-9]*) TAG="$2"; shift ;; esac ;;
    -h|--help) sed -n '2,13p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1 (try --help)" ;;
  esac
  shift
done
[ -n "$MODE" ] || { [ -n "$ROOT" ] && MODE=source || MODE=release; }
[ "$MODE" = release ] || [ -n "$ROOT" ] || die "--source needs a checkout: run ./install.sh from the repository"

done_message() {
  printf '\nInstalled. Run `nexus doctor` to check the install.\n'
  case ":$PATH:" in *":$BIN:"*) ;; *) printf 'Add %s to PATH.\n' "$BIN" ;; esac
}

# -- from a checkout ---------------------------------------------------------------

install_source() {
  need uv "https://docs.astral.sh/uv/"
  need cargo "https://rustup.rs"
  need npm
  need nsh "npm install -g nsh"

  step "Core (uv tool)"
  uv tool install --force --reinstall --no-binary-package claude-agent-sdk "$ROOT/nexus-core"

  step "TUI"
  (cd "$ROOT/nexus-tui" && cargo build --release)
  mkdir -p "$BIN"
  install -m 755 "$ROOT/nexus-tui/target/release/nexus-tui" "$BIN/nexus-tui"

  step "NSH web bundle"
  (cd "$ROOT/nexus-nsh" && { [ -d node_modules ] || npm install; } && npm run build)
  rm -rf "$WEB"
  mkdir -p "$WEB"
  cp -R "$ROOT/nexus-nsh/dist/." "$WEB/"
  done_message
}

# -- from a release ----------------------------------------------------------------

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

release_url() { # file
  if [ -n "$TAG" ]; then printf '%s/download/%s/%s' "$BASE" "$TAG" "$1"; else printf '%s/latest/download/%s' "$BASE" "$1"; fi
}

fetch() { # file, into WORK
  curl -fsSL "$(release_url "$1")" -o "$WORK/$1" || die "could not download $1 from $(release_url "$1")"
}

verify() { # file
  want="$(awk -v f="$1" '$2 == f || $2 == "*" f { print $1 }' "$WORK/SHA256SUMS")"
  [ -n "$want" ] || die "$1 is not listed in this release's SHA256SUMS"
  [ "$(sha256_of "$WORK/$1")" = "$want" ] || die "the checksum of $1 does not match SHA256SUMS; nothing was installed"
}

listed() { awk -v f="$1" '$2 == f || $2 == "*" f { found = 1 } END { exit !found }' "$WORK/SHA256SUMS"; }

install_release() {
  need curl
  need uv "https://docs.astral.sh/uv/"
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$os" in darwin|linux) ;; *) die "Nexus supports macOS and Linux, not $(uname -s)" ;; esac
  if [ "$os" = linux ] && { ldd --version 2>&1 || true; } | grep -qi musl; then
    die "musl (Alpine) is not supported; use a glibc distribution"
  fi
  tui="nexus-tui-$os-$(uname -m)"

  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT INT TERM
  step "Release ${TAG:-latest}"
  curl -fsSL "$(release_url SHA256SUMS)" -o "$WORK/SHA256SUMS" ||
    die "no release found at $(release_url SHA256SUMS); from a checkout, run ./install.sh --source"
  listed "$tui" || die "this release has no build for $os $(uname -m) ($tui)"
  wheel="$(awk '{ n = $2; sub(/^\*/, "", n); if (n ~ /^nexus10-.*\.whl$/) print n }' "$WORK/SHA256SUMS" | head -n 1)"
  [ -n "$wheel" ] || die "this release has no Core wheel"

  step "Download and verify"
  fetch "$wheel"; verify "$wheel"
  fetch "$tui"; verify "$tui"
  web=""
  if listed nexus-web.tar.gz; then web=nexus-web.tar.gz; fetch "$web"; verify "$web"; fi

  step "Core (uv tool)"
  uv tool install --force --reinstall --no-binary-package claude-agent-sdk "$WORK/$wheel"
  step "TUI"
  mkdir -p "$BIN"
  install -m 755 "$WORK/$tui" "$BIN/nexus-tui"
  if [ -n "$web" ]; then
    step "Web bundle"
    rm -rf "$WEB"
    mkdir -p "$WEB"
    tar -xzf "$WORK/$web" -C "$WEB"
  else
    printf 'This release has no web client; `nexus web` needs one built from a checkout.\n'
  fi
  done_message
}

if [ "$MODE" = source ]; then install_source; else install_release; fi
