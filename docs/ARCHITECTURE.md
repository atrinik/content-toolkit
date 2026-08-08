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
1,000,000 tokens, 1 MiB per replacement value, nesting depth 64, and 256
diagnostics. Limits are checked before accepting work or allocating based on a
declared size. Parsing is linear in input bytes; edits sort explicit spans and
copy at most the bounded output once. No library reads or writes a path.

The M1 authored syntax is a byte-preserving line model: blank/comment lines,
ASCII keys, separated opaque byte values, `Object` starts, and `end` records.
Unknown and repeated fields, whitespace, comments, encodings, and LF/CRLF
bytes remain untouched. Malformed records are preserved with classified,
bounded diagnostics. Semantic edits replace only a selected value span and
must match the source SHA-256 revision.

The CLI requires explicit input, source identity, and new output paths. It
preflights and bounded-reads inputs, validates before output, writes and syncs a
same-directory temporary file, then atomically hard-links it into a previously
absent destination. A race cannot overwrite an existing output. Later atomic
multi-file mutation belongs to the transaction milestone, not this bootstrap.
