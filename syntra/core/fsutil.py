"""Filesystem write primitives (#258).

`write_atomic` is the one hardened way to materialize a file Syntra controls (skills, plans,
`.syntra/*`, edited source): it writes to a temp file in the SAME directory then `os.replace`s
it into place (so a crash never leaves a half-written file), sets a restrictive mode from
creation (no world-readable window), and refuses to follow a symlink AT THE TARGET
(`O_NOFOLLOW`) so a planted symlink can't redirect the write outside the intended directory.

Consolidates the scattered temp+replace patterns (secrets.py / registry.py / model_discovery.py)
into one primitive. Consumed by the edit path for #207 (atomic edits) as well.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic_bytes(path, data: bytes, *, mode: int | None = 0o600) -> None:
    """Atomically write raw `data` bytes to `path` — the hardened primitive `write_atomic`
    (str) and the edit path (#207) both build on.

    - temp file in the same dir + `os.replace` → readers never see a partial write, and the
      swap is atomic on POSIX. fsync before the swap for durability.
    - REFUSES to write through a symlink at `path`: if `path` is an existing symlink we raise,
      and the temp file itself is opened `O_NOFOLLOW|O_EXCL` so a race that plants a symlink at
      the temp name fails closed rather than following it out of the directory.
    - `mode`: an int sets that mode from creation (no brief world-readable window) and re-asserts
      it after the swap. `mode=None` PRESERVES an existing file's mode (so editing a 0o755 script
      doesn't silently drop +x) and falls back to the process umask default for a brand-new file.
    """
    p = Path(path)
    if "\x00" in str(p):
        raise ValueError("path contains a null byte")
    # Never write THROUGH a symlink at the destination (a planted link would redirect the write).
    if p.is_symlink():
        raise OSError(f"refusing to write through a symlink: {path!r}")
    # mode=None → preserve the destination's current mode across the replace (temp inherits it).
    preserved: int | None = None
    if mode is None:
        try:
            preserved = os.stat(str(p)).st_mode & 0o777
        except OSError:
            preserved = None                      # brand-new file: leave to umask
    create_mode = mode if mode is not None else (preserved if preserved is not None else 0o666)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
    # O_NOFOLLOW: don't follow a symlink planted at the temp name. O_EXCL: fail if it already
    # exists (no clobber/race). O_CREAT|O_WRONLY|O_TRUNC: fresh file at the requested mode.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, create_mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())          # durability: content on disk before the swap
    except Exception:
        # clean up the temp file on any write failure; never leave a stray .tmp
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(p))          # atomic swap into place
    final_mode = mode if mode is not None else preserved
    if final_mode is not None:
        try:
            os.chmod(str(p), final_mode)  # re-assert mode after replace (umask/inherited/O_EXCL mask)
        except OSError:
            pass


def write_atomic(path, data: str, *, mode: int = 0o600, encoding: str = "utf-8") -> None:
    """Atomically write text `data` to `path` (see `write_atomic_bytes` for the guarantees).

    Encodes to bytes and delegates to the shared bytes primitive so there is ONE hardened
    temp+replace+O_NOFOLLOW path. `mode` defaults to 0o600 (files Syntra materializes into
    predictable dirs — skills/plans/.syntra — may hold secrets)."""
    write_atomic_bytes(path, data.encode(encoding), mode=mode)
