"""artificialanalysis.ai catalog refresh.

AA publishes intelligence_index, coding_index, math_index, instruction-following
benchmarks, output speed, price per token, and more for most major models.

How to get an AA API key (one-time, by the user):
  1. Go to https://artificialanalysis.ai
  2. Click "API" in the navigation
  3. Sign up and confirm email
  4. Copy your API key from the dashboard
  5. Export: `export ARTIFICIALANALYSIS_API_KEY="aa-..."`

Match AA models to our catalog by slug-derived key (e.g. "claude-opus-4-7"
→ "anthropic/claude-opus-4.7"). Store all evaluation indices for role-aware
routing.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

AA_ENDPOINT = "https://artificialanalysis.ai/api/v2/data/llms/models"

EVAL_FIELDS: list[str] = [
    "artificial_analysis_intelligence_index",
    "artificial_analysis_coding_index",
    "artificial_analysis_math_index",
    "ifbench",
    "lcr",
    "livecodebench",
    "terminalbench_hard",
    "gpqa",
    "hle",
    "mmlu_pro",
    "tau2",
    "aime_25",
    "aime",
    "math_500",
    "scicode",
]

class AARefreshError(RuntimeError):
    """AA fetch or merge failed."""

@dataclass
class RefreshResult:
    fetched: int
    matched_existing: int
    added_new: int
    written_path: str
    note: str = ""

def _normalise(s: str) -> str:
    """Strip provider prefix, hyphens, dots to a comparable fingerprint."""
    # Drop provider prefix if present: "anthropic/claude-opus-4.7" → "claude-opus-4.7"
    if "/" in s:
        s = s.split("/", 1)[1]
    s = s.lower()
    # F20: strip effort/variant markers only as WHOLE tokens (bounded by the original
    # separators), BEFORE collapsing separators — a bare .replace("low","") also mangled
    # "flow"/"slow"/"yellow"/"below"/"workflow", which could collapse two different models to
    # the same fingerprint and write AA stats onto the wrong model.
    _MARKERS = {"nonreasoning", "non-reasoning", "high-effort", "low-effort", "reasoning",
                "flash", "fast", "preview", "lite", "turbo", "pro", "mini",
                "low", "medium", "high"}
    tokens = [t for t in re.split(r"[-._ ]+", s) if t and t not in _MARKERS]
    return "".join(tokens)

def _build_aa_lookup(aa_rows: list[dict]) -> dict[str, dict]:
    """Build slug → row lookup, also including name-normalised keys."""
    lookup: dict[str, dict] = {}
    for row in aa_rows:
        slug = (row.get("slug") or "").lower()
        name = (row.get("name") or "").lower()
        if slug:
            lookup[slug] = row
        if name:
            lookup[_normalise(name)] = row
    return lookup

def refresh_catalog(
    *,
    catalog_path: Path,
    api_key: str | None = None,
    timeout: float = 30.0,
    dry_run: bool = False,
) -> RefreshResult:
    api_key = api_key or os.environ.get("ARTIFICIALANALYSIS_API_KEY", "")
    if not api_key:
        raise AARefreshError(
            "ARTIFICIALANALYSIS_API_KEY not set. See docs for how to get one, "
            "or keep using the seeded catalog (it works fine)."
        )

    req = urllib.request.Request(
        AA_ENDPOINT,
        headers={"x-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:
            pass
        raise AARefreshError(f"HTTP {e.code} from artificialanalysis.ai: {detail}") from e
    except urllib.error.URLError as e:
        raise AARefreshError(f"Network error reaching artificialanalysis.ai: {e}") from e

    aa_rows = payload.get("data") or payload.get("models") or payload
    if not isinstance(aa_rows, list):
        raise AARefreshError(
            f"Unexpected AA payload shape (top-level keys: {list(payload)[:5]}). "
            "If their API changed, file an issue."
        )

    aa_by_slug = _build_aa_lookup(aa_rows)

    local = json.loads(catalog_path.read_text())
    existing = {m["id"]: m for m in local.get("models", [])}
    matched = 0
    added = 0

    def _extract_evals(row: dict) -> dict:
        ev = row.get("evaluations", {})
        out: dict[str, float | None] = {}
        for k in EVAL_FIELDS:
            v = ev.get(k)
            out[k] = float(v) if v is not None else None
        return out

    def _extract_pricing(row: dict) -> tuple[float, float]:
        pr = row.get("pricing", {})
        if isinstance(pr, dict):
            return float(pr.get("price_1m_input_tokens", 0) or 0), float(pr.get("price_1m_output_tokens", 0) or 0)
        return 0.0, 0.0

    def _extract_speed(row: dict) -> float:
        return float(row.get("median_output_tokens_per_second", 0) or 0)

    # First pass: update existing models by matching their IDs against AA slugs
    for our_id, our_model in existing.items():
        # Try direct slug match
        our_slug = our_id.lower()
        if "/" in our_slug:
            our_slug = our_slug.split("/", 1)[1].replace(".", "-").replace("_", "-")

        aa_row = aa_by_slug.get(our_slug) or aa_by_slug.get(_normalise(our_id))
        if aa_row is None:
            # Try fuzzy: walk all slugs and find best match by normalised fingerprint
            our_fp = _normalise(our_id)
            for aas, aarow in aa_by_slug.items():
                if _normalise(aas) == our_fp:
                    aa_row = aarow
                    break

        if aa_row is None:
            continue

        matched += 1
        evals = _extract_evals(aa_row)
        pin, pout = _extract_pricing(aa_row)
        speed = _extract_speed(aa_row)

        # Only overwrite if AA has real values
        for ek, ev in evals.items():
            if ev is not None:
                our_model[ek] = ev
        if pin > 0:
            our_model["price_input"] = pin
        if pout > 0:
            our_model["price_output"] = pout
        if speed > 0:
            our_model["speed_tps"] = speed

        # intelligence_index from AA overrides old
        if evals.get("artificial_analysis_intelligence_index") is not None:
            our_model["intelligence_index"] = evals["artificial_analysis_intelligence_index"]
        else:
            our_model["intelligence_index"] = float(our_model.get("intelligence_index", 0))

    # Second pass: add new models that AA reports but we don't have
    seen_slugs: set[str] = set()
    for our_id in existing:
        our_slug = our_id.lower().split("/", 1)[1] if "/" in our_id else our_id.lower()
        seen_slugs.add(_normalise(our_slug))

    for aa_row in aa_rows:
        slug = (aa_row.get("slug") or "").lower()
        if not slug or _normalise(slug) in seen_slugs:
            continue
        seen_slugs.add(_normalise(slug))

        evals = _extract_evals(aa_row)
        pin, pout = _extract_pricing(aa_row)
        speed = _extract_speed(aa_row)
        provider_slug = (aa_row.get("model_creator", {}) or {}).get("slug", "unknown")
        provider = (aa_row.get("model_creator", {}) or {}).get("name", "unknown").lower()
        name = aa_row.get("name", slug)
        intel = float(evals.get("artificial_analysis_intelligence_index") or 0)

        new_model: dict = {
            "id": f"{provider_slug}/{slug}",
            "provider": provider,
            "display_name": name,
            "intelligence_index": intel,
            "speed_tps": speed,
            "price_input": pin,
            "price_output": pout,
            "context_window": 0,
            "max_output": 0,
            "specialties": [],
            "best_roles": [],
            "notes": "Added by AA refresh. Add specialty tags and best_roles manually for routing.",
        }
        new_model.update({ek: ev for ek, ev in evals.items() if ev is not None})

        existing[new_model["id"]] = new_model
        added += 1

    local["models"] = list(existing.values())
    local["_last_refresh"] = {
        "endpoint": AA_ENDPOINT,
        "matched": matched,
        "added": added,
    }

    if not dry_run:
        catalog_path.write_text(json.dumps(local, indent=2, ensure_ascii=False))

    return RefreshResult(
        fetched=len(aa_rows),
        matched_existing=matched,
        added_new=added,
        written_path=str(catalog_path),
        note="dry-run; nothing written" if dry_run else "",
    )
