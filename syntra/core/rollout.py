"""Replayable session rollout + branching.

Every run already appends typed events to `.syntra/tasks/<id>/events.jsonl`. This
module turns that append-only log into a first-class, replayable rollout:

- read(task_id)        -> the ordered list of RolloutEvents for a task
- replay(task_id)      -> a human-readable reconstruction of what happened
- branch(task_id, at)  -> fork a NEW task whose events are this task's events
                          truncated at index `at` (so you can re-run from a point,
                          keeping the history up to the fork)

Branching copies the prefix of the event log + the task.json/plan.json snapshot
into a fresh task id and records the lineage (`branched_from`, `branched_at`), so
sessions form a tree you can explore. Pure-ish (file I/O over the state dir);
unit-tested against a temp state root.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RolloutEvent:
    index: int
    ts: float
    kind: str
    payload: dict


def _events_path(state_root: Path | str, task_id: str) -> Path:
    return Path(state_root) / "tasks" / task_id / "events.jsonl"


def _parse_event(line: str, index: int) -> RolloutEvent | None:
    """Parse one JSONL line into a RolloutEvent, or None if blank/corrupt."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return RolloutEvent(index=index, ts=float(obj.get("ts", 0.0)),
                        kind=obj.get("kind", "?"), payload=obj.get("payload", {}) or {})


def read(state_root: Path | str, task_id: str, *, max_events: int | None = None) -> list[RolloutEvent]:
    """Read a task's rollout as ordered RolloutEvents (empty if none).

    #208: STREAMS the file line-by-line — it never materializes the whole
    transcript as one string, so a multi-GB log can't blow up memory. Pass
    ``max_events`` to stop after the first N events (bounded head read); ``None``
    reads all. A corrupt/blank line is skipped, not fatal (append-only: only the
    tail can tear)."""
    p = _events_path(state_root, task_id)
    if not p.exists():
        return []
    cap = None if max_events is None else max(0, int(max_events))
    if cap == 0:
        return []
    out: list[RolloutEvent] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:                      # iterate lines: O(1) memory, not the whole file
            ev = _parse_event(line, len(out))
            if ev is None:
                continue
            out.append(ev)
            if cap is not None and len(out) >= cap:
                break
    return out


def _tail_lines(p: Path, n: int, *, block: int = 65536) -> list[str]:
    """Return roughly the last `n` non-empty lines of a file via a bounded
    reverse read — reads only the trailing bytes, never the whole file."""
    with p.open("rb") as f:
        f.seek(0, 2)                        # end
        size = f.tell()
        want = n + 1                        # one extra: the run before the first kept line
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= want:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines[-n:] if n > 0 else []


def read_tail(state_root: Path | str, task_id: str, *, max_events: int = 200) -> list[RolloutEvent]:
    """The LAST `max_events` rollout events, via a seek-based tail read (#208).

    For "show recent activity" displays on a huge transcript: reads only the
    trailing bytes instead of the whole file. ``index`` is the event's position
    within the returned tail window (0-based), not its absolute rollout index —
    use :func:`read` when you need absolute coordinates."""
    p = _events_path(state_root, task_id)
    if not p.exists():
        return []
    n = max(0, int(max_events))
    if n == 0:
        return []
    out: list[RolloutEvent] = []
    for line in _tail_lines(p, n):
        ev = _parse_event(line, len(out))
        if ev is not None:
            out.append(ev)
    return out


def replay(state_root: Path | str, task_id: str) -> str:
    """A readable reconstruction of a task's timeline (kind + a compact payload)."""
    evs = read(state_root, task_id)
    if not evs:
        return f"(no rollout for task {task_id})"
    lines = [f"rollout for {task_id} — {len(evs)} events"]
    for e in evs:
        summary = ", ".join(f"{k}={_short(v)}" for k, v in list(e.payload.items())[:4])
        lines.append(f"  #{e.index:>3} {e.kind:<18} {summary}")
    return "\n".join(lines)


def _short(v) -> str:
    s = str(v)
    return (s[:60] + "…") if len(s) > 60 else s


def branch(state_root: Path | str, task_id: str, at: int) -> str:
    """Fork a new task from `task_id`, keeping events [0:at]. Returns the new id.

    Copies the truncated event log + the task/plan snapshots into a fresh task id,
    recording lineage so the rollout forms a tree. `at` is clamped to the log size;
    `at <= 0` means branch from the very start (goal only)."""
    src = read(state_root, task_id)
    n = max(0, min(int(at), len(src)))
    new_id = uuid.uuid4().hex[:12]
    src_dir = Path(state_root) / "tasks" / task_id
    dst_dir = Path(state_root) / "tasks" / new_id
    dst_dir.mkdir(parents=True, exist_ok=True)

    # 1) truncated event log
    with (dst_dir / "events.jsonl").open("w") as f:
        for e in src[:n]:
            f.write(json.dumps({"ts": e.ts, "kind": e.kind, "payload": e.payload}) + "\n")
        f.write(json.dumps({"ts": time.time(), "kind": "branch_created",
                            "payload": {"from": task_id, "at": n}}) + "\n")

    # 2) carry the task.json snapshot (goal/workspace) + plan, tagging lineage
    task_json = _read_json(src_dir / "task.json")
    if task_json:
        task_json["task_id"] = new_id
        task_json["branched_from"] = task_id
        task_json["branched_at"] = n
        task_json["status"] = "branched"
        _write_json(dst_dir / "task.json", task_json)
    for fname in ("plan.json", "decisions.json", "summary.json"):
        snap = _read_json(src_dir / fname)
        if snap is not None:
            _write_json(dst_dir / fname, snap)
    return new_id


def report(state_root: Path | str, task_id: str, *, limit: int = 500) -> dict:
    """Machine-readable rollout report (JSON-serializable) for `/replay`.

    #208: bounds the read at the source (`max_events=limit`) so a huge transcript
    is never fully materialized just to keep the first `limit` events."""
    evs = read(state_root, task_id, max_events=limit if (limit and limit > 0) else None)
    return {
        "_schema": "syntra.replay_report",
        "_schema_version": 1,
        "task_id": str(task_id),
        "events": [
            {
                "index": int(e.index),
                "ts": float(e.ts),
                "kind": str(e.kind),
                "payload": {k: (_short(v) if not isinstance(v, (dict, list)) else v)
                            for k, v in (e.payload or {}).items()},
            }
            for e in evs
        ],
        "event_count": len(evs),
    }


def lineage(state_root: Path | str, task_id: str) -> list[str]:
    """Walk branched_from links from this task back to its root. Returns ids root->task."""
    chain = [task_id]
    seen = {task_id}
    cur = task_id
    while True:
        tj = _read_json(Path(state_root) / "tasks" / cur / "task.json") or {}
        parent = tj.get("branched_from")
        if not parent or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        cur = parent
    return list(reversed(chain))


def _read_json(p: Path):
    try:
        return json.loads(p.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
