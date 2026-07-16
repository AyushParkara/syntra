# Syntra commands — what each one does and how to use it

Plain-English reference for the common `syntra` commands. Run any of them from your terminal.
The CLI also has advanced subcommands; `syntra --help` and `syntra <command> --help`
are the parser-generated source of truth.

> Tip: just typing `syntra` (no command) opens the full-screen cockpit (TUI). Everything below also works as a one-off command.

---

## Everyday commands

### `syntra run "<your task>"`
Do one task. Syntra plans it, does the work, and reviews it — each step handled by the best AI model for that step.
```
syntra run "write a Python function to read a CSV and print column averages, with tests"
```
Useful flags (common list further down):
- `--workspace <dir>` — which folder to work in (default: current folder).
- `--execute` — actually let it edit files (off by default = it only *proposes* changes).
- `--agent` — let it use tools (run commands, read/edit files) to do the work itself.
- `--verify-command "pytest -q"` — run a real check after the work and judge the result on it.
- `--verbose` — show the routing/usage details as it goes.

### `syntra` (or `syntra --tui`)
Open the cockpit — a full-screen view where you type tasks, watch the plan→do→review unfold, and see which model is doing what. Type `--plain` instead if you want the simple line-by-line version.

### `syntra resume <task-id>`
Pick up a task that was paused or partly finished. Syntra rebuilds where it was from saved files (not from chat history) and continues from the first unfinished step.

### `syntra update`
Check whether a newer Syntra is available and offer to upgrade. If you say no, it asks how many days to wait before reminding you. (`--check` just checks; `--yes` upgrades without asking.)

---

## Setup & health

### `syntra init`
A friendly wizard that creates your providers file (`~/.config/syntra/providers.json`) — where your AI provider URLs and API keys live. Keys are never shown on screen and the file is saved private (chmod 600).

### `syntra providers`
List the providers you've set up. Add `--free` to see free/low-cost provider presets you could add.

### `syntra providers remove-key <provider> <last6>`
Remove a single API key (matched by its last 6 characters) from a provider — e.g. a key that's out of credits. **Dry-run by default** (shows what it *would* remove); add `--yes` to actually do it. It backs up your config first and never prints the full key.
```
syntra providers remove-key openrouter dd0408         # preview
syntra providers remove-key openrouter dd0408 --yes   # apply
```

### `syntra login <provider>` / `syntra logout <provider>`
Log in to a provider through your browser (for providers that use sign-in instead of a pasted key), or log out.

### `syntra doctor`
A health check: validates your config, catalog, and routing, and prints fixes for anything wrong.
- `syntra doctor --probe` — also test that each provider URL is reachable.
- `syntra doctor --probe-models` — send a tiny real message to each model Syntra would pick, and report which actually work. Great for catching dead models *before* a real run.

### `syntra verify`
A quick sanity check (no API calls): shows which model gets picked for planner/executor/reviewer and why.

---

## Models & routing

### `syntra route <planner|executor|reviewer>`
Show which model Syntra would pick for that role right now, and the runners-up — with scores and the reasons each candidate was kept or skipped.
```
syntra route executor
```

### `syntra catalog` / `syntra catalog refresh`
`catalog` lists the AI models Syntra knows about (with their routing signals).
`catalog refresh` refreshes supported values through Artificial Analysis when you
have configured `ARTIFICIALANALYSIS_API_KEY`. Artificial Analysis publishes a free,
rate-limited Data API for primary metrics; consult its current API documentation for
the access terms and limit.

### `syntra route-blacklist <model> [--provider P] [--reason R]`
Never use this model (optionally only via a specific provider).

### `syntra route-penalty <model> <multiplier> [--provider P]`
Make a model less (or more) preferred. `multiplier` is a number: `0.5` halves its score, `0` removes it entirely.

