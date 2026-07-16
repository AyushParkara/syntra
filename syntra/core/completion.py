"""Completion audit (the §14b.2 #10 mechanism; delivers req G1).

The strongest anti-"looks done" guard: before a task is marked complete, every
requirement must be backed by CONCRETE EVIDENCE. Intent, partial progress, and a
confident reviewer "pass" are NOT proof. Syntra's evidence-based completion gate:
a task is done only when its requirements are backed by observed artifacts.

Deterministic: requirements are derived from the typed task state (the plan,
step results, failures, repair steps, artifact verification) -- NOT from a model
opinion. So "done" means the structured evidence proves it.

Pure -> unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvidenceRef:
    """A concrete artifact that discharges a requirement — an applied edit (with its
    content hash), a run command, or a test — pulled from the engine's execution log."""

    kind: str          # "edit" | "command" | "test"
    ref: str           # path or command
    sha: str = ""      # content hash when available (edit.after_sha)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "sha": self.sha}


@dataclass
class Requirement:
    """One thing that must be true for the task to count as done.

    T7: `priority` ('must'|'nice') decides whether an unmet requirement BLOCKS shipping;
    `severity` ('high'|'med'|'low') grades a must-have failure — only HIGH blocks, MED/LOW
    are reported as known limitations. Defaults keep legacy behavior (must + high = blocking).
    """

    key: str
    description: str
    satisfied: bool
    evidence: str  # what proves it (or what's missing)
    priority: str = "must"   # must | nice
    severity: str = "high"   # high | med | low  (only meaningful when unmet)
    discharged_by: list = field(default_factory=list)  # list[EvidenceRef] — concrete artifacts

    def blocks(self) -> bool:
        """An unmet requirement blocks completion only when it's a MUST and HIGH severity.
        Met requirements never block; NICE items and MED/LOW must-failures don't block."""
        if self.satisfied:
            return False
        if self.priority == "nice":
            return False
        return self.severity == "high"

    def to_dict(self) -> dict:
        d = {"key": self.key, "description": self.description,
             "satisfied": self.satisfied, "evidence": self.evidence,
             "priority": self.priority, "severity": self.severity}
        if self.discharged_by:
            d["discharged_by"] = [r.to_dict() if hasattr(r, "to_dict") else r
                                  for r in self.discharged_by]
        return d


@dataclass
class CompletionAudit:
    requirements: list[Requirement] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Done when there is at least one requirement and NOTHING blocks (T7): all unmet
        MUST/HIGH requirements must be resolved. Unmet NICE items and MED/LOW must-failures
        are 'known limitations', not blockers — so we ship when the essentials are met."""
        return bool(self.requirements) and not any(r.blocks() for r in self.requirements)

    def unmet(self) -> list[Requirement]:
        return [r for r in self.requirements if not r.satisfied]

    def blockers(self) -> list[Requirement]:
        """Unmet requirements that actually block shipping (MUST + HIGH)."""
        return [r for r in self.requirements if r.blocks()]

    def known_limitations(self) -> list[Requirement]:
        """Unmet requirements that DON'T block — reported honestly as remaining/optional."""
        return [r for r in self.requirements if not r.satisfied and not r.blocks()]

    def to_dict(self) -> dict:
        return {
            "_schema_version": 2,
            "passed": self.passed,
            "requirements": [r.to_dict() for r in self.requirements],
        }

    def summary(self) -> str:
        met = sum(1 for r in self.requirements if r.satisfied)
        line = f"completion audit: {met}/{len(self.requirements)} requirements met"
        blk = self.blockers()
        if blk:
            line += " | BLOCKING: " + "; ".join(r.description for r in blk)
        lim = self.known_limitations()
        if lim:
            line += " | known limitations: " + "; ".join(r.description for r in lim)
        return line


