"""Persisted model-capability cache (#213).

Many AA-refreshed / freshly-discovered catalog rows carry `context_window=0`
(unknown) — which makes context-budget decisions (compaction thresholds, min-context
filtering) fall back to conservative guesses. When a real value is learned (from a
provider's /models response, or a successful call that reveals the window), we cache
it here so subsequent loads can HEAL those zeros without re-querying.

The cache is authoritative ONLY for unknowns: a model whose catalog row already has a
known (non-zero) context_window keeps it — the cache never clobbers real data. Stored
at `<config>/capability_cache.json` as `{model_id: {context_window, max_output}}`.

Pure-ish (JSON over one file); atomic writes; corrupt/missing file is a no-op.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path


def load_capabilities(path: "str | Path") -> dict:
    """Load the cache as `{model_id: {"context_window": int, "max_output": int}}`.
    Missing/corrupt file → empty dict (never raises)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict = {}
    for mid, caps in doc.get("models", doc).items():
        if isinstance(caps, dict):
            try:
                out[mid] = {
                    "context_window": int(caps.get("context_window", 0) or 0),
                    "max_output": int(caps.get("max_output", 0) or 0),
                }
            except (TypeError, ValueError):
                continue
    return out


def record_capability(path: "str | Path", model_id: str,
                      *, context_window: int = 0, max_output: int = 0) -> None:
    """Learn/refresh a model's capabilities. Merges into the existing cache and
    writes atomically. Only stores positive values (a 0 means 'still unknown')."""
    if not model_id:
        return
    caps = load_capabilities(path)
    row = caps.get(model_id, {"context_window": 0, "max_output": 0})
    if context_window and int(context_window) > 0:
        row["context_window"] = int(context_window)
    if max_output and int(max_output) > 0:
        row["max_output"] = int(max_output)
    caps[model_id] = row
    _write_json(Path(path), {"_schema": "syntra.capability_cache",
                             "_schema_version": 1, "models": caps})


def apply_capabilities(models, caps: dict):
    """Return a new list of models with UNKNOWN (0) context_window / max_output filled
    from `caps`. A model with a known (non-zero) value keeps it — the cache heals gaps,
    it never overrides real catalog data. Models are frozen dataclasses, so a healed
    model is a `replace()` copy; unaffected models pass through unchanged."""
    out = []
    for m in models:
        row = caps.get(getattr(m, "id", None))
        if not row:
            out.append(m)
            continue
        cw = m.context_window
        mo = m.max_output
        new_cw = cw if cw and cw > 0 else int(row.get("context_window", 0) or 0)
        new_mo = mo if mo and mo > 0 else int(row.get("max_output", 0) or 0)
        if new_cw != cw or new_mo != mo:
            out.append(replace(m, context_window=new_cw, max_output=new_mo))
        else:
            out.append(m)
    return out


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
