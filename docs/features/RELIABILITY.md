# Syntra Reliability — Plain-English Guide

This guide explains, in everyday language, how Syntra keeps working even when an
AI model breaks, runs out of money, or quietly gives you a bad answer. No coding
knowledge needed. Terms are defined as we go.

## First, a few words you'll see a lot

- **Model** — the actual AI brain that writes the answer (for example
  `deepseek-v4-pro` or `gpt-5`). Think of it as a *specialist you hire*.
- **Provider** — the company that *serves* that model to you over the internet
  (for example `openrouter`, `nvidia`, `deepseek`). Think of it as the *staffing
  agency* you go through to reach the specialist. The same specialist (model) is
  often available through several agencies (providers).
- **API key** — your private password for a provider. It's tied to your account
  and your credit balance. A provider can have more than one key.
- **Route** — one specific combination of *model + provider*. The same model
  reached through two providers is two different routes.

Because the same model can be reached through several providers and keys, Syntra
writes routes in a consistent way so you always know exactly which one ran:

> **`model via provider (…last6 of key)`**
>
> Example: `deepseek-v4-pro via nvidia (…a1b2c3)`

The `…a1b2c3` is just the **last 6 characters of the API key** that was used. You
never see the full key (that would be unsafe), but the last 6 are enough for you
to tell *which* key it was if you have several.

Syntra's reliability has **four layers**, working together:

1. **Switching (failover)** — if a model breaks mid-job, jump to a healthy way to
   keep going, and *show you the switch*.
2. **Preflight** — test your chosen models *before* a job starts, so dead ones
   are skipped up front.
3. **Route-health memory** — remember what failed, for how long, and steer around
   it automatically.
4. **Silent-failure detection** — catch answers that *look* fine but aren't.

Plus **credential help**: when a key is out of money or rejected, Syntra tells you
the exact one-line command to fix it.

---

## 1. Switching when a model fails (failover)

### What it does (plain English)

Imagine you call a plumber and they don't pick up. You don't just sit there — you
call the *same plumber's other phone number*, and if that fails too, you call the
*next-best plumber on your list*. Syntra does exactly this, automatically, in the
middle of a job.

When a model errors out partway through a request, Syntra tries, in this order:

1. **Same model, a different provider.** The specialist is probably fine — it was
   the agency that had a problem. So Syntra re-tries the *same model* through the
   *next provider that offers it*. For example, if `deepseek-v4-pro` fails on the
   `deepseek` provider with a payment error, Syntra immediately retries the same
   model on `nvidia`. Each provider is tried **once** per request, so a genuinely
   broken request can't loop forever.

2. **The next-best model, one at a time.** If no provider can serve that model,
   Syntra walks down its ranked shortlist of *other* good models for the job and
   tries them one by one until one works.

There's also a money-aware case. If the failure is "out of credits" or "rate
limited" (too many requests too fast), Syntra marks *that key* as spent for the
rest of the session and **switches to a backup key** for the same model — or, if
there's no backup key, moves to another provider. It even switches *early*: if a
provider warns that your key is almost out (2 requests left or fewer), Syntra
quietly lines up the backup for the next call instead of waiting for the hard
failure.

### How to see it

You don't do anything to turn this on — it's always running. You just **watch the
live activity line**. When a switch happens, Syntra prints a one-line notice so
you're never left staring at a frozen screen wondering what's going on. The lines
look like this:

- Same model, different provider:
  > `⚠ deepseek-v4-pro via deepseek failed (billing) → switched to nvidia`
- Out-of-credits key swap (backup key for the same provider):
  > `⚠ gpt-5 via openrouter exhausted → switched key to openrouter`
- A key ran dry and there's a backup waiting:
  > `⚠ gpt-5 key …a1b2c3 on openrouter exhausted → next openrouter`

The little `⚠` just means "heads up, something changed" — it is **not** an error
you need to act on. The switch already handled it.

### Tiny example

You ask Syntra to write a function. It picks `deepseek-v4-pro`.

```
▸ EXECUTING (deepseek-v4-pro)
⚠ deepseek-v4-pro via deepseek failed (billing) → switched to nvidia
✓ step s1 done
```

What happened: the `deepseek` agency said "no credits", so Syntra reached the
*same* model through `nvidia` and finished the step. You got your answer; the only
sign of trouble was that one friendly line.

