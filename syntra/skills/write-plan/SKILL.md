---
name: write-plan
description: Break a task into small, ordered, independently-verifiable steps. Triggers on plan this, write a plan, break this down, how should I approach.
model: inherit
---

You are writing an execution plan. Each step must be small and checkable.

**Method:**
1. State the goal in one sentence.
2. Break it into steps that are each ~2-5 minutes of work — small enough that a
   different worker could execute one without further clarification.
3. Order steps so each builds on the previous. Step 1 establishes the core
   decision; later steps use it (no concept drift).
4. For each step, note: what it produces, and how you'd verify it's done.
5. Flag any step that touches risky areas (data loss, money, security).

**Rules:**
- 3-8 steps for most tasks. If more, the task should be split.
- No filler steps. Each must move the goal forward.
- Steps reference concrete files/functions where known.
- End with a verification step (test, run, check) — "done" must be provable.

**Output format:**
```
GOAL: ...
1. <step>  → produces: ... | verify: ...
2. <step>  → produces: ... | verify: ...
...
```
