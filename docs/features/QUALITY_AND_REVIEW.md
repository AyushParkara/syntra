# Quality and Review — How Syntra Checks Its Own Work

This guide explains, in plain English, how Syntra tries to make sure the work it
hands you is actually correct — not just confident-sounding nonsense.

No coding knowledge needed. Where a term shows up, it gets defined right there.

A quick definition you'll need throughout:

- **Model** = an AI brain (like Claude, GPT, Gemini, etc.). Syntra can talk to many
  different ones.
- **Token** = a chunk of text, roughly a word-piece. Models charge by the token, so
  "tokens" is basically "how much text was sent and received," and it maps directly
  to cost.
- **LoopConfig** = the settings sheet for a single run. Think of it like the dials on
  a microwave: you set them once, press start, and they shape what happens. Below,
  every feature lists which dial turns it on and, where one exists, the matching
  command-line flag.

---

## 1. The big idea: plan, do, review (three different brains)

### What it does

When you give Syntra a real task ("build me a login form," "fix this bug"), it does
**not** just throw the job at one AI and hope. It splits the work across three roles,
and it can pick a **different model for each role**:

1. **Planner** — breaks your goal into a short, numbered list of steps. Like a
   project manager writing the to-do list. It is told to *only plan* — never to start
   writing the actual work.
2. **Executor** — does the steps, one at a time. Like the worker who actually builds
   each piece. It is told to *only do the current step* and build on what came before.
3. **Reviewer** — checks the finished work and gives a verdict: pass or fail. Like a
   quality inspector at the end of the line.

### Why three different brains?

Different models are genuinely good at different things — the same way you might ask
one friend to plan a trip, another to drive, and a third to double-check the
hotel booking. One model might be a brilliant planner but sloppy at detail; another
might write great code but be a soft, agreeable reviewer who waves everything
through. By **routing** (automatically choosing) the best-suited model for each role,
you get the strengths of each and avoid leaning on a single brain's blind spots.

A bonus: the reviewer is a *different* model than the one that did the work. People
(and AIs) are bad at catching their own mistakes. A fresh set of eyes catches more.

### Behind the scenes

Before any of this, a quick **analyzer** step reads your message and classifies it
(how hard is it? is it code? is it just chit-chat?). That classification decides
which models get picked and how careful to be. You don't configure this — it just
happens, and it's cached so it's usually free on repeat runs.

### How to use / configure

This three-role flow is the **default** for any real task. You don't turn it on.
A few dials let you steer it:

- `quality_bias` (0.0–1.0, default `0.8`) — higher means "spend more for better
  models," lower means "save money." CLI: `--quality-bias 0.8`.
- `pin_planner`, `pin_executor`, `pin_reviewer` — force a specific model into a role
  instead of letting Syntra choose. CLI: `--planner`, `--executor`, `--reviewer`.

### Tiny example

```python
from syntra.core.loop import LoopConfig

# Let Syntra pick models, but lean toward quality, and force a specific reviewer.
config = LoopConfig(
    quality_bias=0.85,
    pin_reviewer="claude-opus-4-8",
)
```

Command-line equivalent:

```
syntra run "build a password-reset endpoint" --quality-bias 0.85 --reviewer claude-opus-4-8
```

---

## 2. The 3-lens reviewer (one inspector wearing three hats)

### What it does

The reviewer doesn't just glance at the work and say "looks fine." It is instructed
to evaluate the same work through **three independent lenses**, in order, then
combine them into one verdict:

- **Senior Developer (〈dev〉)** — *Is the work correct and well-built?* Checks for
  security holes, logic that only *looks* right, made-up functions that don't really
  exist, missing error handling, and slow or messy approaches.
- **QA Engineer (〈qa〉)** — *Is it actually verified and complete?* Checks that the
  result matches what each step asked for, that edge cases and failure paths are
  handled, that nothing got cut off half-done, and that claimed changes truly
  happened.
- **Product Manager (〈pm〉)** — *Does it meet your actual goal, and only your goal?*
  Checks that your real request is fully met (not a near-miss) and that nobody
  sneaked in extra, unasked-for changes (called **scope creep**, and treated as a
  defect).

Think of it as one inspector putting on three different hats and walking the work
three times, each time asking a different question.

### The honesty rule (important)

A pass requires **all three lenses to be happy**. If any lens finds a problem, the
verdict is forced to **fail** — even if the reviewer tried to say "pass anyway."
Syntra enforces this in code, not just by asking nicely:

