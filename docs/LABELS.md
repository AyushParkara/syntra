# Syntra GitHub label catalogue

Create these labels in the GitHub repository after the first push. Labels are
repository settings, so this document is the source of truth for their names,
descriptions, and intended use.

| Label | Color | Use it for |
| --- | --- | --- |
| `bug` | `d73a4a` | Reproducible incorrect behavior. |
| `enhancement` | `a2eeef` | A confirmed product improvement. |
| `documentation` | `0075ca` | Documentation-only work or feedback. |
| `question` | `d876e3` | Usage/setup support that is not a defect. |
| `good first issue` | `7057ff` | Small, safe, well-scoped work suitable for a first contribution. |
| `help wanted` | `008672` | A maintainer has approved the work and wants outside help. |
| `provider` | `1d76db` | Provider setup, OAuth, model availability, keys, or failover. |
| `routing` | `fbca04` | Model selection, catalog data, cost modes, route health, or overrides. |
| `safety` | `b60205` | Sandboxing, approvals, secrets, file/path boundaries, MCP, browser, or network surfaces. |
| `TUI` | `c5def5` | Full-screen terminal UI, layouts, themes, accessibility, or keyboard flow. |
| `benchmark` | `5319e7` | Benchmark task definitions, methodology, or evaluation evidence. |
| `needs reproduction` | `f9d0c4` | More information is needed before a report can be acted on. |
| `needs maintainer decision` | `ededed` | A product/scope decision is needed before implementation. |

## Triage rules

- Apply one **type** label (`bug`, `enhancement`, `documentation`, or `question`)
  before closing or assigning an issue.
- Add one or more **area** labels where useful (`provider`, `routing`, `safety`,
  `TUI`, `benchmark`).
- Never apply `good first issue` until the issue has an owner area, a definition of
  done, and a safe validation path.
- Do not apply `help wanted` to security-sensitive changes unless a maintainer has
  already designed and reviewed the safe change boundary.
- Use `needs reproduction` instead of closing a potentially useful bug report
  immediately; close only after a reasonable follow-up window.
