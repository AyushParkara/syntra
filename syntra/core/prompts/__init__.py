"""Role system-prompts as editable markdown, with safe in-code fallbacks.

Each role's guidance lives in a sibling `.md` file (planner.md, executor.md,
reviewer.md, reviewer_agent.md) so prompts can be iterated on without touching
code — they ARE the contract for how each mode behaves. `load(name)` reads the
file; if it's ever missing/unreadable (e.g. an odd packaging), it returns a short
built-in fallback so the loop never crashes for want of a prompt.

The prompts are grounded in current agent-review practice: a planner that owns a
lean ordered plan, an executor that stays in scope, and a reviewer that evaluates
through three lenses — Correctness, Completeness, Goal-fit — and returns
a backward-compatible JSON verdict.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent

# Minimal fallbacks — only used if the .md file can't be read. Kept terse on
# purpose; the rich guidance lives in the markdown files.
_FALLBACK = {
    "planner": (
        "You are the PLANNER. Turn the goal into 3-8 concrete ordered steps as "
        'strict JSON {"rationale": "...", "steps": [{"id":"s1","description":"..."}]}. '
        "Step 1 sets the core concept; later steps build on it. One-shot asks = 1 step."
    ),
    "executor": (
        "You are the EXECUTOR. Produce ONLY the current step's deliverable, building "
        "on prior results. Code in fenced blocks. No meta-commentary, no new steps, "
        "no unasked refactors."
    ),
    "reviewer": (
        "You are the REVIEWER, evaluating through three lenses (Correctness / Completeness / "
        "Goal-fit). Judge correctness, verification, and goal-fit. Output strict JSON "
        '{"verdict":"pass"|"fail","confidence":0..1,"issues":["..."],"summary":"..."}. '
        "Any issue => fail. 'no issues' is valid."
    ),
    "reviewer_agent": (
        "You are the REVIEWER (Correctness / Completeness / Goal-fit lenses) with read-only tools. "
        "Inspect the ACTUAL files; don't trust summaries. Output strict JSON "
        '{"verdict":"pass"|"fail","confidence":0..1,"issues":[...],"summary":"..."}. '
        "Any issue => fail."
    ),
    "researcher": (
        "You are the LEAD RESEARCHER. Decompose the topic into 3-6 orthogonal angles PLUS "
        "one DISCONFIRMING angle (search for evidence the thesis is WRONG). Delegate angles "
        "via `task`; use `websearch` then `webfetch` to read PRIMARY sources. Synthesize ONE "
        "report: TL;DR, findings where every non-obvious claim ends with [n], a confidence/"
        "gaps/disputed note, and a numbered ## Sources list mapping [n] -> title + the URL you "
        "ACTUALLY fetched. Prefer >=2 independent sources; flag single-source or conflicting "
        "claims as disputed; never cite an unopened URL. If no search backend, label UNVERIFIED."
    ),
    "reviewer_research": (
        "You are a CITATION AUDITOR with webfetch/websearch. For EACH [n] claim, RE-FETCH the "
        "cited URL and judge supported/overreach/unsupported. FAIL on any unsupported, "
        "misattributed, or uncited factual claim, on all-same-outlet sourcing, or on "
        "overconfidence from thin sources. Output strict JSON "
        '{"verdict":"pass"|"fail","confidence":0..1,"issues":[...],"summary":"..."}. Any issue => fail.'
    ),
}


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the role prompt for `name` (planner|executor|reviewer|reviewer_agent).

    Reads `<name>.md` next to this module; falls back to a built-in string if the
    file is absent or unreadable. Cached, since prompts are static per process.
    """
    path = _DIR / f"{name}.md"
    try:
        text = path.read_text("utf-8").strip()
        if text:
            return text
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        pass
    return _FALLBACK.get(name, "")
