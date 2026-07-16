"""Per-run receipt (M3).

Syntra records a run in MACHINE form (decisions.json, cost.json, failures.json, the completion
audit). What it lacked is a single HUMAN/agent-readable "what happened + why + what's left"
artifact — a receipt you (or a follow-up run) can read to understand a run without replaying its
event log. `render_receipt` builds that markdown DETERMINISTICALLY from typed state: goal, the
verdict, the decisions WITH their rationale, the requirement dispositions with concrete evidence,
cost, and the honest known-limitations / follow-ups.

Pure (no clock, no I/O, no randomness) -> byte-identical for the same state -> unit-tested. The
caller writes it to <task_dir>/receipt.md and may also feed its deltas into the cross-run ledger.
"""

from __future__ import annotations


def _fmt_evidence(reqs) -> list[str]:
    """Concrete artifacts that discharged each MET requirement (edit path+sha / command / test)."""
    lines: list[str] = []
    for r in reqs:
        if not getattr(r, "satisfied", False):
            continue
        arts = getattr(r, "discharged_by", None) or []
        for a in arts:
            kind = getattr(a, "kind", "") or (a.get("kind", "") if isinstance(a, dict) else "")
            ref = getattr(a, "ref", "") or (a.get("ref", "") if isinstance(a, dict) else "")
            sha = getattr(a, "sha", "") or (a.get("sha", "") if isinstance(a, dict) else "")
            if ref:
                tag = f" ({sha[:12]})" if sha else ""
                lines.append(f"  - {kind or 'artifact'}: {ref}{tag}")
    return lines


def render_receipt(state, audit, *, verdict: str = "") -> str:
    """Render a per-run receipt.md from typed state + the completion audit. Pure + deterministic.

    Never raises on a sparse/empty run — every section is optional and simply omitted when there's
    nothing to show (except the header, which always renders)."""
    goal = (getattr(state, "goal", "") or "").strip()
    lines: list[str] = []
    # Header — always present so an empty run still yields a valid receipt.
    lines.append(f"# Run receipt — {goal or '(no goal)'}")
    v = (verdict or "").strip()
    if v:
        flag = "✅ passed" if v == "pass" else f"⚠️ {v}"
        lines.append(f"\n**Outcome:** {flag}")

    # Decisions + WHY (the rationale is the part machine logs bury).
    decisions = list(getattr(state, "decisions", []) or [])
    if decisions:
        lines.append("\n## Decisions")
        for d in decisions:
            desc = (getattr(d, "description", "") or "").strip()
            why = (getattr(d, "rationale", "") or "").strip()
            if not desc:
                continue
            lines.append(f"- {desc}" + (f" — _{why}_" if why else ""))

    # Requirement dispositions: met (with evidence), blockers, known limitations.
    reqs = list(getattr(audit, "requirements", []) or [])
    if reqs:
        met = [r for r in reqs if getattr(r, "satisfied", False)]
        lines.append(f"\n## Requirements ({len(met)}/{len(reqs)} met)")
        lines.extend(f"- ✅ {getattr(r, 'description', '')}" for r in met)
        ev = _fmt_evidence(reqs)
        if ev:
            lines.append("\n### Evidence")
            lines.extend(ev)
        blockers = audit.blockers() if hasattr(audit, "blockers") else []
        if blockers:
            lines.append("\n## Blockers (why it did NOT ship)")
            lines.extend(f"- ❌ {getattr(r, 'description', '')} — {getattr(r, 'evidence', '')}" for r in blockers)
        limits = audit.known_limitations() if hasattr(audit, "known_limitations") else []
        if limits:
            lines.append("\n## Known limitations / follow-ups")
            lines.extend(f"- ⬜ {getattr(r, 'description', '')} — {getattr(r, 'evidence', '')}" for r in limits)

    # Cost — a plain honest number from the typed cost entries.
    try:
        total = float(state.total_cost_usd()) if hasattr(state, "total_cost_usd") else 0.0
    except Exception:  # noqa: BLE001 - a receipt must never crash on cost accounting
        total = 0.0
    if total > 0:
        lines.append(f"\n## Cost\n- ${total:.4f} total")

    return "\n".join(lines) + "\n"
