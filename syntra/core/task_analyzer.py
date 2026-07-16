"""Model-based task analyzer.

Instead of regex/keywords, we ask the LLM itself to analyze the task.
The planner model (or a lightweight model) classifies the goal and returns
structured analysis that drives routing decisions for executor and reviewer.

Boosts are multiplicative (not flat additive) to preserve proportional
separation between models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from ..providers.openai_compat import ChatMessage, ChatResult


TaskCategory = Literal[
    "coding", "reasoning", "creative", "factual",
    "debugging", "planning", "analysis", "general",
]

Complexity = Literal["simple", "medium", "complex"]
Criticality = Literal["low", "medium", "high"]


# Capability AXES — a small, principled set every task is scored against (0..1
# demand). NOT task types: any task (regex, pentest, poem, pipeline) is a point in
# this space. Each axis names the catalog eval keys that measure a model's ability
# on it (used by IRT routing to match task-demand to model-capability).
# (Dimensions are intentionally few; the research warns too many => sparse/noisy.)
CAPABILITY_AXES = ("reasoning", "code", "tool_use", "long_context",
                   "instruction", "speed", "criticality")


@dataclass(frozen=True)
class TaskAnalysis:
    category: TaskCategory
    complexity: Complexity
    criticality: Criticality
    needs_coding: bool
    needs_reasoning: bool
    needs_long_context: bool
    needs_tool_use: bool
    # True for chat/one-shot Q&A that should be answered DIRECTLY in a single
    # call (no plan/execute/review ceremony) -- the smart, cheap path.
    conversational: bool = False
    # Per-role multiplicative boost multipliers (1.0 = no change)
    planner_boost: float = 1.0
    executor_boost: float = 1.0
    reviewer_boost: float = 1.0
    # Continuous per-axis DEMAND vector (0..1) for THIS task — the IRT `a`. Empty
    # dict = not computed (router falls back to role weights). Keys ⊆ CAPABILITY_AXES.
    demands: dict = field(default_factory=dict)
    # F40: the analyzer flags a GENUINELY under-specified goal + the ONE clarifying question
    # to ask, so the loop can ask before guessing (only when clarify_ambiguous is on).
    ambiguous: bool = False
    clarifying_question: str = ""
    # P2: the action could NOT be cleanly undone (delete/overwrite/deploy/pay/creds/auth/infra).
    # Forces criticality=high + tier_cap=top + can drive a confirm gate — independent of topic.
    irreversible: bool = False
    # T5/E2: the cheap analyzer's TIER CEILING for this task ("cap"|"mid"|"top"), derived from
    # complexity+criticality. The router caps candidate strength at this tier in budget/pennies
    # modes; the planner may PROPOSE exceeding it (cost_escalation), and in budget mode that
    # surfaces for the user's approval at the confirm gate. Empty/"top" = no cap (frontier ok).
    tier_cap: str = "top"
    # A short, human-readable session TITLE the analyzer derives from UNDERSTANDING the goal
    # (≤ ~6 words, the topic — not the first few words verbatim). Used for the session/tab name
    # when the user hasn't set one. "" = the analyzer didn't provide one (caller falls back to
    # derive_title()). Free — it rides the classification call the analyzer already makes.
    title: str = ""
    # For a SIMPLE task that skips the planner (the cheap executor tiers), a crisp one-line
    # DIRECTIVE the analyzer hands the executor so it acts decisively without re-planning or
    # wandering — e.g. "glob for image files, pick the newest, show_image it". "" = none (the
    # executor just works from the goal). The "analyzer guides the executor" the user asked for.
    # Free — rides the classification call. NEVER used on the full pipeline (the planner owns that).
    executor_brief: str = ""


_TIER_ORDER = ("cap", "mid", "top")


def derive_tier_cap(complexity: str, criticality: str, *,
                    irreversible: bool = False, explicit_depth: bool = False,
                    ambiguous: bool = False) -> str:
    """The analyzer's tier ceiling for a task (E2): cap | mid | top. Pure, data-driven off
    complexity + criticality (+ irreversibility, an explicit depth request, and unresolved
    ambiguity) — NO model call, NO hardcoded model list. High-stakes/complex/irreversible work
    earns a higher ceiling; trivial/low work is capped to the cheap tier so it never grabs a
    frontier model. The router maps these tiers onto the catalog's own price/score ladder."""
    comp = (complexity or "medium").lower()
    crit = (criticality or "low").lower()
    # An irreversible action is NEVER capped — a wrong cheap-model edit you can't undo is the
    # exact case to spend on. An explicit "use the best / think hard" also floors at top.
    if irreversible or explicit_depth or crit == "high" or comp == "complex":
        tier = "top"
    elif crit == "medium" or comp == "medium":
        tier = "mid"
    else:
        tier = "cap"
    # Unresolved ambiguity → bump one tier (more capability to handle the underspecified case).
    if ambiguous and tier != "top":
        tier = _TIER_ORDER[_TIER_ORDER.index(tier) + 1]
    return tier


