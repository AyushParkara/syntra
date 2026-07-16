# Syntra feature guides

Plain-English guides — what each feature does, how to use it, with examples and a quick how-to cheat sheet at the end of each.

| Guide | Covers |
|---|---|
| **[MODEL_SELECTION.md](MODEL_SELECTION.md)** | How Syntra ranks reachable models for each role (planner/executor/reviewer); the per-message demand vector; pinning, blacklisting, penalizing; seeing the picks. |
| **[RELIABILITY.md](RELIABILITY.md)** | Auto-switching when a model fails; preflight (`doctor --probe-models`); route-health memory; out-of-credits/bad-key help + `remove-key`; catching empty/refusal/garbage/fake-success answers. |
| **[QUALITY_AND_REVIEW.md](QUALITY_AND_REVIEW.md)** | Plan→execute→review; the 3-lens reviewer; the verification gate; proof-of-work; reflexion retries; the multi-model review panel (PoLL); the chit-chat shortcut. |
| **[PROVIDERS_CACHING_MCP.md](PROVIDERS_CACHING_MCP.md)** | Setting up providers + keys; prompt caching (cheaper repeated calls); connecting external tool servers (stdio + hosted HTTP). |
| **[TOOLS_AND_SAFETY.md](TOOLS_AND_SAFETY.md)** | What the agent can do; the OS sandbox; edits + checkpoints/rollback; per-edit error fixing; AGENTS.md project rules. |

New here? Start with the top-level **[../GUIDE.md](../GUIDE.md)**.
