#!/usr/bin/env bash
# Start two isolated Cores (mode-700 homes, no provider calls) for the visual
# harness: a seeded workspace and an empty one. Writes <root>/env for shots.mjs.
# NEXUS_CMD is the Core CLI (default `uv run nexus`, run from nexus-core);
# NEXUS_PY runs seed_messages.py (default `uv run python`, or the python beside
# a NEXUS_CMD that ends in `nexus`, e.g. NEXUS_CMD=/tmp/nexus10-venv/bin/nexus).
# NEXUS_PORT is the seeded Core's port (default 4799) and NEXUS_PORT_EMPTY the
# empty Core's (default 4798); set both when those are taken.
#   tools/webterm/seed.sh <root>            start and seed
#   tools/webterm/seed.sh <root> stop       stop both
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
core="$here/../../../nexus-core"
root="${1:?usage: seed.sh <root> [stop]}"
mkdir -p "$root"
cmd="${NEXUS_CMD:-uv run nexus}"
py="${NEXUS_PY:-uv run python}"
if [ -z "${NEXUS_PY:-}" ] && [ -n "${NEXUS_CMD:-}" ] && [ "${NEXUS_CMD##*/}" = nexus ]; then py="${NEXUS_CMD%nexus}python"; fi
port="${NEXUS_PORT:-4799}"
port_empty="${NEXUS_PORT_EMPTY:-4798}"
[ "$port" != "$port_empty" ] || { echo "seed.sh: NEXUS_PORT and NEXUS_PORT_EMPTY must differ" >&2; exit 2; }
# $cmd and $py are word-split on purpose: they may hold `uv run ...`.
nexus() { (cd "$core" && NEXUS10_HOME="$1" NEXUS_PORT="$2" $cmd "${@:3}"); }
if [ "${2:-}" = "stop" ]; then
  nexus "$root/home" "$port" daemon stop || true
  nexus "$root/home-empty" "$port_empty" daemon stop || true
  exit 0
fi
for h in home home-empty; do rm -rf "$root/$h"; mkdir -p "$root/$h"; chmod 700 "$root/$h"; done
rm -rf "$root/ws"
mkdir -p "$root/ws/nexus-demo" "$root/ws/side-project/src" "$root/ws/side-project/target"
side="$root/ws/side-project"
printf 'Real Core file browsing works.\n' > "$side/hello.txt"
printf '# Side project\n' > "$side/README.md"
printf 'fn main() {}\n' > "$side/src/main.rs"
printf 'fn old() {}\n' > "$side/src/old.rs"
printf 'target/\n' > "$side/.gitignore"
printf 'build output\n' > "$side/target/out"
git -C "$side" init -q
git -C "$side" add -A
git -C "$side" -c user.email=seed@example.invalid -c user.name=seed commit -qm initial
printf '\nchanged\n' >> "$side/README.md"
printf 'pub fn new() {}\n' > "$side/src/new.rs"
rm "$side/src/old.rs"
# nexus-demo is a Git repo too, so the right sidebar's GIT groups and the diff
# view have content: one commit, then a modified, an untracked and a deleted file.
demo="$root/ws/nexus-demo"
mkdir -p "$demo/nexus-core/nexus" "$demo/nexus-core/tests"
printf '# Nexus demo\n\nA small workspace for the visual harness.\n' > "$demo/README.md"
cat > "$demo/nexus-core/nexus/loader.py" <<'EOF'
"""Load the workspace manifest."""
import json
from pathlib import Path


def parse_manifest(path):
    return json.loads(Path(path).read_text())


class Loader:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "manifest.json"

    def load(self, name):
        data = parse_manifest(self.path)
        return data.get(name)
EOF
cat > "$demo/nexus-core/tests/test_loader.py" <<'EOF'
from nexus.loader import Loader


def test_load_returns_entry(tmp_path):
    (tmp_path / "manifest.json").write_text('{"a": 1}')
    assert Loader(tmp_path).load("a") == 1
EOF
printf 'old notes, not needed any more\n' > "$demo/NOTES.txt"
git -C "$demo" init -q
git -C "$demo" add -A
git -C "$demo" -c user.email=seed@example.invalid -c user.name=seed commit -qm initial
cat > "$demo/nexus-core/nexus/loader.py" <<'EOF'
"""Load the workspace manifest."""
import json
from pathlib import Path


def parse_manifest(path):
    return json.loads(Path(path).read_text())


class Loader:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "manifest.json"
        self._key = None
        self._data = None

    def _cached(self, path):
        key = Path(path).stat().st_mtime_ns
        if self._key != key:
            self._data = parse_manifest(path)
            self._key = key
        return self._data

    def load(self, name):
        data = self._cached(self.path)
        return data.get(name)
EOF
printf 'cache notes\n' > "$demo/nexus-core/nexus/cache_notes.txt"
rm "$demo/NOTES.txt"
nexus "$root/home-empty" "$port_empty" daemon start
nexus "$root/home" "$port" daemon start >/dev/null
token=$(python3 -c "import json,sys;print(json.load(open('$root/home/daemon.json'))['token'])")
api() { curl -sf -X POST -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -H "Idempotency-Key: $2" -d "$3" "http://127.0.0.1:$port$1"; }
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
nexus "$root/home" "$port" daemon stop >/dev/null
(cd "$core" && NEXUS10_HOME="$root/home" $py "$here/seed_messages.py" "$first")
nexus "$root/home" "$port" daemon start
printf 'WEBTERM_HOME=%s\nWEBTERM_HOME_EMPTY=%s\nWEBTERM_PORT=%s\nWEBTERM_PORT_EMPTY=%s\n' "$root/home" "$root/home-empty" "$port" "$port_empty" > "$root/env"
echo "seeded; env in $root/env"