- If the reviewer says "pass" but **lists any issues**, Syntra overrides it to fail.
  (Saying "it's fine, but here are the problems" is a contradiction.)
- If the reviewer says "pass" but its **confidence is below 0.70** (on a 0–1 scale),
  Syntra also overrides it to fail.

So a "pass" from Syntra means a genuinely clean bill of health.

### How to use / configure

This is built into every review — there's no on/off switch. The three lenses come
from an editable prompt file at `syntra/core/prompts/reviewer.md`, so an advanced
user can tune the inspector's checklist, but you don't need to touch it.

### Tiny example

If you ask for a function and the reviewer's QA lens notices it never handles an
empty input, you'll see something like this in the issues list:

```
[QA] Function crashes on empty input — add a guard that returns [] when the list is empty.
```

Every issue is required to name the problem **and** the concrete fix, so it's
actionable, not a vague gripe.

---

## 3. The verification gate (a fast bouncer at the door)

### What it does

Before the work even reaches the reviewer, every model output passes through a
**verification gate** — a set of quick, mechanical checks. No AI involved here; these
are simple, predictable rules (so they're fast, free, and never flaky). Think of a
bouncer who checks IDs at the door before anyone gets to the party.

The checks:

| Check | Plain-English meaning | Severity |
|---|---|---|
| **Empty** | The model returned nothing. | Hard fail |
| **Cut off (truncation)** | The answer got chopped off mid-sentence (ran out of room). A half-answer can't be trusted as complete. | Hard fail |
| **Must-be-valid-JSON** | The planner and reviewer must return their answer in a strict structured format (**JSON** — a tidy machine-readable format). A planner or reviewer that can't produce a clean verdict can't be trusted. | Hard fail (planner/reviewer only) |
| **Over-confident without evidence** | The text says things like "guaranteed," "always," "100%," or "definitely" but gives no reason or proof nearby. | Warning (or hard fail in proof-only mode — see §4) |
| **Refusal** | The model declined to help ("I can't assist with that") *and* produced no real work. | Warning |
| **Repetitive / degenerate** | The output collapsed into repeating itself or rambling gibberish — a known failure mode. | Warning |
| **Role drift** | The executor started re-planning, or the planner started writing code — each doing the other's job. | Warning |

### Warnings vs. hard fails (the key distinction)

- **Hard fail (ERROR):** the output is rejected. The step does not get accepted.
  Syntra records why and retries.
- **Warning:** logged, but the work is **not** blocked. Warnings are gentler signals.
  Many would cause false alarms if treated as hard stops (e.g., a long, genuine
  answer that happens to contain the word "violates"). Instead, some warnings quietly
  nudge Syntra's routing — a model that keeps refusing or producing gibberish gets
  "cooled down" so the next pick avoids it.

These checks are deliberately **conservative** — tuned to avoid crying wolf. An honest
"I don't know" is treated as a *pass*, never a refusal, because admitting uncertainty
is a good thing, not a failure.

### How to use / configure

The gate is **always on** for every step — no switch needed. It's the safety net under
everything else. The one thing you *can* tighten is the over-confidence and
made-up-fact behavior, via **proof-only mode** (next section).

### Tiny example

If a model's answer gets cut off because it hit its length limit, the gate catches it
and you'll see the step fail with a reason like:

```
verification gate failed: output truncated (finish_reason=length)
```

Syntra then automatically tries to recover (ask the model to be concise, or continue
where it stopped, or escalate to a roomier model) before giving up.

---

## 4. Proof-artifacts (show me it actually happened)

### What it does

There's a difference between a model **saying** "all tests pass" and the tests
**actually passing**. The verification gate (§3) only judges the *words* the model
wrote. Proof-artifacts judge the **reality**.

As Syntra works, it records hard evidence of what *provably* happened:

- **Command results** — when a real command runs, its **exit code** (a number where
  `0` means success and anything else means failure) is recorded.
- **Applied edits** — which files were actually changed, and whether each change
  succeeded or failed.
- **Step outcomes** — which steps genuinely completed vs. failed.

This evidence is then **shown to the reviewer** alongside the work, with a note:
"ground your verdict in this, not just the summary." So the inspector judges against
what really occurred, not just the worker's say-so.

### The integrity check (catching the bluff)

Here's the powerful part. If the work **claims** success ("tests pass," "it works,"
"no errors") but the recorded evidence shows a **failure**, Syntra flags it as a
provable contradiction (internally called a *tool-bypass* — the AI claimed a result
without the work backing it up). The reviewer is told in bold to **treat this as a
fail**, and the model that bluffed gets cooled down in routing.

