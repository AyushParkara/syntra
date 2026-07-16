"""Learned route quality feedback (Phase 4).

Routing today scores models from the static catalog. This module adds the
missing loop: feed ACTUAL outcomes (did the run pass review? cost? latency?
truncation?) back per (role, provider, model), and blend a bounded historical
quality factor into future scoring -- so a route that keeps passing rises, and
one that keeps failing falls, regardless of its catalog numbers.

Anti-overfitting guards (PLAN Phase 4 risk):
- min_samples gate : the factor is NEUTRAL (1.0) until enough observations.
- bounded          : the factor is clamped to a narrow band; it nudges, never
                     dominates the capability score.
- decayed          : success is tracked as an EMA, so recent outcomes matter
                     more and stale data fades (a route that recovered isn't
                     punished forever).

Pure math + a small JSON store. Deterministic -> unit-tested.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path


# Tunables (documented; conservative by design).
DEFAULT_ALPHA = 0.3            # EMA weight for the newest outcome
DEFAULT_MIN_SAMPLES = 3        # observations required before the factor bites
FACTOR_SPAN = 0.30            # ema 0..1 maps to factor [1-span/2, 1+span/2]
FACTOR_MIN = 1.0 - FACTOR_SPAN / 2  # 0.85
FACTOR_MAX = 1.0 + FACTOR_SPAN / 2  # 1.15
# R6: truncated output is degraded output even when the run passes review. A route that
# keeps getting cut off is docked proportionally to its truncation RATE, up to this cap.
# Bounded + rate-scaled -> a rare truncation barely nudges; chronic truncation demotes the
# route within the same [FACTOR_MIN, FACTOR_MAX] band. Tunable, not a baked decision.
TRUNCATION_PENALTY_MAX = 0.15  # max multiplicative dock at a 100% truncation rate
# R7: confidence weighting. min_samples is a hard gate (below it -> neutral); ABOVE it we
# still shouldn't trust 3 samples as much as 300. Scale how far the factor departs from
# neutral by a saturating confidence = samples / (samples + CONFIDENCE_HALF). At
# samples==CONFIDENCE_HALF the departure is at half strength; it approaches full only with
# ample evidence. This makes observed outcomes DISPLACE the neutral prior in proportion to
# evidence volume (role-model's observed-over-declared discipline) without a cliff. Tunable.
CONFIDENCE_HALF = 12.0
# R7b: freshness. Learned evidence should age -- a route proven months ago is less trustworthy
# than one proven this week (models change, providers degrade, quotas shift). The
# departure-from-neutral is scaled by a half-life decay of the AGE of the last observation:
# weight = 0.5 ** (age_seconds / FRESHNESS_HALFLIFE_S). At one half-life old the learned signal
# is trusted half as much, converging to the neutral prior (1.0) as it goes fully stale.
# 14 days: long enough that active routes stay fully trusted, short enough that abandoned
# evidence fades within a few weeks. Tunable, not a baked decision.
FRESHNESS_HALFLIFE_S = 14 * 24 * 3600.0  # 14 days in seconds
# Observed-speed factor band. DELIBERATELY NARROWER than FACTOR_SPAN (accuracy) so speed can
# never override accuracy — a route that is "fast but wrong" is worthless. A route earns
# speed credit ONLY in proportion to how much it's also SUCCEEDING
# (accuracy gate), measured as tokens/sec vs the model's declared speed (un-confounded by
# answer length). ±SPEED_SPAN/2 around neutral. Tunable, not a baked decision.
SPEED_SPAN = 0.12  # -> speed factor in [0.94, 1.06], strictly inside the [0.85,1.15] accuracy band
# R11: cache-read tokens bill at ~this fraction of full input price (the de-facto standard for
# prompt caching). A route's effective input price = (1-ratio)*full + ratio*this. Tunable.
CACHE_READ_PRICE_MULT = 0.10


def _route_key(role: str, provider: str, model_id: str) -> str:
    return f"{role}|{provider}|{model_id}"


@dataclass
class RouteRecord:
    """Rolling outcome stats for one (role, provider, model) route."""

    role: str
    provider: str
    model_id: str
    samples: int = 0
    ema_success: float = 0.5      # neutral prior
    pass_count: int = 0
    fail_count: int = 0
    truncations: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    total_output_tokens: int = 0  # observed-latency profile: for tokens/sec (speed) computation
    total_input_tokens: int = 0        # R11: for observed cache-hit ratio
    total_cache_read_tokens: int = 0   # R11: cached (cheap) input tokens seen on this route
    last_updated: float = 0.0     # R7b: epoch seconds of the most recent observation (0 = unknown)

    def record(self, *, success: bool, cost_usd: float = 0.0,
               latency_ms: float = 0.0, truncated: bool = False,
               output_tokens: int = 0, input_tokens: int = 0,
               cache_read_tokens: int = 0,
               alpha: float = DEFAULT_ALPHA, now: float | None = None) -> None:
        self.ema_success = (1 - alpha) * self.ema_success + alpha * (1.0 if success else 0.0)
        self.samples += 1
        if success:
            self.pass_count += 1
        else:
            self.fail_count += 1
        if truncated:
            self.truncations += 1
        self.total_cost_usd = round(self.total_cost_usd + max(0.0, cost_usd), 6)
        self.total_latency_ms += max(0.0, latency_ms)
        self.total_output_tokens += max(0, output_tokens)
        self.total_input_tokens += max(0, input_tokens)
        self.total_cache_read_tokens += max(0, cache_read_tokens)
        self.last_updated = time.time() if now is None else now  # R7b: stamp recency

    def observed_cache_ratio(self) -> float:
        """R11: fraction of this route's input tokens served cheap from prompt cache =
        cached-read tokens / total input tokens. High ratio -> the route's EFFECTIVE input
        price is far below sticker (cache reads bill ~10%). 0.0 with no input data."""
        return (self.total_cache_read_tokens / self.total_input_tokens
                if self.total_input_tokens > 0 else 0.0)

    def observed_tps(self) -> float:
        """Measured tokens/sec for this route = total output tokens / total latency seconds.
        This is the UN-CONFOUNDED speed signal: raw latency alone would punish a model that
        merely writes longer answers; per-token throughput is directly comparable to the
        catalog's declared speed_tps. 0.0 when there's no timing/token data yet."""
        secs = self.total_latency_ms / 1000.0
        return (self.total_output_tokens / secs) if secs > 0 else 0.0

    def speed_factor(self, *, declared_tps: float,
                     min_samples: int = DEFAULT_MIN_SAMPLES,
                     now: float | None = None) -> float:
        """Bounded speed multiplier from OBSERVED tokens/sec vs the model's DECLARED speed.

        Load-bearing rule: speed is meaningless without accuracy — a route that is "fast but
        wrong" must NOT be rewarded. So the speed deviation is
        GATED by an accuracy weight = max(0, ema_success-0.5)*2 (0 when a route is failing,
        1 when it's near-perfect). A fast route that keeps failing earns ZERO speed credit.
        Also scaled by confidence (volume) and freshness (recency), like the quality factor.
        Bounded to the NARROW [1-SPEED_SPAN/2, 1+SPEED_SPAN/2] band that sits strictly inside
        the accuracy band, so accuracy always dominates on conflict. Neutral 1.0 without
        enough evidence / no timing data / unknown declared speed.
        """
        if self.samples < min_samples or declared_tps <= 0:
            return 1.0
        obs = self.observed_tps()
        if obs <= 0:
            return 1.0
        # log2(observed/declared): +1 == 2x faster than claimed, -1 == 2x slower. Clamp ±1.
        ratio_signal = max(-1.0, min(1.0, math.log2(obs / declared_tps)))
        # Accuracy gate: no credit unless the route is actually succeeding.
        accuracy_gate = max(0.0, (self.ema_success - 0.5) * 2.0)
        # Confidence (volume) + freshness (recency), same machinery as quality_factor.
        confidence = self.samples / (self.samples + CONFIDENCE_HALF)
        freshness = 1.0
        if self.last_updated > 0:
            ref = time.time() if now is None else now
            age = max(0.0, ref - self.last_updated)
            freshness = 0.5 ** (age / FRESHNESS_HALFLIFE_S)
        deviation = ratio_signal * (SPEED_SPAN / 2.0) * accuracy_gate * confidence * freshness
        factor = 1.0 + deviation
        lo, hi = 1.0 - SPEED_SPAN / 2.0, 1.0 + SPEED_SPAN / 2.0
        return max(lo, min(hi, factor))

    def pass_rate(self) -> float:
        total = self.pass_count + self.fail_count
        return (self.pass_count / total) if total else 0.0

    def avg_cost(self) -> float:
        return round(self.total_cost_usd / self.samples, 6) if self.samples else 0.0

    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / self.samples, 1) if self.samples else 0.0

    def quality_factor(self, *, min_samples: int = DEFAULT_MIN_SAMPLES,
                       now: float | None = None) -> float:
        """Bounded, sample-gated multiplier for routing. 1.0 == neutral.

        Blends learned signals, all bounded to the same [FACTOR_MIN, FACTOR_MAX] band:
          - success EMA (a passing route rises, a failing one falls);
          - truncation RATE (R6: a route that keeps getting cut off is docked, because
            truncated output is degraded output even when the run ultimately passes);
          - evidence VOLUME (R7: departure from neutral scaled by confidence);
          - evidence RECENCY (R7b: departure from neutral decayed by freshness half-life,
            so stale proof converges back to the neutral prior).
        """
        if self.samples < min_samples:
            return 1.0
        factor = 1.0 + (self.ema_success - 0.5) * FACTOR_SPAN
        # R6: dock proportionally to how often this route truncated.
        trunc_rate = self.truncations / self.samples if self.samples else 0.0
        factor *= (1.0 - TRUNCATION_PENALTY_MAX * trunc_rate)
        # R7: trust the departure-from-neutral in proportion to evidence volume, so thin
        # data can't swing the pick as hard as a well-sampled route with the same rates.
        confidence = self.samples / (self.samples + CONFIDENCE_HALF)
        # R7b: age the departure-from-neutral by a half-life decay so stale evidence fades
        # back toward the neutral prior. Unknown timestamp (legacy row) -> no decay.
        freshness = 1.0
        if self.last_updated > 0:
            ref = time.time() if now is None else now
            age = max(0.0, ref - self.last_updated)
            freshness = 0.5 ** (age / FRESHNESS_HALFLIFE_S)
        factor = 1.0 + (factor - 1.0) * confidence * freshness
        return max(FACTOR_MIN, min(FACTOR_MAX, factor))

    def to_dict(self) -> dict:
        return {
            "role": self.role, "provider": self.provider, "model_id": self.model_id,
            "samples": self.samples, "ema_success": round(self.ema_success, 4),
            "pass_count": self.pass_count, "fail_count": self.fail_count,
            "truncations": self.truncations,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "total_output_tokens": self.total_output_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "last_updated": round(self.last_updated, 1),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RouteRecord":
        return cls(
            role=d["role"], provider=d["provider"], model_id=d["model_id"],
            samples=int(d.get("samples", 0)),
            ema_success=float(d.get("ema_success", 0.5)),
            pass_count=int(d.get("pass_count", 0)),
            fail_count=int(d.get("fail_count", 0)),
            truncations=int(d.get("truncations", 0)),
            total_cost_usd=float(d.get("total_cost_usd", 0.0)),
            total_latency_ms=float(d.get("total_latency_ms", 0.0)),
            total_output_tokens=int(d.get("total_output_tokens", 0)),
            total_input_tokens=int(d.get("total_input_tokens", 0)),
            total_cache_read_tokens=int(d.get("total_cache_read_tokens", 0)),
            last_updated=float(d.get("last_updated", 0.0)),
        )


@dataclass
class RouteStats:
    """Global per-route outcome store (routes.json), keyed by role|provider|model."""

    path: Path
    records: dict[str, RouteRecord] = field(default_factory=dict)
    min_samples: int = DEFAULT_MIN_SAMPLES

    @classmethod
    def load(cls, path: Path | str, min_samples: int = DEFAULT_MIN_SAMPLES) -> "RouteStats":
        p = Path(path)
        recs: dict[str, RouteRecord] = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                for key, d in (raw.get("routes", {}) or {}).items():
                    recs[key] = RouteRecord.from_dict(d)
            except Exception:
                recs = {}
        return cls(path=p, records=recs, min_samples=min_samples)

    def record_outcome(self, role: str, provider: str, model_id: str, *,
                       success: bool, cost_usd: float = 0.0,
                       latency_ms: float = 0.0, truncated: bool = False,
                       output_tokens: int = 0, input_tokens: int = 0,
                       cache_read_tokens: int = 0, now: float | None = None) -> None:
        key = _route_key(role, provider, model_id)
        rec = self.records.get(key)
        if rec is None:
            rec = RouteRecord(role=role, provider=provider, model_id=model_id)
            self.records[key] = rec
        rec.record(success=success, cost_usd=cost_usd, latency_ms=latency_ms,
                   truncated=truncated, output_tokens=output_tokens,
                   input_tokens=input_tokens, cache_read_tokens=cache_read_tokens, now=now)

    def quality_factor(self, role: str, provider: str, model_id: str) -> float:
        rec = self.records.get(_route_key(role, provider, model_id))
        return rec.quality_factor(min_samples=self.min_samples) if rec else 1.0

    def speed_factor(self, role: str, provider: str, model_id: str,
                     declared_tps: float, *, now: float | None = None) -> float:
        """Observed-speed multiplier for a route (accuracy-gated, executor-only in the
        router). Neutral 1.0 for an unseen route."""
        rec = self.records.get(_route_key(role, provider, model_id))
        return rec.speed_factor(declared_tps=declared_tps, min_samples=self.min_samples,
                                now=now) if rec else 1.0

    def cache_discount(self, role: str, provider: str, model_id: str) -> float:
        """R11: input-price factor in (0,1] from a route's observed cache-hit ratio -- cached
        tokens bill at CACHE_READ_PRICE_MULT of full. Neutral 1.0 for an unseen route or
        below min_samples (no discounting on thin evidence)."""
        rec = self.records.get(_route_key(role, provider, model_id))
        if rec is None or rec.samples < self.min_samples:
            return 1.0
        ratio = rec.observed_cache_ratio()
        return (1.0 - ratio) + ratio * CACHE_READ_PRICE_MULT

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"_schema_version": 1, "routes": {k: r.to_dict() for k, r in self.records.items()}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(self.path)

    def rows(self) -> list[RouteRecord]:
        return sorted(self.records.values(),
                      key=lambda r: (r.role, -r.ema_success, r.model_id))

    def suggest_quality_bias(self, current: float = 0.8) -> tuple[float, str]:
        """Advisory only: suggest a quality_bias from observed outcomes.

        Conservative, bounded nudge (+/-0.1), gated on enough total samples:
        - high aggregate pass-rate -> we can afford to lean cheaper (lower bias).
        - low aggregate pass-rate  -> lean toward capability (higher bias).
        Returns (suggested_bias, reason). Never auto-applied.
        """
        total = sum(r.samples for r in self.records.values())
        if total < max(5, self.min_samples * 2):
            return current, f"not enough data ({total} runs) -- keep {current:.2f}"
        passes = sum(r.pass_count for r in self.records.values())
        rate = passes / total if total else 0.0
        if rate >= 0.85:
            new = max(0.0, round(current - 0.1, 2))
            return new, f"pass-rate {rate*100:.0f}% is high -> try {new:.2f} to save cost"
        if rate < 0.6:
            new = min(1.0, round(current + 0.1, 2))
            return new, f"pass-rate {rate*100:.0f}% is low -> try {new:.2f} for more capability"
        return current, f"pass-rate {rate*100:.0f}% is healthy -> keep {current:.2f}"
