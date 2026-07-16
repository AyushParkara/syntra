"""Chat minimap rail (message navigator) — pure render model.

The heavy logic for the right-edge rail described in msg_redirect.png / normat_chat_one.png:
- COLLAPSED: a thin column of ticks, one per user message, with the message nearest the current
  scroll position highlighted (the "you are here" tick).
- EXPANDED (on hover): a panel of the user's messages (truncated labels), scrollable, with one
  focused row shown in full.
- HIT-TESTING: map a mouse (x, y) to "is this on the rail?" and to a row index.

No curses, no I/O — fully unit-testable. tui2 owns the actual drawing + mouse plumbing; it feeds
this model the message index (from `run_goal.message_index()`), the chat viewport geometry, and
the current scroll, and renders what it returns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RailTick:
    """One tick in the collapsed rail."""
    row: int            # screen row (0-based within the chat pane) this tick draws on
    turn_index: int     # the history turn this tick points at
    current: bool       # True if this is the message nearest the current scroll position


@dataclass(frozen=True)
class RailRow:
    """One row in the expanded panel."""
    turn_index: int
    label: str
    focused: bool


class MinimapRail:
    """Pure model for the chat minimap rail.

    `index` is the message-navigator index: a list of `(turn_index, label)` for the user's
    messages (from `core.loop.user_message_index`). `pane_height` is the chat pane's height in
    rows; `pane_right` is the screen column of the rail (the chat's right edge)."""

    def __init__(self, index, *, pane_height: int, pane_right: int):
        self.index = list(index or [])
        self.pane_height = max(1, int(pane_height))
        self.pane_right = int(pane_right)

    def __bool__(self) -> bool:
        return bool(self.index)

    # ---- collapsed ticks ---------------------------------------------------
    def ticks(self, current_turn_index: int = -1) -> list[RailTick]:
        """The collapsed rail: one tick per user message, distributed down the pane height.

        `current_turn_index` is the history turn currently at/near the top of the viewport; the
        tick for the user message at-or-before it is marked `current`. Many messages in a short
        pane are spread evenly (so the rail is a true minimap, not a 1:1 list)."""
        n = len(self.index)
        if n == 0:
            return []
        h = self.pane_height
        # pick which message each of the h rows represents (even distribution); for n<=h every
        # message gets its own row, top-aligned.
        cur_pos = self._current_pos(current_turn_index)
        if n <= h:
            # Spread the messages DOWN THE FULL pane height (first at the top row, last at the
            # bottom row) so the rail always spans top→bottom — not clustered at the top with an
            # empty lower half (user: "it's still at top, should run to the bottom"). With one
            # message it sits at the top. Each message still owns a distinct row (n<=h), so the
            # current tick stays exact.
            if n == 1:
                return [RailTick(row=0, turn_index=self.index[0][0], current=True)]
            return [RailTick(row=round(i * (h - 1) / (n - 1)),
                             turn_index=ti, current=(i == cur_pos))
                    for i, (ti, _label) in enumerate(self.index)]
        # n > h: each row samples one message. The current message often ISN'T one of the
        # sampled indices, so an exact i==cur_pos test would drop the "you are here" marker
        # entirely. Instead mark the single row whose sampled message is NEAREST cur_pos.
        rows: list[tuple[int, int]] = []          # (row, message-index)
        for row in range(h):
            # the LAST row always maps to the last message (the newest, at the bottom of the
            # chat) so the rail's extremes match the transcript's.
            i = n - 1 if row == h - 1 else min(n - 1, (row * n) // h)
            rows.append((row, i))
        cur_row = min(rows, key=lambda ri: (abs(ri[1] - cur_pos), ri[0]))[0]
        return [RailTick(row=row, turn_index=self.index[i][0], current=(row == cur_row))
                for row, i in rows]

    def compact_ticks(self, current_turn_index: int = -1, *, max_band: int = 0):
        """A COMPACT rail: the same ticks as ticks(), but packed into a short band that is
        VERTICALLY CENTERED in the pane (not stretched full-height) — so it reads as a small
        navigator dot-strip beside the scrollbar (the ChatGPT look), leaving the rest of the
        right column empty. Returns (rows, band_top): `rows` is RailTick list with `row` already
        offset to the centered band; `band_top` is the first pane row the band occupies.

        The band is at most `max_band` rows (default = pane_height), and never taller than the
        message count (so few messages → a few centered dots, not a full column of them)."""
        n = len(self.index)
        if n == 0:
            return [], 0
        h = self.pane_height
        band = min(h, n, max_band or h)
        band = max(1, band)
        band_top = max(0, (h - band) // 2)
        # reuse the even-distribution + nearest-current logic against the SMALLER band height,
        # then shift each row down into the centered band.
        inner = MinimapRail(self.index, pane_height=band, pane_right=self.pane_right)
        shifted = [RailTick(row=t.row + band_top, turn_index=t.turn_index, current=t.current)
                   for t in inner.ticks(current_turn_index)]
        return shifted, band_top

    def _current_pos(self, current_turn_index: int) -> int:
        """Index into self.index of the user message at-or-before current_turn_index (else last)."""
        if current_turn_index < 0:
            return len(self.index) - 1
        pos = len(self.index) - 1
        for i, (ti, _l) in enumerate(self.index):
            if ti <= current_turn_index:
                pos = i
            else:
                break
        return pos

    # ---- expanded panel ----------------------------------------------------
    def expanded(self, focus: int, *, max_rows: int = 0) -> list[RailRow]:
        """The expanded panel rows, windowed around `focus` (an index into self.index).
        `max_rows` 0 = use pane_height. Scrolls so the focused row stays visible."""
        n = len(self.index)
        if n == 0:
            return []
        focus = max(0, min(focus, n - 1))
        window = max(1, max_rows or self.pane_height)
        if n <= window:
            start = 0
        else:
            start = max(0, min(focus - window // 2, n - window))
        rows: list[RailRow] = []
        for i in range(start, min(n, start + window)):
            ti, label = self.index[i]
            rows.append(RailRow(turn_index=ti, label=label, focused=(i == focus)))
        return rows

    # ---- hit-testing -------------------------------------------------------
    def on_rail(self, x: int) -> bool:
        """True if a mouse x-column is on the rail (its single right-edge column)."""
        return x == self.pane_right

    def row_at(self, y: int, focus: int, *, pane_top: int = 0, max_rows: int = 0) -> int:
        """Map a mouse y (screen row) over the EXPANDED panel to an index into self.index, or
        -1 if outside. Mirrors expanded()'s windowing so a click lands on the row drawn there."""
        rows = self.expanded(focus, max_rows=max_rows)
        r = y - pane_top
        if 0 <= r < len(rows):
            return self._index_of_turn(rows[r].turn_index)
        return -1

    def _index_of_turn(self, turn_index: int) -> int:
        for i, (ti, _l) in enumerate(self.index):
            if ti == turn_index:
                return i
        return -1