def _execution_log(state) -> list:
    """Read execution evidence from in-memory state or ``execution_log.json`` on disk.

    Prefers a cached ``state.execution_log`` list; else loads the per-task log the engine
    writes (``append_execution_log``). Returns [] on any miss — evidence is a BONUS on top
    of the prose check, never a new failure mode."""
    cached = getattr(state, "execution_log", None)
    if cached is not None:
        return list(cached)
    task_dir = getattr(state, "task_dir", None)
    if task_dir is not None:
        try:
            import json
            from pathlib import Path
            path = Path(task_dir) / "execution_log.json"
            if path.is_file():
                doc = json.loads(path.read_text(encoding="utf-8"))
                return doc if isinstance(doc, list) else []
        except Exception:  # noqa: BLE001
            pass
    return []


# Log-entry kinds our engine writes (state.append_execution_log) that count as concrete
# discharge evidence. "edit_applied" is the applied-write entry; "verify_command" is only
# evidence when it actually PASSED (ok truthy) — a blocked/failed check is not proof.
_EVIDENCE_KINDS = frozenset({"edit_applied", "verify_command"})
_KIND_AS_REF = {"edit_applied": "edit", "verify_command": "command"}


def _evidence_for(log: list, step_id: str) -> list[EvidenceRef]:
    """Concrete artifacts in the execution log that discharge one step's requirement."""
    out: list[EvidenceRef] = []
    for e in log:
        if e.get("step_id") != step_id or e.get("kind") not in _EVIDENCE_KINDS:
            continue
        if e.get("kind") == "verify_command" and not e.get("ok"):
            continue   # a blocked/failed verify is NOT evidence of completion
        out.append(EvidenceRef(
            kind=_KIND_AS_REF.get(e["kind"], e["kind"]),
            ref=e.get("path") or e.get("command") or e.get("cmd", ""),
            sha=e.get("after_sha", ""),
        ))
    return out


def audit_completion(state, *, artifact_ok: bool | None = None) -> CompletionAudit:
    """Derive requirements from task state and check each has evidence.

    Requirements (all deterministic, evidence-based):
    - each plan step must be `done` with a non-empty result;
    - skipped/failed/pending steps are unmet (with the reason as evidence);
    - no pending repair step may remain;
    - if an artifact check ran, it must have passed.
    Accepts a TaskState (duck-typed .plan; steps have .id/.status/.result).
    """
    audit = CompletionAudit()
    plan = list(getattr(state, "plan", []) or [])
    log = _execution_log(state)

    for s in plan:
        prio = getattr(s, "priority", "must") or "must"   # T7: must|nice from the planner
        if s.status == "done":
            has_output = bool((s.result or "").strip())
            discharged = _evidence_for(log, s.id)
            audit.requirements.append(Requirement(
                key=f"step:{s.id}",
                description=f"step {s.id} produced a deliverable",
                satisfied=has_output,
                evidence=(f"{len(s.result)} chars of output" if has_output
                          else "status=done but result is EMPTY (no evidence)"),
                priority=prio,
                discharged_by=discharged,
            ))
        else:
            reason = s.failure_reason or f"status={s.status}"
            audit.requirements.append(Requirement(
                key=f"step:{s.id}",
                description=f"step {s.id} completed",
                satisfied=False,
                evidence=f"not done: {reason}",
                priority=prio,
            ))

    # No unresolved repair work may remain.
    pending_repairs = [s for s in plan if s.id.startswith("repair") and s.status == "pending"]
    if pending_repairs:
        audit.requirements.append(Requirement(
            key="repairs",
            description="no pending repair steps",
            satisfied=False,
            evidence=f"{len(pending_repairs)} repair step(s) still pending",
        ))

    # Artifact verification, if it ran, must have passed.
    if artifact_ok is not None:
        audit.requirements.append(Requirement(
            key="artifact",
            description="artifact verification passed",
            satisfied=bool(artifact_ok),
            evidence=("real check passed" if artifact_ok else "real check FAILED"),
        ))

    return audit
