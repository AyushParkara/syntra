"""Pure-stdlib BM25 knowledge index (D4).

Designed for small-to-medium corpora (task histories, failures, notes). Stores
documents as JSON; scoring is computed from stored term stats.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


@dataclass
class KnowledgeIndex:
    index_path: Path
    k1: float = 1.5
    b: float = 0.75
    _docs: dict[str, dict] = field(default_factory=dict)   # id -> {text, meta, tf, len}
    _df: dict[str, int] = field(default_factory=dict)      # term -> doc freq
    _avgdl: float = 0.0

    def __post_init__(self):
        self.index_path = Path(self.index_path)
        self._load()

    def _recompute_globals(self) -> None:
        n = max(1, len(self._docs))
        total_len = sum(int(d.get("len", 0) or 0) for d in self._docs.values())
        self._avgdl = total_len / n
        df: dict[str, int] = {}
        for d in self._docs.values():
            tf = d.get("tf", {}) or {}
            for t in tf.keys():
                df[t] = df.get(t, 0) + 1
        self._df = df

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            doc = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                return
            self.k1 = float(doc.get("k1", self.k1))
            self.b = float(doc.get("b", self.b))
            self._docs = doc.get("docs", {}) if isinstance(doc.get("docs"), dict) else {}
            self._recompute_globals()
        except Exception:  # noqa: BLE001
            return

    def save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "_schema": "syntra.knowledge_index",
            "_schema_version": 1,
            "k1": self.k1,
            "b": self.b,
            "docs": self._docs,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    def add(self, doc_id: str, text: str, meta: dict | None = None) -> None:
        toks = _tokenize(text)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        self._docs[str(doc_id)] = {
            "text": text or "",
            "meta": meta or {},
            "tf": tf,
            "len": len(toks),
        }
        self._recompute_globals()

    def search(self, query: str, k: int = 5) -> list[dict]:
        q = _tokenize(query)
        if not q or not self._docs:
            return []
        N = len(self._docs)
        hits: list[tuple[float, str]] = []
        for doc_id, d in self._docs.items():
            dl = float(d.get("len", 0) or 0)
            tf = d.get("tf", {}) or {}
            score = 0.0
            for term in q:
                f = float(tf.get(term, 0) or 0)
                if f <= 0:
                    continue
                df = float(self._df.get(term, 0) or 0)
                # Robertson/Sparck Jones IDF
                idf = math.log(1.0 + (N - df + 0.5) / (df + 0.5))
                denom = f + self.k1 * (1.0 - self.b + self.b * (dl / max(1e-9, self._avgdl)))
                score += idf * (f * (self.k1 + 1.0) / max(1e-9, denom))
            if score > 0:
                hits.append((score, doc_id))
        hits.sort(reverse=True, key=lambda x: x[0])
        out = []
        for score, doc_id in hits[: max(0, int(k))]:
            d = self._docs.get(doc_id, {})
            out.append({
                "doc_id": doc_id,
                "score": round(float(score), 6),
                "meta": d.get("meta", {}) or {},
                "text": d.get("text", "") or "",
            })
        return out
