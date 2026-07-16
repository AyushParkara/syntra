"""Learned per-(model, capability-axis) ability delta θ (R15).

The IRT router scores a model's ability θ per skill from FROZEN benchmark numbers. This
module adds a learning loop on top: estimate a small, BOUNDED per-axis ability correction
from REAL run verdicts, attributed to the axes the task actually DEMANDED, so the router's
picture of "how good is this model at coding, really" sharpens from the operator's own work
instead of a possibly-saturated or stale public benchmark.

Design rules honored, by construction:
- **Attribution** : a verdict only touches the axes the task demanded (weighted by demand).
                    A passing code task never raises a model's `creativity` ability.
- **Bounded**     : the delta is clamped to ±ABILITY_DELTA_MAX on the 0..1 θ scale -- learned
                    data can only NUDGE the benchmark θ, never invent or destroy capability
                    (evidence still beats claims).
- **Sample-gated**: neutral (0.0) until min_samples observations on that (model, axis).
- **Confidence**  : the delta scales with evidence volume (saturating), like route stats.
- **Fresh**       : the delta decays toward 0 as evidence ages (half-life), like route stats.
- **Model-keyed** : keyed by model_id + axis (ABILITY, not per-provider -- provider speed /
                    reliability live in route stats). One model's ability is one thing.

Pure math + a small JSON store. Deterministic -> unit-tested.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

# Reuse the routing-stats learning constants so the whole learned layer is tuned coherently.
from .stats import DEFAULT_ALPHA, DEFAULT_MIN_SAMPLES, CONFIDENCE_HALF, FRESHNESS_HALFLIFE_S

# Max absolute ability correction on the 0..1 θ scale. Deliberately small: a fully-learned
# axis can shift θ by at most this, so a benchmark-strong model can't be buried (or a weak one
# elevated) by outcome noise -- the learned signal sharpens the frontier, never overrides it.
ABILITY_DELTA_MAX = 0.10


def _axis_key(model_id: str, axis: str) -> str:
    return f"{model_id}|{axis}"


@dataclass
class AxisRecord:
    """Rolling learned-ability stats for one (model, capability-axis)."""

    model_id: str
    axis: str
    samples: int = 0
    # EMA of demand-weighted verdicts, centered at 0: +1 == passed a fully-demanding task,
    # -1 == failed one. Starts at 0.0 (neutral: no correction to the benchmark θ).
    ema_signal: float = 0.0
    last_updated: float = 0.0

    def record(self, *, demand: float, success: bool,
               alpha: float = DEFAULT_ALPHA, now: float | None = None) -> None:
        # Signal weighted by how much THIS task demanded THIS axis: a task that barely needed
        # `code` moves the code ability barely; a code-dominated task moves it fully.
        d = max(0.0, min(1.0, float(demand)))
        if d <= 0:
            return
        target = d * (1.0 if success else -1.0)
        self.ema_signal = (1 - alpha) * self.ema_signal + alpha * target
        self.samples += 1
        self.last_updated = time.time() if now is None else now

    def delta(self, *, min_samples: int = DEFAULT_MIN_SAMPLES,
              now: float | None = None) -> float:
        """Bounded ability correction to ADD to the benchmark θ for this axis. 0.0 == neutral."""
        if self.samples < min_samples:
            return 0.0
        # tanh maps the unbounded EMA into (-1,1) smoothly, then scale to the ±MAX band.
        raw = math.tanh(self.ema_signal) * ABILITY_DELTA_MAX
        # Confidence (volume) + freshness (recency), same machinery as route stats.
        confidence = self.samples / (self.samples + CONFIDENCE_HALF)
        freshness = 1.0
        if self.last_updated > 0:
            ref = time.time() if now is None else now
            age = max(0.0, ref - self.last_updated)
            freshness = 0.5 ** (age / FRESHNESS_HALFLIFE_S)
        delta = raw * confidence * freshness
        return max(-ABILITY_DELTA_MAX, min(ABILITY_DELTA_MAX, delta))

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id, "axis": self.axis, "samples": self.samples,
            "ema_signal": round(self.ema_signal, 4), "last_updated": round(self.last_updated, 1),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AxisRecord":
        return cls(
            model_id=d["model_id"], axis=d["axis"],
            samples=int(d.get("samples", 0)),
            ema_signal=float(d.get("ema_signal", 0.0)),
            last_updated=float(d.get("last_updated", 0.0)),
        )


@dataclass
class AbilityStats:
    """Global per-(model, axis) learned-ability store (ability.json)."""

    path: Path
    records: dict[str, AxisRecord] = field(default_factory=dict)
    min_samples: int = DEFAULT_MIN_SAMPLES

    @classmethod
    def load(cls, path: Path | str, min_samples: int = DEFAULT_MIN_SAMPLES) -> "AbilityStats":
        p = Path(path)
        recs: dict[str, AxisRecord] = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                for key, d in (raw.get("axes", {}) or {}).items():
                    recs[key] = AxisRecord.from_dict(d)
            except Exception:
                recs = {}
        return cls(path=p, records=recs, min_samples=min_samples)

    def record_verdict(self, model_id: str, demands: dict, *, success: bool,
                       now: float | None = None) -> None:
        """Fold one run's verdict into every axis the task demanded, weighted by demand."""
        for axis, demand in (demands or {}).items():
            try:
                d = float(demand)
            except (TypeError, ValueError):
                continue
            if d <= 0:
                continue
            key = _axis_key(model_id, axis)
            rec = self.records.get(key)
            if rec is None:
                rec = AxisRecord(model_id=model_id, axis=axis)
                self.records[key] = rec
            rec.record(demand=d, success=success, now=now)

    def ability_delta(self, model_id: str, axis: str, *, now: float | None = None) -> float:
        """Bounded learned correction to add to this model's benchmark θ on `axis`.
        0.0 for an unseen (model, axis) or below min_samples."""
        rec = self.records.get(_axis_key(model_id, axis))
        return rec.delta(min_samples=self.min_samples, now=now) if rec else 0.0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"_schema_version": 1, "axes": {k: r.to_dict() for k, r in self.records.items()}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(self.path)
