#!/bin/sh
# Install Nexus 0.10: the Core `nexus` command (a uv tool), the TUI binary, `firecrawl-local` (downloaded from its own
# public release, checksum-verified) and, when the release has one, the web bundle.
#
#   curl -LsSf https://github.com/nottherealsanta/nexus/releases/latest/download/install.sh | sh
#   ./install.sh                  from a checkout: build everything here (needs uv, cargo, npm and nsh)
#   ./install.sh --release        from a checkout, but install the latest release instead
#   sh install.sh --release v0.10.0     one release
#   ./install.sh --source         build from source even when run some other way
#   ./install.sh --uninstall      remove them; $NEXUS10_HOME is never touched
#
# With no checkout beside this file the default is the latest GitHub release: the files are downloaded, checked against the
# release's SHA256SUMS, and installed. PREFIX (default ~/.local) holds bin/nexus-tui, bin/firecrawl-local and share/nexus/web.
# NEXUS_RELEASE_BASE overrides https://github.com/nottherealsanta/nexus/releases (a mirror, or file:// for tests).
# NEXUS_FIRECRAWL_BASE overrides the download base of
# https://github.com/nottherealsanta/firecrawl-local-builds/releases (the pinned firecrawl-local binaries).
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
WEB="$PREFIX/share/nexus/web"
BASE="${NEXUS_RELEASE_BASE:-https://github.com/nottherealsanta/nexus/releases}"
# Pinned where the binaries are published, outside this repository; Core pins the same version for the protocol it speaks.
FC_VERSION=0.1.1
FC_BASE="${NEXUS_FIRECRAWL_BASE:-https://github.com/nottherealsanta/firecrawl-local-builds/releases/download}"

die() { printf 'install: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install: warning: %s\n' "$*" >&2; }
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
      rm -f "$BIN/nexus-tui" "$BIN/firecrawl-local"
      rm -rf "$WEB"
      printf 'Removed the nexus10 tool, %s/nexus-tui, %s/firecrawl-local and %s.\nYour data in %s is untouched.\n' \
        "$BIN" "$BIN" "$WEB" "${NEXUS10_HOME:-$HOME/.nexus}"
      exit 0 ;;
    --source) MODE=source ;;
    --release) MODE=release
      case "${2:-}" in v[0-9]*) TAG="$2"; shift ;; esac ;;
    -h|--help) sed -n '2,16p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1 (try --help)" ;;
  esac
  shift
done
[ -n "$MODE" ] || { [ -n "$ROOT" ] && MODE=source || MODE=release; }
[ "$MODE" = release ] || [ -n "$ROOT" ] || die "--source needs a checkout: run ./install.sh from the repository"

restart_daemon() {
  # A running daemon keeps serving the code it started with (possibly from a moved or deleted install), so an update
  # restarts it with the new `nexus`. A daemon that is not running is left stopped.
  nexus_bin="$(uv tool dir --bin 2>/dev/null)/nexus"
  [ -x "$nexus_bin" ] || return 0
  "$nexus_bin" daemon status 2>/dev/null | grep -q '^running' || return 0
  step 'Restart the Core daemon (an interruption leaves it stopped; run `nexus daemon start`)'
  "$nexus_bin" daemon stop >/dev/null 2>&1 || true
  "$nexus_bin" daemon start || printf 'warning: the daemon did not restart; run `nexus daemon start`.\n' >&2
}

done_message() {
  restart_daemon
  printf '\nInstalled. Run `nexus doctor` to check the install.\n'
  printf 'Optional voice: run `nexus voice init` to install the speech runtime and download both models.\n'
  case ":$PATH:" in *":$BIN:"*) ;; *) printf 'Add %s to PATH.\n' "$BIN" ;; esac
}

# -- from a checkout ---------------------------------------------------------------

install_source() {
  need uv "https://docs.astral.sh/uv/"
  need cargo "https://rustup.rs"
  need git
  need node "Node 22 or newer"
  node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)' || die "Node 22 or newer is required"
  need npm
  need nsh "npm install -g nsh"
  if [ "$(uname -s)" = Linux ]; then
    need cc "install a C toolchain (build-essential on Debian/Ubuntu)"
    need pkg-config
    pkg-config --exists alsa || die "ALSA development headers are missing (libasound2-dev on Debian/Ubuntu, alsa-lib-devel on Fedora)"
    [ -x /usr/bin/env ] || die "the Monty kernel launcher requires /usr/bin/env"
  fi

  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT INT TERM
  download_firecrawl

  step "Core (uv tool)"
  uv tool install --quiet --force --reinstall --no-binary-package claude-agent-sdk "$ROOT/nexus-core"

  step "TUI"
  (cd "$ROOT/nexus-tui" && cargo build --release)
  mkdir -p "$BIN"
  install -m 755 "$ROOT/nexus-tui/target/release/nexus-tui" "$BIN/nexus-tui"
  if [ -n "$FC_ASSET" ]; then
    step "firecrawl-local $FC_VERSION"
    install -m 755 "$WORK/$FC_ASSET" "$BIN/firecrawl-local"
  fi

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

