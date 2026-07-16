"""Frecency: usage-based ranking (frequency × recency) for the TUI pickers.

The models/files/commands you actually use should float to the top of their picker,
instead of a fixed alphabetical/intelligence order. We record a timestamp each time an
item is chosen and score it by summing time-decayed weights: many recent uses score
high, a single old use scores low, never-used items score 0. Persisted to
``.syntra/frecency.json`` so it survives across sessions.

Pure scoring + a thin JSON store -> unit-tested. No curses, no globals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Frecency:
    """A per-category usage log: ``events[key]`` is a list of selection timestamps."""

    events: dict = field(default_factory=dict)
    _MAX_EVENTS: int = 50      # cap history per key so the file/score stays bounded

    def record(self, key: str, now: float) -> None:
        """Log that ``key`` was chosen at time ``now``."""
        if not key:
            return
        lst = self.events.setdefault(key, [])
        lst.append(float(now))
        if len(lst) > self._MAX_EVENTS:
            del lst[0:len(lst) - self._MAX_EVENTS]

    @staticmethod
    def _weight(age: float) -> float:
        """Recency bucket weight (Mozilla-style): recent uses count for much more."""
        if age < 3600:        # last hour
            return 100.0
        if age < 86400:       # last day
            return 70.0
        if age < 604800:      # last week
            return 40.0
        if age < 2592000:     # last 30 days
            return 20.0
        return 5.0            # older — still a faint signal

    def score(self, key: str, now: float) -> float:
        """Frecency score: sum of recency-bucketed weights over the key's uses."""
        return sum(self._weight(max(0.0, now - ts)) for ts in self.events.get(key, []))

    def rank(self, items, now: float) -> list:
        """Items ordered by score (desc); ties + unseen keep their original order
        (stable sort), so unseen items fall to the end without being dropped."""
        return sorted(items, key=lambda it: -self.score(it, now))

    # ---- persistence --------------------------------------------------------
    def save(self, path: str | Path) -> None:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"events": self.events}), encoding="utf-8")
        except Exception:  # noqa: BLE001 - persistence is best-effort, never fatal
            pass

    @classmethod
    def load(cls, path: str | Path) -> "Frecency":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            events = raw.get("events", {}) if isinstance(raw, dict) else {}
            clean = {str(k): [float(t) for t in (v or [])]
                     for k, v in events.items() if isinstance(v, list)}
            return cls(events=clean)
        except Exception:  # noqa: BLE001 - a missing/corrupt file -> empty store
            return cls()
