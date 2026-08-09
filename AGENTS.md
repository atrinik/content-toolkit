# Atrinik content toolkit repository guide

## Ownership and architecture

- This repository owns the clean-room MIT Rust content model, lossless parser,
  compiler, validator, catalog, transaction engine, fixtures, and
  `atrinik-content` CLI. Keep it headless, independently buildable/releasable,
  and free of server/client/editor/renderer/GPU/network dependencies.
- Separate lossless authored syntax, typed schema semantics, catalogs/indexes,
  transactions, compiled artifacts, conformance support, and CLI. Do not create
  a second parser, catalog, or write path for one consumer.
- Preserve comments, unknown fields, ordering, whitespace, line endings, and
  untouched bytes. Library APIs use explicit roots and immutable snapshots;
  they never assume sibling checkouts, CWD, mutable globals, or implicit paths.
- Editor consumes released crates/transactions, content automation consumes
  the released CLI, and Go server consumes language-neutral artifacts/fixtures.
  Never add Rust FFI to the server.
- ABIN/AMAP are deterministic bounded contracts with canonical encoding/digest,
  versions/features, integrity checks, allocation limits, and negative
  fixtures. Raw Protobuf bytes are not a canonical content hash.

## Branch, mutation, and resource safety

- `atrinik/content@main` is forward authoring; `content@1.x` is the independent
  classic release line. Make repository, branch, revision, input/output roots,
  and supported versions explicit. Never infer a mutation target from a nearby
  checkout or assume a migration applies to both lines.
- Mutation is dry-run-first with deterministic semantic/text diffs, revision
  preconditions, complete resulting-project validation, destination allowlists,
  and atomic publication. Failure/cancellation leaves either the old or new
  state, never a partial write.
- Bound bytes, records, tokens, nesting, strings, graph work, diagnostics,
  allocation, output, and execution time. Filesystem order, map iteration,
  concurrency, cache warmth, and locale must not change output/diagnostics.
  Incremental and clean builds converge; caches are never truth.

## Licensing, roadmap, and validation

- New code is MIT; authored maps/media/scripts and imported fixtures/packages
  retain their actual licenses. Do not add GPL/AGPL implementation or
  incompatible dependencies. Historical reuse follows local `PROVENANCE.md`
  and canonical `atrinik/atrinik/docs/PROVENANCE.md`, failing closed on
  incomplete, mixed, or uncertain evidence.
- `atrinik/atrinik#168` is the program roadmap. Local issues own toolkit
  acceptance criteria; content equivalence evidence routes to wrapper issues
  #279/#280. Do not copy the M1-M6 schedule here.
- Run the aggregate contract now present:

  ```sh
  tools/validate.sh
  git diff --check
  ```

  `Content toolkit validation` owns formatting, strict Clippy, workspace
  tests/docs/properties, CLI round trips, dependency/provenance gates, release
  dry-run, and pinned toolchain policy. Use explicit fixture paths for focused
  tests and keep them independent of sibling repositories.
- Wrapper replacement build/runtime adapters are not available yet. Use
  repository validation and explicit conformance/corpus commands; do not route
  toolkit work through classic code. Update the wrapper supply-chain inventory
  whenever dependency inputs or validation paths change.
- Commits/PR titles use Conventional Commits and semantic-release owns versioned
  crates, CLI, schemas, fixtures, and artifacts. Every handoff states exact
  validation and any unavailable integration; runtime topology is not
  applicable to a toolkit-only change.