---

## 2. Preflight — test the models *before* the run

### What it does (plain English)

Before a real job, you can have Syntra do a **tiny test call to each model you'd
actually use** — like a sound-check before a concert. Instead of discovering
halfway through your task that a model is dead (and waiting around while it times
out), Syntra finds the broken ones *up front* and routes around them from the
start.

For each role Syntra uses (the **planner** that makes a plan, the **executor**
that does the work, and the **reviewer** that checks it), it asks the top couple
of candidate models a trivial question ("reply with the word OK") and watches what
comes back. It tries each model across its providers until one answers. The result
is remembered (see *route-health* below), so your *next* real run already avoids
the duds.

One smart detail: the test gives the model a small but *real* budget (64 tokens,
roughly 50 words) — not one or two. Some "thinking" models spend their budget
reasoning before they answer, so a too-tiny budget would make a *healthy* model
look broken. Syntra avoids that trap: if a model comes back empty only because it
ran out of room to think, that's marked **inconclusive** and is **not** held
against it.

### How to use it

Run this command:

```
syntra doctor --probe-models
```

This makes real (but tiny and cheap) network calls. The `doctor` command also
checks the rest of your setup; the `--probe-models` flag adds the live model test.

### What the output means

You'll see one line per attempt, then a summary per role:

```
---- probe-models (preflight: do the picked models actually work?) ----
[ ok ] probe-models/planner   deepseek-v4-pro via deepseek (…a1b2c3) -> ok (812ms)
[warn] probe-models/executor  gpt-5 via openrouter (…d4e5f6) -> billing: out of credit
[ ok ] probe-models/executor  gpt-5 via nvidia (…99aa00) -> ok (640ms)
  => executor: using gpt-5 via nvidia (…99aa00)
```

Reading it:

- **`ok`** — that model+provider answered. The `(812ms)` is how long it took
  (lower is snappier).
- **`warn`** — that attempt *failed*, but the role still found a working route, so
  failover did its job. It's informational, not a problem to fix right now. The
  word after the arrow (`billing`, `auth`, `server`…) tells you *why* it failed.
- **`=> executor: using gpt-5 via nvidia (…99aa00)`** — the bottom line: the
  healthy route this role will actually use.
- **`NO working model among top picks`** — the only line you *must* act on. It
  means none of the top candidates for that role answered. Add credits, fix a key,
  or pick different models.

### Tiny example

```
syntra doctor --probe-models
```

If everything is green and every role ends with a `=> ... using ...` line, you're
clear to run real tasks with confidence. If you see a `warn` that got recovered,
you can ignore it — or clean it up later (see credentials below).

---

## 3. Route-health memory — remembering what's flaky

### What it does (plain English)

Syntra keeps a little **memory of which routes have been misbehaving**, like a
notepad of "this plumber flaked on me, and how recently." When picking who does
the next job, it quietly *steers around* routes that are currently in the
doghouse — without permanently banning anyone.

Two things make this fair and self-correcting:

1. **Severity.** Not all failures are equal. A brief hiccup barely counts; a "no
   credits" or "bad password" failure counts a lot. Roughly, from mild to severe:
   a network blip < a server error < an empty reply < rate-limit < bad key
   (auth) < out of credits (billing). The worse it is, the harder Syntra steers
   away.

2. **A fading timer.** Every black mark **fades over time**. The "half-life" is
   about **5 minutes** — meaning a failure's weight is cut in half every 5 minutes.
   So a one-off glitch is basically forgotten within a few minutes and the route
   comes back into play. But problems that *won't* fix themselves by waiting —
   **out of credits, bad key, certificate problems, or "this model can't use
   tools"** — are treated as **sticky** and keep steering routing away until you
   actually fix them. A recent success also gives a route a small credit back, so
   a route that's working again recovers faster.

In short: **temporary problems heal on their own in minutes; real problems stick
until you fix them.** You don't have to manage any of this.

### How to see it

Show the current memory:

```
syntra route-health
```

You'll get a table:

```
Route health: 3 routes tracked
PROVIDER               MODEL                                      SUCCESS  FAILS  COOL
deepseek               deepseek-v4-pro                                  0      2  0.34
nvidia                 deepseek-v4-pro                                 12      0  1.00
openrouter             gpt-5                                            4      1  0.78
```

How to read it:

