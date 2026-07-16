"""Workspace file listing for the file-reference picker (Track T1 / F7).

The picker UI is `SelectList` (fuzzy) + a curses overlay; the only impure piece
is enumerating candidate files, which lives here so it can be tested against a
temp tree. We walk the workspace, skip noise dirs, cap the count, and return
relative POSIX paths sorted for stable display.
"""

from __future__ import annotations

import os
from pathlib import Path

# Directories never worth offering as @-file references.
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
    "reference",  # our cloned reference repos (gitignored, huge)
}


def list_workspace_files(root: str | Path = ".", *, limit: int = 5000) -> list[str]:
    """Relative file paths under root (skipping noise dirs), sorted, capped."""
    root = Path(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs in-place so os.walk doesn't descend into them
        dirnames[:] = sorted(d for d in dirnames
                             if d not in IGNORE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(Path(rel).as_posix())
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)
