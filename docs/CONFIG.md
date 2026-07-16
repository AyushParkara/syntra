# Syntra settings — common knobs, in plain English

There are three places you can configure Syntra:

1. **`LoopConfig`** — settings for a single run (when you use Syntra as a Python library, or via `syntra run` flags). This page lists the common/user-facing knobs; the dataclass in `syntra/core/loop.py` is the full source of truth.
2. **Files** — your providers, model catalog, and overrides.
3. **Environment variables** — a few global switches.

---

## 1. Run settings (`LoopConfig`)

When you call Syntra from Python: `Loop(...).run(goal, workspace_root=..., config=LoopConfig(...))`. Most have a matching `syntra run` flag.

### How good vs how cheap
| Setting | Default | Plain meaning |
|---|---|---|
| `quality_bias` | `0.8` | 0–1. Higher = pick stronger (pricier) models; lower = cheaper. |
| `direct_quality_bias` | `0.6` | Same idea, but for quick chit-chat answers (CLI uses `0.4`). |
| `cost_mode` | `"budget"` | Bundle of routing knobs: `budget`, `im-a-millionaire`, or `pennies` (`andha-paisa` is kept as a compatibility alias). |
| `local_only` | `False` | Privacy gate: only route to local/LAN providers when true. |
| `reasoning` | `""` | How hard models think: `low`/`medium`/`high`/`xhigh` (empty = auto). |

### Which model does which role
| Setting | Default | Plain meaning |
|---|---|---|
| `pin_planner` / `pin_executor` / `pin_reviewer` | `""` | Force a specific model for that role (empty = auto-pick the best). |
| `pin_analyzer` | `""` | Force the cheap analyzer/classifier model. |
| `require_providers` | `()` | Only use these providers. |
| `executor_cost_aware` / `executor_cost_floor` | `True` / `0.88` | Pick the cheapest executor that is still strong enough. |
| `planner_compensates` | `True` | Make planner instructions more explicit when the executor is weaker/local/cheap. |

### Doing real work
| Setting | Default | Plain meaning |
|---|---|---|
| `execute` | `False` | Let it actually edit files (off = it only *proposes* edits). |
| `agent` | `False` | Let it use tools (run commands, read/edit files) to do the work itself. |
| `auto_tools` | `False` | Let simple tool-needing tasks use a one-executor tool path instead of full pipeline. |
| `auto_approve` | `False` | Skip the "approve this edit?" pause (use carefully). |
| `agent_max_turns` | `30` | Max tool-using turns in agent mode. |
| `approval_policy` | `""` | Shell approval policy when active: `untrusted`, `on_request`, `on_failure`, `never`. |
| `sandbox_mode` | `""` | Shell sandbox posture: `read_only`, `workspace_write`, `danger_full_access`. |
| `access_mode` / `access_overrides` | `""` / `{}` | Per-tool access posture (`ask`, `auto`, `off`) for TUI/agent runs. |

### Checking the work
| Setting | Default | Plain meaning |
|---|---|---|
| `verify_command` | `""` | A real command to run after the work (e.g. `pytest -q`) — its pass/fail grounds the review. |
| `verify_timeout` | `120.0` | Seconds before that check is cut off. |
| `proof_only` | `False` | Stricter: a factual claim with no evidence becomes a hard fail. |
| `reflexion` | `True` | On a failed step, write a "why it failed + what to change" note and feed it to the retry. |
| `review_panel` | `1` | Set to 2–3 to have the review done by several **different** model families and majority-voted (less bias). |
| `plan_council` | `1` | Set to 2+ to get plans from several models and judge-pick the best. |
| `plan_approval` | `False` | Pause after planning so you can approve the plan first. |

### Chat vs full pipeline
| Setting | Default | Plain meaning |
|---|---|---|
| `direct_chat` | `True` | Answer chit-chat/opinions in ONE quick call (skip plan→do→review). Real work always uses the full pipeline. |
| `executor_only` | `True` | Allow simple low-risk non-tool tasks to skip planner/reviewer. |
| `executor_with_tools` | `True` | Allow simple low-risk tool tasks to use one tool-capable executor. |

