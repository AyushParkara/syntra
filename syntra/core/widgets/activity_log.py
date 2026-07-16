"""Activity log widget — timestamped event feed.

Shows a scrolling log of what the agent is doing: file reads, writes,
tool calls, routing decisions, with timestamps and status icons.
Like the ACTIVITY LOG panel in the reference image.
"""

from __future__ import annotations

import time
from ..widget import Widget, RenderLine
from ..tui_model import BRAND_MARK


class _Entry:
    __slots__ = ("ts", "text", "status")

    def __init__(self, text: str, status: str = "ok"):
        self.ts = time.strftime("%H:%M:%S")
        self.text = text
        self.status = status  # "ok" | "running" | "error" | "info"


class ActivityLogWidget(Widget):
    kind = "activity_log"
    focusable = True

    def __init__(self, *, title: str = "ACTIVITY LOG", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._entries: list[_Entry] = []
        self._max = 200
        self._scroll = 0          # 0 = follow bottom; >0 = lines back from bottom

    def log(self, text: str, status: str = "ok") -> None:
        self._entries.append(_Entry(text, status))
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        self._scroll_total = len(self._entries)
        out: list[RenderLine] = []
        out.append(RenderLine(" ACTIVITY LOG"[:w], "dim"))

        body_h = max(1, height - 1)
        self._body_h = body_h
        # apply scroll: 0 = show newest at bottom; scroll>0 = look further back
        end = len(self._entries) - self._scroll
        end = max(body_h, min(end, len(self._entries))) if self._entries else 0
        start = max(0, end - body_h)
        visible = self._entries[start:end] if self._entries else []

        for e in visible:
            icons = {"ok": "✓", "running": BRAND_MARK, "error": "✗", "info": "·"}
            styles = {"ok": "diff_add", "running": "accent", "error": "diff_del", "info": "dim"}
            icon = icons.get(e.status, "·")
            style = styles.get(e.status, "dim")
            line = f"{e.ts} {e.text}"
            # icon at the end, right-aligned
            if len(line) + 2 < w:
                pad = w - len(line) - 2
                line = line + " " * pad + icon
            out.append(RenderLine(line[:w], style))

        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        if ch in (curses.KEY_UP, ord("k")):
            self._scroll = min(max(0, len(self._entries) - 1), self._scroll + 1); return True
        if ch in (curses.KEY_DOWN, ord("j")):
            self._scroll = max(0, self._scroll - 1); return True
        if ch == curses.KEY_PPAGE:
            self._scroll = min(max(0, len(self._entries) - 1), self._scroll + getattr(self, "_body_h", 10)); return True
        if ch == curses.KEY_NPAGE:
            self._scroll = max(0, self._scroll - getattr(self, "_body_h", 10)); return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        if button & curses.BUTTON4_PRESSED:
            self._scroll = min(max(0, len(self._entries) - 1), self._scroll + 3); return True
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self._scroll = max(0, self._scroll - 3); return True
        return False
