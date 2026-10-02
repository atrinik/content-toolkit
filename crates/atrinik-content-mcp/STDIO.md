# Explicit configured-root stdio adapter

Run `atrinik-content-mcp --config /absolute/operator/config.json`. There is no
implicit working-directory root and no client path or MCP Roots authorization.
The configuration is a closed JSON object with `snapshots`, each containing
`root`, the complete semantic `identity`, and an explicit `files` inventory.
Each file has `path`, `domain`, `namespace`, optional `single_id`, optional
`required_fields`, and optional `rules`. Rules have a `kind` of `alias`,
`inherits`, `embedded_object`, `label`, `summary`, `tag`, `keyword`, or
`reference`; reference rules also select `domain` and optional `optional`.
Canonical `LineDocumentLoader`, `Schema`, and project snapshots implement these
semantics. Configuration never supplies executable commands or parsers.

The Linux adapter opens every root and file component descriptor-relative with
no symlink following, nonblocking opens, and regular-file/metadata checks.
Hardlinks, dot paths, traversal, known state directories and secret filenames
are rejected. Files must be Git-tracked and not ignored, even if tracked.
Each file is limited to 256 KiB, all snapshot bytes to 8 MiB, and each inventory
to 1,000 files. Canonical parser limits additionally constrain records/tokens.
The configuration file itself is read using the same safe bounded reader.

Fixed, read-only Git metadata queries establish actual HEAD and branch, prove
`main_base_commit` is an ancestor of both the selected commit and local main,
and verify the inventory against tracked/ignored state. Clean file bytes must
match the selected immutable Git blob. Git executes with a cleared environment,
disabled hooks/fsmonitor, no shell, no network and bounded output/time. Linux
`/proc/self/fd` pins its working directory to the opened root descriptor. Other
platforms fail closed until an equivalent descriptor-pinned implementation is
available. Configuration does not constitute ancestry proof.

Dirty registration requires a SHA-256 fingerprint over Git's NUL-delimited
`status --porcelain=v1 -z --untracked-files=no` bytes followed by sorted selected
files. For each file append little-endian u64 path byte length, UTF-8 path,
little-endian u64 content byte length, and exact bytes. Clean registration uses
null. This is an explicit admission fingerprint, never automatically invented
for a changed worktree. Dirty tracked paths outside the admitted inventory and
rename/conflict status fail closed because their complete bytes cannot be
attested by this inventory. Root, Git identity, tracked/ignored inventory, status,
file bytes and modes are checked on each query; changed inputs fail closed.
The canonical immutable catalog stays in memory for warm queries. Restart with
a freshly admitted configuration to change its identity or inventory.

Transport follows the pinned MCP 2026-07-28 schema:
https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/5f5440bb26a62e2cf3440b92da5a667efa03b267/schema/2026-07-28/schema.ts

Every request carries `params._meta` with
`io.modelcontextprotocol/protocolVersion: "2026-07-28"` and
`io.modelcontextprotocol/clientCapabilities: {}`. `server/discover`, `ping`,
`tools/list`, and the single `content_query` tool are available without an
initialization handshake. Successful results include `resultType: "complete"`
and server identity metadata. Unsupported versions return -32022 and supported
versions. Legacy `initialize`, arbitrary execution, writes, publication,
resources and client roots are unavailable.

Newline-delimited JSON requests are limited to 16 KiB and responses to 64 KiB.
Overlong input closes the stream after a bounded error. One worker processes
queries and one bounded reader recognizes `notifications/cancelled` by exact
request ID, including queued requests. At most one request waits behind active
work; overflow cancels outstanding work and closes the stream. Reader shutdown
does not wait for a peer holding a partial frame. Catalog queries and freshness
checks share the same five-second deadline. Domain errors expose stable codes,
never filesystem paths, Git diagnostics, configuration values or source bytes.