- **SUCCESS / FAILS** — how many times this route has worked or failed recently.
- **COOL** — the "coolness" score from **0.00 to 1.00**. **1.00 = perfectly
  healthy**, lower = more cooled-off (steered around). The list is sorted with the
  *most cooled* (most worrying) at the top so problems jump out.

In the example above, `deepseek-v4-pro via deepseek` is cooled (0.34) so routing
prefers the same model `via nvidia` (1.00) — which is why the failover in section
1 chose nvidia.

### Resetting it

If you've fixed a problem (added credits, swapped a key) and want to clear the
slate so Syntra re-tries everything fresh:

```
syntra route-health-clear
```

That clears **all** records. To clear just one provider or one model:

```
syntra route-health-clear --provider deepseek
syntra route-health-clear --model gpt-5
```

(Usually you don't need to — the 5-minute fade handles temporary stuff for you.
This is mainly for *after* you've fixed a sticky problem and don't want to wait.)

### Tiny example

A model fails twice on one provider during a busy moment:

```
syntra route-health
# deepseek    deepseek-v4-pro    0   2   0.34   (cooled — routing avoids it)
```

You wait ~10 minutes (or run other tasks) and check again — the marks have faded
and the route is healthy again, no action needed:

```
syntra route-health
# deepseek    deepseek-v4-pro    0   2   0.88   (recovered)
```

---

## 4. Credentials — when a key is out of money or rejected

### What it does (plain English)

