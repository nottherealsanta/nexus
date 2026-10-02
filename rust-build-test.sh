#!/usr/bin/env bash
# Build and test the native Ratatui client from any working directory.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

cargo build --locked --manifest-path rust/tui/Cargo.toml
cargo test --locked --manifest-path rust/tui/Cargo.toml
