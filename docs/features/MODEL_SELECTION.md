# Model Selection

How Syntra decides *which* AI model does *which* job for every message you send.

This page is for users, not developers. No prior knowledge needed. Any technical
term is defined the first time it appears.

---

## The 30-second version

When you send a message, Syntra does **not** just throw it at one model. It runs a
tiny "front desk" that:

1. **Reads your message** and works out what it actually needs (deep thinking? code?
   speed? carefulness?).
2. **Looks at every model you can actually reach** (the ones you have a key for).
3. **Scores each model** for how well it fits *this specific message*.
4. **Ranks candidates for each of three jobs** — planner, executor, reviewer —
   then selects the highest-scoring reachable candidate.

The three jobs:

- **Planner** — breaks your request into a plan of steps. (Think: the strategist.)
- **Executor** — does the actual work of each step. (Think: the worker.)
- **Reviewer** — checks the work for mistakes. (Think: the quality inspector.)

Because the pick is based on the message, a coding request and a writing request can
end up with **different** models — automatically.

> The router is data-driven. It has no fixed rule that always sends a task category
> to one named model. It combines an editable catalog, role weights, route-health
> data, and the controls you set. The bundled catalog is an approximate seed
> snapshot, so inspect and override a route when it does not match your experience.

---

## The manager analogy

Picture a project manager who has a roster of contractors. Each contractor has a
*track record* of test scores: one is great at coding, one is a brilliant all-round
thinker, one is fast and cheap, one is meticulous about following instructions.

A new job lands on the manager's desk. The manager:

