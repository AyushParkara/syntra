# Syntra — the complete guide (in plain English)

## What Syntra is

Syntra is a command-line assistant that does a task by **using several AI models together** instead of just one. Think of it as a **small team with a manager**:

- a **planner** breaks your task into steps,
- an **executor** does each step,
- a **reviewer** checks the work (as a Senior Dev + QA + Product Manager).

For *each* of those jobs, Syntra ranks the reachable models using its current
catalog, task profile, route health, and your overrides, then selects a candidate
for that role. Different models can suit different tasks. It keeps important
information in **typed files on disk** (the goal, the plan, decisions, failures)
instead of relying on a long chat history, so the durable task record survives
across resumes.

Three ideas make Syntra different from a normal one-model assistant:
1. **A separate route per job.** It scores reachable models against what the task needs and lets you inspect, override, or pin each role's choice.
2. **It checks its own work.** A verification gate + a 3-lens reviewer catch sloppy or fake answers — including "I ran the tests and they pass" when the command actually failed.
3. **Safety boundaries you can inspect.** If a model fails, Syntra can try another route and shows the switch. When an OS sandbox is active, shell commands are confined to the workspace with network disabled; otherwise Syntra warns, and `sandbox=require` fails closed.

---

## Quickstart

```bash
# 1. Set up your AI providers (URLs + API keys) — a guided wizard:
syntra init

# 2. Check everything's healthy:
syntra doctor --probe-models      # tests your models with a tiny real call

# 3. Do a task:
syntra run "write a Python function to parse a CSV and print column averages, with tests"

# 4. Or open the full-screen cockpit:
syntra
```

- Want it to actually change files? add `--execute`.
- Want it to run commands/use tools itself? add `--agent`.
- Want it to prove the work with a real test? add `--verify-command "pytest -q"`.

---

## How a run works, step by step

1. **You type a task.** Syntra reads it and figures out what it needs (coding? deep reasoning? long document? just a quick chat?).
2. **It ranks models.** For each role it scores the reachable candidates and selects the highest-ranked one. Chit-chat can take a single-call fast path.
3. **Plan → do → review.** The planner makes steps; the executor does each one; the reviewer checks them. The work is graded against real evidence (command exit codes, applied edits) — not just the model's say-so.
4. **If something fails**, Syntra writes a short "why + what to try differently" note and retries (possibly on a different model). If a *model* fails, it switches to another provider or model automatically and shows you the switch.
5. **State is saved continuously**, so you can `resume` later or `rollout` to replay/branch it.

---

## The features, and where to read about each

Each link is a plain-English guide with examples and a quick how-to cheat sheet.

| Area | What it covers | Doc |
|---|---|---|
| **Model selection** | How Syntra ranks models per role; pinning/blacklisting/penalizing models; seeing what gets picked. | [features/MODEL_SELECTION.md](features/MODEL_SELECTION.md) |
| **Reliability** | Auto-switching when a model fails; preflight health checks; route-health memory; out-of-credits/bad-key help; catching fake/empty/refusal answers. | [features/RELIABILITY.md](features/RELIABILITY.md) |
| **Quality & review** | The planner→executor→reviewer flow; the 3-lens reviewer; the verification gate; proof-of-work; reflexion retries; multi-model review panel; the chat shortcut. | [features/QUALITY_AND_REVIEW.md](features/QUALITY_AND_REVIEW.md) |
| **Providers, caching, MCP** | Setting up providers + keys; prompt caching (cheaper repeated calls); connecting external tool servers (local + hosted). | [features/PROVIDERS_CACHING_MCP.md](features/PROVIDERS_CACHING_MCP.md) |
| **Tools & safety** | What the agent can do; approval gates, optional OS sandboxing, edits + checkpoints/rollback; per-edit error fixing; AGENTS.md project rules. | [features/TOOLS_AND_SAFETY.md](features/TOOLS_AND_SAFETY.md) |

And two references:

- **[COMMANDS.md](COMMANDS.md)** — common `syntra` commands and what they do; use `syntra <command> --help` for advanced flags.
- **[CONFIG.md](CONFIG.md)** — common settings, files, and environment variables.

---

## Safety in one paragraph

When Syntra runs a shell command for you, two things protect you: a **classifier** refuses obviously dangerous commands (like `rm -rf /`, hidden `$(sudo ...)`, or piping the internet into an interpreter) and makes risky actions ask for approval; and, when available, **bubblewrap** (an OS sandbox) physically confines the command so it can only write inside your project folder, can't reach the network, and can't touch the rest of your computer. In default `auto` mode Syntra warns if the OS sandbox cannot be used; use `sandbox=require` when you want fail-closed behavior. File edits go through an approval gate, are checkpointed (so they can be rolled back), and can't escape your project. You stay in control.

---

## Getting help

- `syntra doctor` — diagnoses config/routing problems and suggests fixes.
- `syntra verify` — shows which model would be picked for each role (no API calls).
- `syntra route <role>` — shows the pick + runners-up + reasons.
- Anything failing live? `syntra doctor --probe-models` tells you which of your models actually work right now.
- Want YouTube transcript/watch support? Set `SYNTRA_YOUTUBE_INNERTUBE_KEY` or
  `~/.config/syntra/youtube.json` with `{ "innertube_key": "..." }` first.