It's the equivalent of a contractor saying "all inspected and passed!" while the
inspection report on the table clearly says FAILED. Syntra notices.

### Grounding the verdict in a real check (`verify_command`)

You can hand Syntra a **real command to run after the work is done**, and let its
result decide the verdict. The classic example is a test suite:

- `verify_command` — e.g. `"pytest -q"` (a command that runs Python tests quietly).
  CLI: `--verify-command "pytest -q"`.
- `verify_timeout` — how many seconds to allow before giving up (default `120`).

If that command **fails**, the verdict is forced to **fail** no matter how confident
the reviewer was — and Syntra automatically adds a "repair" step so you can re-run and
fix it. Plausible-looking code is not the same as *passing* code, and this is how
Syntra holds the line. (For safety, the command runs in a confined sandbox; anything
deemed dangerous is blocked and never executed.)

### How to use / configure

- For the proof evidence and the integrity check: **automatic**, no setup.
- To ground the verdict in a real test run: set `verify_command`.

### Tiny example

```python
config = LoopConfig(
    verify_command="pytest -q",   # run the tests after the work
    verify_timeout=180,           # allow up to 3 minutes
)
```

Command-line equivalent:

```
syntra run "fix the failing checkout test" --verify-command "pytest -q"
```

If the model says "tests now pass" but `pytest` exits non-zero, you get a fail plus a
repair step — not a false victory.

### Proof-only mode (extra strict)

Turning on **proof-only mode** raises the bar on truthfulness:

- `proof_only=True` — CLI: `--proof-only`.

In this mode, two things that are normally *warnings* become *hard fails*:

- Stating a flat fact about the world/code **with no backing** ("the function returns
  a list") — you must either back it up or label it as an assumption.
- Over-confident phrasing with no nearby evidence.

Honest uncertainty ("I'm not sure, need to check") is still always allowed. Use this
when correctness matters more than speed — security work, anything touching money or
production.

```python
config = LoopConfig(proof_only=True)
```

---

## 5. Reflexion (learn from the mistake, then retry)

### What it does

When a step fails the gate, Syntra doesn't just blindly try the exact same thing
again. It first writes a short **post-mortem** — a 2-3 sentence note answering:

1. What was the **root cause** of the failure?
2. What's a **concrete, different approach** to try next time?

That note is then **fed into the retry's instructions**, so the next attempt actually
learns instead of repeating the same wall. It's like a worker pausing to think
"okay, that didn't work because X — let me try Y instead," rather than banging their
head on the same door.

The post-mortem is cheap (capped short) and "blameless" — it's about fixing the
approach, not assigning fault.

### How to use / configure

- `reflexion` (default `True`) — **on by default**. Set it to `False` to disable the
  learn-and-retry note (the retry will then get only the bare "this failed" fact).

There is no dedicated command-line flag; it's controlled through `LoopConfig` (it's
simply on for normal runs).

### Tiny example

```python
config = LoopConfig(reflexion=True)   # the default
```

If a step fails, the next attempt's prompt will include something like:

```
✗ (this step, attempt 1) verification gate failed: output truncated
   ↳ LEARN: The answer was too long and got cut off. Output only the function body,
     no explanation, to stay within the limit.
```

---

## 6. Review panel — "PoLL" (a jury instead of one judge)

### What it does

Normally one reviewer gives the verdict. With a **review panel**, Syntra asks
**several different reviewers** — each from a **different model family** (e.g. one
Claude, one GPT, one Gemini) — and takes a **majority vote**. This is sometimes
called **PoLL** ("Panel of LLMs").

Why a panel? A single model can have a **self-preference bias** — it tends to approve
work that looks like its own style, or it may just be a soft grader. A jury of
*different* model families cancels out any one model's bias. Using several diverse
models is also often cheaper and more reliable than paying for one top-tier judge.

### How the vote works

- It's a straight **majority vote** on pass/fail.
- **Ties go to fail** — the safety-conservative choice.
- The confidence is averaged across the panel, and all the issues each reviewer raised
  are combined (with duplicates removed).
- If Syntra can't find at least two distinct model families to staff the panel, it
  quietly falls back to a single reviewer.

### How to use / configure

- `review_panel` (default `1` = off) — set it to the number of reviewers you want,
  e.g. `3`. Setting `2` or more turns the panel on.

There is no dedicated `syntra run --review-panel` flag yet; this is set directly through
`LoopConfig`. Some interactive/TUI automation paths may wire a chosen council size into
both planning and review, but for scripts the explicit knob is `review_panel`.

