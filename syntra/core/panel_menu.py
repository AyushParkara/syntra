"""Panel toggle checklist (Ctrl+E).

A btop-style checklist of every available panel. Toggle any on/off; enabled
panels dock on the right side of the workspace. Pure state + render; the curses
layer paints it and applies the toggles to the WorkspaceManager.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tui_model import BRAND_MARK


# (kind, display label) — the panels the user can summon to the right side.
PANELS = [
    ("file_tree",     "File Tree"),
    ("file_viewer",   "File Viewer"),
    ("git_status",    "Git Status"),
    ("diff_viewer",   "Diff Viewer"),
    ("activity_log",  "Activity Log"),
    ("token_monitor", "Tokens"),
    ("run_output",    "Terminal"),
    ("model_router",  "Model Router"),
    ("agent_status",  "Agents"),
    ("activity_tree", "Working Tree"),
    ("shortcuts",           "Shortcuts"),
    ("workspace_overview",  "Overview"),
]

# Placement order used by the TUI's _summon_panel. EVERY toggleable panel above
# must appear in exactly one of these, or toggling it does nothing (it gets added
# to the visible set but never placed in the layout — a silent "false-done").
LEFT_ORDER = ("file_tree", "file_viewer", "git_status", "shortcuts", "workspace_overview")
RIGHT_ORDER = ("diff_viewer", "run_output", "activity_tree", "activity_log",
               "token_monitor", "model_router", "agent_status")


@dataclass
class PanelMenu:
    enabled: set = field(default_factory=set)   # set of enabled panel kinds
    selected: int = 0

    def move(self, delta: int) -> None:
        self.selected = (self.selected + delta) % len(PANELS)

    def toggle_selected(self) -> tuple[str, bool]:
        """Toggle the highlighted panel. Returns (kind, now_enabled)."""
        kind = PANELS[self.selected][0]
        if kind in self.enabled:
            self.enabled.discard(kind)
            return kind, False
        self.enabled.add(kind)
        return kind, True

    def is_enabled(self, kind: str) -> bool:
        return kind in self.enabled


def panel_menu_box(menu: PanelMenu, width: int = 30) -> list[tuple[str, str]]:
    """Render the panel checklist as a bordered box -> [(text, style)]. Pure.

    Per-row styles let the curses layer color each line individually:
      - selected row          -> "user"      (highlighted accent)
      - enabled (checked)      -> "diff_add"  (green ◉)
      - disabled               -> "dim"
      - borders/title          -> "accent"
      - hint footer            -> "comment"
    """
    w = max(20, width)
    lines: list[tuple[str, str]] = []
    title = f" {BRAND_MARK} panels "
    lines.append(("╭" + title + "─" * max(0, w - len(title) - 2) + "╮", "accent"))
    for i, (kind, label) in enumerate(PANELS):
        on = kind in menu.enabled
        check = "◉" if on else "○"
        marker = "▌ " if i == menu.selected else "  "
        row = f"│{marker}{check} {label}"
        row = row.ljust(w - 1) + "│"
        if i == menu.selected:
            style = "user"
        elif on:
            style = "diff_add"
        else:
            style = "dim"
        lines.append((row[:w], style))
    lines.append(("├" + "─" * (w - 2) + "┤", "accent"))
    hint = "│ space toggle · esc close"
    lines.append((hint.ljust(w - 1) + "│", "comment"))
    lines.append(("╰" + "─" * (w - 2) + "╯", "accent"))
    return lines
