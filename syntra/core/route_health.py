"""Route health tracker.

Records, per (provider, model) route, recent observed outcomes: success, empty
reply, quota/credit exhaustion, server error, network error. Surfaces a
"cooldown" signal that the router multiplies into its score so unreliable
routes lose without being hard-banned.

Decay: failures naturally age out over time. A 429 from an hour ago should
NOT permanently kill a route. A 429 in the last 60s should.

File: <state_root>/route-health.json
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


FailureKind = Literal[
    "empty",          # 200 OK but text == ""
    "quota",          # 429 / rate limit (transient, recovers)
    "billing",        # 402 / payment required / out of credits (won't recover w/o topup)
    "auth",           # 401 / 403
    "server",         # 5xx
    "network",        # URLError / timeout
    "warming",        # L1: a LOCAL model is cold-loading (503 'loading' / conn refused while spinning up) -> transient, do NOT demote
    "tls",            # TLS cert error (altname/self-signed/raw-IP) -> route around, NEVER bypass
    "tool_incapable", # model can't do tool/function calling -> avoid for tool tasks
    "malformed",      # response not parseable
    "user_penalty",   # synthetic, from overrides
    # --- silent failures: HTTP 200, looks fine, but the output is not a real answer ---
    "refusal",        # safety boilerplate / "I can't help" -> reroute to a less-restrictive model
    "degeneracy",     # repetition loop / collapsed output -> retry/route around
    "off_task",       # low self-consistency (probabilistic) -> soft signal only
    "tool_bypass",    # claimed a tool succeeded but ground truth says it didn't (dangerous)
]

# Cooldown weight per failure kind. Higher = bigger score demerit.
_KIND_WEIGHT: dict[str, float] = {
    "empty": 0.6,
    "quota": 0.9,
    "billing": 0.95,        # near-permanent until the user tops up
    "auth": 0.85,
    "server": 0.5,
    "network": 0.3,
    "warming": 0.02,        # L1: a cold-loading local model is about to be READY — barely dent it, never route away
    "tls": 0.95,            # config/security problem; route around hard
    "tool_incapable": 1.0,  # fundamental capability gap; strongly avoid
    "malformed": 0.4,
    "user_penalty": 1.0,
    # silent failures (researched): refusal/tool_bypass are real quality failures;
    # degeneracy is a retryable glitch; off_task is a SOFT probabilistic signal.
    "refusal": 0.7,
    "degeneracy": 0.5,
    "off_task": 0.25,       # low weight: expensive + probabilistic, never hard-gate
    "tool_bypass": 0.9,     # dangerous (claimed success, did nothing) -> strongly avoid
}

# Half-life of a single failure in seconds. After this long, its weight is halved.
_HALF_LIFE_SEC = 5 * 60   # 5 minutes (fast recovery so transient quota issues don't dominate)

# Some failures are NOT transient: a dead/out-of-credits key (402 billing), an invalid
# key (401 auth), a TLS/config problem, or a fundamental capability gap won't fix itself
# in 5 minutes. Decaying them on the 5-min half-life makes the router re-probe a dead key
# every ~5 min (and re-spam "OUT OF CREDITS … switching" on every message). Give these a
# MUCH longer half-life so a sticky-dead route stays cooled below the router's threshold
# until it ACTUALLY recovers — which a real success on that route clears (see the success
# credit below). Tunable, per-kind; absent kinds use the default 5-min half-life.
_STICKY_HALF_LIFE_SEC = 12 * 60 * 60   # 12 hours
_KIND_HALF_LIFE: dict[str, float] = {
    "billing": _STICKY_HALF_LIFE_SEC,        # 402 — won't recover without a topup
    "auth": _STICKY_HALF_LIFE_SEC,           # 401 — bad/expired key
    "tls": _STICKY_HALF_LIFE_SEC,            # config/security problem
    "tool_incapable": _STICKY_HALF_LIFE_SEC, # fundamental capability gap
}


def _kind_half_life(kind: str) -> float:
    """Half-life for a failure of this kind — long for sticky/non-transient kinds, the
    default fast 5-min recovery for transient ones (quota/server/network/empty)."""
    return _KIND_HALF_LIFE.get(kind, _HALF_LIFE_SEC)


@dataclass
class FailureEvent:
    ts: float
    kind: str
    detail: str = ""


@dataclass
class RouteRecord:
    provider: str
    model_id: str
    failures: list[FailureEvent] = field(default_factory=list)
    successes: int = 0
    last_success_ts: float = 0.0

    def key(self) -> str:
        return route_key(self.provider, self.model_id)

    def cooldown_factor(self, now: float | None = None) -> float:
        """Return a 0..1 multiplier. 1.0 = perfectly healthy, 0.0 = totally cooled.

        Aggregates time-decayed failure weights and clamps to [0, 1].
        """
        if not self.failures:
            return 1.0
        now = now if now is not None else time.time()
        penalty = 0.0
        for f in self.failures:
            age = max(0.0, now - f.ts)
            # Sticky kinds (billing/auth/tls/tool_incapable) decay slowly so a dead key
            # stays cooled until it really recovers; transient kinds recover in ~5 min.
            # A real success AFTER the failure cancels it (the key was topped up / fixed)
            # — so a stale sticky failure never outlives a genuine recovery.
            if self.last_success_ts > 0.0 and f.ts < self.last_success_ts:
                continue
            decay = math.pow(0.5, age / _kind_half_life(f.kind))
            penalty += _KIND_WEIGHT.get(f.kind, 0.5) * decay
        # A small reward for recent successes.
        if self.last_success_ts and self.successes:
            recent_success_age = max(0.0, now - self.last_success_ts)
            success_credit = math.pow(0.5, recent_success_age / _HALF_LIFE_SEC) * 0.3
            penalty = max(0.0, penalty - success_credit)
        # Compress into 0..1
        factor = 1.0 / (1.0 + penalty)
        return round(max(0.0, min(1.0, factor)), 4)


def route_key(provider: str, model_id: str) -> str:
    return f"{provider}::{model_id}"


class RouteHealth:
    """Persistent per-(provider, model) health tracker."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.records: dict[str, RouteRecord] = {}
        # Guards record/save so concurrent council/parallel calls can't corrupt
        # the records dict or the on-disk file. Re-entrant (record_* calls _save).
        self._lock = threading.RLock()
        self._load()

    # -------------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for row in raw.get("routes", []):
            try:
                rec = RouteRecord(
                    provider=row["provider"],
                    model_id=row["model_id"],
                    successes=int(row.get("successes", 0)),
                    last_success_ts=float(row.get("last_success_ts", 0.0)),
                    failures=[FailureEvent(**f) for f in row.get("failures", [])],
                )
            except (KeyError, TypeError, ValueError):
                continue            # skip a malformed/foreign row, never crash startup
            self.records[rec.key()] = rec

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "routes": [
                {
                    "provider": r.provider,
                    "model_id": r.model_id,
                    "successes": r.successes,
                    "last_success_ts": r.last_success_ts,
                    "failures": [f.__dict__ for f in r.failures[-50:]],  # cap history
                }
                for r in self.records.values()
            ]
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(self.path)

    # -------------------------------------------------------------- record API

    def record_success(self, provider: str, model_id: str) -> None:
        with self._lock:
            rec = self._get(provider, model_id)
            rec.successes += 1
            rec.last_success_ts = time.time()
            self._save()

    def record_failure(
        self,
        provider: str,
        model_id: str,
        kind: FailureKind,
        detail: str = "",
    ) -> None:
        with self._lock:
            rec = self._get(provider, model_id)
            rec.failures.append(FailureEvent(ts=time.time(), kind=kind, detail=detail[:200]))
            if len(rec.failures) > 50:          # mirror the _save() cap so memory stays bounded
                del rec.failures[:-50]
            self._save()

    def cooldown_factor(self, provider: str, model_id: str) -> float:
        # F15: hold the lock while reading — rec.cooldown_factor() iterates rec.failures, which
        # record_failure() mutates under the same lock. Without this, concurrent council/parallel
        # routing can hit "list changed size during iteration". _lock is an RLock, so nesting is safe.
        with self._lock:
            rec = self.records.get(route_key(provider, model_id))
            if rec is None:
                return 1.0
            return rec.cooldown_factor()

    def is_cooled(
        self, provider: str, model_id: str, threshold: float = 0.4
    ) -> bool:
        """True when the route is so cooled the router should consider another."""
        return self.cooldown_factor(provider, model_id) < threshold

    def all_records(self) -> list[RouteRecord]:
        return list(self.records.values())

    def clear(self, provider: str | None = None, model_id: str | None = None) -> int:
        if provider is None and model_id is None:
            n = len(self.records)
            self.records.clear()
            self._save()
            return n
        removed = 0
        for k in list(self.records.keys()):
            r = self.records[k]
            if (provider is None or r.provider == provider) and (model_id is None or r.model_id == model_id):
                del self.records[k]
                removed += 1
        if removed:
            self._save()
        return removed

    # ------------------------------------------------------------------ helpers

    def _get(self, provider: str, model_id: str) -> RouteRecord:
        key = route_key(provider, model_id)
        if key not in self.records:
            self.records[key] = RouteRecord(provider=provider, model_id=model_id)
        return self.records[key]


