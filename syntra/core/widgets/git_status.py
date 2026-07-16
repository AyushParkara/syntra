"""Git status widget — branch, changes, staged files.

Shows current branch, ahead/behind, modified/added/untracked files
with color-coded markers like the reference image.
"""

from __future__ import annotations

import subprocess
import shutil
from ..widget import Widget, RenderLine


def _git(*args) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


class GitStatusWidget(Widget):
    kind = "git_status"
    focusable = False

    def __init__(self, *, title: str = "GIT STATUS", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._branch = ""
        self._ahead = 0
        self._behind = 0
        self._changes: list[tuple[str, str]] = []  # (status_code, path)
        self._tick = 0

    def tick(self) -> bool:
        self._tick += 1
        if self._tick % 40 == 1:  # refresh every ~3 seconds
            self._refresh()
            return True
        return False

    def _refresh(self) -> None:
        if not shutil.which("git"):
            return
        self._branch = _git("branch", "--show-current") or "?"
        # ahead/behind
        ab = _git("rev-list", "--left-right", "--count", f"{self._branch}...@{{u}}")
        if ab:
            parts = ab.split()
            if len(parts) == 2:
                self._ahead = int(parts[0] or 0)
                self._behind = int(parts[1] or 0)
        # status
        raw = _git("status", "--porcelain", "-unormal")
        self._changes = []
        for line in raw.split("\n"):
            if len(line) >= 4:
                code = line[:2].strip()
                path = line[3:].strip()
                self._changes.append((code, path))

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        lines: list[RenderLine] = []
        lines.append(RenderLine(self.title[:w], "dim"))

        # branch
        ab = ""
        if self._ahead:
            ab += f" ↑{self._ahead}"
        if self._behind:
            ab += f" ↓{self._behind}"
        lines.append(RenderLine(f"  ✦ {self._branch}{ab}"[:w], "accent"))

        if self._changes:
            mod = sum(1 for c, _ in self._changes if c in ("M", "MM"))
            add = sum(1 for c, _ in self._changes if c in ("A", "AM"))
            unt = sum(1 for c, _ in self._changes if c == "??")
            summary_parts = []
            if mod: summary_parts.append(f"{mod}M")
            if add: summary_parts.append(f"{add}A")
            if unt: summary_parts.append(f"{unt}?")
            lines.append(RenderLine(f"  {' '.join(summary_parts)}"[:w], "dim"))
            for code, path in self._changes[:height - 5]:
                if code in ("M", "MM"):
                    style, marker = "string", "M"
                elif code in ("A", "AM"):
                    style, marker = "diff_add", "A"
                elif code == "D":
                    style, marker = "diff_del", "D"
                elif code == "??":
                    style, marker = "comment", "??"
                elif code == "R":
                    style, marker = "accent", "R"
                else:
                    style, marker = "dim", code
                lines.append(RenderLine(f"  {marker} {path}"[:w], style))
        else:
            lines.append(RenderLine("  clean"[:w], "dim"))

        while len(lines) < height:
            lines.append(RenderLine("", "default"))
        return lines[:height]
