# Project transaction contract

`atrinik-transaction` is the sole mutation planner. Version 1 replaces existing
field values using the lossless `atrinik-source::EditPlan`; it does not normalize
or regenerate documents. A command identifies an allowlisted relative path,
source revision, record index, exact original byte span, replacement bytes, and
human-readable semantic intent. `schemas/transaction-plan.schema.json` describes
the JSON shape; the Rust decoder also rejects unknown and duplicate properties,
unsupported versions, and input larger than the configured bound.

The project revision hashes the complete sorted file inventory, source IDs,
bytes, logical permission modes, repository, branch, authored revision and schema
version with length-delimited SHA-256. Version 1 admits only explicit
`atrinik/content`, `refs/heads/main`, an exact 40-hex authored revision, and schema
version 1. Historical branches are never mutation targets. This identity is a
caller-supplied provenance precondition, not GitHub authentication or proof of a
commit's contents.

`preview` performs no filesystem operation. It compares the project/source/span
preconditions, rejects conflicting commands, builds new immutable documents, and
validates every resulting file with its explicit schema and catalog loader. The
complete catalog validates cross-file references, duplicates and inheritance.
The policy inventory must equal the snapshot inventory; missing policies fail
closed. Invalid results remain reviewable with shared structured diagnostics but
cannot pass the store's sealed validation check.

Semantic changes and diagnostics have deterministic source IDs/spans and ordering.
The text diff identifies original byte ranges and encodes deleted/inserted bytes
as lowercase hexadecimal, preserving arbitrary byte values without terminal
control interpretation or lossy UTF-8 conversion. It is a review format, not a
patch(1) input. The inverse plan carries the new revisions and adjusted spans;
undo is a new validated transaction and fails if any intervening project change
occurred. Reapplying an old plan similarly reports an exact revision mismatch;
no-op value replacements preserve the revision. Version 1 rejects replacements
starting with a space or tab: the parser would absorb those bytes into the field
separator, preventing a value-only inverse from restoring the original bytes.
Empty values and trailing whitespace retain reversible value spans.

Default project limits admit 10,000 files, 128 MiB of document bytes, 4,096 commands,
4 MiB of plan/diff budget, and 256 diagnostics. Source/schema/catalog limits bound
individual fields, tokens, nesting, graph work and diagnostics. Cancellation and
deadline checks occur between bounded commands, documents and catalog stages;
they are cooperative and cannot interrupt a kernel filesystem call or a bounded
single parser/catalog operation.

Disk publication uses the immutable generation store described in
`TRANSACTION-STORE.md`. Consumers must acquire one complete snapshot from that
store. Initializing a store imports a validated snapshot; it does not switch a
legacy checkout reader to the generation protocol. Updating unrelated original
checkout files one by one would not provide project atomicity and is outside this
API's publication contract. No consumer may treat the original import directory
as the store's live authored state.
