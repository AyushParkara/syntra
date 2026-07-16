"""Model catalog: load and query the AA-seeded capability metadata.

The catalog is the source of truth for capability-aware routing. It is data,
not code. Refresh it from artificialanalysis.ai when their API is wired in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


class CatalogError(Exception):
    """Raised when the model catalog can't be loaded (missing or invalid JSON)."""


def _default_catalog_path() -> Path:
    # Packaged catalog (env override → syntra/data → dev fallback). Resolved
    # lazily so it works whether installed as a wheel or run from a checkout.
    from .paths import default_catalog_path
    return default_catalog_path()

_EVAL_KEYS = {
    "artificial_analysis_intelligence_index",
    "artificial_analysis_coding_index",
    "artificial_analysis_math_index",
    "ifbench", "lcr", "livecodebench", "terminalbench_hard",
    "gpqa", "hle", "mmlu_pro", "tau2",
    "aime_25", "aime", "math_500", "scicode",
}

_TIER_FAST = {"flash", "mini", "nano", "haiku", "instant"}
_TIER_PRO = {"pro", "opus", "max"}


def _extract_evals(row: dict) -> dict:
    out = {}
    for k in _EVAL_KEYS:
        v = row.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def _infer_tier(model_id: str) -> str:
    # F9: match tier keywords on TOKEN boundaries, not raw substrings — otherwise "mini" inside
    # "geMINI" mis-tags every Gemini model (incl. gemini-*-pro) as tier="fast". Split the id on
    # the usual separators and compare whole tokens.
    tokens = set(re.split(r"[-_./: ]+", model_id.lower()))
    if tokens & _TIER_FAST:
        return "fast"
    if tokens & _TIER_PRO:
        return "pro"
    return "standard"


def _model_from_row(row: dict) -> "Model":
    """Build a Model from a catalog row (base catalog AND the user overlay use this, so a
    discovered/minimal row is parsed identically). Raises KeyError/ValueError on a malformed
    row — callers decide whether that's fatal (base) or skippable (overlay)."""
    return Model(
        id=row["id"],
        provider=row["provider"],
        display_name=row["display_name"],
        intelligence_index=float(row["intelligence_index"]),
        speed_tps=float(row["speed_tps"]),
        price_input=float(row["price_input"]),
        price_output=float(row["price_output"]),
        context_window=int(row["context_window"]),
        max_output=int(row["max_output"]),
        specialties=tuple(row.get("specialties", [])),
        best_roles=tuple(row.get("best_roles", [])),
        notes=row.get("notes", ""),
        evals=_extract_evals(row),
        tier=row.get("tier", _infer_tier(row["id"])),
        deprecated=str(row.get("deprecated", "") or ""),
    )


@dataclass(frozen=True)
class Model:
    """One model row from the catalog."""

    id: str
    provider: str
    display_name: str
    intelligence_index: float
    speed_tps: float
    price_input: float
    price_output: float
    context_window: int
    max_output: int
    specialties: tuple[str, ...]
    best_roles: tuple[str, ...]
    notes: str = ""
    evals: dict = field(default_factory=dict)
    tier: str = "standard"  # "fast" | "standard" | "pro"
    # #213: retirement date (ISO "YYYY-MM-DD") when the vendor will drop this id, or "".
    deprecated: str = ""

    def deprecation_warning(self) -> str:
        """A one-line retirement notice for the UI/logs, or "" when the model is live.
        Just a surfacing hook — routing still works; the warning nudges the user to
        migrate before the id is retired (pairs with the #212 alias remap)."""
        if not self.deprecated:
            return ""
        return f"{self.id} is deprecated (retires {self.deprecated}) — migrate to a current model"

    def has_specialty(self, tag: str) -> bool:
        return tag.lower() in {s.lower() for s in self.specialties}

    def fits_role(self, role: str) -> bool:
        return role.lower() in {r.lower() for r in self.best_roles}

    def eval(self, key: str, default: float = 0.0) -> float:
        v = self.evals.get(key)
        return float(v) if v is not None else default