### Editing help
| Setting | Default | Plain meaning |
|---|---|---|
| `lsp_client` | `None` | A language server — lets the agent see compiler/type errors. |
| `lsp_autofix` | `True` | After an edit, show those errors to the executor so it fixes them *before* review. |
| `lsp_autofix_rounds` | `1` | How many fix→recheck rounds per step. |

### Safety limits (so a run can't run away)
| Setting | Default | Plain meaning |
|---|---|---|
| `max_steps` | `20` | Max plan steps. |
| `max_tokens` | `500000` | Total token budget for the whole run. |
| `max_output_tokens` | `8192` | Max tokens per single reply. |
| `max_role_retries` | `2` | How many alternate models to try when one fails. |
| `max_repeated_failures` | `2` | Stop if it hits the same wall this many times. |

### Research, memory, and spend
| Setting | Default | Plain meaning |
|---|---|---|
| `research` / `research_angles` | `False` / `5` | Use the research-oriented flow and number of angles to explore. |
| `context_relay` | `True` | Keep long sessions compact by passing summaries/briefs instead of full chat everywhere. |
| `handoff_mode` / `handoff_distill` | `"truncate"` / `False` | How continuity handoff text is produced for resumed/long tasks. |
| `knowledge_index` / `knowledge_index_hits` | `False` / `3` | Optional cross-run recall index. |
| `spend_ledger` / `spend_budget_usd` | `False` / `0.0` | Optional spend tracking and budget-based rerouting. |

### Plumbing (advanced)
`mcp_clients`, `hooks`, `guardian`, `permission_ask`, `question_ask`, `web_search`, `role_temperatures`, `initial_images`, `stream`, `tick_interval_s`, `constraints`, `rules`, `prior_results_char_budget`, `reviewer_step_preview_chars`, `agent_review_rounds`, `final_review_max_cycles`, `clarify_ambiguous`, `agent_brain` — wiring for tools, hooks, approvals, context budgets, and UI streaming. Defaults are sensible; leave them unless you have a specific need.

---

## 2. Files

| File | What it holds |
|---|---|
| `~/.config/syntra/providers.json` | Your AI providers — URLs + API keys (one or many keys each). Created by `syntra init`. Saved private (chmod 600). |
| `~/.config/syntra/overrides.json` | Your pins / blacklists / penalties for models. Edited by `route-blacklist`, `route-penalty`, `/models pin`. |
| `syntra/data/aa_catalog.json` (in the package) | The model catalog — every model's stats + the scoring settings. Refreshed by `syntra catalog refresh`. |
| `~/.config/syntra/state/` or `SYNTRA_STATE_DIR` | Task state, route-health memory, update-check state, TUI history/layouts, etc. |
| `AGENTS.md` / `CLAUDE.md` (in your project) | Project rules (build/test commands, conventions) — auto-loaded into every role. |

See **[features/PROVIDERS_CACHING_MCP.md](features/PROVIDERS_CACHING_MCP.md)** for the providers file shape, and **[features/MODEL_SELECTION.md](features/MODEL_SELECTION.md)** for overrides.

---

## 3. Environment variables

| Variable | What it does |
|---|---|
| `SYNTRA_NO_PROMPT_CACHE=1` | Turn off prompt caching (on by default for supported routes). |
| `SYNTRA_MCP_TOKEN` | Bearer token for HTTP MCP servers (if not given inline). |
| `SYNTRA_YOUTUBE_INNERTUBE_KEY` | Enables YouTube transcript/watch support. Alternatively set `~/.config/syntra/youtube.json` with `{ "innertube_key": "..." }`. |
| `ARTIFICIALANALYSIS_API_KEY` | Needed for `syntra catalog refresh`. |
| `SYNTRA_PROVIDERS_FILE` | Use a different providers file. |
| `SYNTRA_CATALOG_PATH` | Use a different model catalog. |
| `SYNTRA_OVERRIDES_FILE` | Use a different overrides file. |
| `SYNTRA_STATE_DIR` | Where to store task state. |
| `SYNTRA_THEME` | TUI color theme. |
| `SYNTRA_REASONING_EFFORT` | Default thinking effort. |
| `HTTP_PROXY` / `HTTPS_PROXY` | Standard proxy settings (respected for API calls). |