# Leading filler/imperative words to drop when distilling a goal into a title — so "can you
# please help me build a login form" → "Login Form", not "Can You Please Help". Data-driven,
# extend freely; this is wording cleanup, not classification.
_TITLE_STRIP_LEAD = frozenset({
    "can", "could", "would", "will", "please", "pls", "you", "u", "i", "we", "let", "lets",
    "help", "me", "us", "to", "the", "a", "an", "hey", "hi", "ok", "okay", "so", "now", "just",
    "want", "wanna", "need", "like", "make", "do", "build", "create", "write", "add", "fix",
    "implement", "set", "setup", "give", "show", "tell", "explain", "how", "what", "why",
})


def derive_title(goal: str, analyzer_title: str = "") -> str:
    """A short, human-readable session title — the TOPIC of the goal, not its first few words.

    Prefers the analyzer's own title (it UNDERSTANDS the goal). Falls back to distilling the
    goal text: collapse whitespace, drop a run of leading filler/imperative words ("can you
    please help me build…"), keep the meaningful head, Title-Case it, bound to ~6 words / 48
    chars. Pure + deterministic -> unit-tested. NO model call (the analyzer title is free; the
    fallback is string work), NO hardcoded titles."""
    import re as _re

    def _clean(s: str) -> str:
        return _re.sub(r"\s+", " ", (s or "")).strip().strip("\"'`.!?,:;")

    at = _clean(analyzer_title)
    if at:
        words = at.split()
        out = " ".join(words[:8])[:48].strip()
        # Title-case only all-lower words (preserve acronyms / CamelCase the model chose)
        return " ".join(w if any(c.isupper() for c in w) else w.capitalize()
                        for w in out.split())

    text = _clean(goal)
    if not text:
        return ""
    words = text.split()
    # drop a leading run of filler/imperative words (but never strip the whole thing away)
    i = 0
    while i < len(words) - 1 and words[i].lower().strip(",.!?:;") in _TITLE_STRIP_LEAD:
        i += 1
    head = words[i:]
    head = head[:6]                                   # ~6 meaningful words
    title = " ".join(head)[:48].strip().strip(",.:;-")
    if not title:
        title = " ".join(words[:6])[:48].strip()      # fallback: never empty when goal isn't
    # Title-Case lower words, keep existing caps/acronyms
    return " ".join(w if any(c.isupper() for c in w) else w.capitalize()
                    for w in title.split())


