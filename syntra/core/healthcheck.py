"""Smart startup health check (don't re-verify every session).

A daily, continuous user shouldn't sit through a full `verify` on every launch.
But if they just added a provider, or it's been a while, or the last check
failed, we SHOULD re-check. This module decides that, cheaply:

- a config FINGERPRINT (providers + catalog + overrides content/mtime) detects
  changes;
- a small cache (.syntra/verify-cache.json) remembers the last fingerprint,
  timestamp, and result;
- `should_check()` says yes only when: never checked · fingerprint changed ·
  last result was not-ok · or older than max_age (default 7 days).

The decision logic is pure -> unit-tested. Fingerprinting reads files but is
deterministic.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


DEFAULT_MAX_AGE_SEC = 7 * 24 * 3600  # a week


def _digest(path: Path) -> str:
    """Content hash of a file, or 'absent'. Cheap + deterministic."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except (OSError, FileNotFoundError):
        return "absent"


def config_fingerprint(*, providers_path: Path | str | None,
                       catalog_path: Path | str | None,
                       overrides_path: Path | str | None) -> str:
    """Fingerprint the inputs that affect verify. Changes -> re-check."""
    parts = [_digest(Path(p)) if p else "none" for p in (providers_path, catalog_path, overrides_path)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def should_check(cache: dict | None, fingerprint: str, *, now: float | None = None,
                 max_age_sec: int = DEFAULT_MAX_AGE_SEC) -> tuple[bool, str]:
    """Decide whether to run the startup check. Pure. Returns (do_it, reason)."""
    now = time.time() if now is None else now
    if not cache:
        return True, "first run"
    if cache.get("fingerprint") != fingerprint:
        return True, "config changed"
    if not cache.get("ok", False):
        return True, "last check had problems"
    age = now - float(cache.get("ts", 0))
    if age > max_age_sec:
        return True, f"last checked {int(age // 86400)}d ago"
    return False, "fresh"


def load_cache(path: Path | str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_cache(path: Path | str, *, fingerprint: str, ok: bool,
               now: float | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"_schema_version": 1, "fingerprint": fingerprint,
           "ok": bool(ok), "ts": time.time() if now is None else now}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(p)
