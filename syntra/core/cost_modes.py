"""Cost-aware routing modes (T5).

Three user-selectable modes that govern HOW expensive a run is allowed to be, across ALL
paths (chat, executor-only, full pipeline). The mode is a small bundle of the cost knobs the
router + loop already honor (quality_bias, executor_cost_floor, which roles take the
cheapest-strong-enough pick, the chat bias) — so it steers cost WITHOUT a router rewrite.

  budget  (default) — balanced. Cost-floor on executor + reviewer; the planner stays strong
                       (it's load-bearing). On a high-stakes task the planner may PROPOSE
                       exceeding the cap; that escalation surfaces at the confirm gate for the
                       user's OK before any frontier spend.
  im-a-millionaire  — quality-max. Frontier allowed everywhere, cost-floor off. Obeyed
                       silently (no nudge). Aliases: frontier / quality-max / max.
  pennies           — max saving. Aggressive cost-floor on ALL three roles + cheapest viable
                       chat. Obeyed silently.

Pure + deterministic -> fully unit-tested; the loop calls apply_cost_mode() once before routing.
"""

from __future__ import annotations

import dataclasses

# Canonical mode ids + the synonyms a user might type (the TUI/CLI normalize through here).
BUDGET = "budget"
MILLIONAIRE = "im-a-millionaire"
PENNIES = "pennies"
ANDHA_PAISA = "andha-paisa"
MODES = (BUDGET, MILLIONAIRE, PENNIES)

_SYNONYMS = {
    "budget": BUDGET, "balanced": BUDGET, "default": BUDGET,
    "im-a-millionaire": MILLIONAIRE, "millionaire": MILLIONAIRE, "im a millionaire": MILLIONAIRE,
    "frontier": MILLIONAIRE, "quality": MILLIONAIRE, "max": MILLIONAIRE, "quality-max": MILLIONAIRE,
    "pennies": PENNIES, "pennies-in-pocket": PENNIES, "pennies in pocket": PENNIES,
    "andha-paisa": ANDHA_PAISA, "andha paisa": ANDHA_PAISA,
    "cheap": PENNIES, "save": PENNIES, "saving": PENNIES,
}

# Per-mode knob bundle. quality_bias: capability vs cost weight (higher = pricier/stronger).
# cost_floor: "strong enough" = score >= best*floor (higher = closer to flagship, pricier).
# cost_floor_roles: which roles take the cheapest-strong-enough pick. direct_quality_bias: chat.
_BUNDLES = {
    BUDGET: {"quality_bias": 0.7, "executor_cost_floor": 0.88, "executor_cost_aware": True,
                 "cost_floor_roles": ("executor", "reviewer"), "direct_quality_bias": 0.5,
                 "allow_raise": True},
    MILLIONAIRE: {"quality_bias": 0.85, "executor_cost_floor": 0.88, "executor_cost_aware": False,
                      "cost_floor_roles": (), "direct_quality_bias": 0.7, "allow_raise": False},
    PENNIES: {"quality_bias": 0.45, "executor_cost_floor": 0.80, "executor_cost_aware": True,
                  "cost_floor_roles": ("planner", "executor", "reviewer"), "direct_quality_bias": 0.3,
                  "allow_raise": False},
    ANDHA_PAISA: {"quality_bias": 0.45, "executor_cost_floor": 0.80, "executor_cost_aware": True,
                  "cost_floor_roles": ("planner", "executor", "reviewer"), "direct_quality_bias": 0.3,
                  "allow_raise": False},
}


def normalize_mode(value) -> str:
    """Map any user spelling to a canonical mode id; unknown -> budget (the safe default)."""
    key = str(value or "").strip().lower()
    return _SYNONYMS.get(key, BUDGET)


def is_known(value) -> bool:
    """True if `value` spells a real mode (so a typo can be rejected vs silently → budget)."""
    return str(value or "").strip().lower() in _SYNONYMS


def load_mode(state_root) -> str:
    """Read the persisted default cost mode from <state_root>/cost_mode (budget if absent)."""
    from pathlib import Path
    try:
        return normalize_mode(Path(state_root, "cost_mode").read_text().strip())
    except (OSError, ValueError):
        return BUDGET


def save_mode(state_root, mode: str) -> str:
    """Persist the default cost mode; returns the normalized mode written."""
    from pathlib import Path
    norm = normalize_mode(mode)
    p = Path(state_root)
    p.mkdir(parents=True, exist_ok=True)
    (p / "cost_mode").write_text(norm)
    return norm


def mode_bundle(mode: str) -> dict:
    """The knob bundle for a (already-normalized) mode."""
    return dict(_BUNDLES[normalize_mode(mode)])


def apply_cost_mode(config):
    """Return a COPY of `config` with the cost knobs set from its `cost_mode`. Leaves an
    explicitly-pinned model alone (pins always win). Idempotent + never mutates the input."""
    mode = normalize_mode(getattr(config, "cost_mode", BUDGET))
    b = _BUNDLES[mode]
    return dataclasses.replace(
        config,
        cost_mode=mode,
        quality_bias=b["quality_bias"],
        executor_cost_floor=b["executor_cost_floor"],
        executor_cost_aware=b["executor_cost_aware"],
        cost_floor_roles=tuple(b["cost_floor_roles"]),
        direct_quality_bias=b["direct_quality_bias"],
    )


def allows_raise(mode: str) -> bool:
    """True if this mode lets the planner PROPOSE exceeding the cap (ask-to-raise) — only
    budget. im-a-millionaire already allows frontier; pennies never raises."""
    return normalize_mode(mode) == BUDGET


def should_nudge_plan_review(config) -> bool:
    """Budget mode (the default) shows a one-line 'keep plan-review on' nudge ONLY when it's
    the default mode AND plan-review is currently off. An explicitly-set mode is obeyed silently."""
    return (normalize_mode(getattr(config, "cost_mode", BUDGET)) == BUDGET
            and not getattr(config, "plan_approval", False))
