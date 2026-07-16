"""File tree widget — workspace directory listing.

Shows the project file tree with expand/collapse, git status markers,
and selection highlighting. Like the left sidebar in VSCode or the
WORKSPACE panel in the reference image.
"""

from __future__ import annotations

import os
from ..widget import Widget, RenderLine


class _Node:
    __slots__ = ("name", "path", "is_dir", "children", "expanded", "depth")

    def __init__(self, name: str, path: str, is_dir: bool, depth: int):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.children: list[_Node] = []
        self.expanded = depth < 1  # auto-expand first level
        self.depth = depth


def _build_tree(root: str, max_depth: int = 4, max_files: int = 500) -> _Node:
    """Build a file tree from a directory. Skips hidden, __pycache__, node_modules, .git."""
    skip = {".git", "__pycache__", "node_modules", ".syntra", ".venv", "venv",
            ".mypy_cache", ".pytest_cache", "dist", "build", ".egg-info"}
    count = [0]

    def _scan(path: str, depth: int) -> _Node:
        name = os.path.basename(path) or path
        node = _Node(name, path, os.path.isdir(path), depth)
        if not node.is_dir or depth > max_depth:
            return node
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return node
        dirs, files = [], []
        for e in entries:
            if e.startswith(".") and e not in (".gitignore",):
                continue
            if e in skip:
                continue
            fp = os.path.join(path, e)
            if os.path.isdir(fp):
                dirs.append(fp)
            else:
                files.append(fp)
        for d in dirs:
            count[0] += 1
            if count[0] > max_files:
                break
            node.children.append(_scan(d, depth + 1))
        for f in files:
            count[0] += 1
            if count[0] > max_files:
                break
            node.children.append(_Node(os.path.basename(f), f, False, depth + 1))
        return node

    return _scan(root, 0)


def _flatten(node: _Node) -> list[_Node]:
    """Flatten the tree into a visible list (respecting expanded state)."""
    out = [node]
    if node.is_dir and node.expanded:
        for c in node.children:
            out.extend(_flatten(c))
    return out


class FileTreeWidget(Widget):
    kind = "file_tree"
    focusable = True

    def __init__(self, *, title: str = "WORKSPACE", on_event=None, root: str = ""):
        super().__init__(title=title, on_event=on_event)
        self._root = root or os.getcwd()
        self._tree: _Node | None = None
        self._flat: list[_Node] = []
        self._selected = 0
        self._scroll = 0
        self.refresh()

    def refresh(self) -> None:
        self._tree = _build_tree(self._root)
        self._flat = _flatten(self._tree)
        self._selected = min(self._selected, max(0, len(self._flat) - 1))

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        lines: list[RenderLine] = []

        # title
        short_root = os.path.basename(self._root.rstrip("/")) or self._root
        header = f" ~/{short_root}"
        lines.append(RenderLine(header[:w], "accent"))

        body_h = height - 1
        # keep selected in view
        if self._selected < self._scroll:
            self._scroll = self._selected
        if self._selected >= self._scroll + body_h:
            self._scroll = self._selected - body_h + 1
        self._scroll = max(0, self._scroll)

        visible = self._flat[self._scroll:self._scroll + body_h]
        for i, node in enumerate(visible):
            idx = self._scroll + i
            indent = "  " * node.depth
            if node.is_dir:
                icon = "▸ " if not node.expanded else "▾ "
            else:
                icon = "  "
            marker = "▌" if idx == self._selected else " "
            text = f"{marker}{indent}{icon}{node.name}"
            style = "user" if idx == self._selected else ("accent" if node.is_dir else "default")
            lines.append(RenderLine(text[:w], style))

        while len(lines) < height:
            lines.append(RenderLine("", "default"))

        return lines

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        if ch in (curses.KEY_UP, ord("k")):
            self._selected = max(0, self._selected - 1)
            return True
        if ch in (curses.KEY_DOWN, ord("j")):
            self._selected = min(len(self._flat) - 1, self._selected + 1)
            return True
        if ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            if 0 <= self._selected < len(self._flat):
                node = self._flat[self._selected]
                if node.is_dir:
                    node.expanded = not node.expanded
                    self._flat = _flatten(self._tree) if self._tree else []
                else:
                    self.emit("open_file", node.path)
            return True
        if ch == curses.KEY_RIGHT:
            if 0 <= self._selected < len(self._flat):
                node = self._flat[self._selected]
                if node.is_dir and not node.expanded:
                    node.expanded = True
                    self._flat = _flatten(self._tree) if self._tree else []
            return True
        if ch == curses.KEY_LEFT:
            if 0 <= self._selected < len(self._flat):
                node = self._flat[self._selected]
                if node.is_dir and node.expanded:
                    node.expanded = False
                    self._flat = _flatten(self._tree) if self._tree else []
            return True
        if ch == ord("r"):  # refresh
            self.refresh()
            return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        if button & (curses.BUTTON1_CLICKED | getattr(curses, "BUTTON1_PRESSED", 0)):
            idx = self._scroll + y - 1  # -1 for header
            if 0 <= idx < len(self._flat):
                self._selected = idx
                node = self._flat[idx]
                if node.is_dir:
                    node.expanded = not node.expanded
                    self._flat = _flatten(self._tree) if self._tree else []
                else:
                    self.emit("open_file", node.path)
            return True
        if button & curses.BUTTON4_PRESSED:
            self._scroll = max(0, self._scroll - 3); return True
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self._scroll = min(max(0, len(self._flat) - 1), self._scroll + 3); return True
        return False
