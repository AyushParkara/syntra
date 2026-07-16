"""Background task scheduler / waiter (T11).

The engine half of "dispatch long work, then WAIT in the background — not block, not
stop — stay responsive and resume/notify on completion". Today a long run blocks the
loop and a dispatched sub-agent has no non-blocking wait primitive; /bg + /jobs were a
render-only daemon-thread + dict. This is the real engine-owned scheduler both the CLI
and the TUI can drive.

Design:
- Thread-based (the work is provider I/O bound, so threads give real concurrency here).
- Each task runs in its own daemon thread; the scheduler tracks N in-flight tasks and
  fires a structured `task_started` / `task_done` / `task_error` event for EACH (the SEAM
  the render side renders as "running/waiting N" + per-task done/error).
- Auto-resume: a task may declare `on_done(task_id, result)` — a continuation fired when
  it finishes, so a parked main flow resumes when its dependency completes.
- Non-blocking by default; `wait()` is available for callers that DO want to join (with a
  timeout) without busy-looping.

Pure-ish: no global state, no I/O of its own — the caller injects the work `fn` and the
event sink. Fully unit-testable with synchronous fakes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


# Terminal + non-terminal task states.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"
_TERMINAL = (DONE, ERROR)


@dataclass
class BgTask:
    """One tracked background task."""

    task_id: str
    label: str = ""
    status: str = PENDING
    result: object = None          # the fn's return value (on DONE)
    error: str = ""                # str(exception) (on ERROR)
    started_at: float = 0.0
    ended_at: float = 0.0
    _thread: object = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self.status in _TERMINAL

    def elapsed(self, now: float) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or now
        return max(0.0, end - self.started_at)

    def to_dict(self, now: float | None = None) -> dict:
        return {
            "task_id": self.task_id, "label": self.label, "status": self.status,
            "error": self.error, "elapsed_s": round(self.elapsed(now or time.time()), 2),
        }


class BackgroundScheduler:
    """Tracks N in-flight background tasks, fires per-task lifecycle events, and supports
    auto-resume continuations. Thread-safe. `on_event(kind, payload)` is the render SEAM:
      task_started {task_id,label,running,waiting}
      task_done    {task_id,label,running,waiting,elapsed_s}
      task_error   {task_id,label,error,running,waiting}
      monitor      {running,waiting}                       (a "waiting on N" heartbeat)
    `clock`/`spawn` are injectable so tests run synchronously with no real threads/sleep."""

    def __init__(self, on_event: Callable[[str, dict], None] | None = None,
                 *, clock: Callable[[], float] | None = None,
                 spawn: Callable[[Callable], object] | None = None):
        self._on_event = on_event or (lambda kind, payload: None)
        self._clock = clock or time.time
        self._spawn = spawn or self._default_spawn
        self._tasks: dict[str, BgTask] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _default_spawn(target: Callable) -> threading.Thread:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return t

    # --- counts (the "running/waiting N" surface) ---------------------------
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == RUNNING)

    def waiting_count(self) -> int:
        """In-flight = not yet terminal (pending or running). 'waiting N' = this count."""
        with self._lock:
            return sum(1 for t in self._tasks.values() if not t.done)

    def all_status(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            return [t.to_dict(now) for t in self._tasks.values()]

    def status(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t.to_dict(self._clock()) if t else None

    def get(self, task_id: str) -> BgTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def _emit_monitor(self) -> None:
        self._on_event("monitor", {"running": self.running_count(),
                                   "waiting": self.waiting_count()})

    # --- submit / run -------------------------------------------------------
    def submit(self, task_id: str, fn: Callable[[], object], *, label: str = "",
               on_done: Callable[[str, object], None] | None = None) -> BgTask:
        """Dispatch `fn()` in the background and return its BgTask immediately (non-blocking).
        Fires task_started now and task_done/task_error when it finishes. `on_done(task_id,
        result)` (optional) is the auto-resume continuation — fired AFTER task_done so a parked
        main flow can pick the result up. A continuation that raises never breaks the scheduler."""
        with self._lock:
            if task_id in self._tasks and not self._tasks[task_id].done:
                raise ValueError(f"task {task_id!r} already in flight")
            task = BgTask(task_id=task_id, label=label or task_id,
                          status=RUNNING, started_at=self._clock())
            self._tasks[task_id] = task

        self._on_event("task_started", {"task_id": task_id, "label": task.label,
                                        "running": self.running_count(),
                                        "waiting": self.waiting_count()})

        def _run():
            try:
                res = fn()
                with self._lock:
                    task.status = DONE
                    task.result = res
                    task.ended_at = self._clock()
                self._on_event("task_done", {
                    "task_id": task_id, "label": task.label,
                    "elapsed_s": round(task.elapsed(self._clock()), 2),
                    "running": self.running_count(), "waiting": self.waiting_count()})
            except Exception as e:  # noqa: BLE001 - a task failing is a normal outcome, reported
                with self._lock:
                    task.status = ERROR
                    task.error = str(e)[:500]
                    task.ended_at = self._clock()
                self._on_event("task_error", {
                    "task_id": task_id, "label": task.label, "error": task.error,
                    "running": self.running_count(), "waiting": self.waiting_count()})
            finally:
                self._emit_monitor()
                if on_done is not None:
                    try:
                        on_done(task_id, task.result if task.status == DONE else None)
                    except Exception:  # noqa: BLE001 - a continuation must never break the scheduler
                        pass

        task._thread = self._spawn(_run)
        return task

    # --- wait (non-busy) ----------------------------------------------------
    def wait(self, task_ids: "list[str] | None" = None, *, timeout: float | None = None,
             poll: float = 0.05) -> bool:
        """Block the CALLER (not the UI) until the given tasks finish, or timeout. Returns
        True if all finished, False on timeout. Joins real threads when present; otherwise
        polls terminal status (so injected synchronous spawns work too). `task_ids=None` waits
        on everything currently tracked."""
        with self._lock:
            targets = list(self._tasks.values()) if task_ids is None \
                else [self._tasks[i] for i in task_ids if i in self._tasks]
        deadline = None if timeout is None else self._clock() + timeout
        for t in targets:
            th = getattr(t, "_thread", None)
            if th is not None and hasattr(th, "join"):
                remaining = None if deadline is None else max(0.0, deadline - self._clock())
                th.join(remaining)
            else:
                while not t.done:
                    if deadline is not None and self._clock() >= deadline:
                        break
                    time.sleep(poll)
            if not t.done:
                return False
        return all(t.done for t in targets)

    def prune(self) -> int:
        """Drop finished tasks from tracking; returns how many were pruned."""
        with self._lock:
            done_ids = [i for i, t in self._tasks.items() if t.done]
            for i in done_ids:
                del self._tasks[i]
        return len(done_ids)
