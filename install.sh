#!/bin/sh
# Nexus installer (plans/install.md).
#
#   curl -LsSf https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.sh | sh
#
# Installs uv when missing, lets uv provide Python >= 3.11, installs Nexus as an
# isolated uv tool, and puts `nexus` on PATH. Re-running upgrades in place.
# Options (flags win over environment variables):
#   --version X         NEXUS_VERSION      pin a release (or git tag for the git source)
#   --source S          NEXUS_SOURCE       git (default) | pypi | any package spec or path
#   --git-ref R         NEXUS_GIT_REF      branch, tag or commit for the git source (main)
#   --python V          NEXUS_PYTHON       Python for the tool venv (3.12)
#   --extras E          NEXUS_EXTRAS       e.g. documents
#   --no-modify-path    NEXUS_NO_MODIFY_PATH
#   --no-doctor         NEXUS_NO_DOCTOR    skip the post-install smoke check
#   --strict            NEXUS_STRICT       make a failed smoke check fatal
# NEXUS_ALLOW_ROOT=1 permits running as root (containers).
#
# Everything lives in main() and main runs on the last line, so a download cut off
# halfway executes nothing.

REPO_URL="https://github.com/nottherealsanta/nexus"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
PACKAGE="nexus-harness"
MIN_PYTHON="3.11"

say() { printf 'nexus-install: %s\n' "$*"; }
warn() { printf 'nexus-install: warning: %s\n' "$*" >&2; }
die() {
    code=$1
    shift
    printf 'nexus-install: error: %s\n' "$*" >&2
    exit "$code"
}

have() { command -v "$1" >/dev/null 2>&1; }

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --version) [ $# -ge 2 ] || die 1 "--version needs a value"; NEXUS_VERSION=$2; shift 2 ;;
            --source) [ $# -ge 2 ] || die 1 "--source needs a value"; NEXUS_SOURCE=$2; shift 2 ;;
            --git-ref) [ $# -ge 2 ] || die 1 "--git-ref needs a value"; NEXUS_GIT_REF=$2; shift 2 ;;
            --python) [ $# -ge 2 ] || die 1 "--python needs a value"; NEXUS_PYTHON=$2; shift 2 ;;
            --extras) [ $# -ge 2 ] || die 1 "--extras needs a value"; NEXUS_EXTRAS=$2; shift 2 ;;
            --no-modify-path) NEXUS_NO_MODIFY_PATH=1; shift ;;
            --no-doctor) NEXUS_NO_DOCTOR=1; shift ;;
            --strict) NEXUS_STRICT=1; shift ;;
            -h | --help) sed -n '2,19p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die 1 "unknown option: $1" ;;
        esac
    done
    NEXUS_SOURCE=${NEXUS_SOURCE:-git}
    NEXUS_GIT_REF=${NEXUS_GIT_REF:-main}
    NEXUS_PYTHON=${NEXUS_PYTHON:-3.12}
    NEXUS_EXTRAS=${NEXUS_EXTRAS:-}
}

detect_platform() {
    os=$(uname -s 2>/dev/null || echo unknown)
    arch=$(uname -m 2>/dev/null || echo unknown)
    case $os in
        Darwin | Linux) ;;
        *) die 2 "unsupported OS '$os'. On Windows use install.ps1 (or WSL)." ;;
    esac
    case $arch in
        x86_64 | amd64 | arm64 | aarch64) ;;
        *) die 2 "unsupported CPU architecture '$arch'." ;;
    esac
    if [ "$(id -u)" = 0 ] && [ -z "${NEXUS_ALLOW_ROOT:-}" ]; then
        die 2 "refusing to install as root. Run as your normal user (NEXUS_ALLOW_ROOT=1 to override)."
    fi
}

download() {
    if have curl; then
        curl -LsSf "$1"
    elif have wget; then
        wget -qO- "$1"
    else
        die 3 "need curl or wget to download uv."
    fi
}

uv_version_ok() {
    # uv >= 0.5.0 has stable `tool` and `python install`.
    ver=$("$1" --version 2>/dev/null | awk '{print $2}')
    major=${ver%%.*}
    rest=${ver#*.}
    minor=${rest%%.*}
    case $major$minor in
        '' | *[!0-9]*) return 1 ;;
    esac
    [ "$major" -gt 0 ] || [ "$minor" -ge 5 ]
}

locate_uv() {
    if have uv; then
        command -v uv
        return 0
    fi
    for dir in "${UV_INSTALL_DIR:-}" "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            printf '%s\n' "$dir/uv"
            return 0
        fi
    done
    return 1
}