### Tiny example

```python
config = LoopConfig(review_panel=3)   # three diverse reviewers, majority vote
```

You'd reach for this on high-stakes work where you want extra confidence in the
verdict and want to avoid trusting any single model's opinion.

---

## 7. Conversational fast-path (don't bring a committee to a "hi")

### What it does

Not everything deserves the full plan→do→review machinery. If you just say "hi," ask
"who are you?", request a quick opinion, or ask a one-line trivia question, spinning
up three models and a review panel would be slow, wasteful, and a bit absurd.

So the analyzer (§1) detects **conversational / one-shot** messages and answers them
in **a single quick call** — no planning, no separate executor, no reviewer. It's the
difference between answering a quick "what time is it?" yourself versus convening a
project team for it.

This single answer still gets a **light verification check** (empty? cut off?
refusal?) and a completion check, so even the fast path isn't unguarded — it just
skips the heavy ceremony.

### The safety guard

There's one firm rule: a task that needs **code or tool actions** is **never** sent
down the fast-path, even if it looks casual. Those always get the full pipeline so
they're not skipped past the reviewer. Chit-chat is cheap to get wrong; code is not.

### How to use / configure

- `direct_chat` (default `True`) — **on by default**. The fast-path decides
  automatically per message; you don't tag anything.
- To **disable** it and force the full plan→do→review on *everything* (including
  "hello"): set `direct_chat=False`, or CLI `--no-direct`.
- `direct_quality_bias` (default `0.6` in the library; the CLI passes `0.4`) — a
  cost/quality dial just for chat answers. Lower means a cheaper model for casual
  replies. CLI: `--direct-bias 0.4`.

### Tiny example

Casual question — answered in one call automatically:

```
syntra run "what's the capital of France?"
```

Force the full pipeline even for chit-chat:

```
syntra run "what's the capital of France?" --no-direct
```

In code:

```python
config = LoopConfig(direct_chat=True)        # smart fast-path (default)
config = LoopConfig(direct_chat=False)       # always full pipeline
```

---

## 8. Bonus: plan council (optional, get a second opinion on the *plan*)

Closely related to the review panel, but for the **planning** stage. Instead of one
planner, `plan_council=N` asks **N different models** each to draft a plan, then a
"judge" model picks the single best one. More cost, better plan on hard or open-ended
tasks.

- `plan_council` (default `1` = off). CLI: `--council 3`.

```
syntra run "design a caching layer for our API" --council 3
```

---

## Quick how-to cheat sheet

| I want to… | Set this (LoopConfig) | Command-line flag |
|---|---|---|
| Use the plan→do→review flow | (it's the default) | — |
| Lean toward quality (spend more) | `quality_bias=0.9` | `--quality-bias 0.9` |
| Lean toward cheaper | `quality_bias=0.3` | `--quality-bias 0.3` |
| Force a specific reviewer model | `pin_reviewer="claude-opus-4-8"` | `--reviewer claude-opus-4-8` |
| Get the 3-lens review (dev/QA/PM) | (always on) | — |
| Get the verification gate | (always on) | — |
| Run real tests to decide the verdict | `verify_command="pytest -q"` | `--verify-command "pytest -q"` |
| Be extra strict on unproven claims | `proof_only=True` | `--proof-only` |
| Learn-and-retry after a failed step | `reflexion=True` (default) | — |
| Disable learn-and-retry notes | `reflexion=False` | — |
| Use a jury of reviewers (less bias) | `review_panel=3` | no dedicated `syntra run` flag yet |
| Get several plans and pick the best | `plan_council=3` | `--council 3` |
| Answer chit-chat in one quick call | `direct_chat=True` (default) | — |
| Force full pipeline even for chit-chat | `direct_chat=False` | `--no-direct` |
| Cheaper model for casual chat | `direct_quality_bias=0.3` | `--direct-bias 0.3` |

### The 30-second mental model

> A **planner** writes the to-do list, an **executor** does each item, and a fresh
> **reviewer** (wearing three hats: dev, QA, product) signs off. A fast **bouncer**
> rejects empty, cut-off, or broken output before review. The reviewer is shown
> **proof** of what really happened, so a bluffed "tests pass" gets caught — and if
> you give it a real test command, a true failure overrides any optimistic verdict.
> Failed steps get a **learn-from-it note** before retrying. For high stakes, a
> **jury** of diverse models votes on the verdict. And a quick "hello" just gets a
> quick answer — no committee required.
