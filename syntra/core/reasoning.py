"""Reasoning-effort policy + capability gating.

Models differ: some accept a `reasoning_effort` / thinking budget, most do not.
Sending a reasoning parameter to a model that doesn't support it wastes a call
(or errors). Syntra decides the right effort per task and ONLY sends the param
to routes that can actually use it (req A2, A5; PLAN §10 P1, §14b.2 #8).

Design:
- Pure + deterministic (no I/O), like loopguard/verification -> fully unit-tested.
- Capability is detected from catalog metadata (a `reasoning`/`thinking`
  specialty tag), NOT a hardcoded model list (req A5).
- Risk/criticality raise a reasoning FLOOR (escalate vs risk); cost stays bounded
  because effort is only escalated when criticality warrants it (req A2).

Contract:
    ReasoningLevel.NONE < LOW < MEDIUM < HIGH < XHIGH
    resolve_level(model, base, criticality, complexity) -> ReasoningLevel
    reasoning_params(level) -> dict   # {} when NONE / unsupported
"""

from __future__ import annotations

from enum import IntEnum


class ReasoningLevel(IntEnum):
    """Ordered effort levels. NONE means: send no reasoning param at all."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    XHIGH = 4

    @property
    def effort(self) -> str:
        """The provider-facing `reasoning_effort` string (empty for NONE)."""
        return "" if self is ReasoningLevel.NONE else self.name.lower()

    @classmethod
    def parse(cls, value: str | "ReasoningLevel | None") -> "ReasoningLevel":
        if isinstance(value, ReasoningLevel):
            return value
        if not value:
            return cls.NONE
        key = str(value).strip().upper()
        # tolerate common synonyms
        synonyms = {"OFF": "NONE", "MIN": "LOW", "MAX": "XHIGH", "EXTRA_HIGH": "XHIGH"}
        key = synonyms.get(key, key)
        try:
            return cls[key]
        except KeyError:
            return cls.NONE


# Tags on a catalog model that indicate it supports a thinking / reasoning mode.
_REASONING_TAGS = ("reasoning", "thinking", "reasoner", "extended-thinking")


def supports_reasoning(model) -> bool:
    """True if the catalog metadata marks this model as reasoning-capable.

    Dynamic (req A5): based on the model's specialty tags, not a hardcoded list.
    Accepts any object exposing a ``specialties`` iterable.
    """
    specs = getattr(model, "specialties", ()) or ()
    low = {str(s).lower() for s in specs}
    return any(tag in low for tag in _REASONING_TAGS)


# The standard provider-facing effort ladder for a reasoning-capable model. NONE is
# implicit (omit the param); these are the SELECTABLE levels a slider should offer.
_STANDARD_EFFORT_LADDER = ("low", "medium", "high", "xhigh")
_LADDER_ORDER = {name: i for i, name in enumerate(("low", "medium", "high", "xhigh"))}


def effort_levels_for(model) -> tuple[str, ...]:
    """The ordered effort levels a model actually supports (T13).

    Returns ``()`` for a non-reasoning model (so the UI shows NO slider / "n/a"),
    else the supported levels low→…→xhigh. Data-driven, never a hardcoded per-model
    list (req I1 "don't hardcode otherwise it won't work"):
      - a model may declare its own ceiling/levels via catalog metadata — either an
        ``evals['reasoning_levels']`` list, or an ``evals['max_effort']`` string that
        truncates the standard ladder (e.g. "high" -> low/medium/high, no xhigh);
      - otherwise it gets the full standard ladder.
    The runtime ``_reasoning_unsupported`` detection in the loop still strips the param
    if a specific key/endpoint rejects it — this function is the static capability view.
    """
    if not supports_reasoning(model):
        return ()
    evals = getattr(model, "evals", None) or {}
    # explicit per-model level list wins (fully data-driven)
    declared = evals.get("reasoning_levels")
    if isinstance(declared, (list, tuple)) and declared:
        ordered = sorted((str(x).lower() for x in declared
                          if str(x).lower() in _LADDER_ORDER), key=_LADDER_ORDER.get)
        return tuple(dict.fromkeys(ordered)) or _STANDARD_EFFORT_LADDER
    # a declared ceiling truncates the standard ladder
    ceiling = str(evals.get("max_effort", "")).lower()
    if ceiling in _LADDER_ORDER:
        return _STANDARD_EFFORT_LADDER[: _LADDER_ORDER[ceiling] + 1]
    return _STANDARD_EFFORT_LADDER


def floor_for_risk(criticality: str, complexity: str = "medium") -> ReasoningLevel:
    """Minimum reasoning level justified by task risk.

    High-criticality or complex work must not run at trivial effort; low-stakes
    work should not burn an expensive reasoning budget (req A2 cost discipline).
    """
    crit = (criticality or "low").lower()
    comp = (complexity or "medium").lower()

    if crit == "high":
        floor = ReasoningLevel.HIGH
    elif crit == "medium":
        floor = ReasoningLevel.MEDIUM
    else:
        floor = ReasoningLevel.NONE

    # Complex work nudges the floor up one notch. High-criticality + complex is the
    # top of the AUTO range -> XHIGH ("max"), matching how coding agents
    # escalate the hardest tasks to maximum thinking. Medium/low-stakes
    # work stays bounded (HIGH ceiling), so cost only climbs when risk warrants it.
    if comp == "complex":
        cap = ReasoningLevel.XHIGH if crit == "high" else ReasoningLevel.HIGH
        if floor < cap:
            floor = ReasoningLevel(min(int(cap), int(floor) + 1))

    return floor


def resolve_level(
    model,
    *,
    base: ReasoningLevel | str = ReasoningLevel.NONE,
    criticality: str = "low",
    complexity: str = "medium",
) -> ReasoningLevel:
    """Decide the effective reasoning level for a (model, task) pair.

    1. Start from the user's base level.
    2. Escalate to the risk floor (max of base and floor).
    3. Capability gate: if the model can't reason, return NONE (omit the param).
    """
    base_level = ReasoningLevel.parse(base)
    floor = floor_for_risk(criticality, complexity)
    desired = ReasoningLevel(max(int(base_level), int(floor)))

    if desired is ReasoningLevel.NONE:
        return ReasoningLevel.NONE
    if not supports_reasoning(model):
        return ReasoningLevel.NONE  # gate: don't send to incapable routes
    return desired


def reasoning_params(level: ReasoningLevel | str) -> dict:
    """Provider kwargs for a level. Empty dict when NONE / unsupported.

    Uses the de-facto-standard OpenAI-compatible `reasoning_effort` field. The
    caller merges this into the request body only when it is non-empty.
    """
    lvl = ReasoningLevel.parse(level)
    if lvl is ReasoningLevel.NONE:
        return {}
    return {"reasoning_effort": lvl.effort}
