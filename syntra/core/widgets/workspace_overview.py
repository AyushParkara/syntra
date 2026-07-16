"""Workspace overview widget — project summary at a glance.

Shows project name, language breakdown, file count, size, recent git activity.
Like the workspace panel in IDE sidebars.
"""

from __future__ import annotations

import os
import subprocess
from ..widget import Widget, RenderLine
from ..tui_model import BRAND_MARK


class WorkspaceOverviewWidget(Widget):
    kind = "workspace_overview"
    focusable = False

    def __init__(self, *, title: str = "WORKSPACE", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._root = os.getcwd()
        self._name = os.path.basename(self._root) or self._root
        self._file_count = 0
        self._dir_count = 0
        self._lang_counts: dict[str, int] = {}
        self._recent_commits: list[str] = []
        self._tick = 0
        self._loaded = False

    def tick(self) -> bool:
        self._tick += 1
        if not self._loaded or self._tick % 120 == 1:
            self._refresh()
            return True
        return False

    def _refresh(self) -> None:
        from ..repo_map import LANG_BY_EXT, SKIP_DIRS, SKIP_EXTS
        self._file_count = 0
        self._dir_count = 0
        self._lang_counts = {}
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            self._dir_count += 1
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext in SKIP_EXTS:
                    continue
                self._file_count += 1
                lang = LANG_BY_EXT.get(ext, "")
                if lang:
                    self._lang_counts[lang] = self._lang_counts.get(lang, 0) + 1
            if self._file_count > 2000:
                break
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "-5", "--no-color"],
                capture_output=True, text=True, timeout=3, cwd=self._root
            )
            self._recent_commits = [ln.strip() for ln in r.stdout.strip().split("\n") if ln.strip()][:5]
        except Exception:
            self._recent_commits = []
        self._loaded = True

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        out: list[RenderLine] = []

        out.append(RenderLine(f"  {BRAND_MARK} {self._name}"[:w], "accent"))
        out.append(RenderLine(f"    {self._file_count} files · {self._dir_count} dirs"[:w], "dim"))
        out.append(RenderLine("", "default"))

        # Language breakdown (top 5) — redesigned (#17, user picked the density scale over
        # the old gradient block-bar). Each language shows a 10-cell filled/empty scale
        # (▰▱) + a right-aligned %. The dominant language's scale has one cell gently
        # pulsing so the panel still feels alive (reduced-motion freezes it).
        sorted_langs = sorted(self._lang_counts.items(), key=lambda x: -x[1])[:5]
        if sorted_langs:
            out.append(RenderLine("  languages"[:w], "dim"))
            total = sum(c for _, c in sorted_langs)
            from ..tui_model import motion_enabled
            SCALE = 10
            # name column flexes with pane width; the scale+% need ~16 cols
            name_w = max(4, min(10, w - SCALE - 8))
            for li, (lang, count) in enumerate(sorted_langs):
                pct = int(round(count / max(1, total) * 100))
                filled = max(1, min(SCALE, round(pct / 10)))
                cells = ["▰"] * filled + ["▱"] * (SCALE - filled)
                # animate the top language's scale: a brighter half-cell rides the fill
                if li == 0 and filled > 1 and motion_enabled():
                    cells[(self._tick // 2) % filled] = "▱"
                scale = "".join(cells)
                row = f"  {lang[:name_w].ljust(name_w)} {scale} {pct:>3d}%"
                out.append(RenderLine(row[:w], "accent" if li == 0 else "default"))

        if self._recent_commits:
            out.append(RenderLine("", "default"))
            out.append(RenderLine("  recent"[:w], "dim"))
            out.extend(RenderLine(f"  · {c[:w - 5]}"[:w], "comment")
                       for c in self._recent_commits[:height - len(out) - 1])

        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]
