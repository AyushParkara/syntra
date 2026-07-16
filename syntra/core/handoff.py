"""Consumer-pull handoff: assemble typed context briefs from task state."""
from __future__ import annotations

from dataclasses import dataclass, field

from .textutil import clip


@dataclass
class ContextRequest:
    needs: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    budget: int = 4000


@dataclass
class BriefAtom:
    text: str
    source_ref: str
    claim_type: str
    confidence: float = 0.8

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source_ref": self.source_ref,
            "claim_type": self.claim_type,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BriefAtom":
        return cls(
            text=str(d.get("text", "") or ""),
            source_ref=str(d.get("source_ref", "") or ""),
            claim_type=str(d.get("claim_type", "") or ""),
            confidence=float(d.get("confidence", 0.8) or 0.8),
        )


@dataclass
class ContextBrief:
    atoms: list[BriefAtom] = field(default_factory=list)

    def render(self, budget: int) -> str:
        lines: list[str] = []
        total = 0
        for atom in self.atoms:
            line = f"[{atom.claim_type}] {atom.text} ({atom.source_ref})"
            if total and total + len(line) + 1 > budget:
                break
            if len(line) > budget:
                line = clip(line, budget)
            lines.append(line)
            total += len(line) + (1 if lines else 0)
        return "\n".join(lines)

    def to_pack(self) -> dict:
        return {
            "_schema": "syntra.context_brief",
            "_schema_version": 1,
            "atoms": [a.to_dict() for a in self.atoms],
        }

    @classmethod
    def from_pack(cls, pack: dict) -> "ContextBrief":
        atoms = pack.get("atoms", []) if isinstance(pack, dict) else []
        if not isinstance(atoms, list):
            atoms = []
        return cls(atoms=[BriefAtom.from_dict(a) for a in atoms if isinstance(a, dict)])


def _clamp_confidence(refined: ContextBrief, source: ContextBrief) -> ContextBrief:
    """D3: confidence never increases across a distillation hop."""
    cap: dict[tuple[str, str], float] = {}
    for a in source.atoms:
        cap[(a.source_ref, a.claim_type)] = max(cap.get((a.source_ref, a.claim_type), 0.0), a.confidence)
    for a in refined.atoms:
        lim = cap.get((a.source_ref, a.claim_type))
        if lim is not None:
            a.confidence = min(a.confidence, lim)
    return refined


def _atoms_from_failures(state) -> list[BriefAtom]:
    out: list[BriefAtom] = []
    for f in getattr(state, "failures", []) or []:
        reason = (getattr(f, "reason", "") or "").strip()
        if not reason:
            continue
        sid = getattr(f, "step_id", "") or "?"
        out.append(BriefAtom(text=reason, source_ref=f"failure:{sid}",
                             claim_type="rejected", confidence=0.9))
    return out


def _atoms_from_decisions(state) -> list[BriefAtom]:
    out: list[BriefAtom] = []
    for d in getattr(state, "decisions", []) or []:
        desc = (getattr(d, "description", "") or "").strip()
        if not desc:
            continue
        did = getattr(d, "id", "") or "?"
        out.append(BriefAtom(text=desc, source_ref=f"decision:{did}",
                             claim_type="decision", confidence=0.85))
    return out


def _atoms_from_deps(state) -> list[BriefAtom]:
    out: list[BriefAtom] = []
    for s in getattr(state, "plan", []) or []:
        if getattr(s, "status", "") != "done":
            continue
        result = (getattr(s, "result", "") or "").strip()
        if not result:
            continue
        sid = getattr(s, "id", "") or "?"
        out.append(BriefAtom(text=clip(result, 500), source_ref=f"step:{sid}",
                             claim_type="deliverable", confidence=0.8))
    return out


_BUILDERS = {
    "failures": _atoms_from_failures,
    "decisions": _atoms_from_decisions,
    "deps": _atoms_from_deps,
    "results": _atoms_from_deps,
}


def build_handoff(state, request: ContextRequest, *, distiller=None) -> ContextBrief:
    """Deterministic assembler: answer ONLY what ``request.needs`` asks for."""
    needs = [n for n in (request.needs or []) if n not in (request.forbid or ())]
    atoms: list[BriefAtom] = []
    for need in needs:
        builder = _BUILDERS.get(need)
        if builder is not None:
            atoms.extend(builder(state))
    brief = ContextBrief(atoms=atoms)
    if distiller is not None:
        # D2: never-stale memoization keyed by a typed fingerprint.
        try:
            from .brief_cache import BriefCache, key as cache_key
            from .context import _sha256
            from pathlib import Path
            import json

            task_dir = getattr(state, "task_dir", None)
            if task_dir is not None:
                cache = BriefCache(Path(task_dir) / "brief_cache")
                fp = _sha256(json.dumps({
                    "decisions": [getattr(d, "description", "") for d in getattr(state, "decisions", []) or []],
                    "failures": [getattr(f, "reason", "") for f in getattr(state, "failures", []) or []],
                    "done": [(getattr(s, "id", ""), getattr(s, "result", ""))
                             for s in getattr(state, "plan", []) or []
                             if getattr(s, "status", "") == "done"],
                }, ensure_ascii=False, sort_keys=True))
                rk = _sha256(json.dumps({
                    "needs": list(request.needs or []),
                    "forbid": list(request.forbid or []),
                    "budget": int(getattr(request, "budget", 0) or 0),
                }, ensure_ascii=False, sort_keys=True))
                ck = cache_key(fingerprint=fp, consumer="handoff", request_key=rk)
                hit = cache.get(ck)
                if hit:
                    return ContextBrief.from_pack(hit)
                refined = distiller(brief, state, request)
                if isinstance(refined, ContextBrief):
                    refined = _clamp_confidence(refined, brief)
                    cache.put(ck, refined.to_pack())
                    return refined
                return brief
        except Exception:  # noqa: BLE001 — degrade to deterministic floor
            pass
        try:
            refined = distiller(brief, state, request)
            if isinstance(refined, ContextBrief):
                return _clamp_confidence(refined, brief)
        except Exception:  # noqa: BLE001 — degrade to deterministic floor
            pass
    return brief