@dataclass
class Catalog:
    """In-memory model catalog with capability queries."""

    models: list[Model] = field(default_factory=list)
    role_tag_priorities: dict[str, list[str]] = field(default_factory=dict)
    role_score_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    intelligence_floors: dict[str, float] = field(default_factory=dict)
    source: str = ""
    tier_penalties: dict[str, float] = field(default_factory=lambda: {"fast": 0.85, "standard": 1.0, "pro": 1.0})
    scoring_method: str = "weighted_avg"
    reviewer_score_weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Catalog":
        catalog_path = Path(path) if path else _default_catalog_path()
        try:
            raw = json.loads(catalog_path.read_text())
        except FileNotFoundError as e:
            raise CatalogError(f"catalog not found at {catalog_path}. Unset "
                               f"SYNTRA_CATALOG_PATH to use the bundled catalog.") from e
        except json.JSONDecodeError as e:
            raise CatalogError(f"catalog at {catalog_path} is not valid JSON: {e}. "
                               f"Fix it, or unset SYNTRA_CATALOG_PATH for the bundled one.") from e
        except OSError as e:
            raise CatalogError(f"could not read catalog at {catalog_path}: {e}") from e
        models = [_model_from_row(row) for row in raw.get("models", [])]
        # Merge the user catalog OVERLAY (auto-detected models append there, never the bundled
        # catalog). Base wins on id conflict; a missing/corrupt overlay is a no-op — it must
        # never break the base catalog load. See model_discovery + paths.default_catalog_overlay_path.
        try:
            from .paths import default_catalog_overlay_path
            _ov_path = default_catalog_overlay_path()
            if _ov_path.exists():
                _ov_raw = json.loads(_ov_path.read_text())
                _have = {m.id for m in models}
                for _row in _ov_raw.get("models", []):
                    try:
                        _rid = _row["id"]
                    except (KeyError, TypeError):
                        continue
                    if _rid in _have:
                        continue                  # base (or an earlier overlay row) wins
                    try:
                        models.append(_model_from_row(_row))
                        _have.add(_rid)
                    except (KeyError, TypeError, ValueError):
                        continue                  # a malformed overlay row is skipped, not fatal
        except Exception:  # noqa: BLE001 - overlay is best-effort; never break base load
            pass
        # #213: heal context_window=0 (unknown) rows from the persisted capability cache.
        # Best-effort + non-authoritative — a known value is never overridden; a missing
        # cache is a no-op. Keeps context-budget decisions accurate after discovery.
        try:
            from .paths import default_catalog_overlay_path
            from . import capability_cache as _cc
            _caps_path = default_catalog_overlay_path().parent / "capability_cache.json"
            if _caps_path.exists():
                models = _cc.apply_capabilities(models, _cc.load_capabilities(_caps_path))
        except Exception:  # noqa: BLE001 - capability cache is best-effort
            pass
        return cls(
            models=models,
            role_tag_priorities=dict(raw.get("_role_tag_priorities", {})),
            role_score_weights=dict(raw.get("_role_score_weights", {})),
            intelligence_floors=dict(raw.get("_intelligence_floor", {})),
            source=raw.get("_source", str(catalog_path)),
            tier_penalties=dict(raw.get("_tier_penalties", {"fast": 0.85, "standard": 1.0, "pro": 1.0})),
            scoring_method=str(raw.get("_scoring_method", "weighted_avg")),
            reviewer_score_weights=dict(raw.get("_reviewer_score_weights", {})),
        )

    def by_id(self, model_id: str) -> Model | None:
        for m in self.models:
            if m.id == model_id:
                return m
        # #212: a stored/pinned id that a vendor RETIRED won't match above — try the
        # runtime alias remap so an old id transparently resolves to its current
        # model. Unknown ids remap to themselves (no behavior change for live ids).
        from .migrations import remap_model_id
        remapped = remap_model_id(model_id)
        if remapped != model_id:
            for m in self.models:
                if m.id == remapped:
                    return m
        return None

    def with_specialty(self, tag: str) -> list[Model]:
        return [m for m in self.models if m.has_specialty(tag)]

    def image_models(self) -> list[Model]:
        """Models that can GENERATE images (the `image-output` specialty) — distinct from the
        `vision` specialty, which is image INPUT. Used to back the generate_image tool: the
        loop picks one of these (cheapest-first if it wants) instead of guessing. Empty list =
        no image-gen model configured, and the tool reports that cleanly."""
        return sorted(self.with_specialty("image-output"),
                      key=lambda m: (m.price_output, m.price_input))

    def for_role(self, role: str) -> list[Model]:
        return [m for m in self.models if m.fits_role(role)]

    def filtered(
        self,
        *,
        require_specialties: Sequence[str] = (),
        require_min_context: int = 0,
        require_providers: Sequence[str] = (),
        exclude_models: Iterable[str] = (),
    ) -> list[Model]:
        excluded = set(exclude_models)
        provs = {p.lower() for p in require_providers} if require_providers else None
        out = []
        for m in self.models:
            if m.id in excluded:
                continue
            if provs is not None and m.provider.lower() not in provs:
                continue
            # Many AA-refreshed rows have context_window=0 (unknown). Only hard-filter
            # when we have a known context_window and it is insufficient.
            if require_min_context and m.context_window and m.context_window > 0 and m.context_window < require_min_context:
                continue
            if require_specialties and not all(
                m.has_specialty(t) for t in require_specialties
            ):
                continue
            out.append(m)
        return out
