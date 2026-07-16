"""Don't-stop-until-done: commitment-binding continuation invariant (§4).

A common frustration with coding agents: they STOP again and again with work still pending —
worst of all, they PRINT "Continuing now. No stopping." and then yield to the user on the very
next turn (announce-then-stop). This module makes "a stated continuation binds the next action"
a CODE-LEVEL, testable invariant rather than a prose promise.

ABSOLUTE RULE — NO TIMER, NO INTERVAL, NO HARDCODED THRESHOLD. Stopping is never time-based.

Two legitimate stop conditions, nothing else:
  1. work genuinely DONE (proven by the completion audit — not asserted, not "good enough");
  2. genuinely BLOCKED on the user (a real permission gate or a true intent fork the agent can't
     resolve safely).

Everything here is PURE (string classification + a tiny state machine) -> unit-tested. The loop
wires it in: emitting a "continuing/next=X" intent SETS a pending next-action; the loop MUST
dispatch it before it may yield; yielding with a pending continuation and no genuine stop = a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# A STATED continuation: the agent said it will keep working / do a next step. Detected so the
# loop can HOLD it to that (dispatch the next action before yielding). Deliberately broad on the
# "I will keep going" shapes and NARROW enough that a final answer / a real question doesn't match.
_CONTINUATION_RES = (
    re.compile(r"\b(continuing|keep(?:\s+on)?\s+going|carry(?:ing)?\s+on|proceed(?:ing)?)\b", re.I),
    re.compile(r"\bno\s+stop(?:ping)?\b", re.I),
    re.compile(r"\b(next|now|then)\b[^.\n]{0,40}\bi'?ll\b", re.I),
    re.compile(r"\bi'?ll\s+(continue|keep|proceed|now|next|go\s+on|move\s+on|start|implement|fix|finish|build|wire)\b", re.I),
    re.compile(r"\blet\s+me\s+(continue|keep\s+going|proceed|finish|now)\b", re.I),
)

# FAKE-DONE / exhaustion: "enough / tired / good enough / pause here / later" — an AI has NO
# stamina, so this is NEVER a valid stop. Distinct from a genuine evidence-backed "done".
_FAKE_DONE_RES = (
    re.compile(r"\b(enough)\b.{0,20}\b(for\s+now|today)\b", re.I),
    re.compile(r"\bdone\s+enough\b", re.I),
    re.compile(r"\bgood\s+enough\b", re.I),
    re.compile(r"\b(exhaust(?:ed|ing)|tired|worn\s+out)\b", re.I),
    re.compile(r"\b(pause|stop|continue|pick\s+(?:this|it)\s+up)\b[^.\n]{0,20}\b(here|later|another\s+time)\b", re.I),
    re.compile(r"\bthat'?s\s+(probably\s+)?(good\s+enough|enough)\b", re.I),
)

# PARTIAL-THEN-ASK: the agent KNOWS what's incomplete but stops to offer an options-menu
# ("want me to (a) try harder or (b) move on?") instead of just finishing it. A found gap IS the
# next action — the loop should dispatch, not surface a menu. Distinct from a REAL blocking
# question (a true fork / permission gate the agent cannot resolve itself).
_OPTIONS_MENU_RE = re.compile(
    r"(want me to|should i|shall i|do you want me to)\b[^?]*"
    r"(\btry\s+harder\b|\bmove\s+on\b|\bkeep\s+going\b|\(a\)|\bor\b[^?]*\bstop\b|\bor\b[^?]*\bmove\b)",
    re.I,
)
# Signals a question is a GENUINE block (needs the user), which is a legitimate stop — NOT the
# partial-then-ask anti-pattern.
_GENUINE_BLOCK_RES = (
    re.compile(r"\b(cannot|can'?t|unable to)\b[^.\n]{0,40}\b(create|access|resolve|obtain|generate)\b", re.I),
    re.compile(r"\b(credential|password|secret|api\s*key|permission|access)\b", re.I),
)


def states_continuation(text: str) -> bool:
    """True if the agent's output STATES it will keep working / do a next step."""
    t = text or ""
    if not t.strip():
        return False
    return any(rx.search(t) for rx in _CONTINUATION_RES)


def is_fake_done(text: str) -> bool:
    """True if the output claims a stop for a NON-reason (tired / enough / good enough / later).
    An AI has no stamina, so exhaustion is never a valid stop — flag it so the loop keeps going."""
    t = text or ""
    return any(rx.search(t) for rx in _FAKE_DONE_RES)


def is_partial_then_ask(text: str) -> bool:
    """True if the output is the partial-then-ask anti-pattern: knows what's incomplete, but stops
    to ask an options-menu instead of finishing. A GENUINE blocking question (needs a credential /
    a real decision the agent can't make) is NOT this — that's a legitimate block."""
    t = text or ""
    if not _OPTIONS_MENU_RE.search(t):
        return False
    # If it's actually a genuine block (can't-do-X / needs-credential), it's a legit stop, not this.
    if any(rx.search(t) for rx in _GENUINE_BLOCK_RES):
        return False
    return True


def is_stop_legitimate(*, stated_continuation: bool, work_done: bool,
                       blocked_on_user: bool) -> bool:
    """The core §4 invariant. A stop (yield to the user) is legitimate ONLY when the work is
    genuinely DONE or the agent is genuinely BLOCKED on the user. If the agent STATED it would
    continue and neither is true, yielding is the announce-then-stop BUG -> illegitimate.

    When no continuation was stated, yielding is allowed (the agent made no promise to keep going);
    the macro completion loop (autopilot) governs whether to resume in that case."""
    if work_done or blocked_on_user:
        return True
    # not done, not blocked: stopping is only illegitimate if the agent PROMISED to continue.
    return not stated_continuation


@dataclass
class ContinuationState:
    """Commitment-binding state machine. A stated continuation SETS a pending next-action; the
    loop must `dispatched()` it before it `may_yield()`. This is the code-level enforcement of
    'a said commitment binds the next action' — the same discipline the operator demands."""

    _pending: bool = False

    def note_output(self, text: str) -> None:
        """Feed the agent's latest output. A stated continuation arms a pending next-action."""
        if states_continuation(text):
            self._pending = True

    def has_pending(self) -> bool:
        return self._pending

    def dispatched(self) -> None:
        """The loop dispatched the promised next action — the commitment is discharged."""
        self._pending = False

    def may_yield(self, *, work_done: bool, blocked_on_user: bool) -> bool:
        """May the loop yield control to the user right now? No, if a continuation is pending and
        the work is neither done nor blocked (that's announce-then-stop)."""
        return is_stop_legitimate(
            stated_continuation=self._pending, work_done=work_done, blocked_on_user=blocked_on_user)