### `syntra route-health` / `syntra route-health-clear`
`route-health` shows Syntra's memory of which model+provider combos have been failing (and how "cooled" they are). `route-health-clear` wipes that memory (optionally `--provider`/`--model` to clear just one).

### `syntra stats`
Show the learned per-route success/cost numbers Syntra has accumulated from real runs.

### `syntra bench`
A cost-per-success benchmark: compares Syntra's smart routed stack against one pinned model on real calls.

---

## Tasks & history

### `syntra tasks` / `syntra task <id>`
`tasks` lists all your saved tasks; `task <id>` shows the full saved state of one (goal, plan, decisions, failures, costs).

### `syntra rollout <id> [--branch-at N]`
Replay a task's timeline of events, or branch a brand-new task from a chosen point in an old one.

---

## The `run` flags (common list)

| Flag | What it does |
|---|---|
| `--workspace <dir>` | Folder to work in. |
| `--execute` | Apply file edits (default: propose only). |
| `--agent` | Let it use tools (bash, files, etc.). |
| `--auto-approve` | Don't pause for edit approval (use with care). |
| `--autopilot` | Keep retrying failed steps until done or stuck. |
| `--verify-command "<cmd>"` | Real check to ground the review (e.g. `pytest -q`). |
| `--proof-only` | Stricter gate: unbacked factual claims become hard fails. |
| `--council N` | Get plans from N models and judge-pick the best. |
| `--quality-bias 0..1` | Higher = favor quality over cost (default 0.8). |
| `--planner/--executor/--reviewer <model>` | Pin a model to a role. |
| `--reasoning low\|medium\|high\|xhigh` | How hard models should think. |
| `--max-steps N`, `--max-tokens N` | Safety limits for the run. |
| `--lsp "<cmd>"` | Start a language server so the agent sees compiler errors. |
| `--mcp "<cmd-or-url>"` | Add an external tool server (repeatable). |
| `--image <path>` | Attach an image (for vision models). |
| `--mode <budget\|im-a-millionaire\|pennies>` | Pick the cost/quality routing mode for this run. |
| `--model <id>` | Pin one model for all roles (per-role pins still win). |
| `--approval-policy <mode>` | Control when shell/tool actions ask. |
| `--sandbox <mode>` | Control sandbox posture (`read-only`, `workspace-write`, `danger-full-access`). |
| `--deep-research` | Use the research-oriented flow/tools. |
| `--steer` | Accept live mid-run instructions from stdin (plain line = queued follow-up, `!`-prefixed = instant steer). |
| `--no-direct` | Always use full plan→do→review (skip the chit-chat shortcut). |
| `--stream` | Stream output live. |
| `--verbose` | Show routing/usage details. |
| `--dry-run` | Estimate cost without running. |

See **[CONFIG.md](CONFIG.md)** for the matching settings when calling Syntra as a library, and **[features/](features/)** for how each system works.

## Advanced subcommands present in the CLI

These are implemented but only briefly documented here. Use `syntra <command> --help` for exact flags.

| Command | Purpose |
|---|---|
| `syntra apply` | Apply/check a patch envelope. |
| `syntra archive` / `syntra unarchive` | Hide or restore a saved task from default listings. |
| `syntra campaign` | Run a batch/campaign job definition. |
| `syntra compare` | Ask several models and compare/pick an answer. |
| `syntra completion` | Emit shell-completion setup. |
| `syntra delete` | Delete a saved task (confirmation unless `--yes`). |
| `syntra exec` / `syntra x` | Agentic execution-oriented commands with automation flags. |
| `syntra install` | Install a plugin/skill source. |
| `syntra local` | Manage/check local model helpers. |
| `syntra mcp-server` | Expose Syntra itself as an MCP stdio server. |
| `syntra models` | Inspect or set role model pins. |
| `syntra plugins` / `syntra skills` | List/manage plugins and skills. |
| `syntra review` | Review paths or staged changes through Syntra. |
| `syntra swarm` | Run multiple workers on a goal. |
