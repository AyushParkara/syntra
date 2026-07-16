"""Structured compaction + continuity handoff (Phase 2).

The problem this solves: tools compact/summarize context mid-task and the
model "loses its train of thought" -- constraints, rationale, and prior
decisions get destroyed by lazy summarization.

Syntra's answer: task knowledge lives in typed state files (state.py), and the
continuity handoff is generated DETERMINISTICALLY from those files -- never from
a model summary. Compaction may drop disposable reasoning, but the goal,
decisions, failures, and current step are reconstructed verbatim from structured
state, so the concept cannot drift (PLAN Section 15 + risk mitigation).

This module is pure (no I/O, no network) -> fully unit-tested. Persistence
(handoff.md) is done by the caller via TaskStore.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ContextClass(str, Enum):
    """How a piece of context is treated under compaction."""

    PRESERVE = "preserve"            # never drop/summarize (goal, decisions, constraints, current step, failures)
    SUMMARIZE = "summarize"          # may be compressed (old completed-step results, stale reasoning)
    DROP_CANDIDATE = "drop_candidate"  # disposable (debug noise, raw traces)


# Item kinds -> classification. Kinds are stable strings so callers/tests don't
# guess. Anything unknown is conservatively PRESERVED (never silently dropped).
_PRESERVE_KINDS = {
    "goal", "decision", "constraint", "current_step", "active_step",
    "failure", "pinned", "summary",
}
_SUMMARIZE_KINDS = {
    "step_result", "prior_result", "completed_step", "old_reasoning", "history",
}
_DROP_KINDS = {
    "debug", "noise", "raw_event", "trace", "transient",
}


def classify_kind(kind: str) -> ContextClass:
    """Classify a context item by its kind. Unknown -> PRESERVE (safe default)."""
    k = (kind or "").strip().lower()
    if k in _DROP_KINDS:
        return ContextClass.DROP_CANDIDATE
    if k in _SUMMARIZE_KINDS:
        return ContextClass.SUMMARIZE
    # PRESERVE for known-preserve kinds AND for anything unknown: we never drop
    # something we don't understand (anti data-loss).
    return ContextClass.PRESERVE


@dataclass(frozen=True)
class CompactionPolicy:
    """Thresholds that trigger compaction."""

    max_messages: int = 40
    max_tokens: int = 100_000

    def __post_init__(self) -> None:
        if self.max_messages < 0 or self.max_tokens < 0:
            raise ValueError("CompactionPolicy thresholds must be >= 0")


@dataclass(frozen=True)
class CompactionSignal:
    """A snapshot used to decide whether to compact right now."""

    message_count: int = 0
    est_tokens: int = 0
    active_step: bool = False       # a step is mid-execution
    pending_approval: bool = False  # waiting on a human

    def should_compact(self, policy: CompactionPolicy) -> bool:
        """Compact only when over a threshold AND nothing active is in flight.

        Never compact while a step is executing or an approval is pending --
        that is exactly when constraints get lost (PLAN Section 15).
        """
        if self.active_step or self.pending_approval:
            return False
        over_messages = policy.max_messages and self.message_count > policy.max_messages
        over_tokens = policy.max_tokens and self.est_tokens > policy.max_tokens
        return bool(over_messages or over_tokens)


def estimate_tokens(messages) -> int:
    """Cheap token estimate for a list of chat messages (~4 chars/token).

    Counts text content plus any tool-call arguments. Deterministic; used to
    decide auto-compaction without a tokenizer dependency (an approximate
    char-based token count)."""
    chars = 0
    for m in messages:
        chars += len(getattr(m, "content", "") or "")
        for tc in getattr(m, "tool_calls", ()) or ():
            chars += len(getattr(tc, "arguments", "") or "") + len(getattr(tc, "name", "") or "")
    return chars // 4


def auto_compact_messages(messages, *, keep_last: int = 6, max_tokens: int = 100_000,
                          reserve_tokens: int = 0):
    """If `messages` exceeds the effective budget, compact the MIDDLE into a
    summary line, preserving the system prompt, the first user goal, and the last
    `keep_last` turns. Returns (new_messages, compacted: bool). Pure.

    reserve_tokens: headroom kept free for the model's RESPONSE. The effective
    trigger threshold is (max_tokens - reserve_tokens), so we compact BEFORE the
    window is so full there's no room to reply. Defaults
    to 0 → identical to the prior behavior for existing callers.

    Inline auto-compaction: replace stale history with a summary,
    keep the goal + recent context so the model can continue coherently.
    """
    from ..providers.openai_compat import ChatMessage
    effective = max(1, max_tokens - max(0, reserve_tokens))
    if estimate_tokens(messages) <= effective or len(messages) <= keep_last + 2:
        return list(messages), False

    head = []
    rest = list(messages)
    # keep leading system message(s)
    while rest and rest[0].role == "system":
        head.append(rest.pop(0))
    # keep the first user message (the goal)
    if rest and rest[0].role == "user":
        head.append(rest.pop(0))
    tail = rest[-keep_last:] if keep_last > 0 else []
    middle = rest[:len(rest) - len(tail)] if keep_last > 0 else rest
    if not middle:
        return list(messages), False

    n_tool = sum(1 for m in middle if m.role == "tool")
    n_asst = sum(1 for m in middle if m.role == "assistant")
    summary = (f"[context compacted: {len(middle)} earlier messages summarized — "
               f"{n_asst} assistant turns, {n_tool} tool results. The goal and recent "
               f"steps are preserved above/below; continue from here.]")
    new = head + [ChatMessage("system", summary)] + tail
    return new, True


ROLLING_SUMMARY_SYSTEM = """You maintain a COMPACT running summary of an ongoing \
conversation so a coordinator can stay context-aware cheaply. Fold the new earlier turns \
into the existing summary. Keep durable facts, decisions, constraints, the user's \
goals/preferences, and unresolved threads. Drop pleasantries, redundancy, and transient \
phrasing. Be terse and factual -- a few short lines, not prose. Output ONLY the updated \
summary text."""