fetch_url() { # url, destination, label
  # GitHub's asset edge answers 504 under load. curl retries those transient codes on its own; the
  # outer loop covers the rest (a reset mid-transfer, a connection refused), so one bad minute does
  # not sink an install.
  attempts=0
  while :; do
    attempts=$((attempts + 1))
    curl -fsSL --connect-timeout 30 --retry 3 --retry-delay 2 "$1" -o "$2" && return 0
    [ "$attempts" -lt 3 ] || return 1
    printf 'install: download of %s failed; retrying (%d/3)\n' "$3" "$((attempts + 1))" >&2
    sleep 3
  done
}

fetch() { # file, [failure message]
  fetch_url "$(release_url "$1")" "$WORK/$1" "$1" \
    || die "${2:-could not download $1 from $(release_url "$1") after 3 attempts}"
}

verify() { # file
  want="$(awk -v f="$1" '$2 == f || $2 == "*" f { print $1 }' "$WORK/SHA256SUMS")"
  [ -n "$want" ] || die "$1 is not listed in this release's SHA256SUMS"
  [ "$(sha256_of "$WORK/$1")" = "$want" ] || die "the checksum of $1 does not match SHA256SUMS; nothing was installed"
}

listed() { awk -v f="$1" '$2 == f || $2 == "*" f { found = 1 } END { exit !found }' "$WORK/SHA256SUMS"; }

fc_platform() { # sets fc_os and fc_arch for this machine, or fails on an unsupported one
  fc_os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$fc_os" in darwin|linux) ;; *) return 1 ;; esac
  case "$(uname -m)" in
    arm64|aarch64) fc_arch=arm64 ;;
    x86_64|amd64) fc_arch=amd64 ;;
    *) return 1 ;;
  esac
}

# firecrawl-local is built and published outside this repository, so fetch the pinned binary for this machine and
# check it against that release's own SHA256SUMS. A missing release or platform leaves Nexus without the firecrawl
# tool (a warning, not a failed install); a checksum mismatch stops before anything is written.
FC_ASSET=""
download_firecrawl() {
  FC_ASSET=""
  command -v curl >/dev/null 2>&1 || { warn "curl is not on PATH; firecrawl-local is not installed"; return 0; }
  if ! fc_platform; then
    warn "no firecrawl-local $FC_VERSION build for $(uname -s) $(uname -m); the firecrawl tool will be unavailable"
    return 0
  fi
  fc="firecrawl-local-$fc_os-$fc_arch"
  sums="$WORK/firecrawl-SHA256SUMS"
  if ! fetch_url "$FC_BASE/v$FC_VERSION/SHA256SUMS" "$sums" "firecrawl-local $FC_VERSION SHA256SUMS"; then
    warn "could not download the firecrawl-local $FC_VERSION checksums; the firecrawl tool will be unavailable"
    return 0
  fi
  if ! awk -v f="$fc" '$2 == f || $2 == "*" f { found = 1 } END { exit !found }' "$sums"; then
    warn "firecrawl-local $FC_VERSION has no build for $fc_os-$fc_arch; the firecrawl tool will be unavailable"
    return 0
  fi
  if ! fetch_url "$FC_BASE/v$FC_VERSION/$fc" "$WORK/$fc" "$fc"; then
    warn "could not download $fc; the firecrawl tool will be unavailable"
    return 0
  fi
  want="$(awk -v f="$fc" '$2 == f || $2 == "*" f { print $1 }' "$sums")"
  [ "$(sha256_of "$WORK/$fc")" = "$want" ] \
    || die "the checksum of $fc does not match firecrawl-local-builds' SHA256SUMS; nothing was installed"
  FC_ASSET="$fc"
}

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
  fetch SHA256SUMS "no release found at $(release_url SHA256SUMS); from a checkout, run ./install.sh --source"
  listed "$tui" || die "this release has no build for $os $(uname -m) ($tui)"
  wheel="$(awk '{ n = $2; sub(/^\*/, "", n); if (n ~ /^nexus10-.*\.whl$/) print n }' "$WORK/SHA256SUMS" | head -n 1)"
  [ -n "$wheel" ] || die "this release has no Core wheel"

  if [ "$os" = linux ]; then
    # The TUI links the ALSA library (its audio cues); without it the binary fails with a bare linker error.
    ldconfig_bin="$(PATH="$PATH:/sbin:/usr/sbin" command -v ldconfig || true)"
    if [ -n "$ldconfig_bin" ] && ! "$ldconfig_bin" -p 2>/dev/null | grep -q 'libasound\.so\.2'; then
      printf 'warning: nexus-tui needs libasound.so.2. Install it: apt install libasound2 (libasound2t64 on newer Debian and Ubuntu), or dnf install alsa-lib.\n' >&2
    fi
  fi

  step "Download and verify"
  fetch "$wheel"; verify "$wheel"
  fetch "$tui"; verify "$tui"
  web=""
  if listed nexus-web.tar.gz; then web=nexus-web.tar.gz; fetch "$web"; verify "$web"; fi
  download_firecrawl

  step "Core (uv tool)"
  uv tool install --quiet --force --reinstall --no-binary-package claude-agent-sdk "$WORK/$wheel"
  step "TUI"
  mkdir -p "$BIN"
  install -m 755 "$WORK/$tui" "$BIN/nexus-tui"
  if [ -n "$FC_ASSET" ]; then
    step "firecrawl-local $FC_VERSION"
    install -m 755 "$WORK/$FC_ASSET" "$BIN/firecrawl-local"
  fi
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
