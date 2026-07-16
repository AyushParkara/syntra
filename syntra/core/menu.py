"""Chained, discoverable menu system (the command cockpit).

The user never has to memorize commands or read option dumps. They open ONE
menu and navigate: a category opens a sub-menu, a sub-menu may open another, a
leaf runs an action. Lists can be static OR generated on demand (e.g. "all
models", "all themes") so the user always sees the full set of choices.

Pure state + render. The curses layer paints menu_render() centered and forwards
keys to the MenuStack.

Design:
    MenuItem(label, ...)
        - submenu: a callable () -> list[MenuItem]   (lazy; built when entered)
        - action:  an action id (str) the TUI resolves, optionally with `arg`
    MenuStack: holds the navigation stack; enter() pushes, back() pops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .tui_model import BRAND_MARK


@dataclass
class MenuItem:
    label: str
    action: str | None = None          # leaf: an action id for the TUI to run
    arg: str = ""                       # argument passed with the action
    submenu: Callable | None = None     # () -> list[MenuItem]  (lazy sub-menu)
    hint: str = ""                      # right-aligned hint (e.g. current value)

    @property
    def is_submenu(self) -> bool:
        return self.submenu is not None


@dataclass
class MenuLevel:
    title: str
    items: list
    selected: int = 0
    scroll: int = 0


@dataclass
class MenuStack:
    """A navigable stack of menu levels. Pure."""
    levels: list = field(default_factory=list)

    def open(self, title: str, items: list) -> None:
        self.levels = [MenuLevel(title, items)]

    @property
    def current(self) -> MenuLevel | None:
        return self.levels[-1] if self.levels else None

    @property
    def is_open(self) -> bool:
        return bool(self.levels)

    def move(self, delta: int) -> None:
        lvl = self.current
        if not lvl or not lvl.items:
            return
        lvl.selected = (lvl.selected + delta) % len(lvl.items)

    def filter_items(self, query: str) -> list:
        """Items in the current level matching the query (substring, case-insens)."""
        lvl = self.current
        if not lvl:
            return []
        if not query:
            return lvl.items
        q = query.lower()
        return [it for it in lvl.items if q in it.label.lower()]

    def enter(self) -> MenuItem | None:
        """Activate the highlighted item.

        - submenu  -> push a new level, return None
        - leaf     -> return the MenuItem (TUI runs its action, menu closes)
        """
        lvl = self.current
        if not lvl or not lvl.items:
            return None
        item = lvl.items[lvl.selected]
        if item.is_submenu:
            sub_items = item.submenu() or []
            self.levels.append(MenuLevel(item.label, sub_items))
            return None
        return item  # leaf — caller closes the menu and runs the action

    def back(self) -> bool:
        """Pop one level. Returns False if we were already at the root (close)."""
        if len(self.levels) > 1:
            self.levels.pop()
            return True
        self.levels = []
        return False

    def close(self) -> None:
        self.levels = []

    def breadcrumb(self) -> str:
        return " › ".join(l.title for l in self.levels)


def menu_render(stack: MenuStack, width: int = 40, height: int = 18) -> list[tuple[str, str]]:
    """Render the current menu level as a bordered box -> [(text, style)]. Pure.

    The box sizes to its CONTENT: as many rows as items (capped by `height`),
    never padded to fill the screen. A long list (e.g. all models) scrolls
    within a fixed window instead of stretching the whole chat.
    """
    lvl = stack.current
    if not lvl:
        return []
    w = max(24, width)
    lines: list[tuple[str, str]] = []

    # title bar with breadcrumb
    crumb = stack.breadcrumb()
    title = f" {BRAND_MARK} {crumb} "
    if len(title) > w - 2:
        title = title[: w - 3] + " "
    lines.append(("╭" + title + "─" * max(0, w - len(title) - 2) + "╮", "accent"))

    # body height = min(items, available window) — content-sized, capped, with a
    # sensible max so a huge model list never eats the whole screen.
    # Total chrome: 1 (title) + 3 (footer: separator + hint + bottom border) = 4.
    window_cap = max(1, height - 4)
    body_h = max(1, min(len(lvl.items), window_cap, 12))
    # keep selection in view
    if lvl.selected < lvl.scroll:
        lvl.scroll = lvl.selected
    if lvl.selected >= lvl.scroll + body_h:
        lvl.scroll = lvl.selected - body_h + 1
    lvl.scroll = max(0, lvl.scroll)

    visible = lvl.items[lvl.scroll:lvl.scroll + body_h]
    for i, item in enumerate(visible):
        idx = lvl.scroll + i
        sel = idx == lvl.selected
        marker = "▌ " if sel else "  "
        arrow = " ›" if item.is_submenu else ""
        right = (item.hint + arrow) if item.hint else arrow
        # inner width available for marker+label (between "│" and " <right> │")
        inner = w - 2                       # minus the two border columns
        avail = inner - len(marker) - len(right) - 1   # -1 = space before border
        label = item.label
        if len(label) > avail:
            label = label[: max(1, avail - 1)] + "…"
        gap = max(1, inner - len(marker) - len(label) - len(right) - 1)
        row = "│" + marker + label + " " * gap + right + " │"
        # guarantee exact width with a closing border
        if len(row) < w:
            row = row[:-2] + " " * (w - len(row)) + " │"
        row = row[: w - 1] + "│"
        style = "user" if sel else ("accent" if item.is_submenu else "default")
        lines.append((row, style))

    # pad
    while len(lines) < body_h + 1:
        lines.append(("│" + " " * (w - 2) + "│", "default"))

    # footer
    lines.append(("├" + "─" * (w - 2) + "┤", "accent"))
    foot = "│ ↑↓ ⏎ select  ← back  esc close"
    foot = foot[: w - 1].ljust(w - 1) + "│"
    lines.append((foot, "comment"))
    lines.append(("╰" + "─" * (w - 2) + "╯", "accent"))
    return lines