_ANALYSIS_SYSTEM = """You are a Task Analyzer / triage router on the HOT PATH. Read the user's
message (with any prior conversation) and classify it so the system allocates the RIGHT effort,
tools, and model strength. Be fast and decisive — but fill `reasoning` FIRST, then let the labels
follow from it. Output ONLY the JSON below — no preamble, no essay.

Output strict JSON with this exact shape (fill `reasoning` BEFORE the scores):
{
  "analysis": {
    "reasoning": "≤1 clause: the worst plausible outcome if this is done wrong, and is it reversible",
    "category": "coding|reasoning|creative|factual|debugging|planning|analysis|general",
    "complexity": "simple|medium|complex",
    "criticality_1_5": 1-5,
    "criticality": "low|medium|high",
    "irreversible": true|false,
    "conversational": true|false,
    "needs_coding": true|false,
    "needs_reasoning": true|false,
    "needs_long_context": true|false,
    "needs_tool_use": true|false,
    "explicit_depth_request": true|false,
    "ambiguous": true|false,
    "clarifying_question": "one question to ask the user, or \"\"",
    "title": "a 2-5 word topic title (the SUBJECT, e.g. \"OAuth login flow\", not the user's words)",
    "executor_brief": "for a SIMPLE task only: one crisp directive telling the executor exactly what to look at / do, or \"\""
  }
}

Rules:
- reasoning FIRST: in one clause, state the worst plausible outcome if this is done badly or
  maliciously, and whether it could be undone. The scores below must follow from THIS, not from
  the topic words or the user's tone.
- criticality — rate the CONSEQUENCE of getting it wrong + REVERSIBILITY, never the topic or tone.
  Score 1-5 in `criticality_1_5`, then set `criticality`: 4-5 → high, 3 → medium, 1-2 → low.
    5/4 (high): a mistake is HARD/IMPOSSIBLE to reverse, or it touches production, real money,
      credentials/secrets/security, data deletion or exfiltration, or anything affecting other
      people / external systems. Shape (not keywords): deploying, migrating a live DB, deleting or
      overwriting data, sending payments or email, changing auth/permissions, editing infra/CI.
      Default HERE when an action is irreversible even if it "sounds small".
    3 (medium): real work with BOUNDED, RECOVERABLE consequences — code that will be reviewed/
      tested before shipping, local refactors, scoped features, analysis that informs a decision.
      A mistake costs time, not safety or money.
    1-2 (low): NO external consequence — chat, trivia, explanations, throwaway exploration,
      reading/looking at things, drafts. Getting it wrong wastes a reply.
  TONE RULE: emphatic words ("urgent", "ASAP", "critical", CAPS) do NOT raise criticality — a calm
  "delete the prod table" is high; an all-caps "WHAT TIME IS IT" is low. When genuinely torn between
  two levels, choose the HIGHER (under-rating risk is worse than over-rating it).
- irreversible: TRUE iff a mistake here could NOT be cleanly undone (delete/overwrite data, deploy,
  pay, send email, rotate/expose creds, change auth, touch infra/CI/prod). Independent of topic.
- conversational: TRUE for greetings, small talk, identity questions, trivia/short factual Q&A,
  simple arithmetic, single definitions, chit-chat, and short opinions that DON'T require building
  or LOOKING AT anything — answerable in ONE short reply with no plan and no tools. FALSE for
  anything needing building, multi-step work, code, debugging, design, file/tool actions, or
  review. NEVER mark a tool-needing message conversational (see needs_tool_use).
- CONTEXT: prior turns appear ABOVE the GOAL. Resolve elliptical follow-ups ("then do it", "yes",
  "go on") using them — conversational UNLESS the prior turns show real multi-step/code/tool work.
- complexity: simple = one reply/one step; medium = multi-step but bounded; complex = open-ended /
  architectural / many moving parts. Read the actual work, not the wording.
- needs_coding: writing/reviewing/debugging code. needs_reasoning: deduction/math/puzzles/deep
  analysis. needs_long_context: references large text/files/data.
- needs_tool_use: TRUE whenever answering correctly requires LOOKING AT or ACTING ON the real
  machine — filesystem, workspace, a shell command, an API/URL, any external tool. Includes
  messages that sound casual but can only be answered by inspecting the system ("what dir am I in",
  "what's in this folder", "list the files", "read X", "does file Y exist", "what's the branch",
  "run this", "check/open <path>"). ALSO includes CAPABILITY / META questions about yourself that
  can only be answered truthfully by trying — "can you read my files?", "do you have shell access?",
  "are you able to see my repo?": answer those by LOOKING with a tool, never by guessing your own
  limits and saying "no". ALSO TRUE for "SHOW/DISPLAY/RENDER/OPEN the image (or file)", "show me
  that here", "let me see it" — displaying an image or file is a TOOL ACTION (show_image/view_image),
  NOT chat; never answer "I can't display images" — call the tool. If the honest answer is "I'd
  have to look / run / show something", it needs tools. When unsure, lean TRUE — wrongly withholding
  tools makes you falsely claim "I can't access that"; wrongly granting them costs one cheap no-op.
- explicit_depth_request: TRUE if the user explicitly asks for care/depth/thoroughness ("think hard",
  "be thorough", "do it properly", "use the best model", "deep dive"). This raises effort/tier.
- ambiguous: TRUE only when genuinely UNDER-SPECIFIED — you can't tell what they want and ONE
  clarifying question would CHANGE the work (not just polish it). Put that question in
  clarifying_question. Default FALSE; don't ask just to be safe.
- executor_brief: fill ONLY for a SIMPLE task (one that will run as a single cheap step, no
  planner) — a ONE-LINE directive naming the concrete tool actions to take, in order, so the
  executor acts decisively instead of re-planning. E.g. for "show me an image" →
  "glob the workspace for image files (png/jpg/…); if none, say so; else pick the most recent and
  show_image it". For "what dir am I in" → "run pwd / list the workspace root and report the path".
  CRITICAL discipline to bake in: VERIFY before asserting absence — never say a file/thing doesn't
  exist until a tool call (glob/list/read) actually showed nothing; and never contradict yourself
  (don't claim "no image exists" and then display one). Leave "" for chat or for medium/complex
  work (the planner writes the steps there).
"""