ensure_uv() {
    if UV=$(locate_uv); then
        if ! uv_version_ok "$UV"; then
            say "uv at $UV is older than 0.5; trying to update it"
            "$UV" self update >/dev/null 2>&1 || warn "could not update uv; continuing with it"
        fi
        say "using uv: $UV"
        return 0
    fi
    say "uv not found; installing it from astral.sh"
    # uv's installer edits shell profiles unless told not to; we do that ourselves
    # once, after Nexus is installed, through `uv tool update-shell`.
    download "$UV_INSTALLER_URL" | env UV_NO_MODIFY_PATH=1 sh >&2 ||
        die 3 "uv install failed. Check your network or install uv from https://docs.astral.sh/uv/ and re-run."
    UV=$(locate_uv) || die 3 "uv was installed but cannot be found; open a new shell and re-run."
    say "installed uv: $UV"
}

package_spec() {
    extras=""
    if [ -n "$NEXUS_EXTRAS" ]; then extras="[$NEXUS_EXTRAS]"; fi
    case $NEXUS_SOURCE in
        pypi)
            SPEC="$PACKAGE$extras"
            if [ -n "${NEXUS_VERSION:-}" ]; then SPEC="$PACKAGE$extras==$NEXUS_VERSION"; fi
            ;;
        git)
            have git || die 4 "the git source needs git installed (or use --source pypi)."
            ref=$NEXUS_GIT_REF
            if [ -n "${NEXUS_VERSION:-}" ]; then ref="v${NEXUS_VERSION#v}"; fi
            SPEC="$PACKAGE$extras @ git+$REPO_URL@$ref"
            ;;
        *)
            SPEC=$NEXUS_SOURCE
            ;;
    esac
}

stop_running_daemons() {
    # Daemons keep running old code after an upgrade. Stop them through the
    # installed CLI (graceful, verified); never kill processes by name.
    old=""
    if have nexus; then
        old=$(command -v nexus)
    elif [ -x "$BIN_DIR/nexus" ]; then
        old="$BIN_DIR/nexus"
    fi
    [ -n "$old" ] || return 0
    if "$old" daemon stop --all >/dev/null 2>&1; then
        say "stopped running Nexus daemons"
    else
        warn "restart any running sessions with \`nexus daemon restart\` to pick up the new version"
    fi
}

install_nexus() {
    say "installing $SPEC (Python $NEXUS_PYTHON)"
    "$UV" tool install --force --python "$NEXUS_PYTHON" "$SPEC" ||
        die 4 "installing Nexus failed. See the uv output above."
}

path_has_bin() {
    case ":$PATH:" in
        *":$BIN_DIR:"*) return 0 ;;
    esac
    return 1
}

ensure_path() {
    PATH_WAS_SET=1
    if path_has_bin; then return 0; fi
    PATH_WAS_SET=0
    if [ -n "${NEXUS_NO_MODIFY_PATH:-}" ]; then return 0; fi
    "$UV" tool update-shell >/dev/null 2>&1 ||
        warn "could not update your shell profile; add $BIN_DIR to PATH yourself"
}

smoke_test() {
    [ -z "${NEXUS_NO_DOCTOR:-}" ] || return 0
    nexus_bin="$BIN_DIR/nexus"
    [ -x "$nexus_bin" ] || die 4 "installed, but $nexus_bin is missing."
    INSTALLED_VERSION=$("$nexus_bin" --version 2>&1) ||
        die 4 "installed, but \`nexus --version\` failed: $INSTALLED_VERSION"
    scratch=$(mktemp -d "${TMPDIR:-/tmp}/nexus-install.XXXXXX") || return 0
    if "$nexus_bin" --workspace "$scratch" doctor >"$scratch/doctor.out" 2>&1; then
        say "smoke check passed"
    else
        warn "\`nexus doctor\` reported problems:"
        sed 's/^/    /' "$scratch/doctor.out" >&2
        [ -z "${NEXUS_STRICT:-}" ] || {
            "$nexus_bin" --workspace "$scratch" daemon stop >/dev/null 2>&1
            rm -rf "$scratch"
            exit 4
        }
    fi
    "$nexus_bin" --workspace "$scratch" daemon stop >/dev/null 2>&1
    rm -rf "$scratch"
}

next_steps() {
    say "${INSTALLED_VERSION:-nexus} installed -> $BIN_DIR/nexus"
    cat <<EOF

  cd your-project
  nexus chat        # terminal UI; first run asks you to connect a provider
  nexus web         # the same thing in the browser

Update:    nexus update
Uninstall: uv tool uninstall $PACKAGE   (your sessions in ~/.nexus are kept)
EOF
    if [ "$PATH_WAS_SET" = 0 ]; then
        printf '\nOpen a new terminal (or run: export PATH="%s:$PATH") so `nexus` is found.\n' "$BIN_DIR"
    fi
}

main() {
    set -eu
    parse_args "$@"
    detect_platform
    ensure_uv
    package_spec
    BIN_DIR=$("$UV" tool dir --bin 2>/dev/null) || BIN_DIR="$HOME/.local/bin"
    "$UV" python find ">=$MIN_PYTHON" >/dev/null 2>&1 ||
        say "no Python >= $MIN_PYTHON found; uv will download one"
    stop_running_daemons
    install_nexus
    ensure_path
    smoke_test
    next_steps
}

main "$@"
