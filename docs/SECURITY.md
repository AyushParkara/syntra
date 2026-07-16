# Syntra security — threat model, audit, and what's hardened

Plain-English summary of Syntra's security posture: what it protects against, what has been hardened, and where the boundary is.

## Threat model: local-first

Syntra is a local CLI/TUI that runs on **your machine** against **your projects**. The main risks are:

- a model/tool call trying to run a dangerous command;
- a prompt-injected repo/plugin/MCP result trying to hijack the agent;
- accidental writes outside the workspace;
- accidental secret reads/exfiltration;
- untrusted local config in a cloned repo.

Out of scope: using Syntra as a public network service for untrusted clients. If you expose it that way, you need a different server-grade threat model.

## What's hardened

| Area | Current behavior |
|---|---|
| Command classifier | Blocks privilege escalation (`sudo`/`su`), disk wipes, obvious destructive patterns, pipe-to-shell/interpreter RCE, and command substitutions that hide blocked commands. Mutating/unknown shell commands ask. |
| Shell sandbox | `auto` uses Bubblewrap/Seatbelt when available. If setup is impossible in the current container/kernel, Syntra warns loudly and falls back; `require` fails closed. |
| Workspace confinement | Normal workspace-write mode blocks writes outside the workspace. `danger_full_access` can lift only confinement-only blocks after explicit approval; hard-danger blocks stay blocked. |
| Secrets | Secret-looking paths such as `.env`, `.ssh`, `.aws`, `.kube`, Docker/Azure/GCloud credentials, provider configs, and process-environment dumps require approval. Common host credential dirs are masked inside the OS sandbox. |
| Network tools | `webfetch`, `websearch`, repo cloning, MCP HTTP, and browser preview are gated because network egress can exfiltrate data. `webfetch`/`preview` block private/loopback/metadata targets unless explicitly allowed for local development. |
| Browser/preview | Playwright browser tools are optional. `browser_screenshot` is workspace-confined. `preview` is write-gated, rejects `file:` URLs, SSRF-checks URLs, and writes only under `.syntra/preview/`. |
| MCP | Stdio MCP subprocesses get a scrubbed environment and now enforce read/handshake timeouts. HTTP MCP tokens are redacted from errors. MCP tool descriptions/results are sanitized/fenced as untrusted content. |
| Repo-local config | Folder-local provider/hook/MCP/plugin surfaces are trust-gated in CLI contexts where they could auto-spawn code or inject prompt text. |
| Project instructions | `AGENTS.md`, `CLAUDE.md`, and `.cursorrules` are scanned for prompt-injection markers before being injected. Suspicious files are skipped and noted. |
| Edits | File writes go through workspace path resolution, sensitive-file checks, approval, checkpoints, and rollback support. |

## Guardian note

`Guardian` exists as library/config plumbing for auto-approving clearly safe tool calls. It is **not** currently a top-level `--guardian` CLI flag. Even when enabled through configuration, it does not auto-approve shell/network egress tools.

## Boundary in one line

With `sandbox=require`, commands either run inside the OS sandbox — no network, writes confined to the workspace, secret dirs masked — or they do not run. With the default `auto` mode, Syntra tries that same sandbox first and warns loudly if the host cannot provide it.
