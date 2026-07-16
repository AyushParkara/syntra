"""Diff viewer widget — shows file diffs with syntax coloring.

Displays unified diffs with +/- coloring, line numbers, file headers.
Scrollable. Can show the last diff from an agent edit or git diff.
"""

from __future__ import annotations

from ..widget import Widget, RenderLine


class DiffViewerWidget(Widget):
    kind = "diff_viewer"
    focusable = True

    def __init__(self, *, title: str = "DIFF", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._lines: list[tuple[str, str]] = []  # (text, style)
        self._filename = ""
        self._scroll = 0
        self._added = 0
        self._removed = 0

    def set_diff(self, filename: str, diff_text: str) -> None:
        self._filename = filename
        self._lines = []
        self._added = 0
        self._removed = 0
        self._scroll = 0
        lineno = 0
        for raw in diff_text.split("\n"):
            if raw.startswith("@@"):
                self._lines.append((raw, "diff_hunk"))
                # parse hunk header for line number
                try:
                    part = raw.split("+")[1].split(",")[0]
                    lineno = int(part) - 1
                except (IndexError, ValueError):
                    pass
                continue
            if raw.startswith("+"):
                lineno += 1
                self._added += 1
                self._lines.append((f"{lineno:>4} + {raw[1:]}", "diff_add"))
            elif raw.startswith("-"):
                self._removed += 1
                self._lines.append((f"     - {raw[1:]}", "diff_del"))
            else:
                lineno += 1
                self._lines.append((f"{lineno:>4}   {raw}", "code"))

    def clear(self) -> None:
        self._lines = []
        self._filename = ""
        self._scroll = 0

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        self._scroll_total = len(self._lines)
        out: list[RenderLine] = []

        # header
        if self._filename:
            syntax = self._filename.rsplit(".", 1)[-1].upper() if "." in self._filename else ""
            header = f" {self._filename}"
            if syntax:
                header_r = f"SYNTAX: {syntax}"
                pad = max(0, w - len(header) - len(header_r) - 1)
                header = header + " " * pad + header_r
            out.append(RenderLine(header[:w], "accent"))
        else:
            out.append(RenderLine(" DIFF  —  diffs appear here when files change"[:w], "dim"))

        body_h = height - 2  # header + footer
        self._body_h = max(1, body_h)

        # content
        visible = self._lines[self._scroll:self._scroll + body_h]
        for text, style in visible:
            out.append(RenderLine(text[:w], style))
        while len(out) < body_h + 1:
            out.append(RenderLine("", "default"))

        # footer with add/remove counts and scroll position
        if self._lines:
            summary = f" +{self._added} -{self._removed}"
            total = len(self._lines)
            if total > body_h and self._scroll > 0:
                pct = min(100, int((self._scroll + body_h) / total * 100))
                right = f"{pct}% UNIFIED"
            else:
                right = "UNIFIED"
            pad = max(1, w - len(summary) - len(right) - 1)
            footer = summary + " " * pad + right
            out.append(RenderLine(footer[:w], "dim"))
        else:
            out.append(RenderLine(" no diff", "dim"))

        return out[:height]

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        if ch in (curses.KEY_UP, ord("k")):
            self._scroll = max(0, self._scroll - 1); return True
        if ch in (curses.KEY_DOWN, ord("j")):
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + 1); return True
        if ch == curses.KEY_PPAGE:
            self._scroll = max(0, self._scroll - getattr(self, "_body_h", 20)); return True
        if ch == curses.KEY_NPAGE:
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + getattr(self, "_body_h", 20)); return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        if button & curses.BUTTON4_PRESSED:
            self._scroll = max(0, self._scroll - 3); return True
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + 3); return True
        return False
