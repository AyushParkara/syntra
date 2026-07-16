"""Cross-task spend ledger + solvency helpers (D5)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .filelock import file_lock


@dataclass
class Ledger:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path)

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            return doc if isinstance(doc, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per-process temp name: a SHARED "<name>.tmp" let one process's
        # os.replace steal another's temp mid-write → FileNotFoundError crash (#209).
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def record(self, task_id: str, usd: float, *, ts: float | None = None) -> None:
        # #209: the whole read-modify-write is under a cross-process lock so two
        # concurrent CLIs can't both read-then-clobber (lost update).
        with file_lock(self._lock_path()):
            entries = self._read()
            entries.append({
                "ts": float(time.time() if ts is None else ts),
                "task_id": str(task_id),
                "usd": float(usd),
            })
            self._write(entries)

    def total_window(self, days: int = 30, *, now: float | None = None) -> float:
        now = float(time.time() if now is None else now)
        cutoff = now - float(days) * 86400.0
        total = 0.0
        for e in self._read():
            try:
                if float(e.get("ts", 0)) >= cutoff:
                    total += float(e.get("usd", 0) or 0)
            except Exception:  # noqa: BLE001
                continue
        return round(total, 6)


def solvency_check(remaining_budget: float, projected_cost: float) -> str:
    """Return 'ok' when we can afford the run, else 'rebid'."""
    try:
        if float(projected_cost) <= float(remaining_budget):
            return "ok"
    except Exception:  # noqa: BLE001
        return "ok"
    return "rebid"


# The on-disk ledger path — MUST match where the loop writes it (loop records to
# <state_root>/.syntra/spend.json). Reading any other name would summarize an empty ledger.
_LEDGER_REL = (".syntra", "spend.json")


def spend_report(state_root: Path | str, *, days: int = 30, now: float | None = None,
                 limit: int = 20) -> dict:
    """Summarize spend over a window from the on-disk ledger.

    Returns a JSON-serializable dict for CLI/TUI surfacing. Reads the SAME ledger the
    loop writes (``<state_root>/.syntra/spend.json``)."""
    root = Path(state_root)
    led = Ledger(root.joinpath(*_LEDGER_REL))
    now = float(time.time() if now is None else now)
    cutoff = now - float(days) * 86400.0
    entries = [e for e in led._read() if float(e.get("ts", 0) or 0) >= cutoff]
    by_task: dict[str, float] = {}
    for e in entries:
        tid = str(e.get("task_id", "") or "")
        if not tid:
            continue
        try:
            by_task[tid] = by_task.get(tid, 0.0) + float(e.get("usd", 0) or 0.0)
        except Exception:  # noqa: BLE001
            continue
    tasks = sorted(by_task.items(), key=lambda kv: kv[1], reverse=True)
    if limit and limit > 0:
        tasks = tasks[: int(limit)]

    # Best-effort hydrate with titles/goals.
    info: dict[str, dict] = {}
    try:
        from .state import TaskStore
        store = TaskStore(root)
        for tid, _usd in tasks:
            try:
                st = store.load(tid)
                info[tid] = {"title": st.title or "", "goal": (st.goal or "")[:160]}
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        info = {}

    total = 0.0
    for _tid, usd in by_task.items():
        total += float(usd)

    return {
        "_schema": "syntra.spend_report",
        "_schema_version": 1,
        "window_days": int(days),
        "cutoff_ts": cutoff,
        "now_ts": now,
        "total_usd": round(total, 6),
        "entries": len(entries),
        "tasks": [
            {"task_id": tid, "usd": round(usd, 6),
             **(info.get(tid, {}) if isinstance(info.get(tid, {}), dict) else {})}
            for tid, usd in tasks
        ],
    }
