# Provenance review

This repository is MIT clean-room work. The authoritative grantor registry is
`atrinik/atrinik@d64a8e958ca2adad783ad8912493d468a805f3fd:AGENTS.md`;
this repository does not maintain a second grantor list.

## Audited source

The audit used the complete, non-shallow `atrinik/content` history through
`01b1fdb65c2243df4bafe9c8109fc93229df0121`. Its `tools` tree has 50 paths and
105 commits touching the scope. Complete history contains these author and
committer identities:

- Alex Tokar `<admin@atokar.net>`
- Edwin Miltenburg `<mamoru@edwinmiltenburg.nl>`
- Zoey Rose `<zoey@zoeysr.com>`
- Zoey Rose `<3865595+zoeyrose@users.noreply.github.com>`
- GitHub `<noreply@github.com>` as merge committer

Consequently the directory is not admitted as a unit. Its historical
`tools/COPYING` is GPL-2.0 license text, and authored fixtures/data are not
implicitly relicensed.

## Narrow admitted translation

Six independently separable model/parser/limit files are admitted under Zoey
Rose's registry row. Each file was introduced once and has no other historical
touch through the audited revision. Commits
`96073eeff1854fc29347fdafd32e622394f24c07` and
`4aa4aebc5c88dffdf57657a34ae20306a57fbebd` have GitHub API author login
`zoeyrose`, GitHub committer `web-flow`, and `verification.verified=true` with
reason `valid`. Blob IDs, exact paths, destination, transformation, grant,
third-party review, and attribution are in `provenance/reuse.json`.

The Rust destination translates only abstract bounded/lossless model concepts.
It copies no Python source, classic implementation, content, fixture, schema,
generated output, or third-party dependency. All destination tests use new
synthetic MIT inputs. Every other audited path is explicitly excluded.

## Reproduction

From a complete `atrinik/content` checkout at the audited revision:

```sh
git rev-parse --is-shallow-repository
git rev-parse 01b1fdb65c2243df4bafe9c8109fc93229df0121
git log --all --format='%an <%ae>%x09%cn <%ce>' -- tools | sort -u
git log --all --format='%H' -- tools | wc -l
git ls-tree -r --name-only 01b1fdb65c2243df4bafe9c8109fc93229df0121 tools | sort
git log --follow --format='%H%x09%an <%ae>' 01b1fdb65c2243df4bafe9c8109fc93229df0121 -- tools/content_core/parser.py
git rev-parse 01b1fdb65c2243df4bafe9c8109fc93229df0121:tools/content_core/parser.py
```

Run `tools/audit-provenance.sh --source /absolute/content/checkout` to verify
all recorded paths, blobs, one-touch identities, and the unchanged audit head.
It fails if the checkout is shallow or `tools` changed after the audited
revision. Clean-clone CI validates the manifest itself without assuming a
sibling repository.

GitHub signature and identity evidence is reproducible with:

```sh
gh api repos/atrinik/content/commits/96073eeff1854fc29347fdafd32e622394f24c07 --jq '{sha,author:.author.login,committer:.committer.login,verification:.commit.verification}'
gh api repos/atrinik/content/commits/4aa4aebc5c88dffdf57657a34ae20306a57fbebd --jq '{sha,author:.author.login,committer:.committer.login,verification:.commit.verification}'
```
