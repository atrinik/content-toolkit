# Third-party notices

Rust package dependencies retain their licenses as recorded in Cargo metadata
and the release SBOM. `policy/dependencies.json` defines the licenses accepted
by validation without duplicating package versions from `Cargo.lock`. The
admitted historical design translation and synthetic fixtures are documented
in `PROVENANCE.md` and `provenance/reuse.json`.

The two byte-identical machine contracts in `policy/classic-authored-limits.json`
and `schemas/classic-diagnostic.schema.json` retain attribution to Zoey Rose and
are used under the historical MIT provenance grant recorded in
`provenance/linked-content-materials.json`.

No GPL/AGPL code, classic Atrinik implementation, authored game content,
media, or third-party fixture is distributed by this repository.

The transaction engine uses Serde/serde_json for strict versioned JSON and rustix
for safe descriptor-relative filesystem operations. Their exact releases and
licenses are recorded in `policy/dependencies.json`. Rustix and linux-raw-sys
also offer Apache-2.0 with the LLVM exception as an alternative to their
Apache-2.0/MIT terms; the dependency check recognizes that exact exception
expression without admitting other exceptions or copyleft licenses. The lockfile
and release SBOM retain all transitive package identities.
The JSON/derive dependency closure additionally includes memchr (Unlicense OR
MIT), ryu (Apache-2.0 OR BSL-1.0), and unicode-ident ((MIT OR Apache-2.0) AND
Unicode-3.0). Release bundles retain the complete dependency license/copyright notices,
including the Unicode data permission notice, in `third-party-licenses/`. Its
manifest binds each copied notice digest to the exact locked package release;
the SBOM records the complete license expressions.
