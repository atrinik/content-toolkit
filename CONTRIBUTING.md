# Contributing

Work from an issue and preserve the repository boundaries in `AGENTS.md`.
New code is MIT and must not consult or depend on classic implementation source.
Historical reuse requires the complete evidence and approved grant described in
`PROVENANCE.md`; otherwise implement independently from public contracts.

Direct human-written code contributions are welcome. The project currently
develops software primarily through Codex-driven agentic workflows, but changes
written by people or agents follow the same clean-room provenance, maintainer
review, licensing, testing, and repository-validation requirements. Using an
agent is not a contribution requirement.

Before opening a pull request, run:

```sh
tools/validate.sh
actionlint .github/workflows/*.yml
git diff --check
```

Public crate, CLI, diagnostic, schema, fixture, and artifact behavior is a
versioned API. Pull-request titles and commits use Conventional Commits.