1. Reads the job and writes down what it **demands** ("this is 80% coding, needs
   careful instruction-following, speed doesn't matter much").
2. Goes down the roster, and for each contractor asks: *given what this job demands,
   how likely is this person to nail it?*
3. Hands the **planning**, **doing**, and **checking** roles to the highest-ranked
   reachable candidates for those roles.

That manager is Syntra's **router** (the code lives in `syntra/core/router.py`). The
"what the job demands" note is the **demand vector** (built in
`syntra/core/task_analyzer.py`). The contractors' track records are the **catalog**
(`syntra/core/catalog.py`). Your personal notes about contractors ("never use this
one," "this one's unreliable, dock their score") are **overrides**
(`syntra/core/overrides.py`).

---

## Step 1 — Reading your message: the demand vector

Syntra does not guess what you need from keywords. It asks an AI model to **read your
message and classify it**. That classification is turned into a **demand vector**.

**What a "vector" means here:** just a short list of numbers, one per skill, each from
0 (this message doesn't need that skill at all) to 1 (this message needs that skill a
lot). Think of it as a profile bar-chart for your message.

There are **7 skills** Syntra measures (called *capability axes* in the code,
`CAPABILITY_AXES` in `task_analyzer.py`):

| Axis | Plain meaning | Pushed **up** by |
|---|---|---|
| `reasoning` | Logic, math, multi-step thinking | reasoning/analysis/planning tasks; complex tasks |
| `code` | Writing, editing, debugging code | coding or debugging requests |
| `tool_use` | Using tools: files, shell, APIs | tasks that touch files / run commands / call APIs |
| `long_context` | Handling a lot of text/data at once | tasks that reference big files or documents |
| `instruction` | Following exact rules and format | code work and high-stakes work |
| `speed` | Wanting a fast/cheap answer over a perfect one | short, simple, or chatty messages |
| `criticality` | Cost of being wrong | production / money / security / safety work |

**The important change:** this profile is **computed per message**. A message like
"write a regex to match emails" comes out **code-heavy**, so models with stronger
code-related signals rank higher.
A message like "talk me through the trade-offs of microservices" comes out
**reasoning-heavy**, so a strong reasoner wins. Same engine, different pick.

**How the message becomes numbers (two layers):**

1. The analyzer AI can return an explicit `demands` block with its own 0–1 scores per
   axis (richest signal).
2. For any axis it didn't score, Syntra **derives** a sensible value from the rest of
   its classification (the category, complexity, criticality, and the `needs_*` flags).
   For example (from `_derive_demands` in `task_analyzer.py`):
   - `code` becomes **0.85** if the task needs coding (else 0.1).
   - `tool_use` becomes **0.8** if the task needs tools (else 0.15).
   - `speed` becomes **0.8** for simple/chatty tasks, **0.3** otherwise.
   - `criticality` maps low/medium/high to **0.2 / 0.55 / 0.9**.

### How to use / control it

- **You don't have to do anything** — it runs automatically on every `syntra run`.
- **To see it in action**, run with `--verbose`, which prints the per-role plan:
  ```bash
  syntra run "write a python function to parse a CSV" --verbose --dry-run
  ```
  (`--dry-run` shows the plan and cost estimate but makes **no** API calls.)
- **To turn the demand vector off entirely** and force the full pipeline to treat
  everything the same, there's no per-axis switch — but `--no-direct` forces the full
  planner→executor→reviewer pipeline even for chit-chat, and `--quality-bias` (below)
  shifts every role toward quality vs. cost.

### Tiny example

You send: *"Fix the failing test in auth.py and run pytest."*

The analyzer reads it as a **coding + tool-use** job. The demand vector comes out
roughly:

```
code:        0.85   tool_use:    0.8
instruction: 0.7    reasoning:   0.6
criticality: ~0.55  long_context:0.2   speed: 0.3
```

That profile steers the executor pick toward a model with strong coding and tool-use
benchmark scores — not just "the smartest model overall."

---

## Step 2 — Scoring each model: IRT in plain terms

Now Syntra has a **demand** profile for your message and a **capability** profile for
every model. It needs one number per model: *how good a fit is this?*

It uses an approach called **IRT** (Item Response Theory). Don't let the name scare
you — here's the whole idea in one breath:

> For each skill the message demands, compare the model's measured ability on that
> skill to an "average model." Add those comparisons up, weighted by how much the
> message cares about each skill. Squash the total into a 0–1 "probability this model
> handles the job" score.

A bit more concretely (this is what `_score` in `router.py` does when the catalog's
`scoring_method` is `"irt"`, which **is the default** in the shipped catalog):

- Each model has real **benchmark scores** per skill (e.g. a coding benchmark, a
  reasoning benchmark). These are the model's *capability*.
- The message's demand vector says **which skills matter and how much** for this job.
- For each demanded skill: take `(model's score on that skill − 0.5)`. Above 0.5 means
  "better than an average model," below means "worse."
- Add those up, weighted by demand, and run it through a smooth S-curve (a "logistic"
  function) to get a 0–1 score.

**Why the 0.5 "average model" anchor matters:** top models today score very high on
many benchmarks, so they all look the same (this is called *benchmark saturation*).
Centering on 0.5 and using a steepness setting **spreads the top models apart again**
so the best one can actually win instead of tying.

### Why the strongest all-rounder often wins broad tasks (and that's correct)

If your message demands **several skills at once** (a big, open-ended job), then a
model that is strong **across the board** will out-score a one-trick specialist —
because the specialist is weak on the other demanded skills. This is **the right
answer**, not a bug. A broad job genuinely needs broad ability.

### Why specialists win narrow tasks

If your message demands **mostly one skill**, Syntra deliberately **sharpens** the
demand vector toward that dominant skill before scoring (the `_sharpen_demands`
function in `router.py`). Skills far below the top demand get pushed toward 0. Now the
specialist's strength on that one skill dominates the score, and it can beat the
all-rounder.

So: **broad task → best generalist; narrow task → best specialist.** Same math, the
sharpening is what flips it.

### What else nudges the score (all from data, no model names)

After the core fit score, `_score` also applies:

- **Evidence discount** — a model with little or no benchmark data for the demanded
  skills cannot beat a fully-measured model on an unproven guess. (A model claiming to
  be brilliant but with empty test results gets heavily discounted.)
- **Tier penalty** — "fast" tier models (flash/mini/nano/haiku/instant) get a small
  ×0.85 by default; "standard" and "pro" get ×1.0. (Configurable in the catalog.)
- **Cost** — blended input/output price nudges the score; how much it matters depends
  on your `--quality-bias`.
- **Speed bonus (executor only)** — a fast model gets a bonus, but **scaled by how much
  this message actually values speed**. A latency-sensitive chat gives the full bonus;
  a heavy coding job gives almost none, so capability wins.

### How to use / control it

- **`--quality-bias`** (a number 0–1, default **0.8**) is your main dial. Higher =
  lean toward quality and ignore cost; lower = let cheaper models compete.
  ```bash
  syntra run "draft a launch email" --quality-bias 0.4   # let cheaper models win
  syntra run "design the auth system" --quality-bias 0.95 # quality above all
  ```
- The IRT math, the 0.5 anchor, the steepness, the sharpening strength, and the
  evidence-discount floor are all **tunable constants** in the catalog/code — not
  baked-in model choices — but most users never need to touch them.

### Tiny example

Message: *"What's the capital of France?"* → demand is dominated by `speed`, almost
nothing else. Sharpening zeroes out the rest. A fast, cheap model wins, because spending
a top-tier reasoning model on this is wasteful and the trivia score is identical.

Message: *"Prove this algorithm terminates and analyze its complexity."* → demand is
dominated by `reasoning`. A strong-reasoning model wins even if it's slow and pricey.

---

## Step 3 — The reachability rule (only models you can actually use)

**A model is only ever picked if you have a way to reach it.** Syntra will never route
to a model you can't call.

**How "reachable" is decided** (`route_resolver` in `router.py`, backed by
`registry.py`): Syntra looks at your **providers config** — a file listing the AI
services you've set up, each with an API key. A model is reachable if **some configured
provider can serve it**. A provider "serves" a model when either:

- the provider is a **wildcard gateway** (no `allowed_models` list — e.g. OpenRouter
  can serve almost anything), **or**
- the model's id is in that provider's `allowed_models` list.

If no configured provider serves a model, the router silently skips it (it won't even
clutter the "why I skipped" list). So the full model catalog gets filtered down to *just
the ones you've actually wired up* before scoring even matters.

### How to use / control it

Your providers live in **`~/.config/syntra/providers.json`** (override the location
with the `SYNTRA_PROVIDERS_FILE` environment variable, or `XDG_CONFIG_HOME`).

- The easy way to create it:
  ```bash
  syntra init        # interactive wizard; keys are stored chmod-600, never echoed
  ```
- Or copy the template `syntra/data/providers.example.json` and fill in keys.
- Each provider entry needs a `base_url` and a key (`api_key`, or `api_key_env` to read
  it from an environment variable). Add `allowed_models` to restrict which models that
  provider may serve; omit it for a wildcard gateway.
- A local model server (e.g. Ollama) can use `"api_key": "no-auth"`.

To check what's reachable right now:
```bash
syntra providers     # list configured providers
syntra verify        # shows what each role would pick, given your keys
```

### Tiny example

`providers.json` has only OpenRouter (wildcard) and a DeepSeek key with
`allowed_models: ["deepseek-chat", "deepseek-reasoner"]`. Then **every** OpenRouter
model is reachable, **plus** those two DeepSeek models — and nothing else from the
catalog can be picked, no matter how high it scores.

---

## Step 4 — The intelligence floor (keeping weak models out of important jobs)

Each role has a minimum "intelligence" bar. A model whose overall intelligence index is
below the floor for a role is **excluded from that role** before scoring (see
`intelligence_floors` use in `router.py`).

The shipped catalog (`_intelligence_floor`) sets:

| Role | Floor |
|---|---|
| planner | **44.0** |
| reviewer | **44.0** |
| executor | **35.0** |

Plain meaning: planning and reviewing are high-stakes thinking jobs, so weak models are
barred from them. The executor floor is lower because a capable-but-not-genius model can
still do solid hands-on work, and you may want a faster/cheaper one there.

### How to use / control it

The floors live in the catalog JSON under `_intelligence_floor`. Lower a floor to let
cheaper models into a role; raise it to be stricter. When a model is dropped for this
reason, it shows up in the deliberation list as `below intelligence floor` with the
numbers, e.g.:

```bash
syntra route planner
# ... skip  some/cheap-model   below intelligence floor (41.0 < 44.0)
```

---

## Overrides — your personal knowledge beats the benchmarks

Benchmarks don't know everything. Maybe a model that scores great keeps running out of
credits on your account, or hallucinates on your kind of work. **Overrides** let you
overrule the router with what *you* know.

Everything lives in one file: **`~/.config/syntra/overrides.json`** (override the path
with the `SYNTRA_OVERRIDES_FILE` environment variable). You can edit it by hand or use
CLI commands that write to it for you. There are four tools:

### 1. PIN a model to a role (force this exact model)

A **pin** is a hard assignment: "for this role, always use THIS model, skip the
scoring." The router returns the pinned model directly, as long as it still exists in
the catalog, resolves to a provider, and isn't blacklisted.

Inside an interactive Syntra session:
```text
/models pin executor anthropic/claude-opus-4.7
/models pin planner  google/gemini-3.1-pro-preview
/models unpin executor          # back to auto-routing
/models                          # list catalog + show current pins
```
(There's also a shorter `/pin <role> <model_id>` and `/unpin <role>` in the session.)

There is **one pin per role** — pinning a role replaces any previous pin for it.

### 2. BLACKLIST a model (never use it)

A **blacklist** removes a model from consideration entirely. Optionally scope it to one
provider; with no provider it blocks the model on **every** provider.

```bash
syntra route-blacklist google/gemini-3-flash-preview --reason "rate limit pain"
syntra route-blacklist deepseek-ai/deepseek-v4-pro --provider nvidia --reason "hallucinates"
```

### 3. PENALIZE a model (dock its score, don't ban it)

A **penalty** is a score multiplier. `1.0` = no change, `0.5` = halve its score (so it
only wins if it's clearly the best), `1.2` = a boost. A model can still be picked — it
just has to earn it.

```bash
syntra route-penalty anthropic/claude-opus-4.7 0.3 --provider openrouter --reason "credit exhaustion"
syntra route-penalty deepseek-ai/deepseek-v4-pro 0.4 --reason "unreliable on my tasks"
```

A provider-specific penalty beats a generic one. If you set conflicting generic
penalties, the most impactful (lowest for penalties) wins.

### 4. Edit a model's tags / allowed roles (advanced, file-only)

By editing `overrides.json` directly you can also add/remove **specialty tags** or
**remove a role** from a model. Example: stop a model from ever being a planner.

```json
{
  "blacklists": [
    {"model_id": "google/gemini-3-flash-preview", "reason": "rate limit pain"}
  ],
  "penalties": [
    {"provider": "openrouter", "model_id": "anthropic/claude-opus-4.7", "penalty": 0.3, "reason": "credit exhaustion"}
  ],
  "role_pins": [
    {"role": "executor", "model_id": "anthropic/claude-opus-4.7"}
  ],
  "role_overrides": [
    {"model_id": "deepseek-ai/deepseek-v4-pro", "remove_roles": ["planner"]}
  ],
  "extra_specialties": [
    {"model_id": "deepseek-ai/deepseek-v4-pro", "add": ["unreliable"], "remove": []}
  ]
}
```

> Order of authority: a **pin** wins outright (it bypasses scoring). A **blacklist**
> removes a model. A **penalty** just scales its score. The intelligence floor and
> reachability rule apply before scoring either way.

---

## Seeing what gets picked (no API calls, totally safe)

Two commands let you inspect the router's brain without spending a cent.

### `syntra route <role>` — what would win for one role

```bash
syntra route executor
syntra route planner --quality-bias 0.95
syntra route reviewer --needs-tool-use --needs-long-context
```

It prints the winning model, the provider it'd be served from, the score, the
human-readable reason, and a **deliberation** list of which models were skipped and why
(below floor, blacklisted, no provider, penalty=0, quota-cooled, etc.).

> Note: plain `syntra route` uses the role's default weights (no message to analyze), so
> it shows the **baseline** pick. The per-message demand vector kicks in during an
> actual `syntra run`. Use `syntra run ... --verbose --dry-run` to see the
> message-specific picks.

### `syntra verify` — one-shot health + all three picks

```bash
syntra verify
```

It checks your catalog, providers, and overrides loaded correctly, then prints the
planner / executor / reviewer pick side by side with scores and a summary of skips.
This is the fastest way to answer "is my setup sane, and who's doing what?"

---

## Quick how-to cheat sheet

```bash
# --- SEE what the router does (safe, no API calls) ---
syntra verify                         # health + planner/executor/reviewer picks
syntra route executor                 # baseline pick for one role + why others were skipped
syntra route planner --quality-bias 0.95
syntra run "<your task>" --verbose --dry-run   # per-MESSAGE picks + cost estimate

# --- STEER the picks for one run ---
syntra run "<task>" --quality-bias 0.4   # let cheaper models compete (0..1, default 0.8)
syntra run "<task>" --quality-bias 0.95  # quality over cost
syntra run "<task>" --no-direct          # force full planner->executor->reviewer

# --- PIN a model to a role (inside an interactive session) ---
/models pin executor anthropic/claude-opus-4.7
/models unpin executor
/models                                   # list catalog + current pins

# --- BLACKLIST a model (never use it) ---
syntra route-blacklist <model_id> --reason "why"
syntra route-blacklist <model_id> --provider <prov> --reason "why"

# --- PENALIZE a model (dock its score; 1.0=none, 0.5=half, 1.2=boost) ---
syntra route-penalty <model_id> 0.5 --reason "unreliable"
syntra route-penalty <model_id> 0.3 --provider openrouter --reason "credit exhaustion"

# --- SET UP which models are even reachable ---
syntra init                               # wizard to write providers.json (keys hidden)
syntra providers                          # list configured providers
```

**Key files**

- `~/.config/syntra/providers.json` — your AI services + keys (controls *reachability*).
  Override path with `SYNTRA_PROVIDERS_FILE`.
- `~/.config/syntra/overrides.json` — your pins, blacklists, penalties, role/tag edits.
  Override path with `SYNTRA_OVERRIDES_FILE`.

**Remember the mental model:** read the message → build a demand profile → keep only
reachable models above the floor → score each for *this* message → select the
highest-ranked candidate per role, with your overrides having the final say.
