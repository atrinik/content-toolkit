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
files at most 256 KiB; structured results at most 64 KiB. Cancellation and a
five-second deadline are checked during traversal and canonical previews.
Errors contain fixed codes and never echo caller values. Preview is entirely
in memory and grants no permission to publish. No apply, execution, network,
credential, generated-state or write operation exists in this provider.

`input_schema()` and `output_schema()` publish the JSON Schema 2020-12 v1
contract. Synthetic tests compare canonical IDs against six complete pages
of 300 records, check identity-bound cursor invalidation, closed requests,
unsupported operations, cancellation, control characters, and hard limits.