def cold_load_timeout_s(*, is_local: bool) -> float:
    """L1: default request timeout by endpoint locality. A LOCAL model may be COLD-LOADING a
    multi-GB file on the first call, which can take minutes — a tight timeout would kill it
    mid-spin-up and falsely route away. Remote endpoints keep a tight timeout (a slow remote
    IS a problem). Tunable, not a baked decision."""
    return 300.0 if is_local else 60.0


def classify_provider_error(err: Exception, *, is_local: bool = False) -> FailureKind:
    """Map a raised ProviderError to a FailureKind for route_health recording.

    `is_local` (L1): when the endpoint is on-box, a cold-load signal (503 'loading' / a
    connection refused while the server is still spinning up) is classified as the transient
    'warming' kind instead of server/network — so a model that's about to be READY is not
    demoted and routed away. Remote endpoints are unaffected (a remote 503 is a real outage)."""
    msg = str(err).lower()
    # L1: local cold-load detection BEFORE the generic 5xx/network branches. Only for local
    # endpoints, and only for genuine load signals (not every 503).
    if is_local and (
        "loading" in msg or "is starting" in msg or "warming" in msg
        or "connection refused" in msg
        or ("503" in msg and ("load" in msg or "starting" in msg or "unavailable" in msg))
    ):
        return "warming"
    # TLS/cert problems first: they often also look like "network", but must be
    # classified distinctly so we route AROUND and never silently bypass (C7).
    if ("certificate" in msg or "cert_altname" in msg or "ssl" in msg
            or "self signed" in msg or "self-signed" in msg
            or "certificate verify failed" in msg or "hostname mismatch" in msg
            or "err_tls" in msg):
        return "tls"
    # Tool/function-calling capability gaps (route to a tool-capable model).
    if (("tool" in msg or "function calling" in msg or "function_call" in msg)
            and ("not support" in msg or "unsupported" in msg or "no support" in msg
                 or "incapable" in msg or "does not support" in msg)):
        return "tool_incapable"
    if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
        return "auth"
    # Billing (402 / payment / credits / card) is distinct from transient rate-limit
    # (429). Check card/payment phrasings BEFORE the quota branch's bare "credit".
    if ("402" in msg or "payment required" in msg or "payment failed" in msg
            or "insufficient" in msg or "out of credit" in msg or "billing" in msg
            or "credits exhausted" in msg or "credit card" in msg or "card declined" in msg
            or "card was declined" in msg):
        return "billing"
    if "429" in msg or "quota" in msg or "rate limit" in msg or "rate-limit" in msg or "credit" in msg:
        return "quota"
    if any(code in msg for code in ("500", "502", "503", "504", "bad gateway", "service unavailable")):
        return "server"
    if "network error" in msg or "timeout" in msg or "timed out" in msg:
        return "network"
    if "malformed" in msg or "no json" in msg:
        return "malformed"
    return "server"


# Failure kinds that won't fix themselves by waiting (need user action).
_STICKY_KINDS = {"auth", "billing", "tls", "tool_incapable"}


def route_down_reminder(record: "RouteRecord", *, now: float | None = None,
                        window: float = 3600.0, min_failures: int = 3) -> str | None:
    """C6: if a route keeps failing, return a one-line reminder, else None.

    Pure. Fires when there are >= min_failures failures within `window` seconds
    and no success since the most recent failure. "Sticky" kinds (auth/billing/
    tls/tool_incapable) fire on the first such recent failure since they won't
    recover by waiting.
    """
    if not record.failures:
        return None
    now = now if now is not None else time.time()
    recent = [f for f in record.failures if (now - f.ts) <= window]
    if not recent:
        return None
    last = max(recent, key=lambda f: f.ts)
    # A success after the last failure means the route recovered -> no reminder.
    if record.last_success_ts and record.last_success_ts >= last.ts:
        return None
    sticky = last.kind in _STICKY_KINDS
    if not sticky and len(recent) < min_failures:
        return None
    route = route_key(record.provider, record.model_id)
    return f"route {route} keeps failing ({last.kind}); syntra is routing around it"
