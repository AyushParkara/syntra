"""Secret store for provider tokens (Track T2, D1-D2).

Browser-login providers yield OAuth-style tokens (access + refresh + expiry) that
must persist securely alongside API keys. This is the chmod-600 store for them:
`.syntra/secrets.json`, owner read/write only, created without a world-readable
window (O_CREAT with 0600 from the start, same pattern as registry.write_*).

Kept separate from providers.json so rotating tokens never rewrites provider
config. Values are opaque to this module; redaction stays the caller's job when
displaying. Pure file I/O -> testable against a temp dir (incl. the 0600 check).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import fsutil


@dataclass
class TokenRecord:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0          # epoch seconds; 0 == unknown/never

    def is_expired(self, *, skew: float = 60.0, now: float | None = None) -> bool:
        """True if the access token is past (expires_at - skew)."""
        if not self.expires_at:
            return False
        return (now if now is not None else time.time()) >= (self.expires_at - skew)


class SecretStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ---- persistence (0600, atomic) ----------------------------------------
    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        # Shared hardened primitive (#258): temp+fsync+os.replace, O_NOFOLLOW symlink refusal,
        # 0600 from creation (no brief world-readable window) — secrets are owner-only.
        fsutil.write_atomic(self.path, json.dumps(data, indent=2), mode=0o600)

    # ---- token API ----------------------------------------------------------
    def get(self, provider: str) -> TokenRecord | None:
        row = self._read().get(provider)
        if not isinstance(row, dict) or "access_token" not in row:
            return None
        return TokenRecord(
            access_token=row.get("access_token", ""),
            refresh_token=row.get("refresh_token", ""),
            expires_at=float(row.get("expires_at", 0.0) or 0.0),
        )

    def set(self, provider: str, token: TokenRecord) -> None:
        data = self._read()
        data[provider] = asdict(token)
        self._write(data)

    def delete(self, provider: str) -> bool:
        data = self._read()
        if provider in data:
            del data[provider]
            self._write(data)
            return True
        return False

    def providers(self) -> list[str]:
        return sorted(self._read().keys())
