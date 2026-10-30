# Classic schematic prototype

`atrinik-content classic` imports a separately supplied Classic map into an
ordinary Tilewright World 0.1.0 project. The generic Tilewright editor owns its
preview, transaction history and undo/redo. The nonpublished
`atrinik-classic-adapter` crate owns Classic semantics and lossless export;
there is no renderer, editor, GPU or gameplay dependency in this repository.
Classic development continues. This is an offline schematic editing prototype,
not a runtime cutover or a claim of Classic visual fidelity.

## Input, output and commands

Linux local checked filesystems are supported. Provision three fresh, distinct,
non-nested directories with mode 0700, outside the canonical input tree: a World
root, immutable adapter-state root, and later a new export root. Paths must be
absolute and canonical without symlinks. The provisioner owns that separation;
no command silently creates an arbitrary root or changes its permissions.

```text
atrinik-content classic import-map INPUT_ROOT REL_MAP WORLD_ROOT STATE_ROOT SOURCE_COMMIT EXPECTED_ORIGINAL_SHA
atrinik-content classic export-preview STATE_ROOT WORLD_ROOT EXPECTED_ORIGINAL_SHA EXPECTED_WORLD_SHA
atrinik-content classic export-apply STATE_ROOT WORLD_ROOT EXPECTED_ORIGINAL_SHA EXPECTED_WORLD_SHA OUTPUT_ROOT NEW_REL_FILE EXPECTED_OUTPUT_SHA
```

`SOURCE_COMMIT` is the complete lowercase `atrinik/content@main` source revision;
the map SHA-256 is independently supplied and verified. Import returns JSON
with the World revision, placement/movable counts and unsupported features.
The source input is read through the toolkit's existing no-follow reader and
never written. No exit, script, media name or other source reference is followed.

The World root contains ordinary `world.jsonc`, `profile`, `defs` and `planes`
files, plus the strict `tilewright.editor-preview/1` catalog and standalone ATX1
resources in `preview-assets`. Every placement, including floors, is an explicit
object. The profile is `atrinik.classic.schematic`, version `1`. Source-order
ordinal UIDs (`classic_0`, etc.) belong to this import only; archetype names map
injectively to bounded hexadecimal prototype IDs.

Adapter state contains exact `original.map`, `import.json` and `baseline/`
source snapshots. Treat this state as immutable; a new import uses new roots.
The manifest records source identity/license, dimensions, mappings/value spans,
exact baseline digests and capability gaps. It is data, never export authority.
The original map remains separately licensed GPL content, outside MIT source
archives. The generated geometric schematic glyphs are newly authored MIT
pixels; no Classic artwork is copied or licensed by this adapter.

Open the World root in the generic editor. Move only a UID listed as movable.
Use its existing pending/accept/cancel and undo/redo workflow. Export reads only
committed World files. Supply the editor's current full World revision, inspect
the JSON preview (original/output digests and exact value replacements), then
explicitly apply that output digest to a fresh export root. No command has an
overwrite/in-place flag. Repeated publication into an occupied root refuses.

## Export boundary

Only coordinate changes on top-level blocks containing exactly one existing
`x` and one existing `y` value are supported. Positions remain inside the
original 1..128 by 1..128 map bounds, on the same plane and spatial layer.
Omitted coordinates project as zero and remain read-only. Repeated identical
coordinates project read-only; conflicting repeats refuse import. Malformed
nesting, extra headers and structural ambiguity refuse import. Classic `arch`
is interpreted over canonical Field records; messages remain parser-owned.

Export regenerates the complete baseline and source addresses from the original
bytes, compares the supplied manifest and baseline against that regeneration,
and checks the independently supplied original and current World revisions.
It compares the entire authoritative World document inventory and canonical
contents against the coordinate-only projection, plus all typed World parts.
Changed prototypes, order, inventory, schema, profile, definitions, terrain,
fields, custom data, portals or structures refuse export. Cross-chunk movement
also refuses if the editor changes source order. JSONC whitespace/comments may
change only when the complete canonical comparison still matches; an identity
or selected-field diff is never sufficient. Supplemental catalog/resources must
still match the import bytes; UI capabilities are not the security boundary.

Approved values go through the canonical `Transaction::replace_value` engine.
The result is reparsed and reimported, and unchanged byte ranges are checked.
No-op export is byte-identical, including unknown fields, comments, messages,
line endings and nested inventory. Nothing regenerates the whole Classic map.

Private-root publication holds retained directory handles and an exclusive
cooperative lock. It refuses unchecked filesystems, wrong owner/mode, ACL/label
problems, links, traversal and occupied stage/final names. Each fresh file is
staged, synced, reread, renamed, directory-synced and reread. Uncertain outcomes
report stage/final, applied and durable state; surviving evidence is retained,
and that handle blocks retries. Import writes the World manifest and state
manifest last; a failed initialization leaves retained partial roots that must
not be opened as a successful import. Publication does not claim isolation from
hostile processes with the same UID or arbitrary root-alias provisioning.

## Coverage and bounds

The external representative map is `maps/shattered_islands/deserted_tutorial_island/ship_lower_deck`
at content revision `60398f91cd5dc7d0523e6f6b601e237efa84b7c0`, 7,055 bytes,
SHA-256 `ab84048e5a9c4480962e475ccd50ff962ac1e8d04e43e3c1f5345da4e7847e87`.
It has a 5×14 footprint and 151 top-level placements; 107 have unique explicit
x/y coordinates. Stacked placements and omitted defaults are represented.
Original z/align/zoom, layer/sub-layer, lighting, direction/animation, sky,
celestial data, music, region, messages and exits are retained and reported,
but the geometric preview does not reproduce their gameplay or rendering.
Nested inventories are preserved read-only in original source, not rendered.
Structural insertion/deletion, terrain paint, map resizing and coordinate
insertion require a separately reviewed adapter extension.

Caps: 8 MiB source/file, 65,536 records, Classic nesting 16, 128² map cells,
256 archetypes/resources and diagnostics, 4 MiB manifest, 32 MiB generated or
loaded source workspace. Filesystem enumeration has 4,096 entry, 16-level and
64 MiB total bounds. World parser limits are explicit. Tests use newly authored
MIT fixtures with mixed newlines, unknown fields, nested inventory, messages,
default/repeated coordinates, hostile edits, stale/tampered authority, and
publication fault/lock/privacy/root-retarget cases. Original public map and art
are not bundled in tests or release archives.
