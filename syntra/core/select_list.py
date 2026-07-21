"""Filterable select-list model (Track T2).

Pure state machine for fuzzy-filtered pickers (file / model / command palette).
The curses layer renders `visible()` and forwards key events to `move`, `type_char`,
`backspace`; everything here is deterministic and unit-tested. No I/O, no curses.

Behaviour:
- selection RESETS to top when the query changes;
- navigation WRAPS around (up at top -> bottom, down at bottom -> top);
- the viewport CENTERS the selection: start = clamp(sel - height/2, 0, n-height).

Note: we filter with
core.fuzzy (subsequence + ranking) for richer model/file/command matching.
"""

from __future__ import annotations

from dataclasses import dataclass

# ponytail: was from syntra.core.fuzzy import fuzzy_filter (deleted, YAGNI).
# Using difflib.get_close_matches for basic fuzzy matching.


@dataclass
class SelectList:
    items: list[str]
    height: int = 10
    query: str = ""
    selected: int = 0          # index into the FILTERED list
    score_fn: object = None     # optional frecency scorer (str -> float); opt-in (M4)

    # ---- filtering ----------------------------------------------------------
    def filtered(self) -> list[str]:
        """Items matching the current query, best-ranked first. With no query and an
        opt-in ``score_fn`` (frecency), the most-used items float to the top; a real
        query hands ordering to the fuzzy matcher (search intent beats usage). Without
        a score_fn the order is unchanged (insertion order)."""
        if not self.query:
            if self.score_fn is not None:
                return sorted(self.items, key=lambda it: -float(self.score_fn(it)))
            return list(self.items)
        words = self.query.lower().split()
        if len(words) == 1:
            import difflib
            return difflib.get_close_matches(self.query, self.items, n=len(self.items), cutoff=0.3)
        return [c for c in self.items if all(w in c.lower() for w in words)]

    def _clamp(self) -> None:
        n = len(self.filtered())
        if n == 0:
            self.selected = 0
            return
        self.selected = max(0, min(self.selected, n - 1))

    def _viewport_start(self) -> int:
        """Top filtered-index of the viewport, centering the selection."""
        n = len(self.filtered())
        if n <= self.height:
            return 0
        start = self.selected - self.height // 2
        return max(0, min(start, n - self.height))

    # ---- query editing ------------------------------------------------------
    def type_char(self, ch: str) -> None:
        self.query += ch
        self.selected = 0          # pickers reset highlight to top on new filter
        self._clamp()

    def backspace(self) -> None:
        if self.query:
            self.query = self.query[:-1]
            self.selected = 0
            self._clamp()

    # ---- navigation ---------------------------------------------------------
    def move(self, delta: int) -> None:
        """Move selection by delta, WRAPPING around the filtered list."""
        n = len(self.filtered())
        if n == 0:
            self.selected = 0
            return
        self.selected = (self.selected + delta) % n

    def scroll(self, delta: int) -> None:
        """Move by delta but CLAMP at the ends (no wrap) — for wheel/page scroll, so
        reaching the end STOPS instead of jumping back to the top (the flicker)."""
        n = len(self.filtered())
        if n == 0:
            self.selected = 0
            return
        self.selected = max(0, min(n - 1, self.selected + delta))

    def move_to(self, index: int) -> None:
        self.selected = index
        self._clamp()

    # ---- rendering ----------------------------------------------------------
    def visible(self) -> list[str]:
        """The window of filtered items currently on screen."""
        self._clamp()
        f = self.filtered()
        start = self._viewport_start()
        return f[start:start + self.height]

    def visible_selected(self) -> int:
        """Index of the selected row WITHIN visible() (for highlight), or -1."""
        if not self.filtered():
            return -1
        self._clamp()
        return self.selected - self._viewport_start()

    def current(self) -> str | None:
        """The currently highlighted item, or None when nothing matches."""
        f = self.filtered()
        if not f:
            return None
        self._clamp()
        return f[self.selected]