class TaskAnalyzer:
    """Uses an LLM to analyze tasks. Requires a model call function."""

    def __init__(self, caller: Callable[[str, list[ChatMessage]], ChatResult]):
        """caller: function(model_id, messages) -> ChatResult"""
        self.caller = caller

    def analyze(self, goal: str, model_id: str,
                history: list[ChatMessage] | None = None) -> TaskAnalysis:
        messages: list[ChatMessage] = [ChatMessage("system", _ANALYSIS_SYSTEM)]
        if history:
            messages.extend(history)   # prior turns -> context-aware classification
        messages.append(ChatMessage("user", f"GOAL:\n{goal}"))
        result = self.caller(model_id, messages)
        data = self._extract_json(result.text)
        a = data.get("analysis", {})

        category = a.get("category", "general")
        complexity = a.get("complexity", "medium")
        # Criticality: prefer the graded 1-5 (better calibration), collapse to the coarse label
        # deterministically here; fall back to the coarse label the model gave if 1-5 is absent.
        criticality = a.get("criticality", "low")
        c15 = a.get("criticality_1_5")
        try:
            c15 = int(c15)
        except (TypeError, ValueError):
            c15 = None
        if c15 is not None:
            criticality = "high" if c15 >= 4 else ("medium" if c15 == 3 else "low")
        irreversible = bool(a.get("irreversible", False))
        # An irreversible action is high-stakes by definition — never let it read as low/medium.
        if irreversible:
            criticality = "high"

        needs_coding = bool(a.get("needs_coding", False))
        needs_reasoning = bool(a.get("needs_reasoning", False))
        needs_long_context = bool(a.get("needs_long_context", False))
        needs_tool_use = bool(a.get("needs_tool_use", False))
        conversational = bool(a.get("conversational", False))
        ambiguous = bool(a.get("ambiguous", False))
        explicit_depth = bool(a.get("explicit_depth_request", False))
        clarifying_question = str(a.get("clarifying_question", "") or "").strip()
        title = derive_title(goal, str(a.get("title", "") or ""))   # smart session title
        executor_brief = str(a.get("executor_brief", "") or "").strip()[:300]  # cheap-tier directive
        # The classification above is the MODEL's call (dynamic, not keyword
        # parsing). The only guard: never one-shot a CODE/TOOL task, because that
        # would skip the verification gate + reviewer on code -- a real safety
        # risk, not an intent guess. Everything else trusts the model.
        if conversational and (needs_coding or needs_tool_use):
            conversational = False

        # Multiplicative boosts (preserve proportional separation)
        base = 1.0
        if criticality == "high":
            base = 1.15
        elif criticality == "medium":
            base = 1.08

        if complexity == "complex":
            base *= 1.05

        planner_boost = base * 1.05
        executor_boost = base
        reviewer_boost = base

        # The executor writes the actual deliverable, so for SUBSTANTIVE work (anything
        # past trivial one-liners) bias it toward a stronger model — the fast default
        # underperforms on real tasks (for example, a supposedly strong model producing poor work).
        if not (complexity == "simple" and criticality == "low"):
            executor_boost *= 1.12

        if needs_coding:
            executor_boost *= 1.05
        if category == "debugging":
            reviewer_boost *= 1.05

        demands = _derive_demands(a, category, complexity, criticality,
                                  needs_coding, needs_reasoning,
                                  needs_long_context, needs_tool_use, conversational)

        return TaskAnalysis(
            category=category,  # type: ignore[arg-type]
            complexity=complexity,  # type: ignore[arg-type]
            criticality=criticality,  # type: ignore[arg-type]
            needs_coding=needs_coding,
            needs_reasoning=needs_reasoning,
            needs_long_context=needs_long_context,
            needs_tool_use=needs_tool_use,
            conversational=conversational,
            planner_boost=round(min(2.0, planner_boost), 3),
            executor_boost=round(min(2.0, executor_boost), 3),
            reviewer_boost=round(min(2.0, reviewer_boost), 3),
            demands=demands,
            ambiguous=ambiguous,
            irreversible=irreversible,
            clarifying_question=clarifying_question,
            tier_cap=derive_tier_cap(complexity, criticality, irreversible=irreversible,
                                     explicit_depth=explicit_depth, ambiguous=ambiguous),  # E2 ceiling
            title=title,
            executor_brief=executor_brief,
        )

    @staticmethod
    def _extract_json(text: str) -> dict:
        from .jsonutil import extract_json
        return extract_json(text, default={})


