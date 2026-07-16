"""Proof artifacts — execution-evidence verification (recovered from syntra-v1).

Syntra's verification gate (verification.py) judges the model's OUTPUT TEXT
(empty/truncation/refusal/degeneracy/JSON). It never checks whether the work the
model CLAIMS it did actually happened. This module is the missing half: it walks
the task's real session events into typed pass/fail/observed records grounded in
EXECUTION EVIDENCE (command exit codes, applied edits, verify results), so the
reviewer can judge against "did it provably happen", and a contradiction check can
catch "claimed tests pass but the command exited non-zero" (the tool_bypass class).

Pure + deterministic (events in -> records out); fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProofArtifact:
    kind: str          # "command" | "edit" | "step" | "verify"
    status: str        # "passed" | "failed" | "observed"
    summary: str       # short human line
    source: str        # which event kind it came from

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status,
                "summary": self.summary, "source": self.source}


# Success-claim language: the executor/reviewer asserting the work is verified-done.
# Only these phrasings (which IMPLY execution succeeded) are cross-checked against
# evidence — generic "here's the code" is NOT a success claim, so it's never flagged.
_SUCCESS_CLAIMS = (
    "tests pass", "all tests pass", "tests passing", "all green", "build succeeds",
    "build passes", "it works", "works correctly", "verified working", "successfully ran",
    "ran successfully", "passes all", "exit code 0", "exited 0", "no errors", "compiles cleanly",
)


def collect_proof_artifacts(events) -> list[ProofArtifact]:
    """Walk a task's events (list of {kind, payload} dicts, oldest-first) into typed
    execution-evidence records. Recognizes Syntra's real event kinds: verify_result
    (exit code), edit (applied/failed), step_done/step_failed. Pure."""
    out: list[ProofArtifact] = []
    for ev in events or []:
        kind = ev.get("kind", "")
        p = ev.get("payload", {}) or {}
        if kind == "verify_result":
            ok = bool(p.get("ok"))
            ec = p.get("exit_code")
            status = "passed" if ok else "failed"
            cmd = p.get("command", "verify command")
            out.append(ProofArtifact(
                "verify", status,
                f"`{cmd}` -> exit {ec if ec is not None else '?'} ({status})",
                "verify_result"))
        elif kind == "edit":
            st = (p.get("status") or "").lower()
            if st in ("applied", "failed"):
                out.append(ProofArtifact(
                    "edit", "passed" if st == "applied" else "failed",
                    f"edit {st}: {p.get('path', '?')}", "edit"))
        elif kind == "step_failed":
            out.append(ProofArtifact(
                "step", "failed",
                f"step {p.get('step_id', '?')} failed: {str(p.get('error', ''))[:80]}",
                "step_failed"))
        elif kind == "step_done":
            out.append(ProofArtifact(
                "step", "observed", f"step {p.get('step_id', '?')} completed", "step_done"))
    return out


def has_failing_evidence(artifacts) -> bool:
    return any(a.status == "failed" for a in artifacts or [])


def has_passing_evidence(artifacts) -> bool:
    return any(a.status == "passed" for a in artifacts or [])


# Negations that flip a nearby success phrase ("did NOT pass", "tests do NOT pass").
_NEGATIONS = ("not ", "n't ", "no ", "never ", "fail", "without ", "couldn", "can't", "cannot", "isn't")


def claims_success(text: str) -> bool:
    """True if the text ASSERTS verified success. Skips a success phrase that is
    negated just before it ("tests do not pass", "did not produce 'no errors'") so a
    denial isn't read as a claim."""
    low = (text or "").lower()
    for c in _SUCCESS_CLAIMS:
        i = low.find(c)
        while i != -1:
            prefix = low[max(0, i - 24):i]   # the short run of words just before the phrase
            if not any(neg in prefix for neg in _NEGATIONS):
                return True                  # an un-negated success claim
            i = low.find(c, i + 1)
    return False


def success_claim_contradicted(text: str, artifacts) -> bool:
    """True when the text asserts verified success BUT the execution evidence shows
    a FAILURE — a provable contradiction (the tool_bypass / hallucinated-success
    class). Conservative: needs an explicit success claim AND a failing artifact, so
    'I wrote the function' (no claim) or a clean run never trips it."""
    return claims_success(text) and has_failing_evidence(artifacts)


def format_proof(artifacts, limit: int = 12) -> str:
    """Render artifacts as an EXECUTION-EVIDENCE block for a reviewer prompt. Empty
    string when there's nothing to show."""
    arts = list(artifacts or [])[:limit]
    if not arts:
        return ""
    mark = {"passed": "✓", "failed": "✗", "observed": "·"}
    lines = [f"  {mark.get(a.status, '·')} {a.summary}" for a in arts]
    return "EXECUTION EVIDENCE (ground your verdict in this, not just the summary):\n" + "\n".join(lines)
