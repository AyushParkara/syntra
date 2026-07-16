# Security Policy

Thank you for helping keep Syntra safe.

## Reporting a Vulnerability

Please do **not** open a public issue for vulnerabilities that could expose secrets, execute commands unexpectedly, bypass approvals, or escape the workspace/sandbox.

Use GitHub's private vulnerability reporting if it is enabled for this repository. If it is not enabled yet, contact the maintainer through the GitHub profile linked from the repository and include only the minimum details needed to start a private discussion.

## What To Include

- A short description of the issue.
- Steps to reproduce.
- The affected command/tool/surface.
- Whether secrets, files outside the workspace, network egress, or command execution are involved.
- Your environment: OS, Python version, terminal, and Syntra version/commit.

Please redact API keys, local provider configs, `.syntra/` state, and private project paths.

## Security-Sensitive Areas

Syntra treats these areas as high risk:

- shell command classification and sandboxing;
- file writes, edits, patches, and path resolution;
- provider keys and OAuth tokens;
- MCP servers and hosted tools;
- browser/preview/webfetch/websearch surfaces;
- repo-local instructions, hooks, plugins, and config;
- approval/access modes.

For the implementation threat model and current hardening, see `docs/SECURITY.md`.