def _derive_demands(a: dict, category: str, complexity: str, criticality: str,
                    needs_coding: bool, needs_reasoning: bool,
                    needs_long_context: bool, needs_tool_use: bool,
                    conversational: bool) -> dict:
    """Build the per-task DEMAND vector (0..1 per CAPABILITY_AXES) for IRT routing.

    Prefers an explicit `demands` block from the analyzer LLM (richest signal); for
    any axis the model didn't give, DERIVES a value from the existing classification
    signals so this works even without prompt changes. Pure + deterministic.
    """
    # 1) start from any LLM-supplied demands (clamped, known axes only)
    out: dict = {}
    llm = a.get("demands")
    if isinstance(llm, dict):
        for k, v in llm.items():
            if k in CAPABILITY_AXES:
                try:
                    out[k] = max(0.0, min(1.0, float(v)))
                except (TypeError, ValueError):
                    pass

    # 2) derive the rest from classification signals (only fill missing axes)
    comp = {"simple": 0.3, "medium": 0.6, "complex": 0.9}.get(complexity, 0.6)
    crit = {"low": 0.2, "medium": 0.55, "high": 0.9}.get(criticality, 0.2)

    def fill(axis: str, value: float):
        if axis not in out:
            out[axis] = max(0.0, min(1.0, round(value, 3)))

    # reasoning: high for reasoning/analysis/planning tasks, scales with complexity
    base_reason = 0.75 if (needs_reasoning or category in ("reasoning", "analysis", "planning")) else 0.35
    fill("reasoning", max(base_reason, comp))
    # code: dominated by needs_coding / coding|debugging category
    fill("code", 0.85 if (needs_coding or category in ("coding", "debugging")) else 0.1)
    # tool_use / agentic
    fill("tool_use", 0.8 if needs_tool_use else 0.15)
    # long_context
    fill("long_context", 0.8 if needs_long_context else 0.2)
    # instruction precision: always matters; higher for code/critical work
    fill("instruction", max(0.5, crit, 0.7 if needs_coding else 0.0))
    # speed sensitivity: conversational/simple tasks want speed; complex/critical don't
    fill("speed", 0.8 if (conversational or complexity == "simple") else 0.3)
    # criticality axis straight from the criticality signal
    fill("criticality", crit)
    return out


