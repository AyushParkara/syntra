---
name: receive-review
description: Act on review feedback well — fix real issues, push back on wrong ones. Triggers on address the review, fix the feedback, respond to review, apply review comments.
model: inherit
---

You received review feedback. Your job is to act on it correctly — not to blindly
obey every comment, and not to dismiss any.

**Method — triage each comment:**
1. **Real defect?** If the reviewer found a genuine bug/gap, fix it precisely — and
   only it. Don't rewrite surrounding code that wasn't flagged.
2. **Misunderstanding?** If the comment is based on the reviewer missing context,
   don't silently "fix" it — clarify the context (and consider whether the code
   should make that context obvious, which is itself a fix).
3. **Wrong / out of scope?** Push back with a reason. A reviewer is not always right;
   blindly applying a bad suggestion introduces bugs. State why you're not changing it.
4. **Style nitpick on a sound choice?** Note it, apply if cheap, skip if it fights a
   deliberate decision.

**Rules:**
- Fix the SPECIFIC thing flagged; resist the urge to "improve" untouched code (that
  re-opens review surface and risks new bugs).
- Re-verify after fixing — the fix must pass the same criterion that failed.
- For each comment, record the disposition: fixed / clarified / declined(why).
- If feedback and the original plan conflict, surface it rather than guessing.

**Output:**
```
COMMENT: <what the reviewer said>
DISPOSITION: fixed | clarified | declined
ACTION: <what you changed, or why not> + re-verify evidence
```
