"""Shortcuts widget — keybind quick-reference sidebar.

Like the SHORTCUTS panel in the reference image. Lists
available keybindings for the current context.
"""

from __future__ import annotations

from typing import ClassVar

from ..widget import Widget, RenderLine


class ShortcutsWidget(Widget):
    kind = "shortcuts"
    focusable = False

    def __init__(self, *, title: str = "SHORTCUTS", on_event=None):
        super().__init__(title=title, on_event=on_event)

    _BINDS: ClassVar[list[tuple[str, str]]] = [
        ("enter", "Send message"),
        ("drag", "Select + copy"),
        ("↑ ↓", "History"),
        ("PgUp/Dn", "Scroll chat"),
        ("End", "Jump to latest"),
        ("tab", "Cycle panels"),
        ("@", "File mention"),
        ("!", "Shell command"),
        ("", ""),
        ("^P", "Command menu"),
        ("^E", "Toggle panels"),
        ("^Y", "Copy last reply"),
        ("^R", "Search history"),
        ("^L", "Clear chat"),
        ("^K", "Stop run"),
        ("^U", "Clear input"),
        ("^O", "New line"),
        ("esc esc", "Stop / quit"),
    ]

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        out: list[RenderLine] = []
        # No title line: the panel border already labels this " SHORTCUTS " (P17).
        for key, desc in self._BINDS:
            if not key:
                out.append(RenderLine("", "default"))
                continue
            # readable two-column layout (was dim + cramped)
            out.append(RenderLine(f" {key:<8}{desc}"[:w], "default"))
        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]
