---
name: subagent-driven
description: Run each plan step as a fresh sub-agent with a review checkpoint, to avoid context pollution. Triggers on fresh agent per step, isolate each task, subagent per step, clean-context execution.
model: inherit
---

You are executing a plan where EACH step runs in a fresh sub-agent, then gets
reviewed before the next starts. The point: a clean, uncluttered context per step
(no accumulated logs/dead-ends polluting the work) + a quality gate between steps.

**Method:**
1. For the next step, spawn a fresh sub-agent (via `task`) with a CRISP brief: just
   this step's goal, the specific inputs it needs, and its done-criterion. The
   sub-agent does NOT inherit the whole conversation — that's the feature.
2. The sub-agent does the step and returns its deliverable + evidence.
3. **Review gate:** check the result against the step's criterion (the 3-lens
   reviewer, or a read-only check). Pass → record + continue. Fail → return a
   targeted fix-brief to a fresh sub-agent for that step only; re-review just that.
4. Carry forward only the DISTILLED result (a short handoff), not the sub-agent's
   raw transcript — keep the lead context lean.

**Rules:**
- One step = one fresh sub-agent = one clean context. Don't let a step's debris leak
  into the next.
- Each sub-agent is bounded (loopguard per-agent budget) so a stuck one can't run away.
- Keep depth shallow — a sub-agent spawning sub-agents spawning sub-agents is a smell.
- The lead holds the plan + state; sub-agents are crisp-cut workers.

**Why:** this is Syntra's anti-context-rot pattern at the execution layer — the lead
stays clear-headed because each piece of grunt work happens (and its mess stays) in
a throwaway context.
