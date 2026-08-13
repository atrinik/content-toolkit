# Atrinik content toolkit

This independently releasable MIT Rust workspace provides Atrinik's headless
lossless authored-document foundation, schemas, deterministic catalog,
diagnostics, semantic transactions, bounded artifact plumbing, conformance
testkit, and `atrinik-content` CLI. It has no server, client, editor, renderer,
SDL, GPU, network-service, sibling-checkout, or classic implementation
dependency.

## Development model

This toolkit is part of Atrinik's agentic next-generation reimplementation. Its
fresh MIT-licensed Rust code is developed primarily through Codex-driven
workflows under maintainer direction, review, evidence-gated provenance
controls, tests, and repository validation. “Agentic” describes the project's
primary current software-development workflow; it does not mean every line or
commit is agent-written or every contributor uses an agent. Direct
human-written code contributions are welcome under the same controls.

The toolkit reimplements and improves Classic content tooling without being a
wholesale Python or C source port. Its current implementation combines
independently authored work with the narrow, audited grant-covered translations
and machine-contract copies recorded in [`PROVENANCE.md`](PROVENANCE.md).
Future exact historical reuse follows that local record and the
[canonical grant registry](https://github.com/atrinik/atrinik/blob/main/docs/PROVENANCE.md):
admitted destination material may be consulted and MIT-relicensed, while every
uncovered portion remains excluded and the Classic repository remains
GPL-distributed. See the
[replacement roadmap](https://github.com/atrinik/atrinik/issues/168) and
[canonical project authorship statement](https://github.com/atrinik/atrinik/issues/331)
for the project-wide direction and authorship boundaries.

Maps, quests, lore, dialogue, archetypes, pixel art, and other creative game
content are human-authored inputs with their own exact provenance and licenses.
Deterministic parsing, compilation, schema generation, migration, validation,
and their generated artifacts remain distinct from both those creative inputs
and the toolkit's code authorship. These operations must not become a path for
generating creative content.

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

## Stable catalog and diagnostics

`atrinik-catalog` exposes the shared headless catalog API used by CI, CLI, and
editor integrations. `CatalogId` combines an explicit domain, namespace, and
locale-independent local ID for archetypes, maps, faces, animations, treasures,
factions, interfaces, quests, and resources. Ordered indexes resolve canonical
IDs, aliases, inheritance, and typed references. `Query` and `preview` return
bounded metadata without retaining or reparsing resource payloads.

Catalog inputs carry their source revision and schema version. A generation is
derived deterministically from those inputs, while `update_document` and
`remove_document` report the exact changed identities and their transitive
dependents. An unchanged document is a no-op, and an incremental result is
identical to a clean build over the same documents.

`atrinik-diagnostics` is the single structured diagnostic representation. Each
diagnostic has a stable code, severity, source span, related locations,
semantic path, message, optional fix hint, and explicit suppression state.
Catalog conflicts use `catalog.duplicate_id`, `catalog.ambiguous_alias`,
`catalog.missing_reference`, `catalog.ambiguous_reference`, and
`catalog.inheritance_cycle`. Only diagnostics explicitly marked suppressible
can be suppressed, and diagnostic count, related-location count, text size, and
semantic-path depth are bounded.

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

Released language-neutral compatibility material includes the bounded classic
authoring policy at `policy/classic-authored-limits.json` and diagnostic schema
at `schemas/classic-diagnostic.schema.json`. Their exact historical MIT grant,
identity, source, unchanged 1.x copy, destination, and attribution evidence is
in `provenance/linked-content-materials.json`.
