"""Cost pre-flight estimation.

Cost must be visible BEFORE any spend (Pillar 3, PLAN §6 #8, req B1). This
module turns a goal + chosen routes into a grounded cost estimate, so the user
sees "$X" before approving a run.

Pure + deterministic (no I/O, no network) -> fully unit-tested. Estimates are
explicitly approximate: token counts are derived from character length (no
tokenizer dependency, req F10 minimal deps) and step counts are expected values.
The post-run actuals in cost.json are the source of truth; this is the preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Rough chars-per-token for English+code. Real tokenizers vary 3.5-4.5; 4.0 is a
# sane, slightly-conservative default. Kept as a constant so it's tunable/testable.
DEFAULT_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Approximate token count from character length.

    Deterministic and dependency-free. Empty text -> 0. Always rounds up so a
    non-empty string never estimates as 0 tokens.
    """
    if not text:
        return 0
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be > 0")
    import math
    return max(1, math.ceil(len(text) / chars_per_token))


def count_tokens(text: str, *, counter=None, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Accurate token count when a provider `counter` is available, else the heuristic (#166).

    `counter(text) -> int | None` is an optional accurate token-counter (e.g. a provider's
    count-tokens endpoint, injected by the caller so this module stays pure + dependency-free).
    When it returns a positive int, that wins; when it returns None / a bad value / raises, we
    fall back to `estimate_tokens`. Empty text is always 0 (the counter isn't even consulted)."""
    if not text:
        return 0
    if counter is not None:
        try:
            n = counter(text)
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                return n
        except Exception:  # noqa: BLE001 - an accurate count is a bonus; never fail the estimate
            pass
    return estimate_tokens(text, chars_per_token)


def estimate_call_cost(model, input_tokens: int, output_tokens: int,
                       *, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                       web_searches: int = 0,
                       price_cache_read: float | None = None,
                       price_cache_write: float | None = None,
                       price_web_search: float | None = None) -> float:
    """USD for one call. Matches Loop._record_cost so preview ~ actuals.

    Accepts any object with ``price_input`` / ``price_output`` (USD per 1M tokens).

    #213 — full line items. Once prompt-caching is on, input/output-only pricing
    mis-estimates: cached-input tokens are billed at a CHEAPER read rate, a cache
    WRITE carries a surcharge, and a web-search tool call is a flat per-search fee.
    All extras DEFAULT to 0 (and unpriced components fall back sensibly), so the
    classic 3-arg call is byte-identical to before. Per-unit prices are taken from
    the model when present (`price_cache_read`/`price_cache_write` in $/1M,
    `price_web_search` in $/search) and can be overridden per call for estimates."""
    p_in = float(getattr(model, "price_input", 0.0))
    p_out = float(getattr(model, "price_output", 0.0))
    # cache-read defaults to a typical 0.1× of input; cache-write to input rate.
    p_cr = price_cache_read if price_cache_read is not None else \
        float(getattr(model, "price_cache_read", p_in * 0.1))
    p_cw = price_cache_write if price_cache_write is not None else \
        float(getattr(model, "price_cache_write", p_in))
    p_ws = price_web_search if price_web_search is not None else \
        float(getattr(model, "price_web_search", 0.0))
    cost = (
        (input_tokens / 1_000_000) * p_in
        + (output_tokens / 1_000_000) * p_out
        + (max(0, cache_read_tokens) / 1_000_000) * p_cr
        + (max(0, cache_write_tokens) / 1_000_000) * p_cw
        + max(0, web_searches) * p_ws
    )
    return round(cost, 6)


@dataclass
class RoleEstimate:
    role: str
    model_id: str
    provider: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "model_id": self.model_id,
            "provider": self.provider,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class CostEstimate:
    roles: list[RoleEstimate] = field(default_factory=list)
    expected_steps: int = 0

    @property
    def total_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.roles), 6)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.roles)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.roles)

    def to_dict(self) -> dict:
        return {
            "_schema_version": 1,
            "expected_steps": self.expected_steps,
            "total_usd": self.total_usd,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "roles": [r.to_dict() for r in self.roles],
        }


# Typical per-call output sizes by role (input is derived from real text where
# possible). These are expectation values, documented and tunable.
_DEFAULT_OUTPUT_TOKENS = {
    "planner": 800,     # a concise numbered plan
    "executor": 1500,   # per-step deliverable
    "reviewer": 600,    # a verdict + issues
}
# How much context each role sees, as a multiple of the goal-derived base, plus
# the accumulating prior-step results the executor/reviewer carry.
_INPUT_BASE_MULT = {
    "planner": 1.2,
    "executor": 3.0,    # goal + plan + prior results
    "reviewer": 4.0,    # goal + plan + all step previews
}


def estimate_run(
    goal: str,
    *,
    planner,
    executor,
    reviewer,
    planner_provider: str = "",
    executor_provider: str = "",
    reviewer_provider: str = "",
    expected_steps: int = 5,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    counter=None,
) -> CostEstimate:
    """Estimate the cost of one planner->executor->reviewer run.

    Input tokens are GROUNDED in the actual goal length (not a blind constant);
    output tokens + context multipliers are documented expectation values. The
    executor cost scales with ``expected_steps``.

    #166: when a `counter(text)->int|None` is supplied (e.g. the routed provider's
    count-tokens endpoint), the goal-token count comes from it (accurate); otherwise
    the char heuristic is used, so existing callers are unchanged.
    """
    steps = max(1, int(expected_steps))
    goal_tokens = count_tokens(goal, counter=counter, chars_per_token=chars_per_token)
    # A small floor so an empty/short goal still yields a sensible system-prompt-
    # sized input estimate.
    base_in = max(goal_tokens, 150)

    def _role(role, model, provider, calls):
        in_tok = int(base_in * _INPUT_BASE_MULT[role]) * calls
        out_tok = _DEFAULT_OUTPUT_TOKENS[role] * calls
        return RoleEstimate(
            role=role,
            model_id=getattr(model, "id", "?"),
            provider=provider,
            calls=calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_call_cost(model, in_tok, out_tok),
        )

    est = CostEstimate(expected_steps=steps)
    est.roles.append(_role("planner", planner, planner_provider, 1))
    est.roles.append(_role("executor", executor, executor_provider, steps))
    est.roles.append(_role("reviewer", reviewer, reviewer_provider, 1))
    return est
