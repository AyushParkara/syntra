"""Persistent dead-key registry — remember which API keys are currently unusable, so
Syntra stops re-probing (and re-announcing) a dead key on every single message.

WHY THIS IS SEPARATE FROM route_health:
- ``route_health`` tracks ``(provider, MODEL)`` health as a soft SCORE penalty for the
  router — "this model-via-provider has been flaky, prefer others."
- A DEAD KEY is a different fact: a 402-out-of-credits or 401-bad-key kills EVERY model
  on that key, and it is a HARD skip, not a soft demerit. It is keyed by ``(provider,
  key-tail)``, not by model. Tracking it per-model in route_health would need one record
  per (model × provider) and still wouldn't express "skip this key for everything."

So this is a small, focused, persistent ``(provider, key)`` → state store with smart,
kind-aware cooldowns:

- **billing (402 / out of credits)** and **auth (401 / bad key)** are STICKY: the key
  won't fix itself, so it's parked for a long time (default 12h) — long enough that it's
  never re-tried within a working session, but not forever (you might top it up).
- **quota (429 / rate limit)** is TRANSIENT: parked only briefly (default 15 min, or the
  server's ``Retry-After`` when known), then allowed back.
- A real **success** on a key clears it INSTANTLY (you topped it up / rotated it).

The store lives at ``<state>/dead-keys.json`` and is shared across messages and sessions,
so the dead key the user already saw fail is silently skipped next time — no spam, no
wasted round-trip. Concurrency-safe (RLock); a corrupt/foreign file never crashes startup.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .filelock import file_lock


# Default park durations by failure kind (seconds). Absent kinds aren't parked here
# (route_health handles transient/quality demotion); only credential/quota kinds belong.
_STICKY_SEC = 12 * 60 * 60      # billing / auth — won't self-heal; park long
_QUOTA_SEC = 15 * 60           # rate-limit — transient; short park then retry
_PARK_SECONDS: dict[str, float] = {
    "billing": _STICKY_SEC,
    "auth": _STICKY_SEC,
    "tls": _STICKY_SEC,        # config/security — won't fix itself mid-session
    "quota": _QUOTA_SEC,
}


def key_tail(api_key: str | None) -> str:
    """Last 6 chars of a key — the stable, non-secret identity we store/compare on."""
    if not api_key:
        return ""
    return api_key[-6:]


def _kid(provider: str, api_key: str | None) -> str:
    return f"{provider}::{key_tail(api_key)}"


@dataclass
class DeadKey:
    provider: str
    key_tail: str
    kind: str                  # billing | auth | quota | tls
    detail: str = ""
    ts: float = 0.0            # when it was parked
    until: float = 0.0         # parked until (epoch seconds); past this it's retryable

    def is_dead(self, now: float) -> bool:
        return now < self.until


@dataclass
class DeadKeyRegistry:
    """Persistent ``(provider, key-tail)`` → dead-state store with kind-aware cooldowns."""

    path: Path
    keys: dict = field(default_factory=dict)   # kid -> DeadKey
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        self.path = Path(self.path)
        self._load()

    # -------------------------------------------------------------- persistence
    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _load(self) -> None:
        """(Re)build ``self.keys`` from disk. Safe to call repeatedly — it clears
        first, so it doubles as a reload before a locked mutate (#209)."""
        self.keys = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for row in raw.get("dead_keys", []):
            try:
                dk = DeadKey(
                    provider=row["provider"],
                    key_tail=row["key_tail"],
                    kind=row.get("kind", "billing"),
                    detail=row.get("detail", ""),
                    ts=float(row.get("ts", 0.0)),
                    until=float(row.get("until", 0.0)),
                )
            except (KeyError, TypeError, ValueError):
                continue            # skip a malformed/foreign row, never crash startup
            self.keys[_kid(dk.provider, dk.key_tail)] = dk

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"dead_keys": [
            {"provider": d.provider, "key_tail": d.key_tail, "kind": d.kind,
             "detail": d.detail[:200], "ts": d.ts, "until": d.until}
            for d in self.keys.values()
        ]}
        # Unique per-process temp (a shared "<name>.tmp" races across processes).
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    # -------------------------------------------------------------- mutate
    def mark_dead(self, provider: str, api_key: str | None, kind: str,
                  *, detail: str = "", now: float | None = None) -> bool:
        """Park ``(provider, key)`` as unusable for a kind-appropriate duration. Returns
        True if this is a NEW park (so the caller can announce it ONCE), False if the key
        was already parked (so the caller stays quiet). Unknown kinds are ignored (only
        credential/quota kinds belong here)."""
        park = _PARK_SECONDS.get(kind)
        if park is None:
            return False
        now = time.time() if now is None else now
        # #209: cross-process lock + reload-merge-save, so a whole-dict write never
        # clobbers a mark another process made since we loaded.
        with self._lock, file_lock(self._lock_path()):
            self._load()
            kid = _kid(provider, api_key)
            was_dead = kid in self.keys and self.keys[kid].is_dead(now)
            self.keys[kid] = DeadKey(provider=provider, key_tail=key_tail(api_key),
                                     kind=kind, detail=detail, ts=now, until=now + park)
            self._save()
            return not was_dead

    def mark_alive(self, provider: str, api_key: str | None) -> None:
        """A real success on this key — clear any dead mark instantly (topped up/rotated)."""
        with self._lock, file_lock(self._lock_path()):
            self._load()
            kid = _kid(provider, api_key)
            if kid in self.keys:
                del self.keys[kid]
                self._save()

    # -------------------------------------------------------------- query
    def is_dead(self, provider: str, api_key: str | None, now: float | None = None) -> bool:
        """True if this key is currently parked-dead (still within its cooldown)."""
        now = time.time() if now is None else now
        dk = self.keys.get(_kid(provider, api_key))
        return bool(dk and dk.is_dead(now))

    def reason(self, provider: str, api_key: str | None) -> str:
        dk = self.keys.get(_kid(provider, api_key))
        return dk.kind if dk else ""

    def active(self, now: float | None = None) -> list:
        """All keys still parked-dead right now (for a /providers health report)."""
        now = time.time() if now is None else now
        return [d for d in self.keys.values() if d.is_dead(now)]
