"""A/B utilities (Phase 1): compare handoff modes without spending tokens.

Produces a report comparing how prior-step context is relayed into a step prompt
under two handoff modes: legacy truncation vs brief-mode handoff (see handoff.py +
the loop's `_build_executor_prompt` for the live equivalents this mirrors offline).
Purely derives the prompt-relevant text from typed state — NO model calls.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .handoff import ContextRequest, build_handoff
from .textutil import clip


@dataclass(frozen=True)
class ABResult:
    task_id: str
    step_id: str
    compared_at: float
    budget: int
    a_mode: str
    b_mode: str
    a_text: str
    b_text: str

    def to_dict(self) -> dict:
        return {
            "_schema": "syntra.ab_handoff",
            "_schema_version": 1,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "compared_at": self.compared_at,
            "budget": self.budget,
            "a_mode": self.a_mode,
            "b_mode": self.b_mode,
            "a_chars": len(self.a_text),
            "b_chars": len(self.b_text),
            "a_preview": clip(self.a_text, 1200),
            "b_preview": clip(self.b_text, 1200),
        }


def _prior_steps_for(state, step, *, context_relay: bool = True) -> list:
    done = [s for s in getattr(state, "plan", []) or []
            if getattr(s, "status", "") == "done" and getattr(s, "result", "")]
    if not done:
        return []
    if context_relay:
        dep_ids = list((getattr(step, "deps", []) or []))
        if dep_ids:
            return [s for s in done if s.id in dep_ids]
        return done[-1:]
    return done


def _render_prior(state, step, *, budget: int, mode: str) -> str:
    prior = _prior_steps_for(state, step, context_relay=True)
    if not prior:
        return ""
    if mode == "brief":
        brief = build_handoff(state, ContextRequest(needs=["deps"], budget=budget))
        return brief.render(budget)
    # truncate (legacy) — mirrors loop._build_executor_prompt's per-step slicing.
    parts: list[str] = []
    for s in prior:
        parts.append(f"--- {s.id}: {getattr(s, 'description', '')}")
        remaining = max(200, int(budget) // max(1, len(prior)))
        parts.append(clip(getattr(s, "result", "") or "", remaining))
        parts.append("")
    return "\n".join(parts).rstrip()


def compare_handoff(store, task_id: str, *, step_id: str | None = None,
                    budget: int = 12000, a_mode: str = "truncate", b_mode: str = "brief") -> ABResult:
    """Compare the prior-results relay under two modes and persist a report.

    Does NOT call models. Purely derives the prompt-relevant text from typed state.
    """
    state = store.load(task_id)
    step = None
    if step_id:
        step = next((s for s in state.plan if s.id == step_id), None)
    if step is None:
        step = next((s for s in state.plan if s.status in ("pending", "running", "failed", "skipped")), None)
    if step is None:
        step = state.plan[-1] if state.plan else None
    if step is None:
        raise ValueError(f"task {task_id} has no steps")

    a_text = _render_prior(state, step, budget=budget, mode=a_mode)
    b_text = _render_prior(state, step, budget=budget, mode=b_mode)
    out = ABResult(
        task_id=state.task_id,
        step_id=step.id,
        compared_at=time.time(),
        budget=int(budget),
        a_mode=a_mode,
        b_mode=b_mode,
        a_text=a_text,
        b_text=b_text,
    )
    try:
        path = Path(state.task_dir) / "ab_handoff.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass
    return out
