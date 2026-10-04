# Transaction publication contract

The Linux `GenerationStore` publishes a validated complete project by replacing
one `CURRENT` file using `renameat`. A generation is a bounded binary bundle of
all source bytes, source identities, relative paths, and original permission
modes. Its name includes the project revision digest. No sequential per-file
rename is presented as a multifile atomic transaction.

The application creates an explicit, empty, private directory (mode 0700) and
opens it with `GenerationStore::open`. Initialization validates the entire
snapshot against the supplied exact policy inventory. All subsequent changes
require a validated preview whose original project revision equals the current
project revision. Every reader calls `read` once and retains the returned
immutable snapshot throughout its operation. Ordinary directory readers are
outside this publication contract.

The directory must be owned by the effective user. The API opens every absolute
root component without following symlinks, then uses that pinned descriptor for
all operations. A new directory file description and nonblocking advisory flock
serialize every read/publication, including concurrent threads. A busy store
returns `Busy` so the caller can honor its own cancellation/deadline policy.
Published files must be singly linked regular files owned by the effective user,
mode 0400. Source permissions are metadata inside each immutable bundle; the
bundle itself is always private. Current bytes, digest and filesystem metadata
are rechecked under the lock immediately before publication.

This protocol assumes a local Linux filesystem implementing atomic same-directory
rename, advisory flock and file/directory fsync. All writers must use this API;
root permissions must remain private. A malicious process with the same user
identity can ignore advisory locks and change private files, so this is not an
isolation boundary against that process. Detected external edits, invalid
permissions, symlinks, special files, hardlinks or inconsistent digests fail
closed. The caller must keep the root's namespace and ownership under its
control; descriptors deliberately remain attached to their original directory
if an ancestor is renamed.

Publication writes a temporary generation, preserves logical file permissions,
fsyncs the private bundle, installs the generation, and fsyncs the directory.
It then writes/fsyncs a temporary `CURRENT`, checks cancellation and the old
snapshot again, renames `CURRENT`, and fsyncs the directory. Failures before
that rename leave the old snapshot authoritative. After rename, cancellation
cannot undo the committed publication: `CommitOutcome` reports the new revision.
A failed final fsync returns `durable: false` with a warning, because publication
has happened but crash persistence cannot be confirmed. Recovery after a crash
selects whichever complete old/new pointer the filesystem preserved. Durable
success requires the final directory fsync.

`recover` opens and validates `CURRENT` and its generation exactly like `read`.
It never guesses a generation from directory order and never promotes a
partially written generation. Unreferenced generations and crash temporary
files remain inert, available for inspection. Automatic garbage collection is
not implemented; operators may remove only unreferenced files during exclusive
maintenance. The bounded temporary-slot search fails closed if 1024 crash
remnants occupy all slots. This preserves prior generations for audit/recovery
without unbounded directory scanning.

Import uses an explicit complete inventory and `read_source_file` for bounded,
no-follow file reads, including logical mode capture. Files are read individually;
the caller supplies the source revision and owns consistency of its import
inventory. Initialization then validates and publishes the imported snapshot.
This API does not silently mutate or synchronize the imported authored tree.
Exports must write a new explicit destination from one retained snapshot. An
export into an ordinary mutable tree does not acquire this store's atomic
reader contract and must not be described as atomic multifile publication.

Fault injection covers generation creation/write/fsync/install/directory-fsync,
pointer creation/write/fsync, the prepublication check, publication, and durable
completion. Tests reopen the store at every checkpoint and require exactly the
old or new validated snapshot; they also exercise cancellation, stale previews,
metadata/content tampering, source limits, symlinks, hardlinks, permissions,
lock contention and crash-remnant recovery. Fixtures are original synthetic MIT
inputs; no authored game content is imported by the tests.
