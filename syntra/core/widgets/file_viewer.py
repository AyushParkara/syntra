"""File viewer widget — shows the contents of a file with syntax coloring.

Opened when a file is clicked in the file tree. Scrollable, line-numbered,
syntax-highlighted via the existing core/highlight.py.
"""

from __future__ import annotations

import os
from ..widget import Widget, RenderLine


class FileViewerWidget(Widget):
    kind = "file_viewer"
    focusable = True

    def __init__(self, *, title: str = "FILE", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._path = ""
        self._lines: list[str] = []
        self._scroll = 0

    # F22: cap the read so opening a huge file can't exhaust memory / freeze the UI.
    _MAX_BYTES = 2 * 1024 * 1024      # 2 MiB
    _MAX_LINES = 20000

    def open(self, path: str) -> None:
        self._path = path
        self._scroll = 0
        try:
            with open(path, "r", errors="replace") as f:
                data = f.read(self._MAX_BYTES + 1)
            truncated = len(data) > self._MAX_BYTES
            self._lines = data[:self._MAX_BYTES].split("\n")
            if len(self._lines) > self._MAX_LINES:
                self._lines = self._lines[:self._MAX_LINES]
                truncated = True
            if truncated:
                self._lines.append(f"… (truncated at {self._MAX_BYTES // (1024*1024)} MiB / "
                                   f"{self._MAX_LINES} lines — open the file directly to see the rest)")
        except (OSError, UnicodeDecodeError):
            self._lines = ["(cannot read file)"]

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        self._scroll_total = len(self._lines)
        out: list[RenderLine] = []

        # header
        if self._path:
            name = os.path.basename(self._path)
            ext = name.rsplit(".", 1)[-1].upper() if "." in name else ""
            head = f" {name}"
            if ext:
                tag = f"{ext} "
                pad = max(0, w - len(head) - len(tag))
                head = head + " " * pad + tag
            out.append(RenderLine(head[:w], "accent"))
        else:
            out.append(RenderLine(" FILE  —  click a file in the tree", "dim"))

        body_h = height - 1
        self._body_h = max(1, body_h)
        gutter = len(str(len(self._lines))) if self._lines else 1
        visible = self._lines[self._scroll:self._scroll + body_h]
        ext = (self._path or "").rsplit(".", 1)[-1].lower() if self._path and "." in self._path else ""
        py_keywords = {"def", "class", "import", "from", "return", "if", "else", "elif",
                       "for", "while", "try", "except", "with", "as", "raise", "yield",
                       "async", "await", "lambda", "pass", "break", "continue", "in", "not",
                       "and", "or", "is", "True", "False", "None"}
        for i, ln in enumerate(visible):
            lineno = self._scroll + i + 1
            text = f"{lineno:>{gutter}} {ln}"
            stripped = ln.strip()
            # Basic syntax highlighting
            if ext in ("py", "pyi"):
                if stripped.startswith("#"):
                    style = "comment"
                elif stripped.startswith("def ") or stripped.startswith("class "):
                    style = "keyword"
                elif stripped.startswith(("'", '"', "f'", 'f"', "b'", 'b"')):
                    style = "string"
                elif any(stripped.startswith(k + " ") or stripped.startswith(k + "(") for k in py_keywords):
                    style = "keyword"
                else:
                    style = "code"
            elif ext in ("js", "ts", "tsx", "jsx"):
                if stripped.startswith("//"):
                    style = "comment"
                elif stripped.startswith(("const ", "let ", "function ", "class ", "import ", "export ")):
                    style = "keyword"
                else:
                    style = "code"
            elif ext == "json":
                if ":" in stripped:
                    style = "string"
                else:
                    style = "number"
            else:
                style = "code"
            out.append(RenderLine(text[:w], style))

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
