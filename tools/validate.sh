#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

test "$(rustc --version | cut -d' ' -f2)" = 1.97.1
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace --all-targets
cargo test --locked --workspace --doc
cargo build --locked --release --package atrinik-content
cargo run --locked --quiet --package atrinik-content -- --version
cargo run --locked --quiet --package atrinik-content -- \
  validate --input fixtures/corpus/minimal.arc --source-id fixture:minimal

tools/check-provenance.sh
tools/check-dependencies.sh
jq empty schemas/*.json policy/*.json provenance/*.json

release_output=$(mktemp -d /tmp/atrinik-content-toolkit-release.XXXXXX)
rmdir "${release_output}"
tools/package-release.sh "${release_output}"
test -s "${release_output}/SHA256SUMS"
rm -rf -- "${release_output}"

git diff --check
