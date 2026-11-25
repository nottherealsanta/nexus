#!/bin/sh
# Exercises install.sh's release path with no network: a file:// mirror, a stub uv, and a stub curl
# that answers 504 twice for the TUI (GitHub's asset edge does this) and then works. Also checks that
# a download that never succeeds installs nothing, that firecrawl-local comes from its own checksum-verified
# release (a legacy Nexus asset is ignored), that a bad firecrawl-local checksum stops the install, that a
# missing firecrawl-local release only warns, and that --uninstall still removes old binaries.
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
# Older Nexus releases shipped this; the installer must prefer the firecrawl-local-builds release.
LEGACY_FC="firecrawl-local-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
printf 'legacy scraper\n' > "$REL/$LEGACY_FC"
mkdir -p "$TMP/web/Nexus"
printf 'nsh web\n' > "$TMP/web/Nexus/index.html"
printf 'nsh host\n' > "$TMP/web/serve.mjs"
tar -C "$TMP/web" -czf "$REL/nexus-web.tar.gz" Nexus serve.mjs
(cd "$REL" && sums "$WHEEL" "$TUI" "$LEGACY_FC" nexus-web.tar.gz > SHA256SUMS)

# The external contract: firecrawl-local-{darwin|linux}-{arm64|amd64} and SHA256SUMS under v0.1.1.
FC_ARCH="$(uname -m)"
case "$FC_ARCH" in arm64|aarch64) FC_ARCH=arm64 ;; x86_64|amd64) FC_ARCH=amd64 ;; esac
FC="firecrawl-local-$(uname -s | tr '[:upper:]' '[:lower:]')-$FC_ARCH"
FCREL="$TMP/firecrawl/releases/download/v0.1.1"
mkdir -p "$FCREL"
printf 'external scraper\n' > "$FCREL/$FC"
(cd "$FCREL" && sums "$FC" > SHA256SUMS)

cat > "$TMP/bin/uv" <<'EOF'
#!/bin/sh
printf 'uv %s\n' "$*" >> "$UV_LOG"
case "$*" in
  'tool install '*) if [ "${UV_FAIL:-0}" = 1 ]; then echo 'uv: simulated error' >&2; exit 1; fi ;;
esac
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
    NEXUS_RELEASE_BASE="file://$TMP/releases" \
    NEXUS_FIRECRAWL_BASE="${NEXUS_FIRECRAWL_BASE:-file://$TMP/firecrawl/releases/download}" \
    sh "$ROOT/install.sh" --release "$VERSION"
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
[ "$(cat "$TMP/one/bin/firecrawl-local")" = 'external scraper' ] || fail "firecrawl-local was not installed from its own release"
[ -x "$TMP/one/bin/firecrawl-local" ] || fail "firecrawl-local is not executable"
[ "$(cat "$TMP/one/share/nexus/web/Nexus/index.html")" = 'nsh web' ] || fail "the NSH web client was not installed"
[ "$(cat "$TMP/one/share/nexus/web/serve.mjs")" = 'nsh host' ] || fail "the NSH web host was not installed"
[ "$(cat "$TMP/attempts")" = 3 ] || fail "the TUI was downloaded $(cat "$TMP/attempts") times, not 3"
grep -q 'retrying (2/3)' "$TMP/err" || { cat "$TMP/err"; fail "a retry was not announced"; }
grep -q 'uv tool install .*--no-binary-package claude-agent-sdk .*'"$WHEEL" "$TMP/uv.log" || fail "the Core wheel must build Claude's SDK from source"
grep -q 'uv tool install --quiet ' "$TMP/uv.log" || fail "the Core install must use quiet output"
grep -q 'nexus voice init' "$TMP/out" || fail "voice setup hint missing"
grep -q 'download both models' "$TMP/out" || fail "voice model download hint missing"
[ "$(cat "$TMP/daemon.log")" = 'daemon status' ] || fail "install started a stopped daemon"

# Quiet installs must still expose errors and stop before installing the TUI.
curl_stub 1
UV_FAIL=1
export UV_FAIL
if run "$TMP/uv-failure" > "$TMP/out" 2> "$TMP/err"; then fail "a failed Core install succeeded"; fi
unset UV_FAIL
grep -q 'uv: simulated error' "$TMP/err" || fail "Core install error was hidden"
[ ! -e "$TMP/uv-failure/bin/nexus-tui" ] || fail "a failed Core install left a TUI behind"

# A running daemon must use the newly installed code, in stop/start order.
curl_stub 1
: > "$TMP/daemon.log"
NEXUS_TEST_DAEMON_STATE=running run "$TMP/running" > "$TMP/out" 2> "$TMP/err" || fail "running-daemon update failed"
[ "$(cat "$TMP/daemon.log")" = "$(printf 'daemon status\ndaemon stop\ndaemon start')" ] || fail "running daemon was not restarted in order"

