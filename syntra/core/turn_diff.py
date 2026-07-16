"""Turn-level diff tracker — one coherent unified diff for a whole turn.

A turn-level diff tracker. Instead of showing N separate
per-file diffs as edits land, we capture each file's state at FIRST touch
(baseline) and its latest state (current), then emit ONE git-style unified diff
covering the entire turn. Lets the user preview/approve a turn as a transaction.

Pure + filesystem-light: baselines are snapshotted in memory on first touch, so
we never re-read the disk to reconstruct what changed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class TurnDiffCapture:
    # path -> file content at the moment it was first touched this turn ("" = new file)
    _baseline: dict[str, str] = field(default_factory=dict)
    # path -> latest content after the most recent edit ("" = deleted)
    _current: dict[str, str] = field(default_factory=dict)

    def on_before_edit(self, path: str, current_disk_content: str) -> None:
        """Record the baseline the FIRST time a path is touched this turn.
        current_disk_content is what's on disk right now ('' if the file is new)."""
        if path not in self._baseline:
            self._baseline[path] = current_disk_content

    def on_after_edit(self, path: str, new_content: str) -> None:
        """Record the latest content of a path after an edit ('' if deleted)."""
        self._current[path] = new_content

    def reset(self) -> None:
        self._baseline.clear()
        self._current.clear()

    def changed_paths(self) -> list[str]:
        return [p for p in self._current
                if self._current.get(p, "") != self._baseline.get(p, "")]

    def unified_diff(self) -> str:
        """One git-style unified diff for every file changed this turn."""
        chunks: list[str] = []
        for path in sorted(self._current):
            before = self._baseline.get(path, "")
            after = self._current.get(path, "")
            if before == after:
                continue
            a_label = f"a/{path}" if before else "/dev/null"
            b_label = f"b/{path}" if after else "/dev/null"
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=a_label, tofile=b_label,
            )
            body = "".join(diff)
            if body:
                header = f"diff --git a/{path} b/{path}\n"
                if not before:
                    header += "new file\n"
                elif not after:
                    header += "deleted file\n"
                chunks.append(header + body)
        return "\n".join(chunks)

    def summary(self) -> str:
        """One-line stat summary: 'N files, +A -D'."""
        added = removed = 0
        for path in self._current:
            before = self._baseline.get(path, "").splitlines()
            after = self._current.get(path, "").splitlines()
            sm = difflib.SequenceMatcher(None, before, after)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ("replace", "delete"):
                    removed += i2 - i1
                if tag in ("replace", "insert"):
                    added += j2 - j1
        n = len(self.changed_paths())
        return f"{n} file{'s' if n != 1 else ''}, +{added} -{removed}"
