"""Run output widget — terminal-style command output panel.

Shows the output of the last command/tool execution. Scrollable.
Like the RUN OUTPUT / TERMINAL panel at the bottom of the reference image.
"""

from __future__ import annotations

from ..widget import Widget, RenderLine


class RunOutputWidget(Widget):
    kind = "run_output"
    focusable = True

    def __init__(self, *, title: str = "TERMINAL", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._lines: list[str] = []
        self._scroll = 0
        self._tabs = ["TERMINAL", "PROBLEMS", "OUTPUT", "DEBUG"]
        self._active_tab = "TERMINAL"
        self._problem_count = 0

    def append(self, text: str) -> None:
        for line in text.split("\n"):
            self._lines.append(line)
        # auto-scroll to bottom
        self._scroll = max(0, len(self._lines) - 1)

    def set_problems(self, count: int) -> None:
        self._problem_count = count

    def clear(self) -> None:
        self._lines = []
        self._scroll = 0

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        self._scroll_total = len(self._lines)
        out: list[RenderLine] = []

        # tab bar
        tabs = []
        for t in self._tabs:
            if t == "PROBLEMS" and self._problem_count:
                label = f"{t} {self._problem_count}"
            else:
                label = t
            if t == self._active_tab:
                tabs.append(f" {label} ")
            else:
                tabs.append(f" {label.lower()} ")
        tab_line = "  ".join(tabs)
        out.append(RenderLine(tab_line[:w], "dim"))

        body_h = height - 1
        self._body_h = max(1, body_h)
        # auto-follow
        view_start = max(0, len(self._lines) - body_h)
        if self._scroll < view_start:
            view_start = self._scroll
        visible = self._lines[view_start:view_start + body_h]

        for line in visible:
            # color command lines vs output
            if line.startswith("$") or line.startswith(">"):
                out.append(RenderLine(line[:w], "accent"))
            elif line.startswith("[+]") or "matched" in line.lower():
                out.append(RenderLine(line[:w], "diff_add"))
            elif line.startswith("[-]") or "error" in line.lower():
                out.append(RenderLine(line[:w], "diff_del"))
            else:
                out.append(RenderLine(line[:w], "code"))

        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        if ch in (curses.KEY_UP, ord("k")):
            self._scroll = max(0, self._scroll - 1); return True
        if ch in (curses.KEY_DOWN, ord("j")):
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + 1); return True
        if ch == curses.KEY_PPAGE:
            self._scroll = max(0, self._scroll - getattr(self, "_body_h", 15)); return True
        if ch == curses.KEY_NPAGE:
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + getattr(self, "_body_h", 15)); return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        if button & curses.BUTTON4_PRESSED:
            self._scroll = max(0, self._scroll - 3); return True
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self._scroll = min(max(0, len(self._lines) - 1), self._scroll + 3); return True
        return False
