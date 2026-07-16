"""Cross-process advisory file lock for shared on-disk state (#209).

Syntra's shared-state files — the cross-task spend ledger, the dead-key registry —
are read-modify-written by whatever process touches them. An in-process
`threading.Lock` guards threads inside ONE process but does nothing across
processes: two concurrent CLIs (or a CLI + a scheduled job) both read the file,
both append, both write → one silently clobbers the other (lost update).

This module gives a `file_lock(path)` context manager backed by `fcntl.flock`
(POSIX) or `msvcrt.locking` (Windows), so only one process holds the lock at a
time. It is:

- **advisory + cooperative**: only code that takes the lock is serialized; it does
  not stop an unrelated tool from writing the file. That's fine — every Syntra
  writer of a shared file takes it.
- **reentrant within a process**: a thread already holding a given lock path can
  take it again without deadlocking (a per-path in-process RLock sits in front of
  the OS lock, and the OS lock is acquired only by the outermost holder).
- **fail-open**: on a platform with no advisory-lock primitive, it degrades to the
  in-process RLock alone (never worse than today's behavior) rather than raising.

The lock file is a tiny sidecar (e.g. `spend.json.lock`); the data file itself is
never locked, so a reader that doesn't participate is unaffected.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

try:                        # POSIX
    import fcntl
    _HAVE_FCNTL = True
except ImportError:         # pragma: no cover - non-POSIX
    fcntl = None            # type: ignore[assignment]
    _HAVE_FCNTL = False

try:                        # Windows
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None           # type: ignore[assignment]
    _HAVE_MSVCRT = False


# One RLock per lock-file path, shared across threads of THIS process. Guards the
# in-process side and makes the whole thing reentrant. A module-level registry
# (guarded by its own lock) so every caller for the same path gets the same RLock.
_local_locks: dict[str, threading.RLock] = {}
_registry_guard = threading.Lock()
# Per-thread depth of held OS locks, keyed by resolved path → the OS lock is taken
# only at depth 0 and released only back at depth 0 (true reentrancy).
_depth = threading.local()


def _rlock_for(key: str) -> threading.RLock:
    with _registry_guard:
        lk = _local_locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _local_locks[key] = lk
        return lk


def _depths(self=_depth) -> dict[str, int]:
    d = getattr(self, "counts", None)
    if d is None:
        d = {}
        self.counts = d
    return d


@contextmanager
def file_lock(path: "str | Path", *, exclusive: bool = True):
    """Hold an exclusive cross-process lock on ``path`` for the block's duration.

    ``path`` is the LOCK file (a sidecar); it is created if missing. Reentrant in
    the same thread; serialized across threads and processes. Fail-open when no OS
    advisory-lock primitive exists.
    """
    p = Path(path)
    key = str(p.resolve() if p.parent.exists() else p)
    rlock = _rlock_for(key)
    rlock.acquire()
    counts = _depths()
    depth = counts.get(key, 0)
    fh = None
    try:
        if depth == 0 and (_HAVE_FCNTL or _HAVE_MSVCRT):
            p.parent.mkdir(parents=True, exist_ok=True)
            # held open across the yield to hold the OS lock; closed in the finally below.
            fh = open(p, "a+")  # noqa: SIM115 - lifetime is the lock scope, not a `with` block
            if _HAVE_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            elif _HAVE_MSVCRT:      # pragma: no cover - Windows only
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        counts[key] = depth + 1
        yield
    finally:
        counts[key] = max(0, counts.get(key, 1) - 1)
        if fh is not None:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif _HAVE_MSVCRT:  # pragma: no cover - Windows only
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                fh.close()
        rlock.release()