# --- T9: fan-out intent (two SHAPES of multi-agent work) ---------------------
# (i) WORKER fan-out: a main agent spawns sub-agent workers for slices of ITS task
#     ("help me do X with N agents" / "summarize each of these files"). -> "worker"
# (ii) CAMPAIGN: N INDEPENDENT jobs, each its own full plan->exec->review
#      ("run these N independent jobs" / "for each repo, audit it"). -> "campaign"
# Pure + deterministic so the dispatcher can route without a model call. Returns
# "worker" | "campaign" | None (no clear fan-out intent).
import re as _re

_FANOUT_VERB = _re.compile(
    r"\b(spawn|spin\s*up|run|launch|use|fan[\s-]*out|orchestrat\w*|dispatch|kick\s*off)\b",
    _re.IGNORECASE)
_FANOUT_NOUN = _re.compile(r"\b(\d+\s+)?(sub[\s-]?agents?|agents?|workers?|jobs?|tasks?)\b",
                           _re.IGNORECASE)
# Phrases that signal INDEPENDENT jobs (campaign) rather than helpers on one task.
_CAMPAIGN_HINT = _re.compile(
    r"\b(independent|separate|each\s+(of\s+)?(these|the|repo|project|target|file|service)"
    r"|in\s+parallel\s+on|across\s+(these|all)|several\s+(jobs|tasks|projects)|"
    r"different\s+(jobs|tasks|targets|repos|projects))\b", _re.IGNORECASE)
# Phrases that signal helpers on ONE task (worker fan-out).
_WORKER_HINT = _re.compile(
    r"\b(help\s+me|to\s+do|for\s+me|on\s+this|my\s+task|this\s+task|work\s+together|"
    r"divide\s+(this|the)\s+(work|task)|split\s+(this|the)\s+(work|task))\b", _re.IGNORECASE)


def classify_fanout_intent(goal: str) -> "str | None":
    """Classify a goal's multi-agent fan-out SHAPE: 'worker' (sub-agents on one task),
    'campaign' (N independent full pipelines), or None (no clear fan-out ask)."""
    g = goal or ""
    has_verb = bool(_FANOUT_VERB.search(g))
    has_noun = bool(_FANOUT_NOUN.search(g))
    worker = bool(_WORKER_HINT.search(g))
    campaign = bool(_CAMPAIGN_HINT.search(g)) or bool(_re.search(r"\bjobs?\b", g, _re.IGNORECASE))
    # A clear fan-out ask needs (verb + noun), OR a campaign/worker hint paired with the noun
    # ("help me ... with 4 agents" has the noun + a worker hint but no spawn verb).
    if not ((has_verb and has_noun) or (campaign and (has_noun or has_verb))
            or (worker and has_noun)):
        return None
    # "sub-agent(s)" said explicitly = WORKER intent (helpers on ONE task), even alongside an
    # "each X" phrase — "spin up 3 sub-agents to summarize each file" is one task, not N jobs.
    if _re.search(r"\bsub[\s-]?agents?\b", g, _re.IGNORECASE):
        return "worker"
    if campaign and not worker:
        return "campaign"
    if worker and not campaign:
        return "worker"
    # ambiguous / generic "N agents" -> default to WORKER (the safer, cheaper shape:
    # one task, helper sub-agents — campaign is opt-in via clear independence language).
    return "worker"
