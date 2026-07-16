"""Cost-per-success benchmark (Phase 4, proof of value).

The product thesis (req B1): a well-orchestrated mid-tier stack beats one
expensive model used blindly, on COST-PER-SUCCESS. This module measures exactly
that: run a fixed task set under (a) the routed Syntra stack and (b) a single
pinned expensive model for every role, then compare $-per-passing-task.

Split for honesty + testability:
- The report math (cost_per_success, deltas, win/lose) is PURE -> unit-tested.
- run_suite() drives real loop.run() calls -> exercised with a fake provider in
  tests; meaningful numbers require live providers (run via a script, not the
  unit suite, per PLAN Phase 4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchOutcome:
    task: str
    mode: str            # "routed" | "baseline"
    passed: bool
    cost_usd: float
    in_tokens: int = 0
    out_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "task": self.task, "mode": self.mode, "passed": self.passed,
            "cost_usd": round(self.cost_usd, 6),
            "in_tokens": self.in_tokens, "out_tokens": self.out_tokens,
        }


@dataclass
class ModeSummary:
    mode: str
    n: int = 0
    passes: int = 0
    total_cost_usd: float = 0.0

    def success_rate(self) -> float:
        return (self.passes / self.n) if self.n else 0.0

    def cost_per_success(self) -> float:
        """Total cost divided by passing tasks. inf if nothing passed."""
        return (self.total_cost_usd / self.passes) if self.passes else math.inf

    def to_dict(self) -> dict:
        cps = self.cost_per_success()
        return {
            "mode": self.mode, "n": self.n, "passes": self.passes,
            "success_rate": round(self.success_rate(), 4),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "cost_per_success": (None if math.isinf(cps) else round(cps, 6)),
        }


def summarize(outcomes, mode: str) -> ModeSummary:
    s = ModeSummary(mode=mode)
    for o in outcomes:
        if o.mode != mode:
            continue
        s.n += 1
        s.total_cost_usd += max(0.0, o.cost_usd)
        if o.passed:
            s.passes += 1
    s.total_cost_usd = round(s.total_cost_usd, 6)
    return s


@dataclass
class BenchReport:
    routed: ModeSummary
    baseline: ModeSummary

    def cost_per_success_delta(self) -> float:
        """baseline - routed. Positive => routed is cheaper per success."""
        b, r = self.baseline.cost_per_success(), self.routed.cost_per_success()
        if math.isinf(b) or math.isinf(r):
            return 0.0
        return round(b - r, 6)

    def routed_wins(self) -> bool:
        """Routed wins if it is at least as successful AND cheaper per success."""
        if self.routed.success_rate() < self.baseline.success_rate():
            return False
        return self.routed.cost_per_success() <= self.baseline.cost_per_success()

    def savings_pct(self) -> float:
        b = self.baseline.cost_per_success()
        if math.isinf(b) or b == 0:
            return 0.0
        delta = self.cost_per_success_delta()
        return round(100.0 * delta / b, 2)

    def to_dict(self) -> dict:
        return {
            "routed": self.routed.to_dict(),
            "baseline": self.baseline.to_dict(),
            "cost_per_success_delta": self.cost_per_success_delta(),
            "savings_pct": self.savings_pct(),
            "routed_wins": self.routed_wins(),
        }

    def render(self) -> str:
        def _cps(s):
            c = s.cost_per_success()
            return "n/a" if math.isinf(c) else f"${c:.5f}"
        lines = [
            "=== cost-per-success benchmark ===",
            f"{'MODE':10} {'N':>3} {'PASS':>5} {'RATE':>6} {'TOTAL$':>10} {'$/SUCCESS':>11}",
        ]
        lines.extend(f"{s.mode:10} {s.n:>3} {s.passes:>5} {s.success_rate()*100:>5.0f}% "
                     f"{s.total_cost_usd:>10.5f} {_cps(s):>11}" for s in (self.routed, self.baseline))
        verdict = "ROUTED WINS" if self.routed_wins() else "baseline not beaten"
        lines.append(f"delta $/success: {self.cost_per_success_delta():+.5f}  "
                     f"({self.savings_pct():+.1f}%)  -> {verdict}")
        return "\n".join(lines)


def build_report(outcomes) -> BenchReport:
    return BenchReport(routed=summarize(outcomes, "routed"),
                       baseline=summarize(outcomes, "baseline"))


def run_suite(make_loop, tasks, *, workspace_root: str,
              routed_config, baseline_config) -> list[BenchOutcome]:
    """Run each task under routed + baseline configs; collect outcomes.

    make_loop() -> a fresh Loop (so stats/state don't bleed between modes).
    routed_config / baseline_config are LoopConfig instances (baseline pins the
    single expensive model for all roles). Failures count as not-passed at their
    measured cost (so a crash doesn't silently help a mode).
    """
    outcomes: list[BenchOutcome] = []
    for task in tasks:
        for mode, cfg in (("routed", routed_config), ("baseline", baseline_config)):
            loop = make_loop()
            try:
                result = loop.run(task, workspace_root=workspace_root, config=cfg)
                in_tok, out_tok = result.state.total_tokens()
                outcomes.append(BenchOutcome(
                    task=task, mode=mode, passed=(result.verdict == "pass"),
                    cost_usd=result.state.total_cost_usd(),
                    in_tokens=in_tok, out_tokens=out_tok,
                ))
            except Exception:
                outcomes.append(BenchOutcome(task=task, mode=mode, passed=False, cost_usd=0.0))
    return outcomes