def summarize_turns(prior_summary: str, dropped_msgs: list, *, caller,
                    max_chars: int = 2000) -> str:
    """Fold the newly-dropped earlier turns into the rolling summary (Librarian job A).

    Pure-with-injected-caller: ``caller(messages) -> result`` does the model call, so this
    module keeps its no-I/O contract and stays unit-testable with a fake caller. Returns
    the (possibly updated) summary clamped to ``max_chars``. If there is nothing new to
    fold in, returns the prior summary WITHOUT a model call (the rarity short-circuit)."""
    if not dropped_msgs:
        return prior_summary
    from ..providers.openai_compat import ChatMessage
    transcript = "\n".join(f"{m.role}: {m.content}" for m in dropped_msgs)
    user = (f"EXISTING SUMMARY:\n{prior_summary or '(none)'}\n\n"
            f"NEW EARLIER TURNS TO FOLD IN:\n{transcript}\n\n"
            "Return the updated summary, preserving durable facts/decisions/constraints.")
    result = caller([ChatMessage("system", ROLLING_SUMMARY_SYSTEM),
                     ChatMessage("user", user)])
    return (result.text or "").strip()[:max_chars]


def build_handoff(state) -> str:
    """Build a human-readable continuity handoff from structured state.

    DETERMINISTIC: assembled from the typed files (goal, decisions, failures,
    plan), NOT from a model summary. This is the anti-drift guarantee -- the
    concept survives any compaction because it is reconstructed from state.

    Accepts a TaskState (duck-typed: .goal, .plan, .decisions, .failures,
    .summary). Returns markdown.
    """
    lines: list[str] = []
    lines.append("# Continuity Handoff")
    lines.append("")
    lines.append(f"## Goal\n{getattr(state, 'goal', '') or '(none)'}")
    lines.append("")

    plan = list(getattr(state, "plan", []) or [])
    done = [s for s in plan if s.status == "done"]
    lines.append(f"## Progress\n{len(done)}/{len(plan)} steps done")
    lines.append("")

    # Durable decisions (the established concept) -- ALL of them, with rationale.
    decisions = list(getattr(state, "decisions", []) or [])
    lines.append("## Decisions (established concept -- do NOT reinvent)")
    if decisions:
        for d in decisions:
            rationale = (d.rationale or "").strip()
            lines.append(f"- {d.description}" + (f": {rationale}" if rationale else ""))
    else:
        lines.append("- (none recorded)")
    lines.append("")

    # Failures -- so the same wall is not hit twice.
    failures = list(getattr(state, "failures", []) or [])
    lines.append("## Failures (do not repeat)")
    if failures:
        lines.extend(f"- step {f.step_id} attempt {f.attempt}: {f.reason}" for f in failures)
    else:
        lines.append("- (none)")
    lines.append("")

    # Current step + what remains.
    current = next((s for s in plan if s.status not in ("done", "skipped")), None)
    lines.append("## Current Step")
    if current:
        lines.append(f"- [{current.id}] {current.description}")
    else:
        lines.append("- (all steps complete or skipped)")
    remaining = [s for s in plan if s.status == "pending"]
    if remaining:
        lines.append("")
        lines.append("## Remaining Steps")
        lines.extend(f"- [{s.id}] {s.description}" for s in remaining)
    lines.append("")

    summary = (getattr(state, "summary", "") or "").strip()
    if summary:
        lines.append("## Running Summary")
        lines.append(summary)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