# A payload that does not match SHA256SUMS must stop before uv installs anything.
cp "$REL/$WHEEL" "$TMP/wheel.orig"
printf 'tampered\n' > "$REL/$WHEEL"
curl_stub 1
: > "$TMP/uv.log"
if run "$TMP/checksum" > "$TMP/out" 2> "$TMP/err"; then fail "a wheel with the wrong checksum was installed"; fi
mv "$TMP/wheel.orig" "$REL/$WHEEL"
grep -q 'does not match SHA256SUMS' "$TMP/err" || { cat "$TMP/err"; fail "the checksum failure was not reported"; }
[ ! -e "$TMP/checksum/bin/nexus-tui" ] || fail "a checksum failure left a TUI behind"
[ ! -s "$TMP/uv.log" ] || fail "a checksum failure still installed the Core wheel"

# Not flaky: one 504 that never clears must stop before anything is installed.
curl_stub 0
: > "$TMP/uv.log"
if run "$TMP/two" > "$TMP/out" 2> "$TMP/err"; then fail "the install succeeded without the TUI"; fi
grep -q 'after 3 attempts' "$TMP/err" || { cat "$TMP/err"; fail "the failure did not say how many attempts were made"; }
[ ! -e "$TMP/two/bin/nexus-tui" ] || fail "a failed download left a TUI behind"
[ ! -s "$TMP/uv.log" ] || fail "a failed download still installed the Core wheel"

# A firecrawl-local that does not match the external SHA256SUMS must stop before uv installs anything.
cp "$FCREL/$FC" "$TMP/fc.orig"
printf 'tampered\n' > "$FCREL/$FC"
curl_stub 1
: > "$TMP/uv.log"
if run "$TMP/fc-checksum" > "$TMP/out" 2> "$TMP/err"; then fail "a firecrawl-local with the wrong checksum was installed"; fi
mv "$TMP/fc.orig" "$FCREL/$FC"
grep -q "does not match firecrawl-local-builds' SHA256SUMS" "$TMP/err" \
  || { cat "$TMP/err"; fail "the firecrawl-local checksum failure was not reported"; }
[ ! -e "$TMP/fc-checksum/bin/nexus-tui" ] || fail "a firecrawl-local checksum failure left a TUI behind"
[ ! -e "$TMP/fc-checksum/bin/firecrawl-local" ] || fail "a firecrawl-local checksum failure installed the binary"
[ ! -s "$TMP/uv.log" ] || fail "a firecrawl-local checksum failure still installed the Core wheel"

# An unavailable firecrawl-local release only warns: Nexus still installs, without the firecrawl tool.
curl_stub 1
NEXUS_FIRECRAWL_BASE="file://$TMP/no-firecrawl" run "$TMP/no-fc" > "$TMP/out" 2> "$TMP/err" \
  || { cat "$TMP/err"; fail "a missing firecrawl-local release sank the install"; }
grep -q 'firecrawl-local 0.1.1 checksums' "$TMP/err" || { cat "$TMP/err"; fail "the missing firecrawl-local release was not reported"; }
[ "$(cat "$TMP/no-fc/bin/nexus-tui")" = binary ] || fail "a missing firecrawl-local release skipped the TUI"
[ ! -e "$TMP/no-fc/bin/firecrawl-local" ] || fail "a missing firecrawl-local release installed a binary"

# Uninstall removes the TUI, the web bundle and firecrawl-local, keeping user data.
mkdir -p "$TMP/un/bin" "$TMP/un/share/nexus/web" "$TMP/un/data"
printf 'old\n' > "$TMP/un/bin/nexus-tui"
printf 'old\n' > "$TMP/un/bin/firecrawl-local"
printf 'old\n' > "$TMP/un/share/nexus/web/index.html"
: > "$TMP/uv.log"
UV_LOG="$TMP/uv.log" PREFIX="$TMP/un" NEXUS10_HOME="$TMP/un/data" PATH="$TMP/bin:$PATH" \
  sh "$ROOT/install.sh" --uninstall > "$TMP/out" 2> "$TMP/err" || { cat "$TMP/err"; fail "uninstall failed"; }
[ ! -e "$TMP/un/bin/nexus-tui" ] || fail "uninstall left nexus-tui behind"
[ ! -e "$TMP/un/bin/firecrawl-local" ] || fail "uninstall left firecrawl-local behind"
[ ! -e "$TMP/un/share/nexus/web" ] || fail "uninstall left the web bundle behind"
[ -d "$TMP/un/data" ] || fail "uninstall removed the data directory"
grep -q 'tool uninstall nexus10' "$TMP/uv.log" || fail "uninstall did not remove the nexus10 tool"
printf 'install test: ok\n'
