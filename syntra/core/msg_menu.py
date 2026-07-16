"""Per-message action menu (Copy / Revert / Fork / Edit / Retry).

A small popup that appears when the user clicks a message. Pure state + render
math; the curses layer paints menu_box() near the clicked row and forwards keys.

The menu targets a specific message index in the transcript. The actions are
resolved by the TUI:
- copy:   OSC52 the message text to the clipboard
- revert: undo the conversation back to that turn (drop later messages)
- fork:   branch a new session from that point
- edit:   load the message text into the input for editing + resend
- retry:  resend the message as-is
"""

from __future__ import annotations

from dataclasses import dataclass

from .tui_model import BRAND_MARK


# action id -> (label, hotkey)
ACTIONS = [
    ("copy",     "Copy",     "c"),
    ("collapse", "Collapse", "o"),
    ("edit",     "Edit",     "e"),
    ("retry",    "Retry",    "r"),
    ("fork",     "Fork",     "f"),
    ("revert",   "Revert",   "v"),
]


# Which actions apply to which message role. Edit/Retry/Fork/Revert only make
# sense on YOUR turns; background lines (system/tool/mode/thinking) are read-only
# except for copy/collapse (P35 — don't offer Edit on background messages).
def _allowed_actions(role: str) -> set[str]:
    if role == "user":
        return {"copy", "collapse", "edit", "retry", "fork", "revert"}
    if role in ("assistant", "assistant_stream"):
        return {"copy", "collapse", "fork", "revert"}
    return {"copy", "collapse"}   # system / tool / mode / thinking / ok / error


@dataclass
class MessageMenu:
    msg_index: int           # which transcript message this menu acts on
    role: str = "user"       # role of the targeted message
    selected: int = 0        # highlighted action row
    chosen: str | None = None

    def actions(self) -> list[tuple[str, str, str]]:
        """The actions offered for THIS message's role (P35)."""
        allowed = _allowed_actions(self.role)
        return [a for a in ACTIONS if a[0] in allowed]

    def move(self, delta: int) -> None:
        n = max(1, len(self.actions()))
        self.selected = (self.selected + delta) % n

    def hotkey(self, ch: str) -> str | None:
        """Return the action id for a hotkey char, or None (role-filtered)."""
        for aid, _label, key in self.actions():
            if ch.lower() == key:
                return aid
        return None

    def confirm(self) -> str:
        acts = self.actions()
        self.selected = max(0, min(self.selected, len(acts) - 1))
        self.chosen = acts[self.selected][0]
        return self.chosen


def menu_box(menu: MessageMenu, width: int = 22) -> list[tuple[str, str]]:
    """Render the action menu as a small bordered box -> [(text, style)]. Pure."""
    w = max(16, width)
    lines: list[tuple[str, str]] = []
    head = f" {BRAND_MARK} {menu.role} "
    lines.append(("╭" + head + "─" * max(0, w - len(head) - 2) + "╮", "accent"))
    for i, (aid, label, key) in enumerate(menu.actions()):
        marker = "▌ " if i == menu.selected else "  "
        row = f"│{marker}{label}"
        row = row.ljust(w - 3) + f"{key} │"
        style = "user" if i == menu.selected else "default"
        lines.append((row[:w], style))
    lines.append(("╰" + "─" * (w - 2) + "╯", "accent"))
    return lines
