"""Cross-run INCIDENT memory (M5 — the typed-memory taxonomy's clearest win).

Syntra records failures PER TASK (state.failures) and tracks repeated walls WITHIN one run
(loopguard.failure_signatures). What it lacked is a CROSS-RUN incidents shard: a recurring
failure SIGNATURE remembered across tasks, with how often it recurred and — once a later run
succeeds past it — what RESOLVED it. So a new task that hits the same wall gets "seen this N
times before; last time it was fixed by X" instead of re-discovering the fix from scratch.

This is the `incidents/` idea from the reference's memory taxonomy (recursive-mode), clean-room,
scoped to the highest-value shard. Composes with the M4 run-ledger (decisions) and M3 receipt.

Signature normalization is shared with loopguard (`_signature`) so a wall counted mid-run and a
wall remembered across runs are the SAME key. Lock+atomic append-only JSONL, bounded, pure recall.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .filelock import file_lock
from .loopguard import _signature   # reuse the exact (step,error) normalization for consistency

# Bound the store on disk (newest kept) — recall only needs recent recurring incidents; an
# unbounded failure log would grow forever. Tunable.
_MAX_ENTRIES = 300
_FIX_CLAMP = 240


def signature(step_id: str, error: str) -> str:
    """Stable normalized signature for a (step, error) pair — digits/whitespace stripped so
    transient specifics ('timeout after 30s' vs '45s') collapse to one incident. Shared with
    loopguard so within-run and cross-run counting agree. Pure."""
    return _signature(step_id, error)


@dataclass
class IncidentStore:
    path: Path

    def __post_init__(self):
        self.path = Path(self.path)

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> list[dict]:
        """Read the append-only JSONL (one incident record per line); corrupt lines skipped."""
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

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def _upsert(self, sig: str, mutate) -> None:
        """Read-modify-write the record for `sig` under a lock; `mutate(rec)` edits in place.
        Best-effort — an incident store failure must never break a run."""
        try:
            with file_lock(self._lock_path()):
                entries = self._read()
                rec = next((e for e in entries if e.get("sig") == sig), None)
                if rec is None:
                    rec = {"sig": sig, "count": 0, "fix": "", "first_ts": 0.0, "last_ts": 0.0,
                           "sample": ""}
                mutate(rec)
                # F43: keep the trim MRU — move the touched record to the end, then trim by
                # position. Otherwise `entries[-N:]` evicts the oldest-first-SEEN records, which
                # are exactly the long-standing, most-recurring incidents this store exists to
                # remember (and an update to an evicted-position record would be silently lost).
                entries = [e for e in entries if e is not rec]
                entries.append(rec)
                entries = entries[-_MAX_ENTRIES:]
                self._write(entries)
        except Exception:  # noqa: BLE001 - never break a run on incident bookkeeping
            pass

    def record(self, *, step_id: str, error: str, task_id: str = "",
               ts: float | None = None) -> None:
        """Note that a failure signature occurred (this run). Increments its cross-run count.
        Empty step+error is a no-op (nothing to remember)."""
        if not (step_id or "").strip() and not (error or "").strip():
            return
        sig = signature(step_id, error)
        now = float(time.time() if ts is None else ts)

        def _m(rec):
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last_ts"] = now
            if not rec.get("first_ts"):
                rec["first_ts"] = now
            if not rec.get("sample"):
                rec["sample"] = (error or "").strip()[:_FIX_CLAMP]
        self._upsert(sig, _m)

    def resolve(self, *, step_id: str, error: str, fix: str, ts: float | None = None) -> None:
        """Record what RESOLVED a signature (a later run got past it), so the next recurrence
        surfaces the known fix. Creates the record if the failure wasn't previously seen."""
        if not (step_id or "").strip() and not (error or "").strip():
            return  # F44: no signature to resolve — don't create a junk "|" record
        sig = signature(step_id, error)
        now = float(time.time() if ts is None else ts)

        def _m(rec):
            rec["fix"] = (fix or "").strip()[:_FIX_CLAMP]
            rec["resolved_ts"] = now
            if not rec.get("sample"):
                rec["sample"] = (error or "").strip()[:_FIX_CLAMP]
        self._upsert(sig, _m)

    def recall(self, *, step_id: str, error: str) -> dict | None:
        """Return the incident record for this (step, error) if it's been seen before, else None.
        `{sig, count, fix, sample, first_ts, last_ts}`. Pure read (no lock needed)."""
        if not (step_id or "").strip() and not (error or "").strip():
            return None
        sig = signature(step_id, error)
        for rec in self._read():
            if rec.get("sig") == sig:
                return rec
        return None

    def digest(self, *, min_count: int = 2, max_items: int = 5) -> str:
        """A compact digest of the most-recurring incidents (count >= min_count), newest-first,
        with their known fix. Injectable into a new task so it avoids re-hitting a known wall.
        Empty string when nothing recurred that often."""
        # F45: coerce defensively — a corrupt/externally-written line with a non-numeric count/ts
        # must not raise ValueError mid-sort (digest is read-side; unlike the writers it wasn't
        # best-effort). _n() maps anything unparseable to 0.
        def _n(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        recurring = [e for e in self._read() if _n(e.get("count", 0)) >= min_count]
        recurring.sort(key=lambda e: (_n(e.get("count", 0)), _n(e.get("last_ts", 0))),
                       reverse=True)
        lines: list[str] = []
        for e in recurring[:max(0, max_items)]:
            sample = str(e.get("sample", "") or e.get("sig", ""))
            n = int(_n(e.get("count", 0)))
            fix = str(e.get("fix", "") or "")
            lines.append(f"- recurring failure (seen {n}×): {sample[:120]}"
                         + (f" — known fix: {fix}" if fix else " — no fix recorded yet"))
        return "\n".join(lines)
