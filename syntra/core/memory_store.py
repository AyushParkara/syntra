"""Lock-guarded durable-learnings store (#215).

The Librarian's durable facts live in `<config>/session-memory.json` — a flat
`{constraints, conventions, repo_map, architecture}` document. It is written
whenever ANY process finishes a task, so two concurrent Syntra runs (a CLI + a
scheduled job, or two terminals) both read-modify-write it and one silently
clobbers the other's freshly-learned facts (the same lost-update class as the
spend ledger / dead-key registry in #209).

`append_learnings` does the read-modify-write under a cross-process file lock and
writes atomically, so concurrent learners union instead of clobbering. It also
dedups within each field. Pure over one file; best-effort (never raises).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import fsutil
from .filelock import file_lock

_FIELDS = ("constraints", "conventions", "repo_map")


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    for f in _FIELDS:                       # always normalize list fields (incl. the empty doc)
        if not isinstance(doc.get(f), list):
            doc[f] = []
    return doc


def append_learnings(path, *, constraints=None, conventions=None,
                     repo_map=None, architecture: str = "") -> dict:
    """Union new durable learnings into the memory file under a cross-process lock.

    De-dups within each list field; sets `architecture` only when currently empty
    (first writer wins — a summary shouldn't churn). Returns the merged doc. The
    whole read-modify-write is inside the lock so concurrent processes never lose an
    update. Atomic write. Best-effort: returns {} on a write failure without raising."""
    path = Path(path)
    adds = {"constraints": list(constraints or []),
            "conventions": list(conventions or []),
            "repo_map": list(repo_map or [])}
    with file_lock(_lock_path(path)):
        doc = _load(path)
        for f in _FIELDS:
            have = doc[f]
            seen = set(have)
            for item in adds[f]:
                if item and item not in seen:
                    have.append(item)
                    seen.add(item)
        if architecture and not str(doc.get("architecture", "") or ""):
            doc["architecture"] = str(architecture)
        try:
            _atomic_write(path, doc)
        except OSError:
            return {}
        return doc


def replace_all(path, *, constraints=None, conventions=None, repo_map=None,
                architecture: str = "") -> dict:
    """Replace the whole memory doc atomically under the lock (the consolidation path:
    a cheap model re-emits the cleaned FULL set). Unlike `append_learnings` this is a
    deliberate overwrite. Best-effort; returns the written doc or {} on failure."""
    path = Path(path)
    with file_lock(_lock_path(path)):
        doc = {"constraints": list(constraints or []),
               "conventions": list(conventions or []),
               "repo_map": list(repo_map or [])}
        if architecture:
            doc["architecture"] = str(architecture)
        try:
            _atomic_write(path, doc)
        except OSError:
            return {}
        return doc


def _atomic_write(path: Path, doc) -> None:
    # Shared hardened primitive (#258): temp+fsync+os.replace + O_NOFOLLOW symlink refusal.
    # mode=None keeps the prior behavior (umask default for a new file; preserve an existing
    # file's mode) — memory holds no secrets, so it's not force-locked to 0o600.
    fsutil.write_atomic_bytes(
        path, json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"), mode=None)
