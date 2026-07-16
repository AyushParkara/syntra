# How Syntra feedback is triaged

Maintainers use this flow during the public beta:

1. Confirm that reports are sanitized and use the correct channel.
2. Label the report by type and area.
3. Reproduce bugs or request the smallest missing detail.
4. Decide whether the report is a bug, documentation gap, routing-data problem,
   support question, or feature proposal.
5. Turn accepted work into a small issue with a definition of done before inviting
   a contribution.

## Contributor expectations

- Keep pull requests focused.
- Explain user-visible and safety implications.
- Include compile/import/lint evidence and manual validation notes.
- Do not include secrets, private state, or copied private-project content.
- Discuss broad routing, provider, safety, permission, sandbox, or TUI redesigns
  before implementing them.

## Response expectations

Syntra is community-supported beta software. Maintainers aim to acknowledge
actionable reports, but do not promise a response time. Security reports follow
the private process in `SECURITY.md`.