Two key problems *can't* fix themselves by waiting: your key is **out of credits**
(the provider wants money — a 402 "payment required") or your key is **rejected**
(it's wrong or expired — a 401/403 "unauthorized"). When Syntra hits one of these,
it doesn't just fail silently. It tells you **what's wrong, which key, and the
exact command to fix it** — and it only says it **once per key per session** so
it never nags you.

The advice is always the same shape: *add credits to this key, or remove the key
from your config.* And it hands you the ready-to-paste command.

### How to see it

It appears in the live activity line the moment the problem is hit:

- Out of credits:
  > `💳 openrouter key …a1b2c3 is OUT OF CREDITS — add credits to this key, or remove it from config:  syntra providers remove-key openrouter a1b2c3`
- Rejected / bad key:
  > `🔑 openrouter key …a1b2c3 was REJECTED (bad/expired key) — fix it, or remove it from config:  syntra providers remove-key openrouter a1b2c3`

Notice the command at the end already has the provider name and the key's last-6
filled in for you.

### How to use the fix command

The command Syntra hands you is:

```
syntra providers remove-key <provider> <last6>
```

The order is **provider first, then the last-6 of the key** (the `last6` is the
`…a1b2c3` shown in the alert; you can paste it with or without the leading `…`).

It is **safe by default**: running it as-is is a **dry run** — it only *shows* you
what it *would* remove and changes nothing:

```
syntra providers remove-key openrouter a1b2c3
# would remove from /home/you/.config/syntra/providers.json:
#   - openrouter.api_key ending …a1b2c3
#
# DRY RUN (nothing changed). Apply with:
#   syntra providers remove-key openrouter a1b2c3 --yes
```

To actually apply it, add `--yes`. Before writing, Syntra **backs up your config
file** (so you can always undo) and writes the change securely:

```
syntra providers remove-key openrouter a1b2c3 --yes
# removed. backup saved: /home/you/.config/syntra/providers.json.bak-1718000000
# run `syntra doctor --probe-models` to re-check routing.
```

Two helpful notes:

- If your key is stored as an **environment variable** (a named setting on your
  computer) rather than written into the config file, Syntra can't remove it by
  its last-6 (the config only holds the *name* of the variable, not the key). It
  will tell you so and leave it untouched — you'd remove that yourself.
- Prefer to keep the key? Just **add credits** (for the out-of-credits case) or
  **replace it with a working key** (for the rejected case). Removing is only one
  option.

### Tiny example

You see the `💳 OUT OF CREDITS` line during a run. You decide to drop that key:

```
syntra providers remove-key openrouter a1b2c3          # preview — safe, changes nothing
syntra providers remove-key openrouter a1b2c3 --yes    # apply (auto-backs-up first)
syntra route-health-clear --provider openrouter        # forget the old failures
syntra doctor --probe-models                           # confirm routing is healthy again
```

---

## 5. Silent-failure detection — catching bad answers that *look* fine

### What it does (plain English)

The scariest failures aren't crashes — those are obvious. The scary ones are when
the AI returns a perfectly normal-looking reply (the provider even says "200 OK,
all good") but the *content* is useless or wrong. Syntra inspects every answer for
these and **cools the route** that produced one, so it gets steered around next
time. The checks are designed to be **cautious** — they only fire when they're
quite sure, so a good long answer isn't flagged by mistake.

It catches four kinds of "looks-fine-but-isn't":

1. **Empty reply.** The model returned *nothing* even though it claimed success.
   Treated as a hard failure and triggers the switching in section 1.

2. **Refusal / safety boilerplate.** A short non-answer like "I can't help with
   that" or "as an AI, I'm unable to…" — *with no actual work in it*. Importantly,
   an honest **"I don't know"** is *not* counted as a refusal (admitting
   uncertainty is good behavior, not a failure). And a long, genuine answer that
   merely *contains* a cautious word isn't flagged. The fix: steer toward a less
   restrictive model.

3. **Repetitive / garbage output ("degeneracy").** The model got stuck in a loop
   or collapsed into mush — the same phrases over and over, or low-information
   filler. Syntra measures this with plain text statistics (how repetitive the
   words and lines are), and gives legitimate repetitive content like tables and
   code a pass so it isn't a false alarm. The fix: retry / route around.

4. **"Claimed it works but it didn't" (tool-bypass).** This is the most dangerous
   one. The model *says* "tests pass" or "build succeeds" or "it works" — but
   Syntra's record of what *actually ran* shows a failure (a command exited with
   an error, an edit didn't apply). That's a provable contradiction: the answer is
   bluffing. Syntra flags it as a hard fail, warns the reviewer to **not** trust
   the claim, and cools that route. It's careful: it only triggers on a real
   success *claim* combined with real failing *evidence*, so "here's the code I
   wrote" (no claim) never trips it.

### How to see it

These also show up in the live activity line:

- > `⚠ gpt-5 via openrouter: refused / safety non-answer — cooling route`
- > `⚠ gpt-5 via openrouter: degenerate output (repetition) — cooling route`
- > `⚠ gpt-5 via openrouter: claimed tool success but did nothing — cooling route`

After any of these, you can confirm the route got marked by running
`syntra route-health` — you'll see its FAILS count go up and its COOL score drop.

### Tiny example

The executor claims a clean run, but the test command actually failed:

```
▸ EXECUTING (gpt-5)
[verify] FAIL (exit 1)
⚠ gpt-5 via openrouter: claimed tool success but did nothing — cooling route
[repair] added a fix-up step
```

What happened: the model said it worked, the real check disagreed, Syntra caught
the contradiction, cooled `gpt-5 via openrouter`, and even queued a repair step to
fix the actual problem. Nothing slipped through as "done" when it wasn't.

---

## Quick how-to cheat sheet

| I want to… | Command |
|---|---|
| Test that my chosen models actually work, before a real run | `syntra doctor --probe-models` |
| See which routes are flaky right now (the "coolness" scores) | `syntra route-health` |
| Forget all remembered failures (after fixing something) | `syntra route-health-clear` |
| Forget failures for just one provider | `syntra route-health-clear --provider <name>` |
| Forget failures for just one model | `syntra route-health-clear --model <model>` |
| Preview removing a dead/over-budget key (safe, no changes) | `syntra providers remove-key <provider> <last6>` |
| Actually remove that key (auto-backs-up first) | `syntra providers remove-key <provider> <last6> --yes` |

**Things that happen automatically — no command needed:**

- A model fails mid-job → Syntra switches to the same model on another provider,
  then to the next-best model, and **shows you the switch** (`⚠ X via … failed →
  switched to Y`).
- A key runs out of credits or is rejected → Syntra swaps in a backup key (or
  another provider) and tells you the one-line fix command.
- Any route misbehaves (errors, empties, refusals, repetition, or false "it
  works" claims) → Syntra remembers it, cools it, and routes around it. Temporary
  problems fade in about **5 minutes**; real ones (no credits, bad key) stick
  until you fix them.

**How routes are named everywhere:** `model via provider (…last6 of key)` — e.g.
`deepseek-v4-pro via nvidia (…a1b2c3)`. The last 6 characters are just enough to
tell which key, without ever exposing the full key.
