#!/bin/sh
# Exercises install.sh's release path with no network: a file:// mirror, a stub uv, and a stub curl
# that answers 504 twice for the TUI (GitHub's asset edge does this) and then works. Also checks that
# a download that never succeeds installs nothing.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

VERSION=v9.9.9
WHEEL="nexus10-9.9.9-py3-none-any.whl"
TUI="nexus-tui-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
REL="$TMP/releases/download/$VERSION"
mkdir -p "$REL" "$TMP/bin"

fail() { printf 'install test: %s\n' "$*" >&2; exit 1; }

sums() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

printf 'wheel\n' > "$REL/$WHEEL"
printf 'binary\n' > "$REL/$TUI"
(cd "$REL" && sums "$WHEEL" "$TUI" > SHA256SUMS)

cat > "$TMP/bin/uv" <<'EOF'
#!/bin/sh
printf 'uv %s\n' "$*" >> "$UV_LOG"
if [ "$*" = 'tool dir --bin' ]; then printf '%s\n' "$FAKE_NEXUS_BIN"; fi
EOF
chmod +x "$TMP/bin/uv"

curl_stub() { # fail the TUI download the first $1 times; 0 means always
  real="$(command -v curl)"
  cat > "$TMP/bin/curl" <<EOF
#!/bin/sh
case "\$*" in
  *$TUI*)
    n=\$(cat "$TMP/attempts" 2>/dev/null || echo 0); n=\$((n + 1)); echo "\$n" > "$TMP/attempts"
    if [ "$1" -eq 0 ] || [ "\$n" -le "$1" ]; then
      echo 'curl: (22) The requested URL returned error: 504' >&2
      exit 22
    fi ;;
esac
exec "$real" "\$@"
EOF
  chmod +x "$TMP/bin/curl"
}

run() { # prefix
  rm -f "$TMP/attempts"
  FAKE_NEXUS_BIN="$TMP/nexus-bin" NEXUS_TEST_DAEMON_STATE="${NEXUS_TEST_DAEMON_STATE:-stopped}" \
    NEXUS_TEST_DAEMON_LOG="$TMP/daemon.log" UV_LOG="$TMP/uv.log" PATH="$TMP/bin:$PATH" PREFIX="$1" \
    NEXUS_RELEASE_BASE="file://$TMP/releases" sh "$ROOT/install.sh" --release "$VERSION"
}

mkdir -p "$TMP/nexus-bin"
cat > "$TMP/nexus-bin/nexus" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$NEXUS_TEST_DAEMON_LOG"
case "$*" in
  'daemon status') printf '%s\n' "$NEXUS_TEST_DAEMON_STATE" ;;
  'daemon stop'|'daemon start') exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$TMP/nexus-bin/nexus"

# A flaky edge: two 504s, then the real bytes.
curl_stub 2
: > "$TMP/uv.log"
run "$TMP/one" > "$TMP/out" 2> "$TMP/err" || { cat "$TMP/err"; fail "a transient 504 sank the install"; }
[ "$(cat "$TMP/one/bin/nexus-tui")" = binary ] || fail "the TUI was not installed"
[ "$(cat "$TMP/attempts")" = 3 ] || fail "the TUI was downloaded $(cat "$TMP/attempts") times, not 3"
grep -q 'retrying (2/3)' "$TMP/err" || { cat "$TMP/err"; fail "a retry was not announced"; }
grep -q 'uv tool install .*--no-binary-package claude-agent-sdk .*'"$WHEEL" "$TMP/uv.log" || fail "the Core wheel must build Claude's SDK from source"
[ "$(cat "$TMP/daemon.log")" = 'daemon status' ] || fail "install started a stopped daemon"

# A running daemon must use the newly installed code, in stop/start order.
curl_stub 1
: > "$TMP/daemon.log"
NEXUS_TEST_DAEMON_STATE=running run "$TMP/running" > "$TMP/out" 2> "$TMP/err" || fail "running-daemon update failed"
[ "$(cat "$TMP/daemon.log")" = "$(printf 'daemon status\ndaemon stop\ndaemon start')" ] || fail "running daemon was not restarted in order"

# Not flaky: one 504 that never clears must stop before anything is installed.
curl_stub 0
: > "$TMP/uv.log"
if run "$TMP/two" > "$TMP/out" 2> "$TMP/err"; then fail "the install succeeded without the TUI"; fi
grep -q 'after 3 attempts' "$TMP/err" || { cat "$TMP/err"; fail "the failure did not say how many attempts were made"; }
[ ! -e "$TMP/two/bin/nexus-tui" ] || fail "a failed download left a TUI behind"
[ ! -s "$TMP/uv.log" ] || fail "a failed download still installed the Core wheel"

printf 'install test: ok\n'
