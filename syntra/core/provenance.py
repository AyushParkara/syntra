"""Project typed task-state into git trailers so `git log --grep` becomes a
queryable memory of decisions and dead ends. Pure + deterministic.

The FORMAT is the Syntra user's choice (asked once, persisted) — the ``style`` arg:
  - "off"     → no trailers at all (a plain, intent-only commit message).
  - "minimal" → the key Decision line(s) only (no task-id, no dead-ends in history).
  - "neutral" → Task/Decision/Rejected, WITHOUT the "Syntra" brand word.
  - "branded" → Syntra-Task/Syntra-Decision/Syntra-Rejected.
Anything unrecognized is treated as "off" (safe: no trailers rather than a wrong format)."""
from __future__ import annotations

_MAX = 12  # cap trailers so commit bodies stay sane

# style -> (prefix, include_task, include_rejected). Decision is always included (it's the
# minimal signal); "minimal" drops task + dead-ends, the others keep them.
_STYLES = {
    "branded": ("Syntra-", True, True),
    "neutral": ("", True, True),
    "minimal": ("", False, False),
}


def _flat(s: str) -> str:
    return " ".join((s or "").split())[:200]


def commit_trailers(state, style: str = "neutral") -> str:
    """Render commit trailers for ``state`` in the user-chosen ``style`` ("" → empty)."""
    spec = _STYLES.get(style)
    if spec is None:                       # "off" or any unknown value → no trailers
        return ""
    prefix, include_task, include_rejected = spec
    lines: list[str] = []
    if include_task:
        lines.append(f"{prefix}Task: {getattr(state, 'task_id', '') or 'unknown'}")
    for d in (getattr(state, "decisions", []) or [])[:_MAX]:
        desc = _flat(getattr(d, "description", ""))
        if desc:
            lines.append(f"{prefix}Decision: {desc}")
    if include_rejected:
        seen: set[str] = set()
        for f in (getattr(state, "failures", []) or []):
            reason = _flat(getattr(f, "reason", ""))
            if reason and reason not in seen:
                seen.add(reason)
                lines.append(f"{prefix}Rejected: {reason}")
                if len(seen) >= _MAX:
                    break
    return "\n".join(lines)
