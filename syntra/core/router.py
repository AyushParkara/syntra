"""Dynamic AA-based role router.

All routing is done via capability scoring over the full AA catalog.
No hardcoded candidate lists. Selection uses role-specific weights for
intelligence_index, coding_index, ifbench, lcr, gpqa, etc. from real AA data.
Overrides.json can boost/penalize specific models.

New capabilities:
- Tier-based penalties from catalog (no string-matching model IDs)
- Dynamic price normalization from catalog max (no hardcoded $30.0)
- Confidence scoring based on eval coverage
- Nonlinear scoring: weighted_avg | geometric_mean
- pick_top_n() returns ranked list for failover
- Reviewer uses orthogonal metrics from catalog
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .catalog import Catalog, Model

# Evidence floor: how much of a model's claimed capability still counts when it
# has ZERO benchmark coverage for a role. A no-data model keeps this fraction of
# its raw score; a fully-benchmarked one keeps 100%. Stops an unproven, possibly
# mis-rated entry (e.g. a high intelligence_index with empty evals) from hijacking
# the top spot over models with real measured performance.
_MIN_EVIDENCE = 0.55
# Concavity of the evidence discount (see _score). <1 => the discount is steep
# near zero coverage but shallow near full, so missing 1-of-N benches barely
# dents a strong model while a no-data model is still floored. Tunable.
_EVIDENCE_EXP = 0.35

# IRT-shape scoring anchors (used when catalog scoring_method == "irt").
# _IRT_CENTER: the "average model" capability anchor — a model exactly at center on
#   every demanded axis scores P=0.5; stronger -> toward 1, weaker -> toward 0.
# _IRT_SLOPE: logistic steepness. Higher = sharper separation at the frontier (more
#   spread among top models, countering benchmark saturation). Both are tunable, not
#   hardcoded decisions — exposed so they can be re-derived empirically.
_IRT_CENTER = 0.5
_IRT_SLOPE = 6.0

# R3: deterministic tie-break. When the top candidates' final scores fall within this
# band they're treated as a TIE and resolved by an explicit, reproducible key instead of
# catalog insertion order (Python's stable sort would otherwise let a catalog refresh flip
# the winner). Order: higher learned quality -> higher evidence coverage -> cheaper ->
# faster -> model_id lexicographic. All measured/known signals, so this never contradicts
# the core rules (no model names, evidence-first). Tunable, not a baked decision.
_TIE_EPSILON = 0.005

# Map each capability AXIS (what a task DEMANDS) to the catalog eval keys that
# MEASURE a model's ability on it. Uses non-saturated, contamination-resistant
# benches per the research (docs/routing/DIMENSION_RESEARCH.json): coding ->
# coding_index + livecodebench + terminalbench_hard; reasoning -> hle + gpqa;
# instruction -> ifbench; tool_use -> tau2 + terminalbench_hard; long_context ->
# lcr. `speed`/`criticality` have no eval key (handled by speed_bonus / cost /
# evidence elsewhere) so they carry no IRT weight. NOT hardcoded model picks —
# a general axis->benchmark mapping.
_AXIS_TO_EVALS = {
    "reasoning":    {"hle": 0.6, "gpqa": 0.4},
    "code":         {"artificial_analysis_coding_index": 0.5, "livecodebench": 0.25, "terminalbench_hard": 0.25},
    # tool_use: terminalbench_hard (still discriminates frontier, max ~0.6) drives
    # the axis; tau2 is SATURATED (top models all 0.94-0.98) so it can't separate
    # them -> kept only as a secondary signal. (research: route on non-saturated
    # benches; demote saturated ones to a tie-break.)
    "tool_use":     {"terminalbench_hard": 0.6, "tau2": 0.4},
    "long_context": {"lcr": 1.0},
    "instruction":  {"ifbench": 1.0},
    # speed / criticality: no benchmark axis (scored via speed_bonus / cost / evidence)
}


def is_local_url(base_url: str) -> bool:
    """R5: True if a provider base_url is an ON-BOX / LAN (local) endpoint — the basis for
    the 'never send this off-box' privacy gate. Local = loopback (localhost/127.0.0.1/[::1]/
    0.0.0.0), an *.local mDNS host, or an RFC-1918 private LAN address. An empty/unparseable
    URL is treated as local (a keyless self-hosted default), NEVER as an accidental remote.
    Pure."""
    if not base_url:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - a parse failure must not classify as remote
        return True
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        return True
    # RFC-1918 private ranges (LAN): 10.x, 192.168.x, 172.16-31.x.
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


def eval_key_to_axis(eval_key: str) -> str | None:
    """Map a benchmark eval key back to the capability AXIS it primarily measures (R15).
    A key shared by two axes (e.g. terminalbench_hard = code + tool_use) resolves to the axis
    that weights it MOST, so a learned per-axis ability delta can be applied by eval key.
    Returns None for a key no axis measures. Pure."""
    best_axis, best_share = None, 0.0
    for axis, evals in _AXIS_TO_EVALS.items():
        share = evals.get(eval_key, 0.0)
        if share > best_share:
            best_axis, best_share = axis, share
    return best_axis


# Demand-sharpening for routing. Research verdict (docs/routing + memory
# project-syntra-routing-verdict): under a compensatory model a single-axis task
# only routes to its specialist if the demand DIRECTION is concentrated on that
# axis; a broad multi-axis task SHOULD still go to the best all-rounder. So we
# sharpen the demand vector toward its peak before mapping to bench weights. Both
# knobs are tunable module constants -- NOT hardcoded per-axis or per-model picks.
_DEMAND_SHARPEN_GAMMA = 3.0   # >1 concentrates toward the dominant axis; 1.0 = off
_DEMAND_FLOOR_FRAC = 0.35     # axes below this fraction of the peak drop to 0 (background suppression)


def _sharpen_demands(demands: dict) -> dict:
    """Concentrate a demand vector toward its dominant axis/axes (peak-normalized
    power transform + background floor). Pure. A narrow task (one axis far above the
    rest) becomes near-unidimensional -> a specialist can win; a broad task (several
    comparably-high axes) keeps them all -> the all-rounder still wins. The peak axis
    is always preserved (ratio 1 -> survives the floor)."""
    if not demands:
        return demands
    vals = [float(v) for v in demands.values() if isinstance(v, (int, float))]
    mx = max(vals) if vals else 0.0
    if mx <= 0 or _DEMAND_SHARPEN_GAMMA == 1.0:
        return demands
    out: dict = {}
    for ax, d in demands.items():
        try:
            ratio = float(d) / mx
        except (TypeError, ValueError):
            continue
        v = (ratio ** _DEMAND_SHARPEN_GAMMA) * mx
        out[ax] = round(v, 4) if v >= _DEMAND_FLOOR_FRAC * mx else 0.0
    return out


def _demands_to_eval_weights(demands: dict) -> dict:
    """Turn a per-task demand vector (axis->0..1) into IRT eval-key weights by
    distributing each axis's demand across the benches that measure it. The demand
    vector is SHARPENED first so narrow tasks concentrate on their specialist's
    benches. Pure."""
    out: dict = {}
    for axis, demand in (_sharpen_demands(demands) or {}).items():
        try:
            d = float(demand)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        for eval_key, share in _AXIS_TO_EVALS.get(axis, {}).items():
            out[eval_key] = out.get(eval_key, 0.0) + d * share
    return out


@dataclass(frozen=True)
class TaskProfile:
    role: str  # "planner" | "executor" | "reviewer"
    required_specialties: tuple[str, ...] = ()
    nice_to_have_specialties: tuple[str, ...] = ()
    min_context_tokens: int = 0
    needs_tool_use: bool = False
    needs_long_context: bool = False
    needs_vision: bool = False
    quality_bias: float = 0.8
    boost_multiplier: float = 1.0  # multiplicative task-analysis boost
    # Per-task DEMAND vector (0..1 per capability axis) from the analyzer. When set
    # AND scoring_method=="irt", these become the IRT discrimination weights `a`
    # (task-aware), overriding the static role weights. Empty = use role weights.
    demands: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateSkip:
    model_id: str
    reason: str   # short verb phrase: "no provider", "blacklisted", "quota-cooled (0.20)", ...
    detail: str = ""
    # R1: stable machine code for the exclusion (Syntra-native — OUR exclusions, not copied
    # from any reference). Lets the "why not that model" story be aggregated + regression-tested.
    # "" == legacy/unspecified. See _SKIP_CODES for the vocabulary.
    code: str = ""


# R1: the Syntra-native exclusion vocabulary — one stable code per real skip site. These are
# the exclusions Syntra ACTUALLY produces (verified against the pick loop), not an imported list.
_SKIP_CODES = (
    "policy_local_only",          # privacy gate denied a remote endpoint (R5)
    "below_intelligence_floor",   # model intel < the role's floor
    "role_removed",               # user override removed this model from the role
    "missing_specialty",          # an explicitly-required specialty tag is absent
    "provider_not_required",      # require_providers set and this provider isn't in it
    "blacklisted",                # user blacklisted the model (optionally per-provider)
    "penalty_zero",               # a user penalty of 0 (hard-off, not a full blacklist)
    "quota_cooled",               # route-health cooldown below the usable threshold
)


@dataclass(frozen=True)
class RoutingDecision:
    model: Model
    role: str
    provider: str
    score: float          # final score after all penalties/bonuses
    raw_score: float      # AA-based score before penalties
    confidence: float     # 0..1 based on eval coverage
    eval_coverage: float  # fraction of role evals present for this model
    reason: str           # human-readable why
    strategy: str         # "scoring-fallback"
    deliberation: tuple[CandidateSkip, ...] = ()
    # R2: policy-consistent ranked fallback chain — the model ids ranked BELOW this pick at
    # decision time. Lets failover be a lookup (same ranking the decision used) instead of a
    # fresh re-pick that could drift as route-health/stats change mid-run. Also visible in trace.
    fallback_model_ids: tuple[str, ...] = ()
    # R1: structured breakdown of the score COMPONENTS that actually exist in Syntra's scorer
    # (raw, evidence_coverage, learned_quality, speed, cache, cooldown, penalty) — real numbers
    # for /trace explainability, NOT fabricated reference metrics. Empty for pins/legacy.
    score_breakdown: dict = field(default_factory=dict)


class Router:
    """Capability scoring over the full AA catalog. No hardcoded candidate lists.

    Optional hooks let the user supply:
      - `route_resolver(model_id) -> provider_name|None`
      - `is_blacklisted(model_id, provider) -> (bool, reason)`
      - `cooldown_factor(provider, model_id) -> float in [0,1]`
      - `extra_penalty(model_id, provider) -> (multiplier, reason)`
    """

    def __init__(
        self,
        catalog: Catalog,
        *,
        route_resolver=None,
        is_blacklisted=None,
        cooldown_factor=None,
        extra_penalty=None,
        extra_specialties=None,
        role_edits=None,
        quality_factor=None,
        speed_factor=None,
        cache_discount=None,
        ability_delta=None,
        is_remote=None,
        local_only: bool = False,
        pinned_model=None,
        cooldown_threshold: float = 0.65,
        min_evidence: float = _MIN_EVIDENCE,
    ):
        self.catalog = catalog
        self._resolve = route_resolver or (lambda mid: None)
        self._blacklisted = is_blacklisted or (lambda mid, prov: (False, ""))
        self._cooldown = cooldown_factor or (lambda prov, mid: 1.0)
        self._penalty = extra_penalty or (lambda mid, prov: (1.0, ""))
        # Optional override hooks. Each returns (add_set, remove_set).
        self._extra_specialties = extra_specialties or (lambda mid: (set(), set()))
        self._role_edits = role_edits or (lambda mid: (set(), set()))
        # Hard role pin: role -> model_id|None. When set & resolvable, wins.
        self._pinned_model = pinned_model or (lambda role: None)
        # Learned route quality (Phase 4). (role, provider, model) -> bounded mult.
        self._quality_factor = quality_factor or (lambda role, prov, mid: 1.0)
        # Observed-latency profile: (role, provider, model, declared_tps) -> bounded speed
        # mult, ACCURACY-GATED (a fast-but-failing route earns no speed credit). Applied
        # executor-only (planner/reviewer are quality-first). Neutral 1.0 by default.
        self._speed_factor = speed_factor or (lambda role, prov, mid, declared_tps: 1.0)
        # R11: cache-aware cost. (role, provider, model) -> factor in (0,1] scaling the INPUT
        # price the cost penalty uses, from a route's observed cache-hit ratio (cache reads
        # bill ~10%). Neutral 1.0 by default -> sticker price, unchanged behavior.
        self._cache_discount = cache_discount or (lambda role, prov, mid: 1.0)
        # R15: learned per-axis ability correction. (model_id, eval_key) -> bounded delta
        # added to the model's benchmark θ inside IRT scoring, so ability proven in real runs
        # sharpens the pick. Neutral 0.0 by default -> frozen benchmark θ, unchanged behavior.
        self._ability_delta = ability_delta or (lambda mid, eval_key: 0.0)
        # R5: privacy / local-only HARD gate. `is_remote(provider) -> bool` tells the router
        # whether a provider sends data off-box; when `local_only` is set, remote endpoints are
        # excluded BEFORE scoring (typed reason) and NEVER silently fall back to (the whole
        # point of the gate). Off by default -> no privacy filtering. Opt-in.
        self._is_remote = is_remote or (lambda provider: False)
        self.local_only = bool(local_only)
        self.cooldown_threshold = cooldown_threshold
        # Evidence floor (tunable, not hardcoded): a model with no benchmark
        # coverage keeps this fraction of its raw score (see _MIN_EVIDENCE).
        self.min_evidence = min_evidence

        # Dynamic price normalization: compute max blended price from catalog
        prices = [
            m.price_input * 0.3 + m.price_output * 0.7
            for m in catalog.models
            if m.price_input > 0 or m.price_output > 0
        ]
        self._max_blended_price = max(prices) if prices else 30.0

    # ------------------------------------------------------------------- pick

    def pick(
        self,
        profile: TaskProfile,
        *,
        exclude_models: Sequence[str] = (),
        require_providers: Sequence[str] = (),
    ) -> RoutingDecision:
        """Return the single best candidate."""
        top = self.pick_top_n(
            profile,
            n=1,
            exclude_models=exclude_models,
            require_providers=require_providers,
        )
        if not top:
            raise NoModelAvailable(
                f"No catalog model is usable for role={profile.role} after applying "
                "provider resolution, blacklist, penalty, and cooldown filters."
            )
        return top[0]

    def pick_top_n(
        self,
        profile: TaskProfile,
        n: int = 3,
        *,
        exclude_models: Sequence[str] = (),
        require_providers: Sequence[str] = (),
        _ignore_floor: bool = False,
    ) -> list[RoutingDecision]:
        """Return top N ranked candidates for failover.

        `_ignore_floor` (internal): when the absolute intelligence floor would filter out
        EVERY candidate (e.g. a user running only small/local models), we retry with it
        disabled so they always get their best-available option rather than nothing."""
        role = profile.role.lower()
        excluded = set(exclude_models)

        candidates = self.catalog.filtered(
            require_min_context=profile.min_context_tokens,
            exclude_models=excluded,
        )

        # Hard role pin wins outright (if the model still exists & resolves and
        # the caller hasn't excluded it, e.g. mid-failover). This is the
        # user's explicit "use THIS model for THIS role" choice.
        pinned_id = self._pinned_model(role)
        if pinned_id and pinned_id not in excluded:
            pin_provider = self._resolve(pinned_id)
            pin_model = next((m for m in self.catalog.models if m.id == pinned_id), None)
            if (pin_model is not None and pin_provider
                    and (not require_providers or pin_provider in require_providers)):
                blocked, _why = self._blacklisted(pinned_id, pin_provider)
                if not blocked:
                    return [RoutingDecision(
                        model=pin_model,
                        role=role,
                        provider=pin_provider,
                        score=1.0,
                        raw_score=1.0,
                        confidence=1.0,
                        eval_coverage=1.0,
                        reason=f"role={role} PINNED to {pinned_id} via {pin_provider} (user override)",
                        strategy="pinned",
                        deliberation=[],
                    )]

        scored: list[tuple[float, float, float, float, str, float, float, str, Model]] = []
        deliberation: list[CandidateSkip] = []
        floor_skipped = False        # did the absolute floor drop any candidate?

        # Hard filters derived from the task profile.
        # Keep these strict only when the caller explicitly asked for them.
        # (Most catalog rows have incomplete tags; capability "needs_*" signals
        # are treated as soft preferences, not filters.)
        required_specialties = set(profile.required_specialties)

        for m in candidates:
            provider = self._resolve(m.id)
            if not provider:
                # Not usable right now; don't spam deliberation with unservable rows.
                continue

            # R5: privacy / local-only HARD gate. When enabled, a provider that sends data
            # off-box is excluded here (BEFORE scoring), with a typed reason. This is NOT
            # relaxed by the graceful-floor retry below (unlike the intelligence floor) —
            # silently going remote would defeat the entire "never off-box" guarantee; an
            # empty pool becomes an explicit NoModelAvailable instead.
            if self.local_only and self._is_remote(provider):
                deliberation.append(CandidateSkip(m.id, "policy: local-only (remote denied)", provider,
                                                  code="policy_local_only"))
                continue

            # Apply role/specialty edits from overrides (if wired).
            add_specs, remove_specs = self._extra_specialties(m.id)
            effective_specs = set(m.specialties)
            effective_specs.update(add_specs)
            effective_specs.difference_update(remove_specs)

            add_roles, remove_roles = self._role_edits(m.id)
            effective_roles = set(m.best_roles)
            effective_roles.update(add_roles)
            effective_roles.difference_update(remove_roles)

            # Intelligence floor is a safety guardrail to avoid routing to very weak models.
            # SOFT, not absolute: if it would leave zero candidates (a local-only setup),
            # we retry below with it disabled so the user still has options.
            floor = self.catalog.intelligence_floors.get(role)
            if floor is not None and not _ignore_floor:
                intel = m.eval("artificial_analysis_intelligence_index", m.intelligence_index)
                if intel < float(floor):
                    floor_skipped = True
                    deliberation.append(
                        CandidateSkip(m.id, "below intelligence floor", f"{intel:.1f} < {float(floor):.1f}",
                                      code="below_intelligence_floor")
                    )
                    continue

            # If a user explicitly removed this model from the role, respect it.
            if role and (role in {r.lower() for r in remove_roles}):
                deliberation.append(CandidateSkip(m.id, "role removed", role, code="role_removed"))
                continue

            # Hard required specialties (explicit).
            if required_specialties:
                spec_lc = {s.lower() for s in effective_specs}
                missing = [t for t in sorted(required_specialties) if t.lower() not in spec_lc]
                if missing:
                    deliberation.append(CandidateSkip(m.id, "missing specialty", ",".join(missing),
                                                      code="missing_specialty"))
                    continue
            if require_providers and provider not in require_providers:
                deliberation.append(CandidateSkip(m.id, "provider not required", code="provider_not_required"))
                continue
            blocked, reason = self._blacklisted(m.id, provider)
            if blocked:
                deliberation.append(CandidateSkip(m.id, "blacklisted", reason, code="blacklisted"))
                continue
            mult, pen_reason = self._penalty(m.id, provider)
            if mult <= 0.0:
                deliberation.append(CandidateSkip(m.id, "penalty=0", pen_reason, code="penalty_zero"))
                continue
            cooldown = self._cooldown(provider, m.id)
            if cooldown < self.cooldown_threshold:
                deliberation.append(CandidateSkip(m.id, f"quota-cooled ({cooldown:.2f})", code="quota_cooled"))
                continue

            # R11: effective input price for THIS route reflects its observed cache-hit
            # ratio (cache reads bill ~10%), so cache-heavy workloads route to the model
            # that's actually cheapest for them, not the lowest sticker price.
            input_price_factor = self._cache_discount(role, provider, m.id)
            raw, confidence, coverage = self._score(m, profile, input_price_factor=input_price_factor)

            # Specialty/role preference multipliers (soft, to avoid brittle routing
            # when catalog tags are incomplete).
            prefer_mult = 1.0
            spec_lc = {s.lower() for s in effective_specs}

            # Role-tag priorities from the catalog are implicit soft preferences.
            # Keep the multiplier small to avoid overpowering AA eval metrics.
            for t in self.catalog.role_tag_priorities.get(role, []):
                if t.lower() in spec_lc:
                    prefer_mult *= 1.006

            # Caller-specified nice-to-haves are slightly stronger than catalog defaults.
            if profile.nice_to_have_specialties:
                for t in profile.nice_to_have_specialties:
                    if t.lower() in spec_lc:
                        prefer_mult *= 1.01

            if profile.needs_long_context:
                # Prefer explicitly tagged long-context OR large context_window.
                if "long-context" in spec_lc:
                    prefer_mult *= 1.04
                elif m.context_window and m.context_window >= 128_000:
                    prefer_mult *= 1.04
                elif m.context_window and m.context_window > 0:
                    prefer_mult *= 0.92
                else:
                    # context_window==0 means unknown in our seeded/refreshed catalog.
                    # Don't demote on missing metadata.
                    prefer_mult *= 1.0

            if profile.needs_tool_use:
                # Tool-use tag is editorial; treat absence as a mild demotion.
                if "tool-use" in spec_lc:
                    prefer_mult *= 1.04
                elif effective_specs:
                    prefer_mult *= 0.93
                else:
                    # Unknown: don't demote.
                    prefer_mult *= 1.0

            if profile.needs_vision:
                if "vision" in spec_lc:
                    prefer_mult *= 1.03
                elif effective_specs:
                    prefer_mult *= 0.90
                else:
                    # Unknown: don't demote.
                    prefer_mult *= 1.0

            # If best_roles is populated, prefer models explicitly tagged for this role.
            if effective_roles:
                role_lc = {r.lower() for r in effective_roles}
                if role in role_lc:
                    prefer_mult *= 1.02
                else:
                    prefer_mult *= 0.97
            # Apply multiplicative boost from task analysis
            raw *= profile.boost_multiplier
            raw *= prefer_mult
            # Learned route quality (Phase 4): bounded, sample-gated, neutral=1.0.
            quality = self._quality_factor(role, provider, m.id)
            # Observed speed (executor-only): learned tokens/sec vs declared, ACCURACY-GATED
            # so a fast-but-wrong route gets no boost. Bounded + neutral=1.0 -> only breaks
            # near-ties among reliable routes; never overrides the accuracy/quality signal.
            speed = self._speed_factor(role, provider, m.id, m.speed_tps) if role == "executor" else 1.0
            final = raw * mult * cooldown * quality * speed

            scored.append((final, raw, confidence, coverage, provider, mult, cooldown, quality, pen_reason or "", speed, m))

        # R3: sort by final score, but break ties DETERMINISTICALLY. A plain
        # sort(key=final) keeps Python's stable (catalog-insertion) order for equal scores,
        # so a catalog refresh could silently flip which model wins a tie. Instead, order by
        # a full explicit key: (final desc, learned-quality desc, coverage desc, price asc,
        # speed desc, model_id asc). Tuple indices: 0=final 3=coverage 7=quality; the Model
        # is t[-1] (price_input/price_output, speed_tps, id).
        def _sort_key(t):
            final, coverage, quality, m = t[0], t[3], t[7], t[-1]
            blended_price = m.price_input * 0.3 + m.price_output * 0.7
            return (-final, -quality, -coverage, blended_price, -m.speed_tps, m.id)
        scored.sort(key=_sort_key)
        # Did the winner emerge from a genuine TIE (>1 candidate within epsilon of the top
        # final score)? If so, record it so the pick is explainable ("tie-break applied").
        tie_broken = False
        if len(scored) >= 2 and abs(scored[0][0] - scored[1][0]) <= _TIE_EPSILON:
            tie_broken = True

        # Graceful floor: if the absolute intelligence floor filtered out EVERY candidate
        # (e.g. someone running only small/local models), retry WITHOUT the floor so they
        # still get their best-available option instead of an empty list (user scenario).
        if not scored and floor_skipped and not _ignore_floor:
            return self.pick_top_n(profile, n, exclude_models=exclude_models,
                                   require_providers=require_providers, _ignore_floor=True)

        full_deliberation = tuple(deliberation)
        # R2: the full ranked model-id order (across ALL eligible candidates, not just the
        # top-n returned) so each decision can carry the policy-consistent fallback chain =
        # the ids ranked below it. Bounded so a huge catalog doesn't bloat the record.
        _MAX_CHAIN = 8
        ranked_ids = [t[-1].id for t in scored]
        out: list[RoutingDecision] = []
        for rank, (final, raw, confidence, coverage, provider, mult, cooldown, quality, pen_reason, speed, m) in enumerate(scored[:n]):
            extras = []
            if mult != 1.0:
                extras.append(f"mult={mult:.2f}" + (f"({pen_reason})" if pen_reason else ""))
            if cooldown != 1.0:
                extras.append(f"cool={cooldown:.2f}")
            if quality != 1.0:
                extras.append(f"quality={quality:.3f}")
            # R3: annotate the winner when it was chosen by the deterministic tie-break
            # (a near-tie resolved by quality/coverage/cost/speed/id, not score alone).
            if rank == 0 and tie_broken:
                extras.append("tie-break")
            extra_s = (" " + " ".join(extras)) if extras else ""
            # R1: structured breakdown of the REAL score components (for /trace explainability).
            breakdown = {
                "final": round(final, 4),
                "raw": round(raw, 4),
                "evidence_coverage": round(coverage, 3),
                "learned_quality": round(quality, 4),
                "speed": round(speed, 4),
                "cooldown": round(cooldown, 4),
                "penalty": round(mult, 4),
            }
            out.append(RoutingDecision(
                model=m,
                role=role,
                provider=provider,
                score=round(final, 4),
                raw_score=round(raw, 4),
                confidence=round(confidence, 3),
                eval_coverage=round(coverage, 3),
                reason=(
                    f"role={role} raw={raw:.3f} "
                    f"conf={confidence:.2f} cov={coverage:.0%} "
                    f"provider={provider} price=${m.price_input}/{m.price_output}" + extra_s
                ),
                strategy="scoring-fallback",
                deliberation=full_deliberation,
                fallback_model_ids=tuple(ranked_ids[rank + 1: rank + 1 + _MAX_CHAIN]),
                score_breakdown=breakdown,
            ))
        return out

    # ----------------------------------------------------------- internal scoring

    def _score(self, model: Model, profile: TaskProfile, *,
               input_price_factor: float = 1.0) -> tuple[float, float, float]:
        """Return (raw_score, confidence, eval_coverage).

        Uses role-appropriate weights from catalog. Supports nonlinear methods.
        """
        role = profile.role.lower()
        # Reviewer uses orthogonal weights if available
        if role == "reviewer" and self.catalog.reviewer_score_weights:
            weights = self.catalog.reviewer_score_weights
        else:
            weights = self.catalog.role_score_weights.get(role, {})

        method = self.catalog.scoring_method
        total_weight = 0.0
        present_count = 0
        total_keys = len(weights)

        # Compute raw score
        # ---- IRT-shape (Item Response Theory; see docs/ROUTING_ENGINE.md) ----
        # raw_score = P(model handles this role) = sigma( k · a^T·(theta - center) ), where
        #   theta_i = model's normalized capability on axis i (its eval value, 0..1)
        #   a_i     = role demand on axis i (the role weight, normalized to sum 1)
        #   center  = the "average model" anchor (0.5) so strong models -> ~1, weak -> ~0
        #   k       = slope (spread). This SPREADS the saturated top instead of compressing it.
        # Falls through to the SAME tier/cost/speed/evidence/bias handling as the other
        # methods below (no divergent path). Opt-in via catalog scoring_method="irt".
        if method == "irt" and (weights or profile.demands):
            # Task-aware: if the analyzer gave a per-task demand vector, map each
            # demanded axis to the eval keys that measure it -> these become the IRT
            # `a` weights (so a code-heavy task weights coding evals, etc.). Falls
            # back to the static role weights when no demands are supplied.
            irt_weights = _demands_to_eval_weights(profile.demands) if profile.demands else dict(weights)
            weights = irt_weights or dict(weights)
            total_keys = len(weights)
            tw = sum(weights.values()) or 1.0
            acc = 0.0
            for eval_key, weight in weights.items():
                eval_val = model.eval(eval_key, -1.0)
                if eval_val < 0:
                    continue
                present_count += 1
                if eval_key.startswith("artificial_analysis_"):
                    eval_val = eval_val / 100.0
                # R15: add the learned bounded ability correction for this (model, axis) so
                # θ reflects real-run outcomes, not only the frozen benchmark. Re-clamp so the
                # nudged θ stays a valid 0..1 ability.
                eval_val += self._ability_delta(model.id, eval_key)
                eval_val = max(0.0, min(1.0, eval_val))          # clamp to [0,1]
                acc += (weight / tw) * (eval_val - _IRT_CENTER)   # a_i · (theta_i - center)
                total_weight += weight
            # logistic spread. No coverage data -> fall back to the AA intelligence
            # index (like weighted_avg), NOT a flat 0.5 -- otherwise a strong-but-
            # unmeasured model and a weak-but-unmeasured one would score identically.
            # The evidence-discount still heavily penalizes the no-data guess.
            raw_score = (1.0 / (1.0 + math.exp(-_IRT_SLOPE * acc)) if total_weight > 0
                         else min(1.0, max(0.0, model.intelligence_index / 100.0)))
        elif method == "geometric_mean" and weights:
            log_sum = 0.0
            for eval_key, weight in weights.items():
                eval_val = model.eval(eval_key, -1.0)
                if eval_val < 0:
                    continue
                present_count += 1
                if eval_key.startswith("artificial_analysis_"):
                    eval_val = eval_val / 100.0
                # Use epsilon to avoid log(0)
                log_sum += weight * math.log(max(eval_val, 1e-12))
                total_weight += weight
            # No-evidence fallback to the AA intelligence index, matching weighted_avg/irt
            # (else a no-eval model scored 0.0 and was unfairly buried vs the other methods).
            raw_score = (math.exp(log_sum / total_weight) if total_weight > 0
                         else min(1.0, max(0.0, model.intelligence_index / 100.0)))
        else:
            # weighted_avg (default) and softmax both start here
            raw_score = 0.0
            for eval_key, weight in weights.items():
                eval_val = model.eval(eval_key, -1.0)
                if eval_val < 0:
                    continue
                present_count += 1
                if eval_key.startswith("artificial_analysis_"):
                    eval_val = eval_val / 100.0
                if method == "softmax":
                    raw_score += weight * math.exp(eval_val)
                    total_weight += weight
                else:
                    raw_score += eval_val * weight
                    total_weight += weight

            if total_weight == 0:
                raw_score = model.intelligence_index / 100.0
            elif method == "softmax":
                raw_score = math.log(raw_score) / total_weight if raw_score > 0 else 0.0
            else:
                raw_score = raw_score / total_weight

        # Confidence from eval coverage
        coverage = present_count / max(1, total_keys)
        confidence = coverage  # simple: more evals = more confidence

        # Tier penalty from catalog (no string matching)
        tier_pen = self.catalog.tier_penalties.get(model.tier, 1.0)
        raw_score *= tier_pen

        # Dynamic price normalization. R11: discount the INPUT portion by this route's
        # observed cache-hit economics (input_price_factor in (0,1]; 1.0 = no caching /
        # unknown -> sticker price). Cache-heavy agent sessions become genuinely cheaper.
        eff_price_input = model.price_input * input_price_factor
        blended_price = eff_price_input * 0.3 + model.price_output * 0.7
        cost_pen = min(1.0, blended_price / self._max_blended_price) if self._max_blended_price > 0 else 0.0

        # Speed bonus for executor, SCALED by how much THIS task actually values
        # speed (the demand vector's `speed` axis, sharpened the same way bench
        # weights are). A latency-sensitive chat (speed≈peak) gets the full bonus so
        # a fast model wins; a quality-heavy coding/reasoning task (speed≈0 after
        # sharpening) gets ~none, so capability — not tokens/sec — decides. Without a
        # demand vector (CLI route) it falls back to a moderate 0.5 weight. This is
        # what lets the EXECUTOR pick also track the message, not just favor the
        # fastest all-rounder regardless of task.
        speed_bonus = 0.0
        if role == "executor":
            sd = _sharpen_demands(profile.demands) if profile.demands else {}
            speed_demand = float(sd.get("speed", 0.5)) if profile.demands else 0.5
            speed_bonus = min(1.0, model.speed_tps / 200.0) * 0.1 * speed_demand

        # Evidence discount: a model with little/no benchmark data for this role
        # must NOT outrank a fully-measured one on an unproven raw-intelligence
        # guess. CONCAVE in coverage (coverage**_EVIDENCE_EXP, exp<1): bites HARD
        # near zero coverage (stops no-data hijackers) but only LIGHTLY for a model
        # that's missing just 1-of-N benches — otherwise a well-evidenced, genuinely
        # stronger model loses a close race to a weaker one that merely has one more
        # benchmark filled in (a data-completeness artifact, not real capability).
        # Full coverage = no change; zero coverage = capability counts for min_evidence.
        if total_keys > 0:
            evidence = self.min_evidence + (1.0 - self.min_evidence) * (coverage ** _EVIDENCE_EXP)
            raw_score *= evidence

        bias = profile.quality_bias
        final = raw_score * bias + (1.0 - cost_pen) * (1.0 - bias) * 0.5 + speed_bonus
        return final, confidence, coverage


class NoModelAvailable(RuntimeError):
    """Raised when no catalog model satisfies the constraints."""
