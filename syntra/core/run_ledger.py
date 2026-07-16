"""Cross-run decision ledger (M4).

Durable FACTS (constraints/conventions/repo_map/architecture) already carry across separate
tasks via session-memory.json. What does NOT carry is the DECISION rationale + outcome of prior
tasks: a 2nd task on the same repo starts blind to "the last task chose HS256 for auth, and it
passed". This module appends a compact per-task record (goal, verdict, decisions) to one global
`run-ledger.jsonl` and renders a bounded, newest-first digest to inject into a NEW task's
analyzer/planner — so a follow-up task builds on what was already settled instead of re-deciding.

Distinct from:
- session-memory.json (durable declarative FACTS, always-injected) — this is the chronological
  DECISION/outcome ledger (role-model's DECISIONS.md idea, clean-room).
- per-task decisions.json (one task's choices) — this AGGREGATES across tasks.

Lock-guarded, atomic, append-only, bounded. Best-effort: never raises into a run.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .filelock import file_lock

# Keep the ledger bounded on disk (newest kept). A cross-run digest only ever needs the recent
# tail; an unbounded log would grow forever. Tunable.
_MAX_ENTRIES = 200
# Per-decision text clamp so one verbose run can't bloat the injected digest.
_DESC_CLAMP = 160
_RATIONALE_CLAMP = 200


@dataclass
class RunLedger:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path)

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> list[dict]:
        """Read the append-only JSONL, one record per line. A corrupt line is skipped, never
        fatal (the ledger is best-effort context, not authoritative state)."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
        except OSError:
            return []
        return out

    @staticmethod
    def _clean_decisions(decisions) -> list[dict]:
        """Normalize the caller's decisions into a bounded [{description, rationale}] list.
        Tolerates None / non-dict / missing keys (a run's post-processing must never crash)."""
        cleaned: list[dict] = []
        for d in (decisions or []):
            if not isinstance(d, dict):
                continue
            desc = str(d.get("description", "") or "").strip()[:_DESC_CLAMP]
            if not desc:
                continue
            cleaned.append({
                "description": desc,
                "rationale": str(d.get("rationale", "") or "").strip()[:_RATIONALE_CLAMP],
            })
        return cleaned

    def record(self, *, task_id: str, goal: str, verdict: str, decisions,
               ts: float | None = None) -> None:
        """Append one task's decision record to the ledger, under a cross-process lock, then
        trim to the newest `_MAX_ENTRIES`. Best-effort: swallows any error (never breaks a run)."""
        try:
            entry = {
                "ts": float(time.time() if ts is None else ts),
                "task_id": str(task_id or ""),
                "goal": str(goal or "").strip()[:_DESC_CLAMP],
                "verdict": str(verdict or "").strip(),
                "decisions": self._clean_decisions(decisions),
            }
            # Skip a truly empty record (no goal AND no decisions) — nothing to learn from.
            if not entry["goal"] and not entry["decisions"]:
                return
            with file_lock(self._lock_path()):
                entries = self._read()
                entries.append(entry)
                entries = entries[-_MAX_ENTRIES:]
                self._write(entries)
        except Exception:  # noqa: BLE001 - a learning write must never break a run
            pass

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per-process temp (a shared ".tmp" lets one os.replace steal another's — #209).
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                           encoding="utf-8")
            tmp.replace(self.path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def digest(self, *, max_runs: int = 5, exclude_task_id: str = "") -> str:
        """A compact, newest-first digest of recent tasks' goals + decisions (+ a FAIL flag) to
        inject into a new task so it starts informed, not blind. Empty string when there's no
        history. Bounded to `max_runs`. `exclude_task_id` drops that task's own records (so a
        re-plan/resume of the CURRENT task isn't fed its own decisions back)."""
        entries = self._read()
        if exclude_task_id:
            entries = [e for e in entries if str(e.get("task_id", "")) != str(exclude_task_id)]
        if not entries:
            return ""
        # F45: coerce the sort key defensively — a corrupt non-numeric `ts` must not raise
        # ValueError mid-sort (digest is read-side, not best-effort like the writers).
        def _ts(e):
            try:
                return float(e.get("ts", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        recent = sorted(entries, key=_ts, reverse=True)[:max(0, max_runs)]
        lines: list[str] = []
        for e in recent:
            verdict = str(e.get("verdict", "") or "")
            flag = " [FAILED — decisions unproven]" if verdict and verdict != "pass" else ""
            goal = str(e.get("goal", "") or "")
            lines.append(f"- Prior task: {goal}{flag}")
            for d in e.get("decisions", []) or []:
                if not isinstance(d, dict):
                    continue
                desc = str(d.get("description", "") or "")
                why = str(d.get("rationale", "") or "")
                lines.append(f"    · decided: {desc}" + (f" — because {why}" if why else ""))
        return "\n".join(lines)
