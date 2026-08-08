# Atrinik content toolkit

This independently releasable MIT Rust workspace provides Atrinik's headless
lossless authored-document foundation, schemas, deterministic catalog,
diagnostics, semantic transactions, bounded artifact plumbing, conformance
testkit, and `atrinik-content` CLI. It has no server, client, editor, renderer,
SDL, GPU, network-service, sibling-checkout, or classic implementation
dependency.

## Build and validate

The pinned baseline is Rust 1.97.1, edition 2024. The reusable Atrinik Linux
devcontainer supplies Rust, Syft, and Trivy.

```sh
cargo build --locked --workspace
cargo test --locked --workspace
cargo run --locked --package atrinik-content -- --version
tools/validate.sh
```

The aggregate required check is `Content toolkit validation`. It runs format,
Clippy-as-errors, unit/doc/property/adversarial tests, dependency/license and
vulnerability checks, provenance validation, public fixture/CLI conformance,
release builds, and a release dry run.

`Content toolkit corpus` checks out the authored `arch`/`maps` corpus at the
immutable revision recorded in `provenance/reuse.json`, byte-round-trips every
selected authored file or returns a bounded classified failure, and publishes
the deterministic count/digest report in the GitHub job summary. Locally, the
same measurement is:

```sh
cargo run --locked --package atrinik-source --example corpus -- \
  --root /absolute/content/checkout \
  --revision 01b1fdb65c2243df4bafe9c8109fc93229df0121
```

The M1 baseline is 5,590 authored files, 61,379,370 bytes, zero diagnostics or
truncation, and digest
`8cc6a362dcf20ed7760ec2b9813fff9dbdb7803017247c4530e5575a20ffa5e3`.
`compatibility/m1-corpus-baseline.json` records the three non-authored paths
excluded from the selected tree, their owner/milestone, and their zero effect
on authored-format coverage.

## Public fixture round trip

Inputs, source identity, and output are always explicit. Output must not exist.

```sh
cargo run --locked --package atrinik-content -- \
  validate --input crates/atrinik-testkit/fixtures/minimal.arc \
  --source-id fixture:minimal
cargo run --locked --package atrinik-content -- \
  round-trip --input crates/atrinik-testkit/fixtures/minimal.arc \
  --output /tmp/atrinik-minimal-round-trip.arc --source-id fixture:minimal
cmp crates/atrinik-testkit/fixtures/minimal.arc \
  /tmp/atrinik-minimal-round-trip.arc
rm /tmp/atrinik-minimal-round-trip.arc
```

See [architecture and bounds](docs/ARCHITECTURE.md),
[provenance evidence](PROVENANCE.md), and [contribution policy](CONTRIBUTING.md).
