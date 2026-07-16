"""Content-addressed cache for distilled context briefs (D2).

Pure stdlib JSON storage keyed by a fingerprint. Misses are safe: the caller
falls back to the deterministic brief.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .context import _sha256


def key(*, fingerprint: str, consumer: str, request_key: str) -> str:
    return _sha256(f"{fingerprint}|{consumer}|{request_key}")[:16]


@dataclass
class BriefCache:
    root: Path

    def _path(self, k: str) -> Path:
        return self.root / f"{k}.json"

    def get(self, k: str) -> dict | None:
        p = self._path(k)
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def put(self, k: str, doc: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path(k)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
