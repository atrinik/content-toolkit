# Atrinik content toolkit repository guide

- This repository owns the clean-room, MIT-licensed Rust content model,
  compiler, validator, catalog, transaction engine, test fixtures, and
  `atrinik-content` CLI. Keep it independently buildable and releasable from a
  clean checkout.
- Work from an issue in this repository and preserve its milestone outcome,
  acceptance criteria, dependencies, and licensing constraints. Do not use a
  tooling change to redesign established game content or gameplay decisions.
- Treat [atrinik/atrinik#168](https://github.com/atrinik/atrinik/issues/168) as
  the authoritative cross-repository implementation plan. Local issues own
  toolkit delivery units; reflect dependency or exit-gate changes in that plan.
- Pull-request titles and commits use Conventional Commits style. Squash merges
  are released by semantic-release, so treat public crate, CLI, schema,
  diagnostic, fixture, and artifact compatibility as versioned API.

## Architecture and ownership

- Keep the toolkit headless. It must not depend on the server, client, editor,
  renderer, protocol implementation, SDL3, a GPU, or network services.
- Organize the Cargo workspace around independently testable concerns: source
  syntax, lossless authored documents, schemas, catalog/indexing,
  validation/diagnostics, semantic transactions, artifact compilation and
  reference decoding, conformance test support, and the CLI. Do not create a
  second parser, catalog, or write path for one consumer.
- Preserve comments, unknown fields, ordering, whitespace, line endings, and
  untouched bytes in authored formats. Keep the lossless syntax layer separate
  from typed schema semantics and from compiled runtime artifacts.
- Expose immutable snapshots and explicit, revisioned operations. Library APIs
  do not assume mutable global state, a repository layout, sibling checkouts,
  current working directory, or implicit input/output paths.
- The editor consumes released Rust crates and transaction APIs. Content CI and
  automation consume the released CLI. The Go server consumes documented,
  language-neutral artifacts and conformance fixtures, never Rust FFI.
- ABIN and AMAP are deterministic, bounded content contracts. Define canonical
  encoding and digest inputs, version and feature handling, integrity checks,
  allocation limits, and negative fixtures. Do not use raw Protobuf bytes as a
  canonical content hash or make the game wire protocol a toolkit dependency.

## Content branches and mutation safety

- `atrinik/content@main` is the forward authored-content line and may adopt
  migrations such as JSONL through separately reviewed content issues.
  `atrinik/content@1.x` is the maintained classic-release line for the legacy
  GPL game. Never assume that a format, schema, or generated artifact can be
  changed on both lines together.
- Target `content@main` by default. Touch or generate changes for `content@1.x`
  only when an issue expressly owns classic compatibility or maintenance, and
  keep its release inputs reproducible independently of `main`.
- Make repository, branch, revision, input roots, output roots, and supported
  schema/artifact versions explicit. Never discover a mutation target from a
  nearby checkout or silently write across branch boundaries.
- All mutation workflows are dry-run-first. Show deterministic semantic and
  text diffs, validate the complete resulting project, enforce revision
  preconditions and destination allowlists, and then write atomically. Preserve
  permissions where applicable and leave either the fully old or fully new
  state after cancellation or failure.
- Bound bytes, records, tokens, nesting, strings, graph work, diagnostics,
  allocations, output, and execution time before accepting untrusted or
  malformed content. Filesystem enumeration, map iteration, concurrency, cache
  warmth, and locale must not affect output bytes or diagnostics.
- Incremental and clean builds must converge to identical catalogs,
  diagnostics, artifacts, manifests, and digests. Caches are disposable
  accelerators, never sources of truth.

## Licensing and provenance

- New source code in this repository is MIT. Do not add GPL, AGPL, legacy
  Atrinik implementation code, or a dependency that would make the toolkit or
  its distributed artifacts incompatible with that boundary.
- Authored maps, archetypes, scripts, media metadata, imported fixtures, and
  generated packages retain their actual licenses. Repository or tool license
  does not relicense content, and test data is not exempt from provenance.
- The approved historical MIT provenance-grantor registry in
  `atrinik/atrinik/AGENTS.md` is exhaustive and authoritative; do not maintain a
  grantor list here. Apply a grant only after a complete, non-shallow history
  audit, including renames and moves, proves that independently separable
  material is the listed grantor's solely authored original work.
- Verify author identities and every relevant change, and review copied,
  generated, vendored, or embedded third-party material and notices. Current
  blame, a commit author alone, recollection, mixed authorship, incomplete
  history, or uncertain origin is insufficient. Fail closed.
- Record the immutable source repository, paths and revisions; complete history
  and identity evidence; third-party and license review; selected grantor and
  permitted operation; transformation; destination; and retained notices in a
  committed provenance manifest or the destination pull request. Cite the
  exact `atrinik/atrinik` revision containing the registry entry used.
- Prefer independent implementation from documented behavior when reuse cannot
  be proven. Never use an ineligible legacy module or fixture as a temporary
  dependency in a released build.

## Milestone order

- M1 establishes the pinned Rust/Cargo foundation, provenance audit, and
  bounded lossless document model. Independent bootstrap work may proceed while
  provenance is audited; direct reuse waits for its own completed evidence.
- M2 builds the stable catalog and diagnostics, transactional patches,
  deterministic ABIN/AMAP contracts, and versioned crates, CLI, schemas, and
  conformance fixtures. Freeze narrow contracts so parser, indexing,
  transaction, compiler, release, and independent Go-loader work can proceed in
  parallel.
- M3 consumes released M2 contracts in the first playable replacement. Toolkit
  work in this gate is limited to owned compatibility, fixture, diagnostic, and
  release fixes; game runtime behavior belongs to its component repositories.
- M4 migrates checker, collector, schema, audit, and packaging workflows, then
  proves clean/incremental whole-corpus equivalence and cache behavior. Split
  migration by workflow group only after the shared model and transaction
  contracts are stable.
- M5 supports reviewed authored-content migrations and complete gameplay/world
  parity without moving server runtime scripting into this repository or
  changing product design. M5 closure must not hide unowned, silently skipped,
  unclassified, or unsupported content behavior behind aggregate success.
- M6 hardens releases, compatibility, migration, rollback, and cutover. Remove
  an old production workflow only after its replacement and corpus evidence
  meet the coordinated roadmap gate.

## Validation and handoff

- During the seed stage, commands beyond `git diff --check` do not exist. Issue
  #1 owns the Cargo workspace and the required aggregate `Content toolkit
  validation` check; do not invent success for absent tooling.
- Once bootstrap commands exist, run the repository-documented pinned toolchain
  equivalents of formatting checks, Clippy with warnings denied, workspace
  unit/doc/property tests, relevant fuzz targets, dependency/license/audit
  checks, whole-corpus or conformance tests for affected contracts, and release
  dry-run checks. Run `git diff --check` for every change.
- A clean clone must build, test, and run the CLI without sibling repositories.
  Use explicit fixture paths for focused tests; use the thin `./atrinik`
  wrapper for cross-repository profile, build, supply-chain, and runtime
  verification once this component has a stable wrapper contract.
- Update `atrinik/atrinik/supply-chain/inventory.json` in a coordinated wrapper
  change whenever toolchains, package sources, Actions, images, vendored inputs,
  licenses, owners, update cadence, EOL response, or validation paths change.
  Pin Actions and images immutably, retain updater hints, and do not add Git
  submodules.
- Every handoff states what was validated and what could not yet run. Include
  copy-pasteable component commands and, when integration is relevant, exact
  wrapper profile and build commands. Runtime topology steps are not applicable
  to a headless toolkit-only change.
- Publish component conformance, whole-corpus coverage, determinism,
  compatibility, exclusion, and skipped-path reports as evidence for
  [atrinik/atrinik#279](https://github.com/atrinik/atrinik/issues/279) and
  [atrinik/atrinik#280](https://github.com/atrinik/atrinik/issues/280). Every
  exception needs an owning issue, milestone, and explicit effect on cutover.
