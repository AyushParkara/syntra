"""User steering / mid-run message injection (req F5).

A steering/follow-up split: instant steers vs queued follow-ups. Implemented
clean for Syntra's step-based loop; not copied (req A1).

Two thread-safe channels so a TUI/CLI background input thread can push while the
synchronous Loop polls between steps:

- steer  (instant)   : injected into the NEXT executor step's prompt.
- queue  (follow-up) : applied when the run WOULD END, reviving it with new steps.

Pure data structure — no I/O, no network. The Loop owns the polling; the input
source (terminal thread, TUI, RPC, a test) owns the pushing. This decoupling is
the design keeps the mechanism independent of the interaction surface.
"""

from __future__ import annotations

import threading


class SteeringInbox:
    """Thread-safe two-channel queue of user instructions for a running task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steer: list[str] = []
        self._followup: list[str] = []
        self._stop = False

    # ------------------------------------------------------------- push (input)

    def steer(self, text: str) -> bool:
        """Add an INSTANT instruction (handled before the next step).

        Returns True if a non-empty instruction was queued.
        """
        t = (text or "").strip()
        if not t:
            return False
        with self._lock:
            self._steer.append(t)
        return True

    def queue(self, text: str) -> bool:
        """Add a QUEUED follow-up (handled when the run would otherwise end)."""
        t = (text or "").strip()
        if not t:
            return False
        with self._lock:
            self._followup.append(t)
        return True

    # ----------------------------------------------------------- drain (loop)

    def drain_steering(self) -> list[str]:
        """Atomically return + clear all pending instant instructions."""
        with self._lock:
            out, self._steer = self._steer, []
        return out

    def drain_followup(self) -> list[str]:
        """Atomically return + clear all pending follow-up instructions."""
        with self._lock:
            out, self._followup = self._followup, []
        return out

    # ----------------------------------------------------------------- status

    def pending(self) -> tuple[int, int]:
        """(instant_count, followup_count) currently waiting."""
        with self._lock:
            return (len(self._steer), len(self._followup))

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._steer or self._followup)

    # -------------------------------------------------------------------- stop

    def request_stop(self) -> None:
        """Signal the loop to interrupt the WHOLE run at its next step boundary
        (double-Esc / Ctrl+K in the TUI). One-way: cleared only by a fresh run."""
        with self._lock:
            self._stop = True

    def should_stop(self) -> bool:
        with self._lock:
            return self._stop
