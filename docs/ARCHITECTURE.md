# M1 architecture and bounds

The workspace is headless and layered in one direction:

```text
diagnostics <- source <- schema/catalog/transaction <- atrinik-content
                                  artifact -----------^
source -------------------------------------- testkit
```

`atrinik-source` owns byte spans, immutable source/revision snapshots, bounded
parsing, exact untouched bytes, diagnostics, and revisioned edit plans. Schema
semantics do not alter syntax. Catalogs use ordered maps. Transactions preview
a new immutable document. The small `ATF1` envelope proves deterministic,
bounded compiler/decoder plumbing; it is not the future ABIN/AMAP contract.

Default source limits are 8 MiB per file, 64 KiB per line, 250,000 records,
1,000,000 tokens, 1 MiB per replacement value, 4,096 nonoverlapping edits,
nesting depth 64, and 256 diagnostics. Limits are checked before accepting work
or allocating based on a declared size. Parsing is linear in input bytes; edits
sort explicit spans and copy at most the bounded output once. No library reads
or writes a path.

The M1 authored syntax is a byte-preserving line model: blank/comment lines,
ASCII keys, separated opaque byte values, `Object` starts, and `end` records.
Unknown and repeated fields, whitespace, comments, encodings, and LF/CRLF
bytes remain untouched. Malformed records are preserved with classified,
bounded diagnostics. Semantic edits replace only a selected value span and
must match the source SHA-256 revision.

## Catalog boundary

The catalog consumes already parsed `CatalogDocument` values; it neither owns
resource payloads nor reads paths. A document records its `SourceId`, SHA-256
revision, schema version, definitions, aliases, inheritance, typed reference
edges, bounded preview metadata, and opaque provenance/license evidence
references. Evidence references identify records for later policy checks; the
catalog makes no legal or ownership determination.

Stable IDs have the form `domain:namespace/local-id`. Their domain and local ID
are never derived from directory enumeration, display text, or localization.
All externally observable traversal uses ordered maps and sets. Catalog
generations hash canonical semantic input order, including schema versions and
source revisions, so the same input is reproducible regardless of discovery
order.

`LineDocumentLoader` is the common adapter boundary for all nine owned domains.
It maps selected parsed fields to aliases, inheritance, typed references, and
preview metadata without a second parse. Integrations can also construct
`CatalogDocument` directly when a domain has a different source grammar.
`resolve`, `search`, `preview`, and `dependents` are the shared query surface for
CLI, CI, and editor consumers.

Document replacement and removal return a new immutable catalog plus an
`Invalidation`: changed canonical IDs and aliases, followed through both old
and new reverse-reference indexes to obtain transitive dependents. Identical
documents return the existing generation with empty invalidation. Limits cap
documents, definitions, aliases, references, preview values, strings, graph
work, diagnostics, and invalidation breadth before unbounded work can occur.

## Diagnostic boundary

Parser, schema, and catalog layers share `atrinik-diagnostics::Diagnostic`.
Stable machine codes accompany severity, an exact source span, related
locations, a semantic path, a human message, and an optional fix hint.
Suppression is allowlisted by code and only affects diagnostics whose producer
explicitly permits suppression; conflicts, ambiguity, and required missing
references remain active errors. A `DiagnosticSet` preserves deterministic
insertion order and reports truncation when any configured count, text, or
depth bound is reached.

The CLI requires explicit input, source identity, and new output paths. It
preflights and bounded-reads inputs, validates before output, writes and syncs a
same-directory temporary file, then atomically hard-links it into a previously
absent destination. A race cannot overwrite an existing output. Later atomic
multi-file mutation belongs to the transaction milestone, not this bootstrap.
