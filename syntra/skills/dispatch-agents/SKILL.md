---
name: dispatch-agents
description: Fan independent work out to parallel sub-agents, then merge results. Triggers on do these in parallel, split this up, run agents, fan out, multiple agents.
model: inherit
---

You are coordinating parallel sub-agents (via the `task` tool / orchestrator). Use
this only when the work genuinely splits into INDEPENDENT pieces — parallelism on
dependent work just creates conflicts.

**Method:**
1. **Decompose** the goal into pieces that don't depend on each other's output. If
   piece B needs piece A's result, they are sequential — do NOT parallelize them.
2. **Scope each agent tightly:** give it only the context it needs for its piece (a
   crisp brief), an explicit deliverable, and a done-criterion. Not the whole goal.
3. **Isolate writers:** if two agents write files, give them separate areas (or a
   worktree) so they can't clobber each other.
4. **Bound each agent:** a per-agent budget so one runaway agent can't burn the
   whole run (the loopguard enforces this).
5. **Merge:** collect results, dedup, resolve any conflicts, and synthesize ONE
   coherent output. Note which agent produced what.

**Rules:**
- Parallelize for INDEPENDENT breadth (explore 3 areas, review N files), not for
  a single dependent chain.
- Cap the fan-out — more agents ≠ better; each costs tokens and adds merge work.
- A failed agent must not sink the batch; collect the survivors and report the gap.
- The lead (you) owns the final synthesis — agents return raw pieces, you assemble.

**Good fits:** explore several subsystems at once, review many files by dimension,
generate N candidate approaches to compare. **Bad fits:** "write the parser then
wire it in" (sequential), anything where order matters.
