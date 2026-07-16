"""Output verification gate (anti-hallucination core differentiator).

Deterministic, pure (no I/O, no network) checks applied to every model output
*before* it is accepted. This is the §14b engine encoded: it catches the
failure modes that make LLM output untrustworthy, BEFORE the reviewer ever runs.

Checks (PLAN §10 Phase 1, §14):
- empty output                         -> ERROR (gate fail)
- truncation                           -> ERROR (gate fail)
- JSON contract (planner/reviewer)     -> ERROR if invalid / missing keys
- role drift (executor planning, etc.) -> WARNING
- certainty markers without evidence   -> WARNING (or ERROR in proof-only mode)

Severity tiers (PLAN §14): WARNING (log, continue) vs ERROR (gate / fail).
Conservative by default to avoid false-positives blocking good output; the
caller decides whether warnings are surfaced.

Claim taxonomy (PLAN §14b.3): FACT / PLAN / DECISION / ASSUMPTION / OPINION.
An ungrounded FACT is blocked only in proof-only mode (req A6).

Everything here is deterministic so it is fully unit-tested; only the model
calls themselves are nondeterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    WARNING = "warning"   # log and continue
    ERROR = "error"       # gate: forces fail


class ClaimType(str, Enum):
    FACT = "fact"               # about repo/world/tool result -> must be grounded
    PLAN = "plan"               # what we intend to do
    DECISION = "decision"       # a chosen approach + rationale
    ASSUMPTION = "assumption"   # explicitly-labelled guess
    OPINION = "opinion"         # recommendation, never stated as fact


# Stable check identifiers so callers/tests don't string-match free text.
CHECK_EMPTY = "empty_output"
CHECK_TRUNCATION = "truncation"
CHECK_JSON_CONTRACT = "json_contract"
CHECK_ROLE_DRIFT = "role_drift"
CHECK_CERTAINTY = "certainty_without_evidence"
CHECK_UNGROUNDED_FACT = "ungrounded_fact"
CHECK_REFUSAL = "refusal"
CHECK_DEGENERACY = "degeneracy"


# Words that assert certainty. If present without a nearby evidence marker, the
# output is over-confident (root cause #6 in §14b.1).
_CERTAINTY_TERMS = (
    "guaranteed", "guarantee", "always", "never", "impossible",
    "100%", "definitely", "certainly", "undoubtedly", "without a doubt",
    "obviously", "absolutely",
)
# Markers that indicate the claim is grounded in evidence/reasoning.
_EVIDENCE_TERMS = (
    "because", "based on", "per ", "according to", "see ", "as shown",
    "evidence", "output shows", "the test", "file:", "line ", "ref:",
    "documented", "verified", "confirmed by",
)

# Phrases that indicate the EXECUTOR is trying to (re)plan instead of execute.
_EXECUTOR_PLANNING = (
    "here is the plan", "here's the plan", "step 1:", "the steps are",
    "i will first", "my plan is", "let me outline the steps",
    "the following steps", "plan:\n",
)
# Phrases that indicate the PLANNER is executing instead of planning.
_PLANNER_EXECUTING = (
    "```python", "```bash", "```js", "```typescript", "```java", "```go",
    "here is the code", "here's the code", "here is the implementation",
)

# Calibrated abstention is a *pass*, not a failure (§14b.2 #3). Detect it so we
# never flag an honest "I don't know" as over-confident or ungrounded.
_ABSTENTION_TERMS = (
    "i don't know", "i do not know", "unknown", "cannot determine",
    "unable to verify", "need to check", "not enough information",
    "i'm not sure", "i am not sure", "unclear",
)

# Refusal / safety-boilerplate markers. Researched stance: a phrase list ALONE is
# brittle (paraphrase/cross-lingual evasion), so this is used only as a WEAK PRIOR
# *combined* with a structural test (short reply + no task artifact). A few common
# multilingual stems so it's not English-only. Honest abstention (above) is NOT a
# refusal -- that's a rewarded "I don't know", handled separately.
_REFUSAL_TERMS = (
    "i can't help", "i cannot help", "i can't assist", "i cannot assist",
    "i'm unable to", "i am unable to", "i won't", "i will not",
    "i'm not able to", "i am not able to", "i'm sorry, but i can",
    "as an ai", "i cannot provide", "i can't provide", "i must decline",
    "against my guidelines", "i'm not allowed", "violates", "not appropriate",
    "no puedo ayudar", "je ne peux pas", "es tut mir leid",  # es / fr / de stems
    "对不起", "抱歉，我无法",                                 # zh stems
)


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class VerificationReport:
    role: str
    step_id: str = ""
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    def passed(self) -> bool:
        """A report passes the gate iff it has no ERROR-severity findings."""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "_schema_version": 1,
            "role": self.role,
            "step_id": self.step_id,
            "passed": self.passed(),
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------- checks


def _contains_any(text: str, terms) -> bool:
    low = text.lower()
    return any(t in low for t in terms)


def check_empty(text: str) -> Finding | None:
    if not (text or "").strip():
        return Finding(CHECK_EMPTY, Severity.ERROR, "output is empty")
    return None


def check_truncation(text: str, finish_reason: str = "") -> Finding | None:
    """Truncation is an ERROR: a cut-off answer cannot be trusted as complete."""
    if finish_reason and finish_reason.lower() == "length":
        return Finding(CHECK_TRUNCATION, Severity.ERROR,
                       "output truncated (finish_reason=length)",
                       detail=f"finish_reason={finish_reason}")
    return None


def _has_task_artifact(text: str) -> bool:
    """True if the text carries a concrete work artifact (code fence, structured
    list, table, JSON, a path/diff marker). Used to tell a real answer from a
    short non-answer -- a refusal almost never carries one."""
    low = text.lower()
    if "```" in text or "~~~" in text:
        return True
    if "|" in text and text.count("|") >= 4:        # a table row or two
        return True
    if any(m in text for m in ("\n- ", "\n* ", "\n1. ", "\n2. ")):  # a list
        return True
    # JSON-ish: the WHOLE output is a structured object/array, or it has SEVERAL
    # key:value pairs (a real payload) -- not a single incidental quote in a refusal.
    if low.strip().startswith(("{", "[")) or text.count('": ') >= 2:
        return True
    if any(m in text for m in ("diff --git", "@@ ", "+++ ", "--- ")):  # a diff
        return True
    return False


def check_refusal(text: str, *, role: str = "executor") -> Finding | None:
    """Detect a safety/refusal NON-answer (HTTP 200 but the model declined).

    Non-brittle by design: a refusal phrase is only a WEAK PRIOR; we flag only
    when it co-occurs with the STRUCTURAL signature of a non-answer -- a SHORT
    reply that carries NO task artifact (no code/list/table/diff). Honest
    abstention ("I don't know") is never a refusal. A WARNING (remediation =
    reroute to a less-restrictive model), never a hard gate, to avoid killing a
    long, genuine answer that happens to contain the word "violates".
    """
    low = (text or "").lower()
    if not low.strip():
        return None  # empty is its own check
    if _contains_any(low, _ABSTENTION_TERMS):
        return None  # calibrated "I don't know" is a pass, not a refusal
    if not _contains_any(low, _REFUSAL_TERMS):
        return None
    # Structural guard: a real answer that merely mentions a refusal-ish word is
    # long and/or carries an artifact. A true refusal is short and bare.
    if _has_task_artifact(text) or len(text) > 600:
        return None
    return Finding(CHECK_REFUSAL, Severity.WARNING,
                   "looks like a refusal / safety non-answer (short, no work produced)",
                   detail=text[:120])


def _distinct_ngram_ratio(tokens: list[str], n: int) -> float:
    if len(tokens) < n:
        return 1.0
    grams = [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]
    return len(set(grams)) / max(1, len(grams))


def check_degeneracy(text: str) -> Finding | None:
    """Detect degenerate output: repetition loops / collapsed low-entropy text.

    Pure text statistics (language/model-agnostic, no internals): a composite of
    distinct-3gram ratio, zlib compression ratio, and unique-line ratio. A
    structural discount avoids false-positives on legitimately repetitive content
    (tables/JSON/lists). WARNING (remediation = retry, often with a repetition
    penalty), not a hard gate. Only meaningful on reasonably long text.
    """
    import zlib

    s = text or ""
    if len(s) < 240:
        return None  # too short to judge; avoid false alarms on terse answers
    tokens = s.split()
    distinct3 = _distinct_ngram_ratio(tokens, 3)
    raw = s.encode("utf-8", "ignore")
    comp_ratio = len(zlib.compress(raw, 6)) / max(1, len(raw))   # low => very repetitive
    lines = [ln for ln in s.splitlines() if ln.strip()]
    unique_line = (len(set(lines)) / max(1, len(lines))) if lines else 1.0

    # Health score in [0,1]; high = healthy. Weighted per research (distinct-3gram
    # .35, compression .30, unique-line .15 here folded with .20 half-overlap proxy).
    health = 0.45 * distinct3 + 0.35 * min(1.0, comp_ratio / 0.35) + 0.20 * unique_line
    # Structural discount: code/tables/lists are legitimately repetitive.
    if _has_task_artifact(s):
        health = min(1.0, health + 0.25)
    if health < 0.45:
        return Finding(CHECK_DEGENERACY, Severity.WARNING,
                       "output looks degenerate (repetition / collapsed text)",
                       detail=f"distinct3={distinct3:.2f} comp={comp_ratio:.2f} uniq_lines={unique_line:.2f}")
    return None


def check_json_contract(text: str, required_keys: tuple[str, ...]) -> Finding | None:
    """Planner/reviewer must emit schema-valid JSON (§14b.2 #5).

    Uses the loop's tolerant extractor so fenced/embedded JSON still passes.
    """
    from .loop import _extract_json  # local import to avoid a cycle at module load
    try:
        payload = _extract_json(text)
    except Exception:
        return Finding(CHECK_JSON_CONTRACT, Severity.ERROR,
                       "output is not valid JSON", detail=text[:160])
    if not isinstance(payload, dict):
        return Finding(CHECK_JSON_CONTRACT, Severity.ERROR,
                       "JSON is not an object", detail=str(type(payload)))
    missing = [k for k in required_keys if k not in payload]
    if missing:
        return Finding(CHECK_JSON_CONTRACT, Severity.ERROR,
                       "JSON missing required keys", detail=",".join(missing))
    return None


def check_role_drift(role: str, text: str) -> Finding | None:
    """Executor must not (re)plan; planner must not execute (§14b.2 #4)."""
    low = text.lower()
    if role == "executor" and _contains_any(low, _EXECUTOR_PLANNING):
        return Finding(CHECK_ROLE_DRIFT, Severity.WARNING,
                       "executor appears to be planning, not executing")
    if role == "planner" and _contains_any(low, _PLANNER_EXECUTING):
        return Finding(CHECK_ROLE_DRIFT, Severity.WARNING,
                       "planner appears to be executing (emitting code), not planning")
    return None


def check_certainty(text: str, proof_only: bool = False) -> Finding | None:
    """Flag over-certain phrasing lacking adjacent evidence (§14b.2 #9).

    Honest abstention is never flagged. In proof-only mode this is an ERROR;
    otherwise a WARNING.
    """
    low = text.lower()
    if _contains_any(low, _ABSTENTION_TERMS):
        return None
    if not _contains_any(low, _CERTAINTY_TERMS):
        return None
    # Evidence must be ADJACENT to back the certainty: a stray evidence word far away
    # in a long answer shouldn't launder an unbacked claim. A certainty sentence is
    # "backed" if it OR a neighbouring sentence (±1, so "It works. I tested it." is
    # fine) carries an evidence marker. Flag only when some certainty claim is unbacked.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", low) or [low]
    for i, s in enumerate(sentences):
        if _contains_any(s, _CERTAINTY_TERMS) and not _contains_any(s, _ABSTENTION_TERMS):
            window = " ".join(sentences[max(0, i - 1):i + 2])
            if not _contains_any(window, _EVIDENCE_TERMS):
                sev = Severity.ERROR if proof_only else Severity.WARNING
                return Finding(CHECK_CERTAINTY, sev,
                               "over-certain claim without adjacent evidence")
    return None


def classify_claim(text: str) -> ClaimType:
    """Best-effort single-label classification of a short statement.

    Heuristic and deterministic. Used to decide what proof-only mode must block.
    Order matters: explicit labels win, then opinion, then plan, then fact.
    """
    low = text.lower().strip()
    if low.startswith(("assumption:", "assume ", "assuming")) or "i assume" in low:
        return ClaimType.ASSUMPTION
    if _contains_any(low, ("i recommend", "i suggest", "in my opinion", "i think", "probably", "might be", "could be")):
        return ClaimType.OPINION
    if _contains_any(low, ("we will", "i will", "next step", "plan to", "going to")):
        return ClaimType.PLAN
    if _contains_any(low, ("decided", "we chose", "the decision", "rationale")):
        return ClaimType.DECISION
    return ClaimType.FACT


def check_ungrounded_fact(text: str, proof_only: bool) -> Finding | None:
    """In proof-only mode, a FACT claim with no grounding is blocked (§14b.2 #2).

    Only active when proof_only is True. Abstention is allowed. A FACT is
    considered grounded if it carries an evidence marker.
    """
    if not proof_only:
        return None
    low = text.lower()
    if _contains_any(low, _ABSTENTION_TERMS):
        return None  # abstention is a rewarded pass
    if classify_claim(text) is not ClaimType.FACT:
        return None  # plans/opinions/assumptions are allowed to be ungrounded
    if _contains_any(low, _EVIDENCE_TERMS):
        return None  # grounded
    return Finding(CHECK_UNGROUNDED_FACT, Severity.ERROR,
                   "ungrounded factual claim in proof-only mode "
                   "(ground it or mark ASSUMPTION/UNKNOWN)")


# --------------------------------------------------------------- orchestrator


def verify_output(
    *,
    role: str,
    text: str,
    finish_reason: str = "",
    step_id: str = "",
    json_required_keys: tuple[str, ...] = (),
    proof_only: bool = False,
) -> VerificationReport:
    """Run the relevant checks for a role's output and collect findings.

    - empty + truncation: always.
    - json_contract: only when json_required_keys is given (planner/reviewer).
    - role_drift + certainty: always (drift is a warning).
    - ungrounded_fact: only in proof_only mode and only for executor-style prose
      (skipped when a JSON contract is expected, since structured output isn't prose).
    """
    report = VerificationReport(role=role, step_id=step_id)

    for finding in (check_empty(text), check_truncation(text, finish_reason)):
        if finding:
            report.add(finding)

    # If the output is empty, downstream content checks are meaningless.
    if any(f.check == CHECK_EMPTY for f in report.findings):
        return report

    if json_required_keys:
        f = check_json_contract(text, json_required_keys)
        if f:
            report.add(f)

    # Prose-shaped checks (role-drift, certainty, refusal, degeneracy) only apply to
    # FREE-FORM output. For JSON-contract roles (planner/reviewer) the substring
    # heuristics would falsely fire on the CONTENTS of JSON string fields (a verdict
    # "summary":"this definitely works" -> spurious certainty; a planner string with
    # ```python -> spurious role-drift). The JSON contract check already governs them.
    if not json_required_keys:
        drift = check_role_drift(role, text)
        if drift:
            report.add(drift)
        cert = check_certainty(text, proof_only=proof_only)
        if cert:
            report.add(cert)
        ref = check_refusal(text, role=role)
        if ref:
            report.add(ref)
        deg = check_degeneracy(text)
        if deg:
            report.add(deg)

    # Ungrounded-fact only applies to free-form prose (not JSON-contract roles).
    if proof_only and not json_required_keys:
        uf = check_ungrounded_fact(text, proof_only=True)
        if uf:
            report.add(uf)

    return report
