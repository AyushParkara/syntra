"""Loop guard: stop spirals and token explosions.

The cognitive loop (planner -> executor -> reviewer) must NEVER run forever and
must NEVER quietly burn an unbounded number of tokens. Legacy's pain (P6:
"infinite retry loops, token explosions") is fixed here with a small,
deterministic, fully unit-testable guard.

Design notes:
- The guard holds NO I/O. It is pure counters + policy thresholds, so the
  decision logic is deterministic and testable offline.
- Persistence is done by the caller via ``to_dict()`` (loop writes ``loop.json``).
- A "repeated-failure signature" is ``step_id|normalized(error)``. Hitting the
  exact same wall N times means we are stuck, not making progress -> halt. This
  is the anti-"hit the same wall twice" rule from PLAN 14b.4.

Contract (PLAN 10, Phase 1):
    LoopPolicy(max_steps, max_retries_per_role, max_repeated_failures, max_tokens)
    evaluate() -> LoopDecision(can_continue, reason, tokens_used, budget)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Reasons a guard halts. Kept as constants so callers/tests don't string-match
# free text.
HALT_MAX_STEPS = "max_steps_exceeded"
HALT_MAX_TOKENS = "token_budget_exceeded"
HALT_REPEATED_FAILURE = "repeated_failure"
HALT_ROLE_RETRIES = "role_retry_budget_exhausted"
HALT_AGENT_TOKENS = "agent_token_budget_exceeded"

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+")


def _signature(step_id: str, error: str) -> str:
    """Normalize a (step, error) pair into a stable signature.

    Strips digits and collapses whitespace so that "timeout after 30s" and
    "timeout after 45s" count as the same wall. Truncated to keep it bounded.
    """
    norm = (error or "").strip().lower()
    norm = _NUM.sub("#", norm)
    norm = _WS.sub(" ", norm)
    return f"{step_id}|{norm[:160]}"


@dataclass(frozen=True)
class LoopPolicy:
    """Thresholds for one loop run. All limits are inclusive ceilings."""

    max_steps: int = 20
    max_retries_per_role: int = 3
    max_repeated_failures: int = 2
    max_tokens: int = 500_000
    # Per-agent token ceiling for delegated subagents. A single runaway low-signal
    # subagent is the documented dominant multi-agent cost driver; this halts it
    # before it pollutes the lead's context. 0 = disabled (backward-compatible).
    max_tokens_per_agent: int = 0

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_retries_per_role", "max_repeated_failures",
                     "max_tokens", "max_tokens_per_agent"):
            if getattr(self, name) < 0:
                raise ValueError(f"LoopPolicy.{name} must be >= 0")


@dataclass
class LoopDecision:
    """Result of an ``evaluate()`` call."""

    can_continue: bool
    reason: str          # "" when can_continue is True; a HALT_* code otherwise
    tokens_used: int
    budget: int          # the token budget (policy.max_tokens) for visibility

    @property
    def halted(self) -> bool:
        return not self.can_continue


@dataclass
class LoopLedger:
    """Mutable running tally for one loop. Persisted as ``loop.json``."""

    steps_started: int = 0
    retries_by_role: dict[str, int] = field(default_factory=dict)
    failure_signatures: dict[str, int] = field(default_factory=dict)
    tokens_used: int = 0
    tokens_by_agent: dict[str, int] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "_schema_version": 2,
            "steps_started": self.steps_started,
            "retries_by_role": dict(self.retries_by_role),
            "repeated_failures": {k: v for k, v in self.failure_signatures.items() if v > 1},
            "tokens_used": self.tokens_used,
            "tokens_by_agent": dict(self.tokens_by_agent),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


class LoopGuard:
    """Deterministic spiral / budget guard. No I/O.

    Usage in the loop:
        guard = LoopGuard(LoopPolicy(...))
        ...
        guard.record_step()
        d = guard.evaluate()
        if d.halted: stop with d.reason
        ...
        guard.record_tokens(in_tok + out_tok)
        guard.record_retry("executor")
        guard.record_failure(step_id, error_text)
    """

    def __init__(self, policy: LoopPolicy | None = None, ledger: LoopLedger | None = None):
        self.policy = policy or LoopPolicy()
        self.ledger = ledger or LoopLedger()

    # -------------------------------------------------------------- recording

    def record_step(self) -> None:
        self.ledger.steps_started += 1

    def record_tokens(self, n: int, agent: str | None = None) -> None:
        """Add tokens to the global tally; also to a per-agent tally when `agent`
        is named (for delegated subagents). `agent=None` = main loop, global only."""
        if n > 0:
            self.ledger.tokens_used += int(n)
            if agent:
                self.ledger.tokens_by_agent[agent] = self.ledger.tokens_by_agent.get(agent, 0) + int(n)

    def agent_tokens(self, agent: str) -> int:
        return self.ledger.tokens_by_agent.get(agent, 0)

    def record_retry(self, role: str) -> int:
        """Record one retry for a role. Returns the new retry count for that role."""
        self.ledger.retries_by_role[role] = self.ledger.retries_by_role.get(role, 0) + 1
        return self.ledger.retries_by_role[role]

    def record_failure(self, step_id: str, error: str) -> int:
        """Record a failed attempt. Returns how many times THIS exact wall was hit."""
        sig = _signature(step_id, error)
        self.ledger.failure_signatures[sig] = self.ledger.failure_signatures.get(sig, 0) + 1
        return self.ledger.failure_signatures[sig]

    # -------------------------------------------------------------- evaluation

    def evaluate(self) -> LoopDecision:
        """Decide whether the loop may continue, given the current ledger.

        Order of checks is stable so the *first* violated limit is reported.
        Once halted, the ledger remembers the reason (idempotent).
        """
        p = self.policy
        led = self.ledger

        reason = ""
        if led.steps_started > p.max_steps:
            reason = HALT_MAX_STEPS
        elif p.max_tokens and led.tokens_used > p.max_tokens:
            reason = HALT_MAX_TOKENS
        elif any(c > p.max_repeated_failures for c in led.failure_signatures.values()):
            reason = HALT_REPEATED_FAILURE
        elif any(c > p.max_retries_per_role for c in led.retries_by_role.values()):
            reason = HALT_ROLE_RETRIES
        elif p.max_tokens_per_agent and any(
                c > p.max_tokens_per_agent for c in led.tokens_by_agent.values()):
            reason = HALT_AGENT_TOKENS

        if reason:
            led.halted = True
            led.halt_reason = reason
            return LoopDecision(False, reason, led.tokens_used, p.max_tokens)

        return LoopDecision(True, "", led.tokens_used, p.max_tokens)

    # -------------------------------------------------------------- helpers

    def remaining_role_retries(self, role: str) -> int:
        used = self.ledger.retries_by_role.get(role, 0)
        return max(0, self.policy.max_retries_per_role - used)

    def to_dict(self) -> dict:
        return self.ledger.to_dict()
