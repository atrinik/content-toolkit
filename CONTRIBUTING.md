# Contributing

Work from an issue and preserve the repository boundaries in `AGENTS.md`.
New code is MIT and must not make builds or runtime depend on Classic
implementation source. Independent implementation from public contracts is the
default where exact reuse is not proven. Exact historical Classic material may
be inspected or reused only when the
[local provenance record](PROVENANCE.md) and
[canonical grant registry](https://github.com/atrinik/atrinik/blob/main/docs/PROVENANCE.md)
admit every copyrightable portion at an exact source revision. Complete
rename-aware history must prove each portion is original past work solely
authored by its grantor; present-day blame, majority authorship, later edits,
and agent-assisted commits are insufficient. An admitted destination may copy,
adapt, port, translate, and MIT-relicense that material, but must not depend on
the GPL Classic source. The destination grant does not change the Classic
repository's GPL distribution. Record the exact evidence and exclude every
uncovered portion.

Direct human-written code contributions are welcome. The project currently
develops software primarily through Codex-driven agentic workflows, but changes
written by people or agents follow the same evidence-gated provenance,
maintainer review, licensing, testing, and repository-validation requirements.
Using an agent is not a contribution requirement.

Before opening a pull request, run:

```sh
tools/validate.sh
actionlint .github/workflows/*.yml
git diff --check
```

Public crate, CLI, diagnostic, schema, fixture, and artifact behavior is a
versioned API. Pull-request titles and commits use Conventional Commits.
