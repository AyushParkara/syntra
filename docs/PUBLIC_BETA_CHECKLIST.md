# Syntra public-beta community and release checklist

This is the maintainer checklist for turning Syntra into a healthy public beta.
It separates work that can live in the repository from settings that require the
maintainer's GitHub or PyPI account.

## Status vocabulary

- **Required** — complete before the public beta announcement.
- **Recommended** — complete before inviting broad outside contribution.
- **Optional** — useful, but not needed for this beta.
- **Deferred** — intentionally postponed until Syntra has stable real-world use.

## Required repository files

| Item | Status | Notes |
| --- | --- | --- |
| `README.md` | Present | Explains the beta, installation paths, routing, and feedback. |
| `LICENSE` | Present | Apache-2.0. |
| `CONTRIBUTING.md` | Present | Explains setup, validation, private tests, and safety-sensitive changes. |
| `SECURITY.md` | Present | Explains private vulnerability reporting and redaction expectations. |
| `CODE_OF_CONDUCT.md` | Present | Sets community behavior expectations. |
| Bug-report form | Present | Collects reproducible, sanitized reports. |
| Feature-request form | Present | Collects problem-first proposals. |
| Issue routing configuration | Required | Sends questions/support/security reports to the right place. |
| Documentation-feedback form | Required | Lets beta users report confusing or stale docs. |
| Routing-feedback form | Required | Captures model-route, cost, failover, and provider feedback. |
| Pull-request template | Present | Requires validation and risk declaration. |
| Release-note template | Required | Keeps every public release consistent. |
| Design references notice | Required | Records intentional palette/design references without legal conclusions. |
| Support policy | Required | Defines bugs vs questions vs security reports. |
| Discussions drafts | Required before enabling Discussions | Provides pinned welcome and beta-feedback posts. |

## Required GitHub account settings

These cannot be applied from a local checkout. Configure them after creating
`AyushParkara/syntra`.

1. Make the repository public and enable GitHub Actions.
2. Protect `main`:
   - require a pull request before merge;
   - require the `ci / build` and `ci / lint` checks;
   - require code-owner review when collaborators can merge;
   - disallow force pushes and deletion.
3. Enable private vulnerability reporting.
4. Enable Dependabot alerts/security updates, secret scanning, and push protection
   where available.
5. Create the labels listed below.
6. Create the protected `pypi` Actions environment.
7. Configure PyPI pending Trusted Publishing with:

   ```text
   PyPI project name: syntra
   GitHub owner: AyushParkara
   GitHub repository: syntra
   Workflow filename: release.yml
   Environment name: pypi
   ```

## Labels to create in GitHub

Labels are GitHub repository settings, not files that GitHub automatically reads.
Use the definitions in [LABELS.md](LABELS.md) when creating them.

The essential beta labels are:

```text
bug
documentation
enhancement
question
good first issue
help wanted
provider
routing
safety
TUI
benchmark
needs reproduction
needs maintainer decision
```

## Issue and support routing

| Need | Where it goes |
| --- | --- |
| Reproducible product defect | Bug report issue form |
| Desired product behavior | Feature request issue form |
| Incorrect, unclear, or stale docs | Documentation-feedback issue form |
| Poor route/cost/failover/provider outcome | Routing-feedback issue form |
| Normal setup or usage question | GitHub Discussion after Discussions is enabled; otherwise the question/support issue form |
| Vulnerability, secret exposure, approval bypass, sandbox escape, unexpected command execution | GitHub private vulnerability report — never a public issue |

## Discussions setup

Discussions are recommended after the first public release, not required before
the repository exists. When enabled, use these categories:

```text
Announcements
Questions & help
Ideas
Routing & provider feedback
Show and tell
Contributor coordination
```

Publish the drafts in `docs/community/discussions/` as the first pinned posts:

1. Welcome to Syntra public beta.
2. Known beta limitations and what feedback is most useful.
3. How to contribute and how maintainers triage feedback.

## Release and tag format

Keep the Git tag, Python package version, Syntra CLI version, GitHub Release, and
PyPI version aligned.

```text
Git tag:        v0.1.0
pyproject:      0.1.0
syntra version: 0.1.0
PyPI version:   0.1.0
```

Use semantic versioning:

```text
v0.1.0  first public beta
v0.1.1  beta bug-fix release
v0.2.0  meaningful beta milestone
v1.0.0  stable core release
```

Mark beta GitHub Releases as **pre-releases**. Use
[RELEASE_TEMPLATE.md](RELEASE_TEMPLATE.md) to draft the release notes.

## Contributor backlog before broad outreach

Before asking for contributions, create five to ten small issues with clear:

- problem and user impact;
- owned files or area;
- definition of done;
- safe validation command or manual validation steps;
- labels, especially `good first issue` or `help wanted` where appropriate.

Good early work includes documentation improvements, provider examples, model
catalog tags, routing feedback analysis, small TUI polish, and public benchmark-task
definitions. Do not use first-time contributor issues for sandbox, approval, secret,
or permission changes.

## Intentionally deferred

These are valuable, but wait until Syntra has stable real usage:

- public benchmarks and benchmark report;
- polished terminal recording, screenshots, or GIFs;
- Hacker News, Product Hunt, Reddit, X, and LinkedIn launch campaign;
- GitHub Sponsors or `FUNDING.yml`;
- a separate community plugin repository;
- formal contributor agreement or complex governance process.

## Final pre-announcement check

1. The repository is public and the canonical URL works.
2. The release tag/version/changelog agree.
3. The `pypi` environment and pending Trusted Publisher are configured.
4. `syntra --version`, `syntra --help`, package build, and private tests pass.
5. Private vulnerability reporting and branch protection are enabled.
6. Labels and starter issues are created.
7. The design references notice is accurate.
8. Only then create the first `v0.1.0` pre-release and publish it to PyPI.
