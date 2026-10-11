# Public library releases

The seven reusable libraries listed in `policy/crate-release.json` publish to
crates.io: artifact, catalog, diagnostics, schema, source, testkit and transaction.
Each manifest explicitly restricts publication to `crates-io`. The `atrinik-content`
CLI and `atrinik-content-mcp` application set `publish = false`; their binaries,
crate archives remain individual GitHub release artifacts. Schemas, fixtures,
policy and third-party licenses are retained in a deterministic
`content-assets-VERSION.tar.gz` asset. Every downloadable file is included in the
flat `SHA256SUMS` inventory.

Cargo versions are a separate contract from the semantic-release source version.
The workspace starts at Cargo version `0.1.0`, and its public crates and internal
exact-version dependencies advance together. Change `[workspace.package].version`
and each internal `=VERSION` constraint together when a package's contents change.
A source-only documentation or CI release can retain the Cargo version if the
packaged library contents are unchanged. Such releases omit already-owned public
crate coordinates and their archives; CLI/application archives remain available. PR validation compares crate inputs to
the base revision. Release preparation compares exact packaged contents to the
latest release that owns each crate version; an unchanged version with changed
contents fails before semantic-release creates or publishes a GitHub release.

The read-only source-package job calculates the source version with the pinned
commit analyzer and the existing release rules. `tools/package-release.sh` packages
and compiles the complete compatibility set without credentials, then prepares
`crate-release.json` before checksums and GitHub publication. The separate write
job repeats semantic-release analysis and validates the exact prepared version
and revision; its preparation hook performs no build or asset mutation. Its manifest binds the repository identity, immutable source
revision and source tag, exact archive checksums, complete file inventories,
registry metadata and package dependency graph. The registry publisher downloads
these already verified bytes and never runs Cargo, build scripts or package code.
Library dependencies publish in topological order after their exact versions and
checksums become visible on crates.io. Retries accept only an existing identical
version; an existing version with a different checksum fails closed.

After semantic-release completes, protected-main automation dispatches
`publish-crate.yml` with the exact source release ID, revision and manifest digest.
The publisher uses the `crates-io-release` environment and a short-lived crates.io
OIDC credential. Hourly reconciliation repairs a missed dispatch or interrupted
publication from immutable GitHub release assets. A draft, prerelease, moved tag,
changed asset or mismatched source revision cannot authorize publication.

## One-time owner setup

The seven names are initially unregistered. crates.io Trusted Publishing cannot
create their initial releases. The repository owner must bootstrap those names
locally using a crates.io API token and the same verified release archives, then
configure each crate's Trusted Publishing identity for the repository ID in the
policy, workflow `publish-crate.yml`, environment `crates-io-release`, and GitHub
OIDC audience. The token stays on the owner's machine; CI accepts only OIDC.
Enable owner-enforced immutable GitHub releases before the first source tag
through repository governance. Every registry write requires a published source
release whose public API record reports `immutable: true`. The source workflow
does not query administration-only settings. If repository setup produces a
mutable source release, publication fails closed; preserve its assets and obtain
explicit owner review for recovery. Automation never deletes or rewrites it.
Configure the
protected environment and workflow permissions before activating publication. Preparing this source does not perform bootstrap, change repository
settings or activate a publisher.

After the reviewed tooling is on `main` and an immutable source release exists,
run the owner bootstrap from a local terminal with explicit public account
coordinates:

```sh
python3 tools/crate_release.py bootstrap \
  --release-id SOURCE_RELEASE_ID --revision FULL_SOURCE_SHA \
  --manifest-sha256 MANIFEST_SHA256 \
  --expected-owner CRATES_IO_LOGIN --expected-owner-id CRATES_IO_ACCOUNT_ID
```

The helper verifies the complete release first, requires terminal confirmation,
and reads the token through a hidden prompt. It accepts only initially absent
names and verifies owner identity after publication. crates.io exposes no token
identity API, so the owner must create the short-lived token in that verified
account. Never supply it in environment variables or workflow secrets.

Use the helper's `--help` for read-only verification and reconciliation interfaces.
Retain the exact source release coordinates and successful checksum evidence for
bootstrap and recovery. Never recover by rebuilding or replacing release assets.
