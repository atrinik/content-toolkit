# Canonical content MCP adapter v1

This MIT adapter queries immutable `atrinik-catalog` snapshots and calls
`atrinik-transaction::project::preview` in memory. Its seven operations are
search, inspect, references, impact, validate, compare, and preview. Search
uses the canonical catalog's stable typed IDs, domains, presentation text and
path/field filters. Inspect accepts an entity ID or an allowlisted document
path, returning field bytes as hex with exact source spans. Unknown domain or
schema versions return a bounded unsupported-schema error; no ad hoc parser
interprets unimplemented content formats.

The local transport owns configured-root validation and commit ancestry.
`Snapshot::new` requires a full selected commit, explicit main base commit,
repository, branch, source/view role, worktree, authorization, manifest,
profile, registry and version. Main and review snapshots stay separate;
Classic and replacement views refer to the same selected content commit.
Callers must establish ancestry before constructing snapshots. Content and
labels are untrusted data. Display text never substitutes for identity.

The provider retains at most 128 snapshots and 8 MiB of source bytes. It has
no persistent cache. Replacing a provider with refreshed canonical snapshots
invalidates all earlier cursor coordinates: cursors bind the complete identity,
canonical project revision, index generation, all effective query parameters,
and both snapshots for comparison. Dirty observations have zero TTL. The
startup transport must refresh or fail stale before each mutable observation.

Requests are at most 16 KiB; queries at most 1,024 bytes; pages at most 50;
scans at most 1,000 records; graphs at most depth 8 and 1,000 edges; source
files at most 256 KiB; routine structured results at most 32 KiB (64 KiB protocol hard ceiling). Cancellation and a
five-second deadline are checked during traversal and canonical previews.
Errors contain fixed codes and never echo caller values. Preview is entirely
in memory and grants no permission to publish. No apply, execution, network,
credential, generated-state or write operation exists in this provider.

`input_schema()` and `output_schema()` publish the JSON Schema 2020-12 v1
contract. Synthetic tests compare canonical IDs against six complete pages
of 300 records, check identity-bound cursor invalidation, closed requests,
unsupported operations, cancellation, control characters, and hard limits.

Review previews retain the validated outer MCP identity: the actual selected
branch, source role, base commit, selected commit and dirty fingerprint. The
transaction model's `SourceIdentity.reference = refs/heads/main` identifies the
sole authored-source line; it is not a claim that the selected review checkout
is on that branch and never selects a Git write destination. The MCP adapter
provides previews only and exposes no apply operation.

Measure the complete stdio path using the released executable:

```sh
python3 crates/atrinik-content-mcp/tools/benchmark.py \
  /absolute/path/to/atrinik-content-mcp
```

Run in an offline environment with Python 3 and Git available. The script creates
one temporary synthetic repository with 300 definitions and the admitted content
origin. It checks the same stable identity in 30 persistent-process queries and
30 fresh-process queries. Both paths perform the provider's normal source
freshness checks; the fresh path also pays startup and index construction.
Stdin remains open until each response arrives, preserving cancellation
semantics. Output includes correctness, binary SHA-256, response bytes, startup
time, p50/p95, totals and median speedup. This measures the complete semantic
provider against process-per-query execution, while the ignored Rust benchmark
isolates canonical index reuse. Neither benchmark accesses a real content root.
