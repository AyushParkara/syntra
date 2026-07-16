"""Pure TUI core models (Track T1).

The renderable STATE of the terminal UI, with NO curses dependency, so the hard
parts (text wrapping, scrolling, search, the status line) are deterministic and
unit-tested. The curses layer (cli/tui.py, added next) is a thin draw loop over
these models. This separation is deliberate: legacy's TUI became an untestable
blob; keeping the logic pure here prevents that.

Patterns synthesized from a study of modern terminal UIs — see
reference/snippets/tui-patterns.md.
"""

from __future__ import annotations

import os
import random
import textwrap
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str            # "user" | "assistant" | "system" | "tool"
    text: str
    task_id: str = ""    # the run/task this turn belongs to (for correct forking)
    # F31: alternative outputs for the SAME assistant turn (Retry stores variants
    # in-place instead of appending a new reply). Empty = a single, non-toggleable
    # message. When non-empty, the shown body is variants[variant_idx].
    variants: list = field(default_factory=list)
    variant_idx: int = 0


class ReasoningLineBuffer:
    """BUG1 fix: coalesce a token-streamed chain-of-thought into WHOLE lines.

    Many providers stream reasoning one word/token at a time (no newline until the
    thought line ends). The old trace path emitted each chunk as its own row → the
    "one word per line" glitch. This buffer accumulates chunks and returns only
    COMPLETE lines (split on ``\\n``), holding the partial tail until the next
    newline; ``flush()`` emits whatever tail remains when the thought ends.
    Blank/whitespace-only lines are dropped (they were never real thought rows).
    Pure + stateful; unit-tested."""

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, chunk: str) -> list[str]:
        """Absorb a streamed chunk; return any newly-COMPLETED (non-blank) lines."""
        if not chunk:
            return []
        self._tail += chunk
        if "\n" not in self._tail:
            return []                       # no complete line yet — keep buffering
        parts = self._tail.split("\n")
        self._tail = parts.pop()            # last fragment is incomplete → hold it
        return [p.strip() for p in parts if p.strip()]

    def flush(self) -> list[str]:
        """Emit the held tail (if any) when the thought ends. Resets the buffer."""
        rest = self._tail.strip()
        self._tail = ""
        return [rest] if rest else []


def classify_fold_row(row_text: str) -> "str | None":
    """BUG2: decide what a click on a RENDERED row should fold/expand, from the row text
    itself — NOT a recomputed line-count that can disagree with what was actually drawn.

    Returns:
      "trace"    → a background-trace summary row ("… click here or /trace …") → toggle the trace
      "expand"   → a folded answer/output/code/plan card ("… Enter or click to expand" /
                   "… click to expand the plan") → expand THAT element
      "collapse" → an expanded block's re-fold affordance ("▾ collapse — … to fold")
      None       → an ordinary content row (a click here must NOT toggle anything)

    Matching the DRAWN affordance guarantees the click acts on the specific foldable element
    under the cursor (answer card vs trace), independently. Pure."""
    t = (row_text or "").strip().strip("│").strip()   # tolerate the bubble border
    if not t:
        return None
    if "/trace" in t and ("click here" in t or "background line" in t):
        return "trace"
    if t.startswith("▾") and "to fold" in t:
        return "collapse"
    if "click to expand the plan" in t or "Enter or click to expand" in t:
        return "expand"
    return None


def wrap_lines(text: str, width: int) -> list[str]:
    """Wrap text to width, preserving blank lines, never cutting mid-stream.

    A line longer than width is wrapped (not truncated) so long messages stay
    fully visible (messages must wrap, not cut).
    """
    width = max(1, int(width))
    out: list[str] = []
    for raw in (text or "").split("\n"):
        if not raw:
            out.append("")
            continue
        out.extend(textwrap.wrap(raw, width=width, replace_whitespace=False,
                                 drop_whitespace=False) or [""])
    return out


@dataclass
class Transcript:
    """Scrollable, wrapping, searchable message log. Pure (no curses)."""

    messages: list[Message] = field(default_factory=list)
    width: int = 80
    scroll: int = 0          # index of the first visible wrapped line (from top)
    _follow: bool = True     # stick to the bottom until the user scrolls up
    markdown: bool = True    # render assistant text as markdown (code-fence aware)
    collapsed: set = field(default_factory=set)  # message indices the user manually collapsed
    trace_collapsed: bool = True   # fold background trace (ANALYZE/route/thinking) to 1 line by default (user [180]); /trace or click expands
    COLLAPSE_THRESHOLD: int = 8  # lines above which auto-collapse
    # Long outputs (big code blocks, tool dumps, long answers) AUTO-FOLD to a single
    # "▸ <label> (N lines) — Enter/click to expand" summary so you don't scroll past a
    # 300-line wall (user #5). `expanded` holds the message indices the user opened back
    # up. A message folds when its body exceeds AUTOFOLD_THRESHOLD lines and it's not in
    # `expanded` and not manually `collapsed`. The newest assistant turn is exempt (you're
    # reading it). Pure state — the widget toggles membership on click/Enter.
    expanded: set = field(default_factory=set)
    AUTOFOLD_THRESHOLD: int = 30  # body lines above which a block auto-folds
    # The view geometry of the LAST render, in the SAME line space the chat widget
    # actually paints (render_bubbles count + content height). Scroll ops clamp
    # against these so the scrollbar/viewport math matches what's on screen — the
    # widget renders render_bubbles() but the old scroll math used _rendered(),
    # a different line count, which made scrolling feel broken (P3/P36).
    _view_total: int = 0     # total rendered lines (bubbles) at last paint
    _view_height: int = 0    # visible content height at last paint

    def add(self, role: str, text: str) -> None:
        self.messages.append(Message(role=role, text=text))

    def append_stream(self, chunk: str) -> None:
        """Append a streamed token chunk to the in-progress assistant message,
        creating it on the first chunk. Lets tokens render live as they arrive."""
        if self.messages and self.messages[-1].role == "assistant_stream":
            self.messages[-1].text += chunk
        else:
            self.messages.append(Message(role="assistant_stream", text=chunk))

    def end_stream(self) -> None:
        """Finalize a streaming message: promote it to a normal assistant message."""
        if self.messages and self.messages[-1].role == "assistant_stream":
            self.messages[-1].role = "assistant"

    def last_assistant_index(self) -> int:
        """Index of the most recent assistant turn, or -1. (F31 retry target.)"""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role in ("assistant", "assistant_stream"):
                return i
        return -1

    def add_variant(self, msg_index: int, text: str) -> bool:
        """Store `text` as another alternative of an assistant turn and switch to it
        (F31 Retry: alternatives live IN-PLACE, not appended below). Seeds the list
        with the original on first use. Returns True on success."""
        if not (0 <= msg_index < len(self.messages)):
            return False
        m = self.messages[msg_index]
        if m.role not in ("assistant", "assistant_stream"):
            return False
        if not m.variants:
            m.variants = [m.text]
        m.variants.append(text)
        m.variant_idx = len(m.variants) - 1
        m.text = text                      # keep .text mirrored to the shown variant
        return True

    def cycle_variant(self, msg_index: int, delta: int) -> bool:
        """Flip an assistant turn's shown alternative by ±1 (wraps). Returns True if
        it actually changed (i.e. the turn has >1 variant)."""
        if not (0 <= msg_index < len(self.messages)):
            return False
        m = self.messages[msg_index]
        n = len(getattr(m, "variants", None) or [])
        if n <= 1:
            return False
        m.variant_idx = (m.variant_idx + delta) % n
        m.text = m.variants[m.variant_idx]
        return True

    def _message_text(self, m: "Message") -> str:
        """Assistant markdown -> readable plain text (code fences preserved)."""
        if self.markdown and m.role in ("assistant", "assistant_stream"):
            from syntra.core.markdown import render_plain
            return render_plain(m.text)
        return m.text

    def rendered_lines(self) -> list[str]:
        """All wrapped display lines (with a role prefix per message)."""
        return [text for text, _role in self._rendered()]

    def rendered_lines_with_roles(self) -> list[tuple[str, str]]:
        """Wrapped display lines as (text, role) so a UI can color per line."""
        return self._rendered()

    def _rendered(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for m in self.messages:
            prefix = {"user": "> ", "assistant": "", "assistant_stream": "",
                      "system": "· ", "tool": "  "}.get(m.role, "")
            body = wrap_lines(self._message_text(m), self.width - len(prefix))
            if not body:
                body = [""]
            out.append((prefix + body[0], m.role))
            out.extend(((" " * len(prefix)) + b, m.role) for b in body[1:])
            out.append(("", m.role))            # blank separator between messages
        return out

    def total_lines(self) -> int:
        return len(self.rendered_lines())

    def visible(self, height: int) -> list[str]:
        """The `height` lines currently in view, honoring scroll/follow."""
        return [t for t, _r in self.visible_with_roles(height)]

    def visible_with_roles(self, height: int) -> list[tuple[str, str]]:
        """Viewport lines as (text, role), honoring scroll/follow (for colored UI)."""
        height = max(1, int(height))
        lines = self._rendered()
        if self._follow:
            self.scroll = max(0, len(lines) - height)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - height)))
        return lines[self.scroll:self.scroll + height]

    # ---- scrolling (PgUp/PgDn/Home/End) ----

    def sync_view(self, total: int, height: int) -> None:
        """Record the geometry of the current paint (total rendered lines + visible
        height) so scroll ops clamp against what's actually drawn. Called by the
        chat widget each render with the render_bubbles() count + content height."""
        self._view_total = max(0, int(total))
        self._view_height = max(1, int(height))

    def max_scroll(self, height: int | None = None) -> int:
        """Largest valid scroll offset for the current view geometry. Once a paint
        has recorded the real geometry (sync_view), that wins over any passed
        height so the clamp matches exactly what's on screen."""
        if self._view_total:
            h = self._view_height or height or 1
            return max(0, self._view_total - h)
        h = height or self._view_height or 1
        return max(0, len(self.rendered_lines()) - h)

    def scroll_up(self, n: int = 1) -> None:
        self.scroll = max(0, self.scroll - max(1, n))
        self._follow = False

    def scroll_down(self, n: int = 1, height: int | None = None) -> None:
        top = self.max_scroll(height)
        self.scroll = min(top, self.scroll + max(1, n))
        # Re-enable follow once we've scrolled back to the bottom.
        self._follow = self.scroll >= top

    def to_top(self) -> None:
        self.scroll = 0
        self._follow = False

    def scroll_to_fraction(self, frac: float) -> None:
        """Jump to an absolute position: frac 0.0 = top, 1.0 = bottom. Used by the
        scrollbar DRAG — the thumb's y within the track maps straight to a scroll offset.
        Clamped to the current view geometry; re-enables follow only when dragged to the end."""
        top = self.max_scroll()
        self.scroll = max(0, min(top, int(round(max(0.0, min(1.0, frac)) * top))))
        self._follow = self.scroll >= top

    def is_autofoldable(self, idx: int) -> bool:
        """True if message `idx` is a long block that auto-folds (user #5): its wrapped
        body exceeds AUTOFOLD_THRESHOLD lines and it isn't the newest turn (you're reading
        that one). Used by the click handler to toggle membership in `expanded`."""
        if not (0 <= idx < len(self.messages)):
            return False
        m = self.messages[idx]
        body = self._message_text(m)
        # The plan card folds by default regardless of length (it's chrome, not the answer), so
        # it's foldable/expandable even when it's the newest message or short.
        if getattr(m, "role", "") == "system" and body.lstrip().startswith("📋 plan"):
            return True
        if idx == len(self.messages) - 1:
            return False                      # newest turn is always shown in full
        n = len(wrap_lines(body, max(1, self.width - 4)))
        return n > self.AUTOFOLD_THRESHOLD

    def toggle_expanded(self, idx: int) -> None:
        """Flip a long block between folded (summary) and expanded (full)."""
        if idx in self.expanded:
            self.expanded.discard(idx)
        else:
            self.expanded.add(idx)

    def to_bottom(self) -> None:
        self._follow = True
        if self._view_total:
            self.scroll = self.max_scroll()

    def at_bottom(self) -> bool:
        """True when the viewport is showing the latest line (nothing below)."""
        return self._follow or self.scroll >= self.max_scroll()

    # ---- find (F3) ----

    def find(self, query: str) -> list[int]:
        """Return wrapped-line indices containing query (case-insensitive)."""
        if not query:
            return []
        q = query.lower()
        return [i for i, ln in enumerate(self.rendered_lines()) if q in ln.lower()]

    def scroll_to(self, line: int, height: int) -> None:
        """Scroll so wrapped-line index `line` is visible (centered when possible)."""
        height = max(1, int(height))
        total = len(self.rendered_lines())
        top = int(line) - height // 2
        self.scroll = max(0, min(top, max(0, total - height)))
        self._follow = self.scroll >= max(0, total - height)


class TranscriptSearch:
    """#216: in-transcript incremental search — the real find-in-page (unlike the old
    static /msgs list). Over a snapshot of rendered lines it collects EVERY occurrence of
    the query as ``(line_index, col_start, col_end)`` matches, tracks a current match for
    ``n``/``N`` navigation (wrapping), reports an ``X/total`` label, and yields per-line
    highlight spans so the paint can underline/invert matches. Pure — no curses — so the
    match/nav logic is fully unit-tested; the curses layer just calls scroll_to on
    ``current_line()`` and paints ``spans_for_line()``.

    Case-insensitive substring search. Overlapping matches on a line are advanced past the
    end of each hit (so ``aa`` in ``aaaa`` yields 2 matches, not 3), matching editor find."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._query: str = ""
        self._matches: list[tuple[int, int, int]] = []
        self._cur: int = 0
        # spans grouped by line index for O(1) paint lookup
        self._by_line: dict[int, list[tuple[int, int]]] = {}

    def set_lines(self, lines) -> None:
        """Replace the searched line snapshot (call when the transcript changes) and
        re-run the current query against it."""
        self._lines = [str(x) for x in (lines or [])]
        self._recompute()

    def set_query(self, query: str) -> None:
        """Set the search text and re-find. Resets the current match to the first hit."""
        self._query = query or ""
        self._recompute()

    def _recompute(self) -> None:
        self._matches = []
        self._by_line = {}
        q = self._query.lower()
        if q:
            n = len(q)
            for li, ln in enumerate(self._lines):
                hay = ln.lower()
                start = 0
                while True:
                    j = hay.find(q, start)
                    if j < 0:
                        break
                    self._matches.append((li, j, j + n))
                    self._by_line.setdefault(li, []).append((j, j + n))
                    start = j + n            # non-overlapping, like an editor find
        self._cur = 0

    # ── query state ──
    def query(self) -> str:
        return self._query

    def matches(self) -> list[tuple[int, int, int]]:
        return list(self._matches)

    def total(self) -> int:
        return len(self._matches)

    def current_index(self) -> int:
        return self._cur if self._matches else 0

    def current_match(self):
        if not self._matches:
            return None
        return self._matches[self._cur % len(self._matches)]

    def current_line(self):
        m = self.current_match()
        return m[0] if m else None

    def label(self) -> str:
        """``X/total`` (1-based), or ``0/0`` when nothing matches."""
        if not self._matches:
            return "0/0"
        return f"{(self._cur % len(self._matches)) + 1}/{len(self._matches)}"

    # ── navigation (wrapping) ──
    def next(self) -> None:
        if self._matches:
            self._cur = (self._cur + 1) % len(self._matches)

    def prev(self) -> None:
        if self._matches:
            self._cur = (self._cur - 1) % len(self._matches)

    def spans_for_line(self, line_index: int) -> list[tuple[int, int]]:
        """``(col_start, col_end)`` spans of every match on one rendered line (for the
        paint's highlight). Empty when the line has no match."""
        return list(self._by_line.get(int(line_index), []))

    def is_current(self, line_index: int, col_start: int) -> bool:
        """True if the given (line, col_start) is the CURRENTLY-selected match — the paint
        colours it distinctly (the 'active' hit vs the other dim hits)."""
        m = self.current_match()
        return bool(m and m[0] == line_index and m[1] == col_start)


def selection_spans(y0: int, x0: int, y1: int, x1: int,
                    cols: int) -> list[tuple[int, int, int]]:
    """Screen-coordinate drag selection -> per-row (row, col_start, col_end) spans.

    `col_end` is EXCLUSIVE. The anchor (x0,y0) and current point (x1,y1) may be in
    any order; they're normalized so the earlier point (top-to-bottom, then
    left-to-right) comes first. A single-row selection covers min..max inclusive;
    a multi-row selection runs anchor→end-of-line, full middle rows, start→x1.

    Both the highlight renderer (P1) and the clipboard extractor (P2) use this, so
    what's highlighted is exactly what's copied. Pure -> unit-tested.
    """
    cols = max(0, int(cols))
    # normalize: top-to-bottom, then left-to-right on a single row
    if (y1, x1) < (y0, x0):
        y0, x0, y1, x1 = y1, x1, y0, x0
    spans: list[tuple[int, int, int]] = []
    for y in range(y0, y1 + 1):
        if y0 == y1:
            a, b = min(x0, x1), max(x0, x1) + 1
        elif y == y0:
            a, b = x0, cols
        elif y == y1:
            a, b = 0, x1 + 1
        else:
            a, b = 0, cols
        a = max(0, a)
        b = min(cols, b)
        if b > a:
            spans.append((y, a, b))
    return spans


# ── content-anchored chat selection (scroll-to-extend copy) ───────────────────
# selection_spans above anchors to SCREEN rows: scrolling moves the text out from under it, so a
# selection can't survive a scroll and copy can't reach off-screen lines. The helpers below anchor
# instead to CONTENT lines (the transcript's pre-scroll line index) + column, so a chat selection
# survives scrolling and copy reads the FULL line list, not just the visible grid. Pure (no curses)
# so they're unit-tested. Mirror selection_spans' normalization exactly so highlight == copy.

def chat_content_line(screen_y: int, content_top: int, scroll: int) -> int:
    """Map a screen row to the content-line index it shows. `content_top` is the first content
    row of the chat (just below the tab bar); `scroll` is the first visible content line. Inverse
    of the paint projection `screen_y = content_top + (content_line - scroll)`."""
    return scroll + (int(screen_y) - int(content_top))


def _row_bounds(cl: int, cl0: int, ccol0: int, cl1: int, ccol1: int,
                cols: int) -> tuple[int, int]:
    """Column bounds (a, b-exclusive) of content line `cl` within the selection (cl0,ccol0)→
    (cl1,ccol1). Same shape as selection_spans: first line ccol0→cols, full middle rows, last line
    0→ccol1+1, single line min..max+1. Caller normalizes endpoints top-first. Returns (0,0) if the
    line is outside the range. Shared by the highlight + copy helpers so they can't diverge."""
    cols = max(0, int(cols))
    if cl < cl0 or cl > cl1:
        return (0, 0)
    if cl0 == cl1:
        a, b = min(ccol0, ccol1), max(ccol0, ccol1) + 1
    elif cl == cl0:
        a, b = ccol0, cols
    elif cl == cl1:
        a, b = 0, ccol1 + 1
    else:
        a, b = 0, cols
    return (max(0, a), min(cols, b))


def content_selection_spans(cl0: int, ccol0: int, cl1: int, ccol1: int,
                            scroll: int, content_top: int, content_h: int,
                            cols: int) -> list[tuple[int, int, int]]:
    """Project a content-anchored selection onto the VISIBLE screen rows -> [(screen_y, a, b)].
    Endpoints may be in any order (normalized top-first). Lines scrolled off-screen are clipped
    out; the visible slice keeps its first/last-line column bounds. Drop-in for the highlight
    renderer (same (row, a, b-exclusive) tuples selection_spans returns)."""
    if (cl1, ccol1) < (cl0, ccol0):
        cl0, ccol0, cl1, ccol1 = cl1, ccol1, cl0, ccol0
    content_h = max(0, int(content_h))
    out: list[tuple[int, int, int]] = []
    for cl in range(cl0, cl1 + 1):
        sy = content_top + (cl - scroll)
        if sy < content_top or sy >= content_top + content_h:
            continue                                   # off-screen — clipped
        a, b = _row_bounds(cl, cl0, ccol0, cl1, ccol1, cols)
        if b > a:
            out.append((sy, a, b))
    return out


def content_selection_copy(bubble_texts: list, cl0: int, ccol0: int,
                           cl1: int, ccol1: int, cols: int) -> str:
    """Extract the selected text from the FULL content-line list `bubble_texts` (every rendered
    chat line, pre-scroll) — so the copy reaches lines that scrolled off-screen. Endpoints may be
    in any order. Each line is column-bounded via the same `_row_bounds` the highlight uses,
    rstripped, and joined with newlines. Pure -> unit-tested."""
    if (cl1, ccol1) < (cl0, ccol0):
        cl0, ccol0, cl1, ccol1 = cl1, ccol1, cl0, ccol0
    n = len(bubble_texts)
    parts: list[str] = []
    for cl in range(max(0, cl0), min(n - 1, cl1) + 1):
        a, b = _row_bounds(cl, cl0, ccol0, cl1, ccol1, cols)
        if b > a:
            parts.append(str(bubble_texts[cl])[a:b])
        else:
            parts.append("")
    return "\n".join(s.rstrip() for s in parts).strip("\n")


def input_viewport(text: str, cursor: int, width: int) -> tuple[str, int]:
    """BUG5: a single-line box viewport that SCROLLS to keep the cursor visible.

    Returns (visible_slice, cursor_col_within_slice). A box that just did ``text[:width]``
    showed the HEAD, so once you typed past the box edge the new chars (and the cursor) went
    off-screen — "it writes somewhere I can't see". This slides a window so the cursor is
    always inside it (showing the tail you're typing), with a leading/trailing ``…`` marker
    when text is clipped on that side. Pure + deterministic."""
    width = max(1, int(width))
    text = text or ""
    n = len(text)
    cursor = max(0, min(int(cursor), n))
    if n <= width:
        return text, cursor
    # keep the cursor within [start, start+width]; bias so the cursor sits near the right
    # edge while typing forward (so you see what you just typed) but never past the window.
    start = max(0, min(cursor - width + 1, n - width))
    start = max(0, start)
    end = min(n, start + width)
    slice_ = text[start:end]
    col = cursor - start
    # add clip markers WITHOUT growing past width: replace an edge char with '…'
    lead = start > 0
    trail = end < n
    if lead and slice_:
        slice_ = "…" + slice_[1:]
    if trail and slice_:
        slice_ = slice_[:-1] + "…"
    col = max(0, min(col, len(slice_)))
    return slice_, col


def input_rows(text: str, cursor: int, width: int, *, max_rows: int = 6,
               prompt: str = "❯ ") -> tuple[list[str], int, int]:
    """Lay out the input buffer into visual rows that GROW (up to max_rows) and
    SCROLL to keep the cursor visible (P27). Returns (rows, cursor_row, cursor_col):
      - rows: display strings, `prompt` on the first row, a matching indent after;
      - cursor_row: index into the RETURNED rows;
      - cursor_col: screen column of the cursor within its row.

    Char-wrapped (not word-wrapped) so cursor math is exact. Pure -> unit-tested.
    """
    width = max(len(prompt) + 1, int(width))
    indent = " " * len(prompt)
    avail = max(1, width - len(prompt))

    # flatten logical lines (\n) into avail-sized visual segments, tracking the
    # text offset each segment starts at so we can place the cursor exactly.
    segs: list[tuple[int, str]] = []   # (text_offset, seg_text)
    off = 0
    for line in (text or "").split("\n"):
        if line == "":
            segs.append((off, ""))
        else:
            start = 0
            while start < len(line):
                segs.append((off + start, line[start:start + avail]))
                start += avail
        off += len(line) + 1           # +1 for the consumed newline
    if not segs:
        segs = [(0, "")]

    # which segment holds the cursor?
    cur_seg, cur_off = len(segs) - 1, len(segs[-1][1])
    for i, (so, st) in enumerate(segs):
        if so <= cursor <= so + len(st):
            cur_seg, cur_off = i, cursor - so
            break

    # window of at most max_rows segments, keeping the cursor row visible
    max_rows = max(1, int(max_rows))
    total = len(segs)
    if total <= max_rows:
        start = 0
    else:
        start = min(max(0, cur_seg - max_rows + 1), total - max_rows)
    window = segs[start:start + max_rows]

    rows: list[str] = []
    for j, (_so, st) in enumerate(window):
        g = start + j
        rows.append((prompt if g == 0 else indent) + st)
    cursor_row = cur_seg - start
    # cursor_col must be a SCREEN CELL column (it feeds stdscr.move) — so measure the
    # DISPLAY width of everything left of the cursor, not the code-point count. With a
    # wide/zero-width glyph before the caret (emoji/CJK/combining) the two differ; using
    # len() here is the #137 wide-char cursor-drift. The prompt/indent is ASCII (width
    # == len) but the segment text may not be.  (#177)
    seg_text = segs[cur_seg][1]
    prefix_cells = display_width(seg_text[:cur_off])
    cursor_col = len(prompt if (cur_seg == 0) else indent) + prefix_cells
    return rows, cursor_row, cursor_col


@dataclass
class StatusSeg:
    """One field in the top status bar."""
    key: str
    full: str                 # preferred text
    short: str                # abbreviated fallback when `full` won't fit
    style: str                # theme role for color
    side: str = "left"        # "left" packs from col 0; "right" packs flush-right
    priority: int = 50        # lower = kept longer when space is tight
    bold: bool = False


# East-Asian-wide + emoji code-point ranges that occupy TWO terminal cells. Used so
# the status bar's column math matches what the terminal actually paints (🔒/⚡ etc.
# are 2 cells but len()==1). Not a full wcwidth table — covers the glyphs we use.
_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2329, 0x232A), (0x2E80, 0x303E), (0x3041, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF), (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
    (0x1F000, 0x1FAFF),
)
_WIDE_SINGLES = frozenset({
    0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3,
    0x23F8, 0x23F9, 0x23FA, 0x25FD, 0x25FE, 0x2614, 0x2615, 0x26A1,
    0x2B1B, 0x2B1C, 0x2B50, 0x2B55,
})


# Zero-width code points: combining marks, joiners, and the emoji modifiers that
# attach to a preceding base glyph (skin tone, variation selectors). These occupy no
# cell of their own — the terminal composes them onto the previous glyph. Getting these
# right is what stops the #137 wide-char cursor-drift (a ZWJ family emoji is ONE glyph,
# not 3 base glyphs). #177.
_ZERO_WIDTH_RANGES = (
    (0x0300, 0x036F),   # combining diacritical marks
    (0x0483, 0x0489),   # combining cyrillic
    (0x0591, 0x05BD), (0x05BF, 0x05BF), (0x05C1, 0x05C2), (0x05C4, 0x05C5),
    (0x0610, 0x061A), (0x064B, 0x065F), (0x0670, 0x0670), (0x06D6, 0x06DC),
    (0x0483, 0x0489),
    (0x0E31, 0x0E31), (0x0E34, 0x0E3A), (0x0EB1, 0x0EB1), (0x0EB4, 0x0EB9),
    (0x1AB0, 0x1AFF),   # combining diacritical marks extended
    (0x1DC0, 0x1DFF),   # combining diacritical marks supplement
    (0x200B, 0x200F),   # ZWSP, ZWNJ, ZWJ, LRM/RLM
    (0x2028, 0x202E),   # line/para sep + bidi embeds/overrides
    (0x2060, 0x2064),   # word joiner, invisible operators
    (0x20D0, 0x20FF),   # combining marks for symbols
    (0xFE00, 0xFE0F),   # variation selectors (VS1–VS16)
    (0xFE20, 0xFE2F),   # combining half marks
    (0xFEFF, 0xFEFF),   # BOM / zero-width no-break space
    (0x1F3FB, 0x1F3FF), # emoji skin-tone modifiers
    (0xE0100, 0xE01EF), # variation selectors supplement
)


def char_width(ch: str) -> int:
    """Terminal cell width of one character (0, 1, or 2). Approximate.

    Zero-width covers control chars, combining marks, joiners (ZWJ/ZWSP), variation
    selectors, and emoji skin-tone modifiers — so a composed cluster measured via
    `display_width` matches the ONE glyph the terminal paints (#177 / the #137 bug)."""
    o = ord(ch)
    if o == 0 or o < 0x20 or o == 0x7F:
        return 0
    if any(a <= o <= b for a, b in _ZERO_WIDTH_RANGES):
        return 0
    if o in _WIDE_SINGLES or any(a <= o <= b for a, b in _WIDE_RANGES):
        return 2
    return 1


def display_width(s: str) -> int:
    """Total terminal cell width of a string, GRAPHEME-CLUSTER aware (#177).

    A multi-code-point glyph (ZWJ family emoji, skin-toned emoji, keycap, flag) paints
    as ONE cluster. Summing code-point widths over-counts and drifts the cursor (#137).
    We fold two cluster classes the code-point pass can't: (a) regional-indicator PAIRS
    (flags) → 2 cells for the pair, and (b) an emoji base followed by ZWJ-joined bases
    (e.g. 👨‍👩‍👧) → 2 cells for the whole run, since the joined bases render composed."""
    s = s or ""
    total = 0
    i = 0
    n = len(s)
    while i < n:
        o = ord(s[i])
        # regional-indicator flag: two RIs → one 2-cell flag glyph
        if 0x1F1E6 <= o <= 0x1F1FF:
            if i + 1 < n and 0x1F1E6 <= ord(s[i + 1]) <= 0x1F1FF:
                total += 2
                i += 2
                continue
            total += 2
            i += 1
            continue
        # keycap sequence: base + VS16(FE0F) + combining-enclosing-keycap(20E3) →
        # one 2-cell emoji glyph even though the base (a digit / '#' / '*') is 1 cell.
        if i + 2 < n and ord(s[i + 1]) == 0xFE0F and ord(s[i + 2]) == 0x20E3:
            total += 2
            i += 3
            continue
        if i + 1 < n and ord(s[i + 1]) == 0x20E3:      # keycap without explicit VS16
            total += 2
            i += 2
            continue
        w = char_width(s[i])
        # ZWJ-emoji sequence: a wide base joined by ZWJ to further bases composes into
        # ONE glyph. Consume the whole run (base [ (ZWJ|modifier|VS) base ]*) as `w`
        # cells (the width of the leading base), so joined parts don't each add 2.
        if w == 2:
            j = i + 1
            while j < n:
                oj = ord(s[j])
                if oj == 0x200D:                       # ZWJ → expect another base next
                    j += 1
                    if j < n:
                        j += 1                          # swallow the joined base
                    continue
                if char_width(s[j]) == 0:              # VS / modifier / combining
                    j += 1
                    continue
                break
            total += 2
            i = j
            continue
        total += w
        i += 1
    return total


def clip_to_width(s: str, width: int) -> str:
    """Truncate `s` so it occupies at most `width` cells."""
    if display_width(s) <= width:
        return s
    out, used = [], 0
    for c in s:
        w = char_width(c)
        if used + w > width:
            break
        out.append(c); used += w
    return "".join(out)


def fit_to_width(s: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate `s` to at most `width` cells, appending `ellipsis` when content is
    actually cut (so a clipped status field reads as "there's more" instead of a
    silent chop). Cell-accurate — wide glyphs count as 2. When `width` is too small
    to hold even the ellipsis, degrades to a plain clip. Pure -> unit-tested."""
    width = max(0, int(width))
    if width == 0:
        return ""
    if display_width(s) <= width:
        return s
    ew = display_width(ellipsis)
    if width <= ew:
        return clip_to_width(s, width)
    return clip_to_width(s, width - ew) + ellipsis


def truncate_start(s: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate from the LEFT, keeping the END visible — for file paths where the FILENAME
    matters more than the leading dirs (``…widgets/chat.py`` beats ``core/widgets/…``). Cell-
    accurate. Pure -> unit-tested (#221/#217)."""
    width = max(0, int(width))
    if width == 0:
        return ""
    if display_width(s) <= width:
        return s
    ew = display_width(ellipsis)
    if width <= ew:
        return clip_to_width(s, width)
    # keep the last (width - ew) cells
    keep = width - ew
    used, out = 0, []
    for ch in reversed(s):
        cw = char_width(ch)
        if used + cw > keep:
            break
        out.append(ch); used += cw
    return ellipsis + "".join(reversed(out))


def toast_anchor_x(text: str, cols: int, *, margin: int = 2) -> int:
    """Left x for a right-anchored toast/notice: ``cols - width - margin``, clamped
    to >=0. Uses cell-accurate `display_width` (not len) so an emoji/CJK toast doesn't
    hang a wide glyph off the right edge. Pure -> unit-tested."""
    return max(0, int(cols) - display_width(text) - int(margin))


def modal_geometry(cols: int, rows: int, want_w: int, want_h: int, *,
                   min_w: int = 24, min_h: int = 3,
                   margin_w: int = 4, margin_h: int = 2) -> tuple[int, int, int, int]:
    """Responsive centered-box geometry for a modal. Returns (w, h, x, y) that:
      • never exceeds the screen: ``x+w <= cols`` and ``y+h <= rows``;
      • honors ``want_w/want_h`` when there's room (minus the screen margin);
      • keeps a usable floor (``min_w/min_h``) UNLESS the screen is smaller, in which
        case it shrinks to the screen instead of going negative;
      • is centered, clamped to non-negative origin.
    Pure -> unit-tested. Replaces the scattered ``min(96, cols-4)`` + un-clamped
    ``(rows-h)//2`` math (which could place a tall box at a negative y)."""
    cols = max(0, int(cols)); rows = max(0, int(rows))
    w = min(int(want_w), max(0, cols - margin_w))
    w = min(max(w, min(min_w, cols)), cols)
    h = min(int(want_h), max(0, rows - margin_h))
    h = min(max(h, min(min_h, rows)), rows)
    x = max(0, (cols - w) // 2)
    y = max(0, (rows - h) // 2)
    return (w, h, x, y)


import re as _re
_URL_RE = _re.compile(r"https?://[^\s)>\]\"'`]+")
# file.ext:line(:col) — requires a dotted extension whose first char is a letter, so a
# bare ratio like "3:14" is NOT a link.
_PATHLINE_RE = _re.compile(r"[\w./~+-]*\.[A-Za-z][A-Za-z0-9]*:\d+(?::\d+)?")
_URL_TRAIL = ".,;:!?)]}>\"'"

# BARE file path (no :line) — conservative so prose is NEVER linkified. A token is a file link
# only when it (a) contains a "/" (a real path like core/loop.py or .syntra/plans/x.md), OR
# (b) ends in one of these known doc/code/data extensions. Everything else (e.g. "etc.", "v1.2",
# a sentence's final word + period) is left as plain text. The token must end at the extension,
# so trailing sentence punctuation ("see plan.md.") is excluded by the \b boundary.
_BARE_EXTS = ("md", "txt", "py", "js", "ts", "tsx", "jsx", "rs", "go", "java", "rb", "c", "h",
              "cpp", "cc", "hpp", "json", "yaml", "yml", "toml", "ini", "cfg", "sh", "html",
              "css", "sql", "png", "jpg", "jpeg", "gif", "webp", "pdf", "csv", "log", "lock")
_BARE_PATH_RE = _re.compile(
    r"(?<![\w/.~+-])"                                  # left boundary: not mid-token
    r"(?:[\w.~+-]+/)*[\w.~+-]+\." + f"(?:{'|'.join(_BARE_EXTS)})" + r"\b")


def find_links(text: str) -> list:
    """Find clickable spans in a transcript line: URLs, ``file.ext:line(:col)`` refs, and bare
    ``file.ext`` / ``dir/file.ext`` paths (known extensions or a slash — never plain prose).
    Returns sorted ``(start, end, target)`` (target = the matched text; an opener treats '://'
    as a URL, else a file). Pure -> unit-tested (M7)."""
    text = text or ""
    spans: list = []
    for m in _URL_RE.finditer(text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1] in _URL_TRAIL:   # trim trailing sentence punctuation
            e -= 1
        spans.append((s, e, text[s:e]))
    for m in _PATHLINE_RE.finditer(text):
        if any(s <= m.start() < e for s, e, _t in spans):   # don't double-count inside a URL
            continue
        spans.append((m.start(), m.end(), m.group(0)))
    # bare file paths — only where they don't overlap a URL or a file.ext:line span already found
    for m in _BARE_PATH_RE.finditer(text):
        ms, me = m.start(), m.end()
        tok = m.group(0)
        # accept only path-ish tokens: a slash OR a clean name.ext (the regex already enforces a
        # known ext, so this mainly guards against weird multi-dot prose like "e.g.md" — require
        # the part before the final dot to be non-empty and not purely punctuation).
        if "/" not in tok:
            stem = tok.rsplit(".", 1)[0]
            if not stem or not _re.search(r"[A-Za-z0-9]", stem):
                continue
        if any(s < me and ms < e for s, e, _t in spans):    # overlaps an existing span → skip
            continue
        spans.append((ms, me, tok))
    spans.sort()
    return spans


def link_at(text: str, col: int) -> str | None:
    """The link target whose span covers display column ``col``, or None. Pure."""
    for s, e, t in find_links(text):
        if s <= col < e:
            return t
    return None


def extract_code_blocks(messages) -> list:
    """All fenced code blocks across the transcript, in order, as raw content strings
    (the lines between ``` fences). 1-indexed for display; powers ``/copy <n>`` and
    ``/blocks``. An unterminated trailing fence (still streaming) is still captured.
    Pure -> unit-tested."""
    blocks: list = []
    for m in messages:
        text = getattr(m, "text", "") or ""
        in_code = False
        cur: list = []
        for ln in text.split("\n"):
            if ln.strip().startswith("```"):
                if in_code:
                    blocks.append("\n".join(cur))
                    cur = []
                in_code = not in_code
                continue
            if in_code:
                cur.append(ln)
        if in_code and cur:                       # unterminated fence at message end
            blocks.append("\n".join(cur))
    return blocks


def word_diff_spans(before: str, after: str):
    """Char-level changed-run ranges between two strings, for WORD-LEVEL diff
    highlighting: returns ``(del_ranges, add_ranges)`` of ``(start, end)`` over
    `before` / `after`, marking only the chars that differ (so an edit of one word
    highlights that word, not the whole line). Pure -> unit-tested."""
    import difflib
    sm = difflib.SequenceMatcher(None, before or "", after or "", autojunk=False)
    dels, adds = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            dels.append((i1, i2))
        if tag in ("replace", "insert") and j2 > j1:
            adds.append((j1, j2))
    return dels, adds


def osc9_notify(message: str) -> str:
    """Build an OSC 9 desktop-notification escape sequence (``ESC ] 9 ; <msg> BEL``) —
    iTerm2/kitty/WezTerm and others raise a real OS notification, a stronger 'your long
    run finished' cue than the terminal bell. Strips control chars (incl. BEL/ESC/newline)
    so an embedded sequence can't terminate early or inject another OSC. Pure -> tested."""
    msg = "".join(c for c in (message or "") if c >= " " and c != "\x7f")
    return f"\x1b]9;{msg}\x07"


# OSC 9;4 progress states (ConEmu / Windows Terminal / Ghostty / iTerm taskbar + tab).
_OSC94_STATE = {"clear": 0, "running": 1, "error": 2, "indeterminate": 3, "paused": 4}


def osc9_4_progress(state: str, percent: int = 0) -> str:
    """Build an OSC 9;4 taskbar/tab progress escape (``ESC ] 9 ; 4 ; <state> ; <pct> BEL``) —
    a cheap AMBIENT 'agent working / done / failed' signal OUTSIDE the TUI (the terminal's
    own taskbar button or tab fills/colors). #178.

    ``state``: ``running`` (normal fill), ``error`` (red), ``indeterminate`` (pulsing, no
    percent), ``paused``, or ``clear`` (state 0 = remove the indicator). ``percent`` is
    clamped 0..100. Version/terminal-gating is the caller's job (emit only where supported);
    this just builds the bytes. Pure -> unit-tested."""
    st = _OSC94_STATE.get(str(state).lower(), 1)
    pct = max(0, min(100, int(percent)))
    return f"\x1b]9;4;{st};{pct}\x07"


def stall_fade(idle_seconds: float, *, grace: float = 3.0, ramp: float = 2.0) -> float:
    """#233: how 'stuck' the working line should LOOK, as a 0.0→1.0 fade factor.

    A run with no new tokens and no active tool for `grace` seconds starts easing the
    spinner/verb toward red over `ramp` seconds; it saturates at 1.0 (fully red) and resets
    to 0.0 the instant new tokens arrive (the caller passes the fresh idle time). Pure so the
    timing curve is unit-tested; the curses layer maps the factor to a color."""
    t = float(idle_seconds) - float(grace)
    if t <= 0:
        return 0.0
    if ramp <= 0:
        return 1.0
    return max(0.0, min(1.0, t / ramp))


def stall_fade_style(idle_seconds: float, *, threshold: float = 0.5, **kw) -> str:
    """The theme role for the working line given how long it's been idle (#233): normal
    ``accent`` until the fade passes `threshold`, then ``error`` (red) to flag 'looks
    stuck'. Uses `stall_fade` so the grace/ramp windows stay in one place."""
    return "error" if stall_fade(idle_seconds, **kw) >= threshold else "accent"


# Density ramp for the activity heatmap: a faint dot for an EMPTY day (so the strip reads
# as a continuous calendar, not a row with holes) then a light→dark block ramp for busier
# days. Sequential single-hue by design (dataviz: magnitude = one hue light→dark, never a
# rainbow); the day's COLOR lane stays constant, the GLYPH carries the magnitude.
_HEATMAP_GLYPHS = "·░▒▓█"   # 5 levels: empty → full (index 0..4)


def usage_stats(entries, *, now: float, days: int = 30) -> dict:
    """#218: aggregate the spend ledger into a cross-session usage summary.

    `entries` is the ledger list (`{ts, task_id, usd}`); `now` is the reference time (passed
    in so this stays pure/deterministic). Buckets by whole-day offset from `now` (offset 0 =
    the last 24h), and computes per-day cost+runs, active-day count, current + longest
    contiguous streak, and a heatmap (list of `(day_offset, level)` for offsets 0..days-1,
    newest first — level 0..4 scaled to the busiest day). Pure → unit-tested."""
    days = max(1, int(days))
    per_day_usd: dict[int, float] = {}
    per_day_runs: dict[int, int] = {}
    total_usd = 0.0
    total_runs = 0
    for e in entries or []:
        try:
            ts = float(e.get("ts", 0) or 0)
            usd = float(e.get("usd", 0) or 0)
        except Exception:  # noqa: BLE001
            continue
        off = int((now - ts) // 86400.0)
        if off < 0:
            off = 0
        total_usd += usd
        total_runs += 1
        if off < days:
            per_day_usd[off] = per_day_usd.get(off, 0.0) + usd
            per_day_runs[off] = per_day_runs.get(off, 0) + 1

    active = sorted(per_day_runs.keys())
    active_days = len(active)

    # current streak: consecutive day-offsets 0,1,2,… with activity
    current = 0
    while current in per_day_runs:
        current += 1

    # longest streak: longest run of consecutive active offsets
    longest = 0
    run = 0
    prev = None
    for off in active:
        if prev is None or off == prev + 1:
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        prev = off

    busiest = max(per_day_runs.values(), default=0)
    heatmap = []
    for off in range(days):
        r = per_day_runs.get(off, 0)
        level = 0 if r == 0 else min(4, 1 + int(3 * (r - 1) / max(1, busiest - 1))) if busiest > 1 else (4 if r else 0)
        heatmap.append((off, level))

    return {
        "total_usd": round(total_usd, 6),
        "total_runs": total_runs,
        "today_usd": round(per_day_usd.get(0, 0.0), 6),
        "today_runs": per_day_runs.get(0, 0),
        "active_days": active_days,
        "current_streak": current,
        "longest_streak": longest,
        "window_days": days,
        "per_day_usd": per_day_usd,
        "per_day_runs": per_day_runs,
        "heatmap": heatmap,
    }


def render_heatmap(grid, *, width: int = 40) -> list[str]:
    """Render a usage heatmap (`[(day_offset, level)]`, newest-first) as rows of block
    glyphs, oldest→newest left-to-right so it reads like a calendar strip. Wraps to `width`
    columns. Level 0..4 → ` ░▒▓█`. Pure."""
    width = max(1, int(width))
    if not grid:
        return []
    # oldest on the left: reverse the newest-first offsets
    cells = [_HEATMAP_GLYPHS[max(0, min(4, lvl))] for _off, lvl in sorted(grid, reverse=True)]
    rows: list[str] = ["".join(cells[i:i + width]) for i in range(0, len(cells), width)]
    return rows


_RG_LINE_RE = _re.compile(r"^(?:\./)?(.+?):(\d+):(.*)$")


def parse_rg_lines(output: str):
    """#217: parse ripgrep/grep ``path:line:text`` output into ``[(path, line_no, text)]``.

    Strips a leading ``./``, splits ONLY on the ``:<digits>:`` match coordinate (so colons in
    the matched text — URLs, Windows drives — stay intact), left-trims the text, and skips any
    line that isn't a match coordinate. Pure → unit-tested."""
    rows = []
    for ln in (output or "").splitlines():
        m = _RG_LINE_RE.match(ln)
        if not m:
            continue
        path, num, text = m.group(1), m.group(2), m.group(3)
        try:
            rows.append((path, int(num), text.strip()))
        except ValueError:
            continue
    return rows


def search_mention(result) -> str:
    """#217: format a search result ``(path, line, text)`` as an ``@path#Lnn`` mention to
    insert into the input (line 0/absent → a bare ``@path``). Pure."""
    path = str(result[0]) if result else ""
    line = int(result[1]) if result and len(result) > 1 else 0
    if not path:
        return ""
    return f"@{path}#L{line}" if line > 0 else f"@{path}"


def search_result_label(result, width: int = 72) -> str:
    """#217: a one-line picker label for a search hit — ``path:line  text``, start-truncated
    on the PATH (keep the filename+line visible) and end-clipped on the text, fit to `width`.
    Pure → unit-tested."""
    width = max(10, int(width))
    path = str(result[0]) if result else ""
    line = int(result[1]) if result and len(result) > 1 else 0
    text = str(result[2]) if result and len(result) > 2 else ""
    head = f"{path}:{line}" if line > 0 else path
    # reserve ~55% for the location, the rest for the matched text
    loc_w = max(8, int(width * 0.55))
    head = truncate_start(head, loc_w) if display_width(head) > loc_w else head
    rest = width - display_width(head) - 2
    if rest > 2 and text:
        return f"{head}  {fit_to_width(text, rest)}"
    return fit_to_width(head, width)


_EIGHTHS = " ▏▎▍▌▋▊▉█"   # 0..8 eighths of a full cell (index = eighths filled)


def progress_bar(frac: float, width: int = 20) -> str:
    """#236: a fractional progress bar with EIGHTH-BLOCK sub-cell resolution — the last
    partial cell renders as one of ``▏▎▍▌▋▊▉`` so the bar shows finer progress than whole
    cells (context %, effort gauge, download, etc.). Always exactly `width` display cells.
    `frac` is clamped to 0..1. Pure → unit-tested."""
    width = max(1, int(width))
    frac = max(0.0, min(1.0, float(frac)))
    total_eighths = int(round(frac * width * 8))
    full = total_eighths // 8
    rem = total_eighths % 8
    out = "█" * min(full, width)
    if full < width and rem:
        out += _EIGHTHS[rem]
    return (out + " " * (width - len(out)))[:width]


def list_edge_arrows(*, scroll: int, total: int, height: int) -> tuple[str, str]:
    """#236: the reusable "↑ N more / ↓ N more" edge markers for a windowed list. Returns
    ``(above, below)`` strings ("" when there's nothing off-screen in that direction) so any
    scrollable list can show how much is hidden. Pure → unit-tested."""
    total = max(0, int(total))
    height = max(1, int(height))
    scroll = max(0, min(int(scroll), max(0, total - height)))
    above = scroll
    below = max(0, total - height - scroll)
    up = f"↑ {above} more above" if above > 0 else ""
    down = f"↓ {below} more below" if below > 0 else ""
    return (up, down)


def sparkline(values) -> str:
    """A one-line unicode sparkline for a numeric series (tokens/day, cost/day). Each value
    → one of ▁▂▃▄▅▆▇█ scaled to the max. Empty series → "". Flat series → a mid glyph.
    Pure → unit-tested."""
    vals = [float(v) for v in (values or [])]
    if not vals:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return bars[3] * len(vals)          # flat → a steady mid bar
    span = hi - lo
    return "".join(bars[min(len(bars) - 1, int((v - lo) / span * (len(bars) - 1)))] for v in vals)


def context_breakdown(sources, *, window: int, droppable=None, file_reads=None) -> dict:
    """#220: attribute the context window to its sources + produce ranked reduction advice.

    `sources` = `[(label, tokens)]` (the TUI computes each from the real transcript/memory/
    system text). `window` is the model's context size. `droppable` names sources the user
    can shed (files, old history…). `file_reads` is the list of paths read this session — a
    path read >1× is flagged as wasted duplicate context. Returns used/pct + sources sorted
    biggest-first (each `(label, tokens, pct_of_window)`) + human `advice` strings. Pure."""
    window = max(1, int(window))
    droppable = set(droppable or ())
    src = [(str(l), max(0, int(t))) for l, t in (sources or []) if int(t) > 0]
    used = sum(t for _l, t in src)
    src_sorted = sorted(src, key=lambda lt: lt[1], reverse=True)
    out_src = [(l, t, round(100.0 * t / window, 1)) for l, t in src_sorted]
    pct = min(100, int(round(100.0 * used / window))) if used else 0

    advice: list[str] = []
    # 1) biggest droppable source → concrete token saving
    for l, t in src_sorted:
        if l in droppable and t > 0:
            advice.append(f"drop {l} → save ~{abbrev_count(t)} tokens ({round(100.0*t/window)}% of the window)")
            break
    # 2) duplicate file reads waste context
    if file_reads:
        counts: dict[str, int] = {}
        for p in file_reads:
            counts[str(p)] = counts.get(str(p), 0) + 1
        dups = sorted(((p, n) for p, n in counts.items() if n > 1), key=lambda pn: pn[1], reverse=True)
        for p, n in dups[:3]:
            advice.append(f"{p} was read {n}× — a duplicate file read; re-reading wastes context")
    # 3) near-full nudge
    if pct >= 80:
        advice.append(f"window {pct}% full — /compact soon to keep responses sharp")
    return {
        "used": used,
        "window": window,
        "pct": pct,
        "sources": out_src,
        "advice": advice,
    }


def render_context_dashboard(bd: dict, width: int = 72) -> list:
    """#220: compose the /context dashboard as styled rows ``[(text, theme_role)]``.

    Design (dataviz): occupancy is a proportion bar (parts-of-a-whole), each source gets a
    right-sized mini-bar + token count + %-of-window (magnitude), and reduction advice is a
    ranked list. Color = hierarchy (accent header, dim captions, number values, a fill ramp).
    Pre-fit to `width`; wrapped in make_styled_overlay by the TUI. Pure → unit-tested."""
    width = max(24, int(width))
    rows: list = []
    used, window, pct = bd.get("used", 0), bd.get("window", 1), bd.get("pct", 0)
    rows.append(("CONTEXT WINDOW", "accent"))
    rows.append(("", "default"))

    # occupancy bar — eighth-block sub-cell resolution (#236 progress_bar) so a small
    # occupancy still shows a visible sliver rather than snapping to empty whole cells.
    barw = min(width - 8, 40)
    bar = progress_bar(pct / 100.0, barw)
    bar_style = "error" if pct >= 90 else ("number" if pct >= 70 else "diff_add")
    rows.append((f"  {bar} {pct}%", bar_style))
    rows.append((f"  {used:,} / {window:,} tokens", "dim"))
    rows.append(("", "default"))

    # per-source attribution: label · mini-bar · tokens · %
    sources = bd.get("sources", [])
    if sources:
        rows.append(("BY SOURCE", "accent"))
        top = max((t for _l, t, _p in sources), default=1) or 1
        label_w = min(14, max((len(l) for l, _t, _p in sources), default=6))
        mini_w = max(6, min(18, width - label_w - 20))
        for label, tok, pw in sources:
            fill = int(mini_w * tok / top)
            mb = "▬" * fill + " " * (mini_w - fill)
            line = f"  {fit_to_width(label, label_w).ljust(label_w)} {mb} {abbrev_count(tok):>5} {pw:>4.0f}%"
            rows.append((fit_to_width(line, width), "number"))
        rows.append(("", "default"))

    # ranked reduction advice
    advice = bd.get("advice", [])
    if advice:
        rows.append(("REDUCE", "accent"))
        rows.extend((fit_to_width("  → " + a, width), "dim") for a in advice)
    else:
        rows.append(("  ✓ context is lean — nothing to trim", "diff_add"))
    return rows


def _stat_tiles(pairs, width: int) -> list:
    """Lay headline stat tiles across `width` as two stacked rows — a big VALUE line over a
    dim LABEL line — evenly spaced (KPI row). `pairs` = [(label, value_str)]. Returns
    [(text, style)] (value row = 'number', label row = 'dim'). Pure."""
    n = max(1, len(pairs))
    col = max(8, width // n)
    val_row, lbl_row = [], []
    for label, value in pairs:
        v = fit_to_width(str(value), col - 1)
        l = fit_to_width(str(label).upper(), col - 1)
        val_row.append(v.ljust(col))
        lbl_row.append(l.ljust(col))
    return [("".join(val_row).rstrip(), "number"),
            ("".join(lbl_row).rstrip(), "dim")]


def render_stats_dashboard(stats: dict, width: int = 72) -> list:
    """#218: compose the /stats usage dashboard as styled rows ``[(text, theme_role)]``.

    Design (dataviz method): headline numbers are STAT TILES (not a chart); activity over
    time is a density HEATMAP calendar strip with a light→dark ramp + a `less▁▁▁more` LEGEND
    (so the ramp is never color-alone); the cost trend is a single-hue SPARKLINE. Color is
    hierarchy: `accent` section headers, `dim` captions, `number` values, `diff_add` for the
    activity ramp/streak. Everything is pre-fit to `width` so the popup never overflows.
    Pure → unit-tested; the TUI wraps it in make_styled_overlay."""
    width = max(24, int(width))
    rows: list = []

    if not stats or stats.get("total_runs", 0) == 0:
        rows.append(("  No usage recorded yet.", "dim"))
        rows.append(("  Run a few tasks — your activity, streaks and cost land here.", "dim"))
        return rows

    days = stats.get("window_days", 30)
    # ── headline stat tiles ──
    rows.append((f"USAGE · last {days} days", "accent"))
    rows.append(("", "default"))
    rows.extend(_stat_tiles([
        ("runs", str(stats.get("total_runs", 0))),
        ("spent", f"${stats.get('total_usd', 0.0):.2f}"),
        ("active days", str(stats.get("active_days", 0))),
        ("streak", f"{stats.get('current_streak', 0)}d"),
    ], width))
    rows.append(("", "default"))

    # ── activity heatmap (calendar strip, oldest→newest) ──
    rows.append(("ACTIVITY", "accent"))
    hm = stats.get("heatmap", [])
    # single-hue density (green = activity)
    rows.extend(("  " + line, "diff_add") for line in render_heatmap(hm, width=width - 2))
    # legend so the ramp reads without relying on color alone
    rows.append(("  less " + _HEATMAP_GLYPHS + " more", "dim"))
    rows.append(("", "default"))

    # ── cost trend sparkline (oldest→newest) ──
    per_day = stats.get("per_day_usd", {}) or {}
    if per_day:
        series = [per_day.get(off, 0.0) for off in range(days - 1, -1, -1)]
        spark = sparkline(series)
        rows.append(("COST / DAY", "accent"))
        rows.append(("  " + fit_to_width(spark, width - 2), "number"))
        rows.append((f"  peak ${max(series):.2f} · today ${stats.get('today_usd', 0.0):.2f}", "dim"))
        rows.append(("", "default"))

    # ── streak footer ──
    rows.append((f"  🔥 current {stats.get('current_streak', 0)}-day streak"
                 f"  ·  best {stats.get('longest_streak', 0)} days", "diff_add"))
    return rows


class TurnDiffHistory:
    """#221: a browsable history of per-turn diffs. Each finished turn's coherent diff (from
    TurnDiffCapture / the turn_diff event) is recorded; the user steps ◀ older / newer ▶ to
    review what changed on any past turn (`/diff` shows the latest; this shows all). Pure →
    unit-tested; the TUI renders `render()` in a styled overlay and maps ←/→ to prev/next."""

    def __init__(self, limit: int = 50) -> None:
        self._turns: list[dict] = []     # [{summary, lines}] oldest→newest
        self._cur = 0                    # index into _turns; points at the current view
        self._limit = max(1, int(limit))

    def record(self, summary: str, lines) -> None:
        """Append a finished turn's diff. A turn that changed nothing (no lines) is skipped.
        Recording jumps the view to the newest turn (what you just did)."""
        lines = [str(x) for x in (lines or [])]
        if not lines:
            return
        self._turns.append({"summary": str(summary or ""), "lines": lines})
        if len(self._turns) > self._limit:
            self._turns = self._turns[-self._limit:]
        self._cur = len(self._turns) - 1

    def count(self) -> int:
        return len(self._turns)

    def current(self):
        if not self._turns:
            return None
        self._cur = max(0, min(self._cur, len(self._turns) - 1))
        return self._turns[self._cur]

    def label(self) -> str:
        if not self._turns:
            return "no turns"
        return f"turn {self._cur + 1}/{len(self._turns)}"

    def prev(self) -> None:
        """Step to the OLDER turn (clamped)."""
        if self._turns:
            self._cur = max(0, self._cur - 1)

    def next(self) -> None:
        """Step to the NEWER turn (clamped)."""
        if self._turns:
            self._cur = min(len(self._turns) - 1, self._cur + 1)

    def render(self, width: int = 72) -> list:
        """Render the current turn's diff with a ◀ turn N/M ▶ nav header, colored +/-/@@.
        Styled rows [(text, role)], fit to width. Pure."""
        width = max(24, int(width))
        rows: list = []
        cur = self.current()
        if cur is None:
            rows.append(("  No turn diffs yet — run a task that edits files.", "dim"))
            return rows
        rows.append((f"◀  {self.label()}  ▶   ·   ←/→ step · Esc close", "accent"))
        if cur.get("summary"):
            rows.append((fit_to_width("  " + cur["summary"], width), "dim"))
        rows.append(("", "default"))
        for ln in cur.get("lines", []):
            s = ln.lstrip()
            if s.startswith("+") and not s.startswith("+++"):
                style = "diff_add"
            elif s.startswith("-") and not s.startswith("---"):
                style = "diff_del"
            elif s.startswith("@@"):
                style = "diff_hunk"
            else:
                style = "default"
            rows.append((fit_to_width(ln, width), style))
        return rows


class TurnDiffCapture:
    """#141: reconstruct the whole-TURN diff from the feed the engine renders.

    The engine emits a `turn_diff` event which the shared progress bridge renders as a
    marker line ``⎿ changes: <summary>`` followed by the unified-diff body lines and a
    trailing ``… +N more diff lines — /diff …`` hint. The cockpit doesn't get the raw
    payload (the bridge lives in the CLI layer), so we recover the coherent diff by
    watching those feed lines: open a capture on the marker, append real diff lines
    (``+``/``-``/``@``/``diff ``), and close on the first non-diff line. The result powers
    a `/diff` that shows THIS TURN's changes (not the whole git tree) + a card. Pure →
    unit-tested; the drain just forwards each ``(role, line)`` and reads `summary`/`lines`."""

    _MARKER = "⎿ changes:"

    def __init__(self) -> None:
        self.summary: str = ""
        self.lines: list[str] = []
        self._capturing = False

    def feed(self, role: str, line: str) -> bool:
        """Process one feed item. Returns True while a turn-diff is being captured (so the
        caller knows the line was consumed as diff content)."""
        if role != "tool":
            self._capturing = False
            return False
        if self._MARKER in line:
            self.summary = line.split(self._MARKER, 1)[1].strip()
            self.lines = []
            self._capturing = True
            return True
        if self._capturing:
            s = line.lstrip()
            if s.startswith("… +") or s.startswith("…+"):   # the "N more lines" hint — skip
                return True
            # a unified-diff body line: +add / -del / @@hunk / "diff " header / a CONTEXT
            # line (starts with a literal space in col 0). The feed indents lines slightly,
            # so accept a hunk char after optional leading spaces, and treat a still-nonempty
            # lstripped line during capture as context (keeps multi-hunk diffs whole).
            if s[:1] in ("+", "-", "@") or s.startswith("diff ") or s == "" or line[:1] == " ":
                self.lines.append(line)
                return True
            self._capturing = False          # a real non-diff line ended the body
        return False

    def has_diff(self) -> bool:
        return bool(self.lines)

    def text(self) -> str:
        return "\n".join(self.lines)

    def stats(self) -> tuple[int, int]:
        """(added, removed) line counts, ignoring the +++/--- file headers."""
        added = sum(1 for l in self.lines
                    if l.lstrip().startswith("+") and not l.lstrip().startswith("+++"))
        removed = sum(1 for l in self.lines
                      if l.lstrip().startswith("-") and not l.lstrip().startswith("---"))
        return (added, removed)

    def reset(self) -> None:
        self.summary = ""
        self.lines = []
        self._capturing = False


class HeightRatchet:
    """#230a: a grow-only minimum-height lock for a live UI region.

    A live zone (the action-feed + working line above the input) changes height frame to
    frame as tools come and go — each shrink then re-grow reflows the transcript and makes
    the spinner→result transition JITTER. This holds the region at its high-water mark
    WHILE a run is active, so the reserved space only ever grows during the run; once the
    run goes idle the reserve is released (the next run starts fresh). Pure → unit-tested;
    the caller reserves `height(desired, active=...)` rows instead of `desired` directly."""

    def __init__(self) -> None:
        self._hi = 0

    def height(self, desired: int, *, active: bool) -> int:
        d = max(0, int(desired))
        if not active:
            self._hi = 0        # idle → collapse; next run doesn't inherit the mark
            return d
        self._hi = max(self._hi, d)
        return self._hi

    def reset(self) -> None:
        self._hi = 0


def _osc_safe(s: str) -> str:
    """Strip control chars (BEL/ESC/newline/DEL) so an embedded byte can't terminate an
    OSC string early or inject a second escape (same guard as `osc9_notify`)."""
    return "".join(c for c in (s or "") if c >= " " and c != "\x7f")


def sanitize_terminal_title(title: str, *, limit: int = 256) -> str:
    """#250(a): make a string safe to write into an OSC-0 window-title sequence
    (``ESC ] 0 ; <title> BEL``). The session title can be MODEL-DERIVED (the analyzer names
    the session from the goal), so a prompt-injected title could otherwise embed a BEL/ESC
    and break out of the OSC to inject arbitrary terminal control codes. Strips every control
    char (incl. BEL/ESC/newline/DEL) + caps the length. Pure → unit-tested."""
    return _osc_safe(title)[:max(0, int(limit))]


def _osc8_id(url: str) -> str:
    """A short, stable id for an OSC-8 link, hashed from the URL. A terminal uses the
    ``id=`` param to treat multiple hyperlink runs (a link wrapped across lines, or the
    same URL emitted twice) as ONE hoverable link. Deterministic (no per-run salt)."""
    import hashlib
    return hashlib.sha1((url or "").encode("utf-8", "replace")).hexdigest()[:8]


def osc8_link(text: str, url: str) -> str:
    """Wrap visible ``text`` in a REAL OSC-8 terminal hyperlink pointing at ``url``:

        ESC ] 8 ; id=<hash> ; <url> BEL   <text>   ESC ] 8 ; ; BEL

    Cmd/Ctrl-clickable in modern terminals, and it survives copy-paste (unlike Syntra's
    own mouse-region links). A URL-hashed ``id=`` keeps a wrapped link one link. Both the
    URL and the text are control-char-sanitized so a crafted link can't break the escape
    or inject another OSC. An empty URL returns the plain text (nothing to link).
    Pure -> unit-tested (#176). NOTE: emit only on a raw byte stream (inline mode / a tty
    write) — never via curses `addnstr`, which mangles embedded escapes."""
    url = _osc_safe(url).strip()
    vis = _osc_safe(text)
    if not url:
        return vis
    return f"\x1b]8;id={_osc8_id(url)};{url}\x07{vis}\x1b]8;;\x07"


def _link_target_url(target: str) -> str:
    """Turn a `find_links` target (a URL, a ``file.ext:line`` ref, or a bare path) into a
    clickable URI. Real URLs pass through; file paths become ``file://`` absolute URIs so
    the terminal opens them. ``file.ext:line(:col)`` keeps the line via the ``#L`` anchor
    many terminals honour. Best-effort — returns "" if it can't form a safe URI."""
    t = (target or "").strip()
    if not t:
        return ""
    if "://" in t:
        return t
    if t.startswith("www."):
        return "https://" + t
    # a file path, optionally file.ext:line(:col)
    import os as _os
    line = ""
    path = t
    m = _re.match(r"^(.*?):(\d+)(?::\d+)?$", t)
    if m:
        path, line = m.group(1), m.group(2)
    try:
        ap = _os.path.abspath(_os.path.expanduser(path))
    except Exception:  # noqa: BLE001
        return ""
    uri = "file://" + ap
    if line:
        uri += f"#L{line}"
    return uri


def linkify_osc8(visible: str) -> str:
    """Wrap every URL / file-path span in an ALREADY-VISIBLE line with a real OSC-8
    hyperlink. Operates on final on-screen text (post-wrap/clip) so the added escape bytes
    have zero display width and don't disturb layout. Spans come from `find_links` (the
    same detector the mouse-region handler uses), so what the terminal makes clickable
    matches what Syntra highlights. Pure -> unit-tested (#176). Emit on a raw byte stream
    only (inline mode / tty write), never through curses `addnstr`."""
    spans = find_links(visible)
    if not spans:
        return visible
    out = []
    last = 0
    for s, e, target in spans:
        if s < last:                       # defensive: skip any overlap
            continue
        out.append(visible[last:s])
        url = _link_target_url(target)
        seg = visible[s:e]
        out.append(osc8_link(seg, url) if url else seg)
        last = e
    out.append(visible[last:])
    return "".join(out)


def clamp_cursor(*, in_top: int, cur_row: int, content_x: int, cur_col: int,
                 rect_y: int, rect_h: int, content_w: int,
                 screen_rows: int, screen_cols: int) -> tuple[int, int]:
    """Clamp the hardware cursor into the chat input rect AND the physical screen.
    A shrink-resize (or a stale wide column from a previous width) could otherwise
    place ``stdscr.move`` off-screen / outside the rect — the prior code only clamped
    the upper bound. Pure -> unit-tested."""
    y = max(rect_y, min(rect_y + rect_h - 1, in_top + cur_row))
    x = max(content_x, min(content_x + content_w - 1, content_x + cur_col))
    y = max(0, min(screen_rows - 1, y))
    x = max(0, min(screen_cols - 1, x))
    return (y, x)


def abbrev_count(n: int) -> str:
    """Compact a token/byte count: 1234 -> '1.2k', 1_200_000 -> '1.2M'."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def layout_status_bar(segments: list, width: int, *, sep: str = "  ·  ",
                      gap_min: int = 1) -> list:
    """Lay out status segments into `width`, RESPONSIVELY (P-? top-bar redesign).

    Rules that make it un-buggy:
      • segments are kept in PRIORITY order (lowest number kept longest);
      • a segment is shown whole, or in its `short` form, or DROPPED — never chopped
        mid-field;
      • left-side segments pack from column 0, right-side from the right edge;
      • the result provably never exceeds `width` (a final clip guards the math).

    Returns [(col, text, style, bold)] placements. Pure -> unit-tested.
    """
    width = max(0, int(width))
    segs = [s for s in segments if (s.full or s.short)]
    if width <= 0 or not segs:
        return []
    seplen = display_width(sep)   # cell-accurate (handles wide emoji in segments)

    # greedily admit segments by priority; full form if it fits the running budget,
    # else the short form. Charging a separator per admitted segment (after the first)
    # plus one inter-block gap slightly OVER-estimates — which only ever drops a
    # borderline field, never overflows.
    chosen: list = []
    used = 0
    for seg in sorted(segs, key=lambda s: s.priority):
        for text in (seg.full, seg.short):
            if not text:
                continue
            extra = display_width(text) + (seplen if chosen else 0)
            if used + extra + (gap_min if chosen else 0) <= width:
                chosen.append((seg, text))
                used += extra
                break
    if not chosen:                       # not even the top field fits → hard clip it
        top = min(segs, key=lambda s: s.priority)
        return [(0, clip_to_width(top.short or top.full, width), top.style, top.bold)]

    order = {id(s): i for i, s in enumerate(segs)}
    left = sorted([c for c in chosen if c[0].side == "left"], key=lambda c: order[id(c[0])])
    right = sorted([c for c in chosen if c[0].side == "right"], key=lambda c: order[id(c[0])])

    placements: list = []
    x = 0
    for i, (s, t) in enumerate(left):
        if i:
            placements.append((x, sep, "dim", False)); x += seplen
        placements.append((x, t, s.style, s.bold)); x += display_width(t)

    rw = sum(display_width(t) for _, t in right) + seplen * max(0, len(right) - 1)
    rx = max(x + gap_min, width - rw)
    for i, (s, t) in enumerate(right):
        if i:
            placements.append((rx, sep, "dim", False)); rx += seplen
        placements.append((rx, t, s.style, s.bold)); rx += display_width(t)

    # final safety clip: nothing may start past the edge or run over it (in CELLS)
    out: list = []
    for col, text, style, bold in placements:
        if col >= width:
            continue
        if col + display_width(text) > width:
            text = clip_to_width(text, width - col)
        if text:
            out.append((col, text, style, bold))
    return out


# #222: signatures for the plain-English permission explainer. Deterministic + instant
# (no model call) — a rule-based read of what the tool will actually do, so the risk level is
# never a hallucination. HIGH = irreversible / exfil / privilege; MED = a write/mutation;
# LOW = a read or a search.
_PERM_HIGH_PATTERNS = (
    "rm -rf", "rm -r", "rmdir", " rm ", "mkfs", "dd ", ">/dev/", "chmod -r", "chown -r",
    "git push", "git reset --hard", "git clean", "truncate", "shred", ":(){", "sudo ",
)
_PERM_NET_PATTERNS = ("curl ", "wget ", "scp ", "sftp", "ssh ", "nc ", "netcat", "rsync ",
                      "ftp ", "telnet", "git clone", "git ls-remote", "pip install",
                      "npm install", "http://", "https://")
_PERM_READ_PATTERNS = ("cat ", "less ", "head ", "tail ", "grep ", "rg ", "find ", "ls ",
                       "wc ", "stat ", "file ", "echo ", "pwd", "git status", "git log",
                       "git diff", "which ")


def tool_safety_badges(annotations: dict) -> list:
    """#227: map an MCP tool's annotation hints to human safety badges. `readOnlyHint` →
    'read-only', `destructiveHint` → 'destructive', `openWorldHint` → 'open-world' (reaches
    the network / outside world), `idempotentHint` → 'idempotent'. Empty → []. Pure."""
    a = annotations or {}
    out = []
    if a.get("readOnlyHint"):
        out.append("read-only")
    if a.get("destructiveHint"):
        out.append("destructive")
    if a.get("openWorldHint"):
        out.append("open-world")
    if a.get("idempotentHint"):
        out.append("idempotent")
    return out


_BADGE_STYLE = {"read-only": "diff_add", "idempotent": "diff_add",
                "destructive": "error", "open-world": "number"}


def render_tool_schema(name: str, schema: dict, *, annotations: dict = None, width: int = 72) -> list:
    """#227: render an MCP tool's input JSON-schema as a param TABLE + safety badges, so a
    user can VET the tool before allowing it (strong for a security-focused product). Rows:
    a header (name + badges), then one line per property (`name  type  req  — description`).
    Styled (accent header, error/number badges, dim descriptions), fit to width. Pure."""
    width = max(24, int(width))
    schema = schema or {}
    rows: list = []
    badges = tool_safety_badges(annotations or {})
    rows.append((f"⚙ {name}", "accent"))
    if badges:
        # one badge chip line, each colored by risk
        chip = "  " + "  ".join(f"[{b}]" for b in badges)
        rows.append((fit_to_width(chip, width),
                     "error" if "destructive" in badges else
                     ("number" if "open-world" in badges else "diff_add")))
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", []) or [])
    if not props:
        rows.append(("  (no parameters)", "dim"))
        return rows
    rows.append(("", "default"))
    rows.append(("  PARAMETERS", "dim"))
    name_w = min(18, max((len(p) for p in props), default=6))
    for pname, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        ptype = str(spec.get("type", "any"))
        req = "required" if pname in required else "optional"
        desc = str(spec.get("description", "") or "")
        head = f"  {fit_to_width(pname, name_w).ljust(name_w)}  {ptype:8} {req:8}"
        if desc:
            line = f"{head} — {fit_to_width(desc, max(4, width - display_width(head) - 3))}"
        else:
            line = head
        rows.append((fit_to_width(line, width),
                     "number" if pname in required else "default"))
    return rows


def agent_slug(name: str) -> str:
    """#228: a filesystem-safe agent slug from a free-form name (lowercased, non-alnum →
    dashes, trimmed). Empty → 'agent'. Pure → unit-tested."""
    s = _re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-")
    return s or "agent"


def build_agent_markdown(*, name: str, description: str = "", tools=None,
                         system_prompt: str = "", model: str = "inherit",
                         color: str = "blue") -> str:
    """#228: serialize an agent definition into the plugin-loader's agent .md format —
    YAML-ish frontmatter (name/description/model/color/tools) + the system prompt as the
    body. Round-trips through `plugin_loader._parse_frontmatter`. The AI-authored flow feeds
    the model's synthesized fields here; the writer persists it. Pure → unit-tested."""
    tools = list(tools or [])
    # control-char-strip the free-text fields so a crafted value can't break the frontmatter.
    def _one_line(s):
        return " ".join(str(s or "").split())
    tools_str = "[" + ", ".join(f'"{_one_line(t)}"' for t in tools) + "]"
    fm = [
        "---",
        f"name: {_one_line(name)}",
        f"description: {_one_line(description)}",
        f"model: {_one_line(model) or 'inherit'}",
        f"color: {_one_line(color) or 'blue'}",
        f"tools: {tools_str}",
        "---",
    ]
    return "\n".join(fm) + "\n\n" + (system_prompt or "").strip() + "\n"


# #228: known tool names an agent may be granted (used to annotate/keep tool suggestions).
# Kept permissive — the loader tolerates unknown names (a bad tool just isn't granted), so we
# don't hard-filter, we only coerce to clean strings. This list is for the prompt's guidance.
_AGENT_TOOL_HINTS = (
    "Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch",
    "Task", "TodoWrite",
)


def agent_synthesis_prompt(description: str) -> str:
    """#228: build the instruction that asks the model to SYNTHESIZE an agent definition
    for the one-sentence `description`. We do NOT parse free prose downstream — we force a
    single JSON object matching a fixed contract, so `parse_agent_spec` can validate it.
    Pure string builder (no I/O) → unit-tested."""
    desc = " ".join(str(description or "").split())
    tools = ", ".join(_AGENT_TOOL_HINTS)
    return (
        "You are configuring a specialized sub-agent for a coding/security assistant.\n"
        f"The operator wants: {desc}\n\n"
        "Return ONLY a single JSON object (no prose, no markdown fences) with EXACTLY these keys:\n"
        '  "name"          — a short kebab-case identifier for the agent\n'
        '  "description"    — one sentence: WHEN this agent should be used\n'
        '  "tools"          — a JSON array of tool names this agent needs (subset of: '
        f"{tools})\n"
        '  "system_prompt"  — the agent\'s full system prompt (its role, method, and rules)\n\n'
        "Output the JSON object and nothing else."
    )


def _strip_control_chars(s, keep_newlines: bool = False) -> str:
    """#228: drop ASCII control chars so a crafted field can't break the frontmatter. Name /
    description want single-line-safe values (keep_newlines=False → newlines collapse too);
    the system prompt goes in the markdown BODY, which can be multi-line (keep_newlines=True →
    real line breaks survive, only other control chars are stripped)."""
    out = "".join(ch for ch in str(s or "") if ch >= " " or (keep_newlines and ch == "\n"))
    return out if keep_newlines else out.replace("\n", " ")


def parse_agent_spec(text):
    """#228: extract + validate the first JSON object from a model reply into a clean agent
    spec `{name, description, tools[list[str]], system_prompt}`, or None if unusable.

    Tolerant of ```json fences and leading/trailing prose. Requires a non-empty `name` AND a
    non-empty `system_prompt` (else None → caller falls back to the deterministic scaffold).
    All free-text fields are control-char-stripped; `tools` is coerced to a clean list of
    non-empty strings (non-strings dropped). Pure → unit-tested."""
    import json as _json
    if not text or not str(text).strip():
        return None
    raw = str(text)
    obj = None
    # 1) try the whole thing as JSON (the ideal case)
    try:
        cand = _json.loads(raw.strip())
        if isinstance(cand, dict):
            obj = cand
    except Exception:  # noqa: BLE001
        obj = None
    # 2) else scan for the first balanced {...} block and try to parse it
    if obj is None:
        start = raw.find("{")
        while start != -1 and obj is None:
            depth = 0
            for i in range(start, len(raw)):
                c = raw[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = raw[start:i + 1]
                        try:
                            cand = _json.loads(chunk)
                            if isinstance(cand, dict):
                                obj = cand
                        except Exception:  # noqa: BLE001
                            pass
                        break
            start = raw.find("{", start + 1)
    if not isinstance(obj, dict):
        return None
    # validate + clean
    name = _strip_control_chars(obj.get("name", "")).strip()
    system_prompt = _strip_control_chars(obj.get("system_prompt", ""), keep_newlines=True).strip()
    if not name or not system_prompt:
        return None
    description = _strip_control_chars(obj.get("description", "")).strip()
    tools = []
    for t in (obj.get("tools") or []):
        if isinstance(t, str):
            tt = _strip_control_chars(t).strip()
            if tt:
                tools.append(tt)
    return {"name": name, "description": description, "tools": tools,
            "system_prompt": system_prompt}


def render_agent_inspector(*, name: str, description: str = "", tools=None,
                           system_prompt: str = "", width: int = 72) -> list:
    """#228: render a rich agent inspector → styled rows. Shows the agent name, its
    when-to-use (description), the tool surface it's allowed, and its system prompt (wrapped).
    Styled (accent header, dim captions, number tool chips). Pure → unit-tested."""
    width = max(24, int(width))
    tools = list(tools or [])
    rows: list = []
    rows.append((f"◆ {name}", "accent"))
    rows.append(("", "default"))
    if description:
        rows.append(("  WHEN TO USE", "dim"))
        rows.extend(("  " + ln, "default") for ln in wrap_lines(description, width - 2))
        rows.append(("", "default"))
    rows.append(("  TOOLS", "dim"))
    if tools:
        # chip the tools onto wrapped lines
        line = "  "
        for tname in tools:
            chip = f"[{tname}] "
            if display_width(line + chip) > width:
                rows.append((line.rstrip(), "number")); line = "  "
            line += chip
        if line.strip():
            rows.append((line.rstrip(), "number"))
    else:
        rows.append(("  (inherits the default tool set)", "dim"))
    if system_prompt:
        rows.append(("", "default"))
        rows.append(("  SYSTEM PROMPT", "dim"))
        rows.extend(("  " + ln, "comment")
                    for para in system_prompt.split("\n")
                    for ln in (wrap_lines(para, width - 2) or [""]))
    return rows


def render_violation_feed(events, *, width: int = 72, limit: int = 10) -> list:
    """#226: render the last-N blocked-operation feed as styled rows. Each event is
    ``{cmd, reason}`` (a sandbox/permission denial). Newest first, capped at `limit`. Styled
    error-red with a dim reason. Pure → unit-tested; the caller feeds a subscribable store."""
    width = max(24, int(width))
    events = list(events or [])[-int(limit):][::-1]     # newest first
    rows: list = []
    rows.append(("BLOCKED OPERATIONS", "accent"))
    rows.append(("", "default"))
    if not events:
        rows.append(("  No blocked operations this session.", "dim"))
        return rows
    for e in events:
        cmd = str((e or {}).get("cmd", "") or "")
        reason = str((e or {}).get("reason", "") or "")
        rows.append((fit_to_width("  ✗ " + cmd, width), "error"))
        if reason:
            rows.append((fit_to_width("      " + reason, width), "dim"))
    return rows


def status_chip(name: str, state: str) -> tuple:
    """#226: a startup credential/MCP status chip → ``(text, theme_role)``. state ∈
    valid/connected → green, invalid/failed → red, missing/idle → dim. Pure."""
    state = (state or "").lower()
    if state in ("valid", "connected", "ok"):
        return (f"● {name}", "diff_add")
    if state in ("invalid", "failed", "error", "rejected"):
        return (f"✗ {name}", "error")
    return (f"○ {name}", "dim")


def render_mcp_screen(*, servers, live: dict = None, width: int = 72) -> list:
    """#227: render the MCP management screen — configured servers with live status. Each
    server row shows its spec (start-truncated), a connected/idle chip, and a live tool count
    when a client is connected. `live` maps a server spec → `{connected, tools:[…]}`. Styled
    (accent header, diff_add=connected, dim=idle), fit to width. Pure → unit-tested."""
    width = max(24, int(width))
    servers = list(servers or [])
    live = live or {}
    rows: list = []
    rows.append(("MCP SERVERS", "accent"))
    rows.append(("", "default"))
    if not servers:
        rows.append(("  No MCP servers configured.", "dim"))
        rows.append(("  Add one with  /mcp add <command-or-url>", "dim"))
        return rows
    for spec in servers:
        info = live.get(spec) or {}
        connected = bool(info.get("connected"))
        n_tools = len(info.get("tools", []) or [])
        chip = "● connected" if connected else "○ idle"
        tail = f"  · {n_tools} tools" if connected else ""
        specw = max(10, width - display_width(chip) - display_width(tail) - 4)
        label = truncate_start(str(spec), specw) if display_width(str(spec)) > specw else str(spec)
        rows.append((fit_to_width(f"  {label}   {chip}{tail}", width),
                     "diff_add" if connected else "dim"))
    rows.append(("", "default"))
    rows.append(("  ↑↓ select · Enter inspect a server's tools · /mcp add|remove", "dim"))
    return rows


def rule_to_english(rule: str) -> str:
    """#223: turn a permission grant KEY into a plain-English sentence. Keys look like
    ``bash`` (any use of the tool) or ``bash:python`` (a scoped sub-key, e.g. the `python`
    interpreter / a command prefix). Pure → unit-tested."""
    rule = str(rule or "").strip()
    if not rule:
        return ""
    if ":" in rule:
        tool, sub = rule.split(":", 1)
        return f"{tool} commands scoped to “{sub}”"
    return f"Any use of the {rule} tool"


def _rule_shadows(broad: str, specific: str) -> bool:
    """True if grant `broad` already covers `specific` (so `specific` is redundant). A bare
    tool key (`bash`) shadows any scoped key of the same tool (`bash:ls`)."""
    if broad == specific:
        return False
    return ":" not in broad and (specific == broad or specific.startswith(broad + ":"))


def render_rule_board(*, always, session, off, width: int = 72) -> list:
    """#223: render the permission-rule board as styled rows ``[(text, theme_role)]``.

    Three sections — ALLOW (durable) / ALLOW (this session) / OFF or DENIED — each rule shown
    with its plain-English meaning. A SESSION rule already covered by an ALWAYS rule is a
    SHADOWED (redundant) rule and is flagged. Read-only view of the grants Part 1's
    PermissionStore exposes (`always_allowed()`, `granted_for_session()`, `tool_is_off`).
    Pure → unit-tested; the TUI wraps it in make_styled_overlay."""
    width = max(24, int(width))
    always = sorted(always or ())
    session = sorted(session or ())
    off = sorted(off or ())
    rows: list = []

    if not (always or session or off):
        rows.append(("  No permission rules yet.", "dim"))
        rows.append(("  Approve a tool with “always/session”, or turn one off, and it lands here.", "dim"))
        return rows

    def _section(title, keys, style, note_fn=None):
        rows.append((title, "accent"))
        if not keys:
            rows.append(("  (none)", "dim"))
        for k in keys:
            line = fit_to_width(f"  • {k}", max(10, width // 2))
            eng = rule_to_english(k)
            rows.append((fit_to_width(f"{line}   {eng}", width), style))
            if note_fn:
                note = note_fn(k)
                if note:
                    rows.append((fit_to_width("      ⚠ " + note, width), "number"))
        rows.append(("", "default"))

    _section("ALLOW · durable (across sessions)", always, "diff_add")

    def _shadow_note(k):
        for b in always:
            if _rule_shadows(b, k):
                return f"shadowed by durable “{b}” — this session rule is redundant"
        return ""
    _section("ALLOW · this session", session, "number", note_fn=_shadow_note)

    _section("OFF / DENIED", off, "error")
    # trailing rule if we added a blank last
    if rows and rows[-1][0] == "":
        rows.pop()
    return rows


def explain_permission(name: str, danger, args: dict) -> dict:
    """#222: a plain-English what/why/risk for a permission prompt. Returns
    ``{"what", "why", "risk"}`` where risk ∈ {LOW, MED, HIGH}. Rule-based (deterministic,
    instant) so the label can't be a hallucination. Pure → unit-tested.

    - bash: reads the command → delete/network/mutation/read heuristics.
    - write/edit: names the file being changed (a mutation → MED, or HIGH under a repo path).
    - websearch/read-only tools: LOW.
    """
    name = str(name or "tool")
    args = args or {}
    cmd = str(args.get("command", "") or "").strip()
    low = cmd.lower()
    danger = (danger or "").lower() if isinstance(danger, str) else ""

    if name == "bash":
        if any(p in low for p in _PERM_HIGH_PATTERNS):
            verb = "delete files / make an irreversible change" if ("rm" in low or "shred" in low
                    or "truncate" in low or "clean" in low or "reset --hard" in low) else "run a privileged / destructive command"
            return {"what": verb + (f": {cmd[:60]}" if cmd else ""),
                    "why": "this cannot be undone — review the exact target before allowing",
                    "risk": "HIGH"}
        if any(p in low for p in _PERM_NET_PATTERNS):
            return {"what": f"reach the network: {cmd[:60]}" if cmd else "reach the network",
                    "why": "network access can send your data out or pull code in — an exfil/supply-chain surface",
                    "risk": "HIGH"}
        if any(low.startswith(p.strip()) or p in low for p in _PERM_READ_PATTERNS):
            return {"what": f"read/inspect: {cmd[:60]}" if cmd else "read files",
                    "why": "read-only — it looks at files but changes nothing",
                    "risk": "LOW"}
        return {"what": f"run a shell command: {cmd[:60]}" if cmd else "run a shell command",
                "why": "a general command — check what it does before allowing",
                "risk": "MED" if not danger else "HIGH"}
    if name in ("write", "edit"):
        path = str(args.get("path", "") or "")
        return {"what": f"modify {path}" if path else "modify a file",
                "why": "writes change files on disk — reversible via undo/checkpoints, but review the change",
                "risk": "MED"}
    if name == "websearch":
        return {"what": f"search the web for: {args.get('query', '')[:50]}",
                "why": "sends the query to a search provider; no local change",
                "risk": "LOW"}
    # unknown tool: fall back to its danger tag
    risk = {"destructive": "HIGH", "exec": "HIGH", "write": "MED", "safe": "LOW"}.get(danger, "MED")
    return {"what": f"run {name}", "why": "review before allowing", "risk": risk}


_PERM_RISK_STYLE = {"HIGH": "error", "MED": "number", "LOW": "diff_add"}


def permission_box(req: dict, width: int, reason=None, *, show_explain: bool = False) -> list:
    """Tool-permission prompt CONTENT lines (the caller draws the border box).

    req = {"name", "danger", "detail", "explain"?}. Shows what wants to run + the once/session/
    reject/auto choices. Pure -> unit-tested (A5).

    reason: when not None, the user chose [6] deny+tell and is TYPING a reason for the agent —
    render an input line (with a caret) instead of the action buttons, so the guidance capture
    is visible. Pure; the caller feeds keystrokes into the string.

    show_explain (#222): when True and `req["explain"]` is present ({what,why,risk}), render a
    plain-English risk explainer block (LOW/MED/HIGH color-coded) under the command. LAZY by
    default (hidden) — the prompt always advertises the Ctrl+E toggle so it's discoverable.
    """
    inner = max(8, int(width))
    name = str((req or {}).get("name", "tool"))
    danger = (req or {}).get("danger")
    dlabel = f" · {danger}" if isinstance(danger, str) and danger else ""
    out: list = [((f"⚠ {name}{dlabel} wants to run" if danger else f"{name} wants to run"),
                  "error" if danger else "accent")]
    detail = str((req or {}).get("detail", "")).strip()
    if detail:
        out.append(("", "default"))
        # The command/detail is WHAT you're approving — it must be clearly READABLE, not a dim
        # near-invisible grey (user: "the command is very dark, barely visible → make it white").
        # Render it bright ("user" = the bright fg the input/user text uses), with a ❯ lead on the
        # first wrapped line so it reads as the command. Continuation lines indent under it.
        _cw = max(1, inner - 4)
        # F8: split on newlines FIRST (write/edit detail embeds "\n" for content/old→new preview),
        # then width-wrap each logical line — otherwise a literal "\n" inside a slice makes addnstr
        # break the row and corrupts the modal frame.
        _wrapped = []
        for _line in detail.split("\n"):
            if _line == "":
                _wrapped.append("")
            else:
                _wrapped.extend(_line[i:i + _cw] for i in range(0, len(_line), _cw))
        _wrapped = _wrapped or [detail]
        out.append(("  ❯ " + _wrapped[0], "user"))
        out.extend(("    " + _c, "user") for _c in _wrapped[1:])
    # #222: the plain-English risk explainer, shown only when toggled on (lazy). The Ctrl+E
    # hint is always offered below so the user knows it's there.
    _explain = (req or {}).get("explain") if reason is None else None
    if show_explain and isinstance(_explain, dict):
        _risk = str(_explain.get("risk", "MED")).upper()
        out.append(("", "default"))
        out.append((f"  risk: {_risk}", _PERM_RISK_STYLE.get(_risk, "number")))
        _cw = max(1, inner - 4)
        for _label, _key in (("what", "what"), ("why", "why")):
            _txt = str(_explain.get(_key, "")).strip()
            if _txt:
                _wr = [_txt[i:i + _cw] for i in range(0, len(_txt), _cw)] or [_txt]
                out.append((f"  {_label}: {_wr[0]}", "dim"))
                out.extend(("        " + _c, "dim") for _c in _wr[1:])
    out.append(("", "default"))
    if reason is not None:
        # deny+tell capture mode: show the reason input instead of the button row.
        out.append(("  deny + tell the agent why / what to do instead:", "accent"))
        # BUG5: scroll to keep the cursor (end of what you type) visible instead of clipping
        # to the head — a long reason now shows the freshly-typed TAIL inside the box.
        _r, _ = input_viewport(str(reason), len(str(reason)), max(1, inner - 6))
        out.append(("  ❯ " + _r + "▏", "user"))
        out.append(("  Enter send · Esc back", "dim"))
    else:
        out.append((_PERM_ACTION_LINE, "default"))
        # #222: always advertise the risk-explainer toggle (Ctrl+E) — hide/show plain-English
        # what/why/risk. Discoverable without cluttering the default prompt.
        if isinstance((req or {}).get("explain"), dict):
            out.append((("  Ctrl+E hide explanation" if show_explain
                         else "  Ctrl+E explain risk"), "dim"))
    return out


# The clickable action line + each option's marker -> action. Single source of truth so
# the keyboard intercept, the click hit-test, and the rendered text can't drift apart.
# Five choices (Gap 4): allow once / session / always, deny once / all. "always" remembers
# across sessions; "deny all" stops asking for this action for the rest of the session.
_PERM_ACTIONS = (("[1]", "once"), ("[2]", "session"), ("[3]", "always"),
                 ("[4]", "deny"), ("[5]", "deny_all"), ("[6]", "deny_guide"))
# Kept short so the whole line fits inside the centered modal box (≈68 cols). "allow" is
# implied by 1-3 and "deny" by 4-6; the box header already says "<tool> wants to run".
# [6] deny+tell = deny AND type a reason/suggestion the agent gets back (#83).
_PERM_ACTION_LINE = ("[1] once  [2] session  [3] always  [4] deny  [5] deny all  [6] deny+tell")


def permission_action_spans(line: str) -> list:
    """``[(start, end, action)]`` clickable spans on the permission action line. Each
    option's span runs from its ``[n]`` marker to just before the next marker, so the
    LABEL text is part of the hit/hover region, not just the bracket. The single source
    of truth for both click hit-testing and cursor-hover highlighting. Pure -> tested."""
    line = line or ""
    found = []
    for marker, action in _PERM_ACTIONS:
        i = line.find(marker)
        if i >= 0:
            found.append((i, action))
    found.sort()
    spans = []
    for idx, (start, action) in enumerate(found):
        end = found[idx + 1][0] if idx + 1 < len(found) else len(line)
        spans.append((start, end, action))
    return spans


def permission_click_action(line: str, col: int) -> str | None:
    """Map a column WITHIN the permission action line to an action
    ('once'|'session'|'always'|'deny'|'deny_all'|'deny_guide'), or None if it missed every
    option. Used for both a click (resolve) and hover (highlight the option under the cursor).
    Pure -> tested."""
    for start, end, action in permission_action_spans(line):
        if start <= col < end:
            return action
    return None


def context_window_label(used_tokens: int, window: int) -> str:
    """Status-bar context label: ``'<used>/<window>'`` (abbreviated) when the model's
    context window is known, else just ``'<used>'``. Pure -> unit-tested (A1)."""
    used = abbrev_count(max(0, int(used_tokens or 0)))
    if window and int(window) > 0:
        return f"{used}/{abbrev_count(int(window))}"
    return used


def status_line(*, model: str = "", cost_usd: float = 0.0, task_id: str = "",
                context_pct: int | None = None, width: int = 80,
                extra: str = "") -> str:
    """Compact status line: model · cost · ctx · task."""
    parts: list[str] = []
    if model:
        parts.append(f"model {model}")
    if context_pct is not None:
        parts.append(f"ctx {context_pct}%")
    parts.append(f"${cost_usd:.4f}")
    if task_id:
        parts.append(f"task {task_id}")
    if extra:
        parts.append(extra)
    line = "  ·  ".join(parts)
    if len(line) > width:
        line = line[: max(0, width - 1)] + "…"
    return line


# ─────────────────────────────────────────────────────────────────────────────
# Branding intensity (user: "i dont want this much branding"). Three modes via
# SYNTRA_BRANDING — DEFAULT is "minimal" (a quiet, fixed identity):
#   minimal (default): one fixed quiet glyph "·", no "0.1" version, per-bubble mark
#                       is a faint dot — calm, not loud.
#   full:    the original look — rotating ⌥/⥁ pool, "SYNTRA 0.1", "⌥ syntra" bubbles.
#   off:     no brand glyph or wordmark anywhere; bubbles just say "syntra".
# SYNTRA_BRAND_MARK still pins an exact glyph (tests / personal preference) and, when
# set, implies the full mark is wanted.
def _branding_mode() -> str:
    m = (os.environ.get("SYNTRA_BRANDING") or "").strip().lower()
    return m if m in ("minimal", "full", "off") else "minimal"


BRANDING = _branding_mode()

# Syntra's brand mark. Used for the brand wordmark, the assistant label, the READY
# badge, the window title, and the menu headers — the whole TUI imports this ONE
# constant. In FULL mode each launch picks one at random from the pool (⌥ = branch/
# route among models, ⥁ = always-orchestrating orbit). In minimal/off it's a single
# quiet glyph so the chrome stays calm.
_BRAND_POOL = ("⌥", "⥁")
if os.environ.get("SYNTRA_BRAND_MARK"):
    BRAND_MARK = os.environ["SYNTRA_BRAND_MARK"]
elif BRANDING == "full":
    BRAND_MARK = random.choice(_BRAND_POOL)
elif BRANDING == "off":
    BRAND_MARK = ""
else:  # minimal
    BRAND_MARK = "·"

# Glyphs that mark a line as already-formatted activity-tree content (a child row,
# a route line, a thinking line). Such lines carry their own indentation, so the
# bubble renderer must pass them through verbatim — never add a `┆` status gutter.
_TREE_GLYPHS = ("├", "└", "┄", "▸", "✶", "〈", "⊙", "◎", "▣", "▶", "↻", "⛔") + _BRAND_POOL


def _is_tree_line(text: str) -> bool:
    return text.lstrip()[:1] in _TREE_GLYPHS if text.strip() else False


# ── Activity feed (F53): clean, glyph-gutter one-liners for live agent actions ──
# Each high-signal action (read/edit a file, reference a file, web search, run a
# command) renders as ONE tidy line: a glyph + an aligned verb column + the target.
# Syntra's own look — not a copy of any other CLI. These render VERBATIM and stay
# VISIBLE (role "activity" is deliberately NOT in _TRACE_ROLES, so the fold never
# hides them); the live/in-progress one animates via activity_working_line().
_ACTIVITY_GLYPH = {
    # NB: deliberately NO diamond glyph — the user is on record disliking those (Gap 6 +
    # the PTY guard). "read" uses a soft open bullet ◦ instead.
    "read":   "◦",
    "edited": "✎",
    "wrote":  "✎",
    "edit":   "✎",
    "ref":    "⌗",
    "web":    "⚲",
    "fetch":  "⚲",
    "search": "⌕",
    "grep":   "⌕",
    "run":    "⊳",
    "plan":   "▸",
    "done":   "✓",
}
# Tool/verb aliases the engine emits ("read: X", "bash: cmd", "websearch: q") -> our verb.
_ACTIVITY_VERB_ALIAS = {
    "bash": "run", "shell": "run", "exec": "run", "command": "run",
    "websearch": "web", "web_search": "web", "search_web": "web",
    "write": "wrote", "apply_patch": "edited", "str_replace": "edited",
    "view": "ref", "open": "ref", "reference": "ref", "read_file": "read",
    "grep": "search", "glob": "search", "find": "search", "fetch": "web", "url": "web",
}
_ACTIVITY_VERB_W = 6   # widest common verb ("edited"/"search") -> aligned columns


def normalize_activity(raw: str) -> tuple[str, str]:
    """Parse an engine activity string like 'read: loop.py' or 'bash: curl …' into
    (verb, target). Unknown shapes fall back to (raw-name, rest). Pure."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    name, _, rest = raw.partition(":")
    name = name.strip().lower()
    target = rest.strip()
    if not _:                       # no colon -> the whole thing is the name/verb
        name, target = raw.strip().lower(), ""
    verb = _ACTIVITY_VERB_ALIAS.get(name, name)
    return verb, target


def activity_line(action: str, target: str = "", detail: str = "") -> str:
    """One glyph-gutter activity line (the text only; the caller tags role 'activity').

    >>> activity_line("read", "loop.py", "212 lines")
    '  ◦ read   loop.py · 212 lines'
    """
    verb = (action or "").strip().lower()
    verb = _ACTIVITY_VERB_ALIAS.get(verb, verb)
    glyph = _ACTIVITY_GLYPH.get(verb, "·")
    body = target or ""
    if detail:
        body = f"{target} · {detail}" if target else detail
    return f"  {glyph} {verb.ljust(_ACTIVITY_VERB_W)} {body}".rstrip()


# A twinkling star cycle for the live "thinking/working" glyph (real motion, unlike
# the single-frame brand pulse). Drives F53's one animated line.
_THINK_FRAMES = ("✶", "✷", "✸", "✹", "✺", "✹", "✸", "✷")


def motion_enabled() -> bool:
    """False when the user opted out of animation (``SYNTRA_REDUCED_MOTION`` /
    ``SYNTRA_NO_ANIMATION``). Spinners/shimmer then freeze to a single static frame —
    for screen-reader users and motion sensitivity (the terminal analog of the web's
    prefers-reduced-motion). Pure-ish (reads env)."""
    import os
    return not (os.environ.get("SYNTRA_REDUCED_MOTION")
                or os.environ.get("SYNTRA_NO_ANIMATION"))


def _frame(frames, tick: int):
    """Pick the animation frame for `tick`, or a FIXED frame when motion is disabled."""
    return frames[(int(tick) % len(frames)) if motion_enabled() else 0]


# The chat-pane view tabs (user keeps all 4 but wanted them LIVE/creative). Each carries
# a glyph cue; the ACTIVE tab's glyph twinkles. (chat=conversation, plan=steps,
# search=find, memory=notes.)
_TAB_ICONS = {"chat": "▌", "plan": "☰", "search": "⌕", "memory": "✦"}
_TAB_TWINKLE = ("●", "◉", "◍", "◉")


def tab_bar_row(tabs: list, active: str, tick: int = 0, width: int = 80):
    """Build the chat view-tab strip as (text, spans). The ACTIVE tab is bright (accent)
    with a twinkling lead glyph; inactive tabs are dim. `spans` = [(start, end, style)]
    (3-tuples, matching the chat-region span convention) so the TUI paints each tab in its
    own color + animates the active one (#9 'make it more live'). Pure given (tabs, active,
    tick)."""
    parts: list[str] = []
    spans: list = []
    col = 0
    twinkle = _frame(_TAB_TWINKLE, tick) if (tick and motion_enabled()) else "●"
    for i, t in enumerate(tabs):
        if i:
            sep = "  "
            col += len(sep)
            parts.append(sep)
        is_active = (t.lower() == active)
        icon = twinkle if is_active else _TAB_ICONS.get(t.lower(), "·")
        label = f"{icon} {t.upper() if is_active else t.lower()}"
        start = col
        parts.append(label)
        col += len(label)
        spans.append((start, col, "accent" if is_active else "dim"))
    text = "".join(parts)
    hint = "   ·  ^←/^→ or click to switch"
    if len(text) + len(hint) <= width:
        text = text + hint
    return text, spans


# Syntra's OWN playful "working…" verbs — a rotating pool so the live line has life
# (user wanted the "fiddle-faddling" vibe, in our own words, no product names). Used
# only when the active role is generic ("working"); a real role (planner/executor/…)
# shows its own name instead. Rotates slowly (one word every ~few seconds).
_WORKING_VERBS = (
    "pondering", "wrangling", "untangling", "noodling", "synthesizing",
    "orchestrating", "deliberating", "weaving", "marshalling", "tinkering",
    "puzzling", "scheming", "finagling", "percolating", "cogitating",
)


def working_verb(tick: int) -> str:
    """A rotating Syntra-own 'working' verb chosen by `tick` (slow cadence). Frozen to a
    single steady verb when reduced-motion is set."""
    if not motion_enabled():
        return _WORKING_VERBS[0]
    return _WORKING_VERBS[(tick // 12) % len(_WORKING_VERBS)]


def activity_working_line(label: str, elapsed: str = "", tokens: int = 0,
                          effort: str = "", *, tick: int = 0) -> str:
    """The ONE live/in-progress activity line, animated (the glyph twinkles; F53 +
    user: 'text as well as the shape should animate'). Re-rendered each frame with a
    fresh `tick`, so unlike the committed lines above it visibly breathes.

    e.g. '✶ thinking · 2m33s · ↓1.5k · xhigh'
    """
    glyph = _frame(_THINK_FRAMES, tick)   # animated twinkle (static if reduced-motion)
    parts = [f"{glyph} {label or 'working'}"]
    if elapsed:
        parts.append(elapsed)
    if tokens:
        parts.append(f"↓{abbrev_count(tokens)}")
    if effort:
        parts.append(effort)
    return " · ".join(parts)


# The per-bubble assistant label scales with branding: "⌥ syntra" (full), "· syntra"
# (minimal — a faint dot), or just "syntra" (off). No leading space when the mark is empty.
_ASSISTANT_LABEL = (f"{BRAND_MARK} syntra" if BRAND_MARK else "syntra")
_ROLE_LABEL = {"user": "❯ you", "assistant": _ASSISTANT_LABEL,
               "assistant_stream": _ASSISTANT_LABEL,
               "system": "system", "tool": "•"}
_ROLE_GUTTER = {"user": "│ ", "assistant": "│ ", "assistant_stream": "│ ",
                "system": "  ", "tool": "  "}


def freeze_thinking(messages):
    """A1 reasoning cell: collapse each COMPLETED chain-of-thought run — a '✶ thinking'
    header plus its indented CoT lines — into one '✶ thought · N lines' summary, once the
    step has moved on (the run is followed by other content). The LIVE run (the last one,
    still streaming with nothing after it) is left expanded so you watch it think. Pure;
    returns a new list. Only touches runs that begin with the real '✶ thinking' header, so
    arbitrary 'thinking'-role lines are untouched."""
    out = []
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.role == "thinking" and m.text.strip().startswith("✶ thinking"):
            j = i + 1
            while (j < n and messages[j].role == "thinking"
                   and not messages[j].text.strip().startswith("✶ thinking")):
                j += 1
            cot = [messages[k] for k in range(i + 1, j) if messages[k].text.strip()]
            complete = j < n                     # something follows -> the thought ended
            if complete and cot:
                c = len(cot)
                out.append(Message("thinking", f"✶ thought · {c} line{'s' if c != 1 else ''}"))
            else:
                out.extend(messages[i:j])        # live (still streaming) -> keep expanded
            i = j
        else:
            out.append(m)
            i += 1
    return out


def _freeze_thinking_orig_index(messages):
    """F25: the ORIGINAL message index for each message freeze_thinking() emits, so callers can
    map rendered rows back to `transcript.messages` (the raw list). Mirrors freeze_thinking's
    control flow exactly: a collapsed run → one summary pointing at the run's start index; a
    live/kept run → each kept message maps to its own original index; everything else 1:1."""
    idx: list[int] = []
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.role == "thinking" and m.text.strip().startswith("✶ thinking"):
            j = i + 1
            while (j < n and messages[j].role == "thinking"
                   and not messages[j].text.strip().startswith("✶ thinking")):
                j += 1
            cot = [messages[k] for k in range(i + 1, j) if messages[k].text.strip()]
            complete = j < n
            if complete and cot:
                idx.append(i)                 # one summary message → run start
            else:
                idx.extend(range(i, j))       # kept expanded → each maps to its own index
            i = j
        else:
            idx.append(i)
            i += 1
    return idx


def group_tool_cells(messages):
    """A1 tool cell (Option C): pin the FIRST result line ('  ⎿ …') of a tool call to its
    call — the visible glyph-feed 'activity' line — so call + outcome read as ONE cell.
    Any further output stays a foldable 'tool' line. Pure render-time transform (NO
    buffering), so the call still appears live the instant it's emitted; the result joins
    on the next render. A '⎿' line is promoted iff the line BEFORE it (in the ORIGINAL
    list) is an 'activity' call — so only the first result attaches, not a whole output."""
    import dataclasses as _dc
    out = []
    prev_role = None
    for m in messages:
        if (m.role == "tool" and m.text.lstrip().startswith("⎿") and prev_role == "activity"):
            out.append(_dc.replace(m, role="activity"))   # pin to the call (visible)
        else:
            out.append(m)
        prev_role = m.role                                 # ORIGINAL role drives the next test
    return out


def render_bubbles(messages, width: int, collapsed: set | None = None,
                   trace_collapsed: bool = False, tick: int = 0,
                   expanded: set | None = None, autofold_threshold: int = 0
                   ) -> list[tuple[str, str]]:
    """Pure: render messages as 4-sided rounded boxes -> (text, role) lines.

    `tick` (>0) twinkles the glyph of the LIVE '✶ thinking' line (A1: animate while
    streaming; freeze_thinking already collapses the finished ones). Pure given tick.

    AUTO-FOLD (user #5): when `autofold_threshold` > 0, any turn whose body exceeds it
    lines folds to ONE summary line `▸ <label> (N lines) — Enter/click to expand`, UNLESS
    its index is in `expanded` (user opened it) or it's the LAST message (you're reading
    the freshest answer). `collapsed` (manual collapse) still shows the 3-lines+more form.

    Each non-system, non-tool turn is a panel:
        ╭─ label ───────────╮
        │ content            │
        ╰────────────────────╯
    Boxes are sized to the wider of the content/label, capped at the pane width.
    Code-fence bodies are tagged role "code" so the UI can syntax-highlight them;
    fences themselves stay visible. system/tool lines stay compact (no box).
    """
    class _BubbleList(list):
        """A list that can also carry line_msg_index (row -> message index) and
        line_spans (row -> intra-line styled spans for syntax/diff highlighting)."""
        line_msg_index: list
        line_spans: list

        def __init__(self, *args):
            super().__init__(*args)
            self.line_msg_index = []
            self.line_spans = []
    out = _BubbleList()
    total = max(20, int(width))
    # Boxes are sized to their CONTENT, capped near the full pane width (small right
    # margin) so long messages use the screen instead of wrapping early at ~72 cols
    # and leaving the right third empty (P6/P7). Short messages still stay compact.
    max_box = min(total, max(40, total - 4))
    inner = max_box - 4                          # 2 border cols + 2 padding spaces

    def _content_width(text_block: str, label: str) -> int:
        """Widest content line (capped to inner), min the label width. Code/diff lines
        carry a 2-col '▎ ' marker, so they need 2 extra cols or they wrap a hair early
        (a real pre-existing off-by-2 that also broke single-line word-diff)."""
        widest = display_width(label)
        in_code = False
        for ln in text_block.split("\n"):
            if ln.strip().startswith("```"):
                in_code = not in_code
                continue
            ls = ln.lstrip()
            is_diff = (ls.startswith("@@")
                       or (ls.startswith("+") and not ls.startswith("+++"))
                       or (ls.startswith("-") and not ls.startswith("---")))
            # cell-accurate width (emoji/CJK are 2 cells) so the box is sized to what the
            # terminal actually paints — a len()-based width left the right border short.
            widest = max(widest, display_width(ln) + (2 if (in_code or is_diff) else 0))
        return max(8, min(widest, inner))

    def _box_top(label: str, bw: int) -> str:
        bw = max(6, bw)
        return f"╭ {label}" + " " * max(0, bw - display_width(label) - 3) + "╮"

    def _box_bottom(bw: int) -> str:
        bw = max(6, bw)
        return "╰" + " " * max(0, bw - 2) + "╯"

    def _box_row(content: str, bw: int, *, raw: bool = False) -> str:
        iw = max(1, bw - 4)
        # clip + pad by display WIDTH, not len — otherwise a wide glyph (emoji/CJK) eats an
        # extra cell and the right border (│) is pushed out of alignment (the 'Sure! 😊' box).
        c = clip_to_width(content, iw)
        return "│ " + c + " " * max(0, iw - display_width(c)) + " │"

    # parallel lists: for each entry in `out`, which message index produced it, and
    # its intra-line styled spans (empty for most lines; set for code/diff lines).
    line_msg_index: list[int] = []
    line_spans: list[list] = []

    def _emit(item, spans=None):
        out.append(item)
        line_msg_index.append(_cur_mi[0])
        line_spans.append(spans or [])

    _cur_mi = [0]

    # P29: optionally fold each maximal run of background trace lines (the
    # ⊙ ANALYZE / ⋯ planner / ✶ thinking activity) into ONE collapsible summary.
    # system/user/assistant stay visible — only the noisy trace folds.
    # 'error' is deliberately NOT collapsible — a failure must stay visible even when
    # the background trace is folded (else the user sees "⋯ N hidden" and no reason).
    _TRACE_ROLES = ("mode", "tool", "thinking", "ok")
    # A1: freeze each finished chain-of-thought to a '✶ thought · N lines' summary (the
    # live one stays expanded) BEFORE the trace-fold runs over the result.
    # F25: freeze_thinking collapses runs (changes the count), so keep a map from each
    # post-freeze message back to its ORIGINAL index in `messages`; `orig` below is translated
    # through it so line_msg_index points into the RAW transcript, not the post-transform list.
    _ft_orig = _freeze_thinking_orig_index(messages)
    messages = freeze_thinking(messages)
    # A1 (Option C): pin each tool call's FIRST result line to the call so they read as
    # one cell; extra output stays foldable. Render-time only — liveness preserved.
    # group_tool_cells is 1:1 (one output per input), so _ft_orig stays aligned.
    messages = group_tool_cells(messages)
    # A clear, clickable toggle header marks every background-trace run in BOTH states,
    # so it's always obvious where to click to show/hide the behind-the-scenes activity.
    disp: list = []
    orig: list[int] = []
    i = 0
    while i < len(messages):
        if messages[i].role in _TRACE_ROLES:
            j = i
            while j < len(messages) and messages[j].role in _TRACE_ROLES:
                j += 1
            cnt = j - i
            plural = "s" if cnt != 1 else ""
            if trace_collapsed:
                disp.append(Message("trace_summary",
                    f"▸ {cnt} background line{plural} hidden — click here or /trace to show"))
                orig.append(i)
            else:
                disp.append(Message("trace_summary",
                    f"▾ behind the scenes ({cnt} line{plural}) — click here or /trace to hide"))
                orig.append(i)
                for k in range(i, j):
                    disp.append(messages[k]); orig.append(k)
            i = j
        else:
            disp.append(messages[i]); orig.append(i); i += 1

    _blk = [0]   # running fenced-code-block index (M5: [n] tags for /copy <n>)
    for _di, m in enumerate(disp):
        _mi = orig[_di]
        _cur_mi[0] = _mi
        role = m.role
        if role == "trace_summary":
            _emit((m.text[:total], "dim"))
            continue
        if role in ("system", "tool", "mode", "thinking", "ok", "error", "activity"):
            # Compact, de-emphasized log/status lines (the activity tree lives here).
            # Activity-tree lines (mode headers, tree children, thinking, verdicts)
            # already carry their own indentation + glyphs, so they render VERBATIM.
            # Only PLAIN status lines get a gutter (`· ` for system, `┆ ` for a bare
            # tool log). A tool line that's a tree child (starts with ├ └ ┄ ▸ ✶ etc.)
            # must NOT get a second gutter — that double-indents the tree.
            # role "activity" (F53 glyph feed) carries its own "  ◦ read …" gutter, renders
            # verbatim, and is NOT in _TRACE_ROLES above -> it stays VISIBLE (never folded).
            if role in ("mode", "thinking", "ok", "error", "activity") or _is_tree_line(m.text):
                prefix = ""
            else:
                prefix = "· " if role == "system" else "  ┆ "
            style = "tool" if role == "activity" else role     # soft, readable tint
            # A1: the LIVE '✶ thinking' line twinkles its glyph while streaming (the
            # finished thoughts are already frozen to '✶ thought · N' by freeze_thinking).
            if role == "thinking" and tick and m.text.strip() == "✶ thinking":
                _tg = _frame(_THINK_FRAMES, tick)
                _emit((m.text.replace("✶", _tg, 1), "thinking"))
                continue
            # A1 inline diff cell: a tool line that IS a unified-diff line renders in the
            # diff colors (added/removed/hunk) with no extra gutter — its own +/-/@@ marker
            # carries it. Lets the actual changed lines show inline, not just a count.
            _ls = m.text.lstrip()
            if role == "tool" and (_ls.startswith("@@")
                                   or (_ls.startswith("+") and not _ls.startswith("+++"))
                                   or (_ls.startswith("-") and not _ls.startswith("---"))):
                _drole = ("diff_hunk" if _ls.startswith("@@")
                          else "diff_add" if _ls.startswith("+") else "diff_del")
                for ln in wrap_lines(m.text, total):
                    _emit((ln, _drole))
                continue
            _wrapped = wrap_lines(m.text, total - len(prefix))
            # PLAN CARD: a system message whose first line is the "📋 plan · …" header ALWAYS folds
            # to that header line ("click to expand the plan"), regardless of length — the user
            # asked for a click-to-expand/collapse plan card. Click toggles membership in `expanded`
            # (same set + click handler the long-block fold uses). The newest turn is NOT exempted
            # here (the card is chrome, not the answer the user is reading).
            _is_plan_card = (role == "system" and _wrapped and "📋 plan" in _wrapped[0])
            if _is_plan_card and (expanded is None or _mi not in expanded):
                _emit((f"{prefix}▸ {_wrapped[0].strip()} — click to expand the plan", "accent"))
                continue
            # #5: a long PLAIN system/tool log (e.g. a 60-line shell dump) auto-folds to a
            # single "▸ output (N lines) — Enter or click to expand" line, unless expanded
            # or it's the newest turn. Tree/trace roles are handled by trace_collapse, not here.
            if (role in ("system", "tool") and not _is_tree_line(m.text)
                    and autofold_threshold > 0 and _mi != (len(messages) - 1)
                    and (expanded is None or _mi not in expanded)
                    and len(_wrapped) > autofold_threshold):
                _emit((f"{prefix}▸ output ({len(_wrapped)} lines) — Enter or click to expand",
                       "accent"))
                continue
            for ln in _wrapped:
                _emit((prefix + ln, style))
            if (role in ("system", "tool") and not _is_tree_line(m.text)
                    and autofold_threshold > 0 and expanded is not None and _mi in expanded
                    and len(_wrapped) > autofold_threshold):
                _emit((f"{prefix}▾ collapse — Enter or click to fold", "dim"))
            continue

        label = _ROLE_LABEL.get(role, role)

        # F31: an assistant turn with stored alternatives shows the selected one.
        _variants = getattr(m, "variants", None) or []
        if _variants:
            _vi = m.variant_idx if 0 <= m.variant_idx < len(_variants) else 0
            text = _variants[_vi]
        else:
            text = m.text
        try:
            if role.startswith("assistant") and "```" not in text:
                from syntra.core.markdown import render_plain
                # M2: hold back a trailing table while still streaming so it doesn't
                # reflow its columns on every token (aligns once the turn finalizes).
                # #219: pass the bubble's usable content width so a wide table fits the
                # terminal (shrinks+wraps, or vertical fallback) instead of overflowing.
                text = render_plain(text, streaming=(role == "assistant_stream"),
                                    width=max(20, inner))
        except Exception:  # noqa: BLE001
            pass

        # size THIS box to its content (capped) so it doesn't span the full pane
        bw = _content_width(text, label) + 4
        bw = max(bw, len(label) + 4)
        # #132: a fenced code block gets a "[n] lang   ⧉ copy" clickable header ADDED at render
        # time (not in the raw text), so widen the box to fit it — else "⧉ copy" clips to "⧉ co"
        # and the click affordance is unreadable/unmatchable.
        if "```" in text:
            import re as _re_cb
            _langs = _re_cb.findall(r"```(\w*)", text)
            _hdr_w = max((len(f"[9] {(lg or 'code')}   ⧉ copy") for lg in _langs), default=0)
            bw = max(bw, _hdr_w + 4)
        # F31: a toggle row for assistant turns that have >1 stored alternative.
        _nav = ""
        if role.startswith("assistant") and len(_variants) > 1:
            _nav = f"‹ {(m.variant_idx % len(_variants)) + 1}/{len(_variants)} ›  ◂ ▸ switch"
            bw = max(bw, len(_nav) + 4)
        # If this turn will AUTO-FOLD (long body, not the newest, not expanded), the box
        # must be wide enough to show the whole "▸ … expand" summary, since its content
        # rows are hidden. Estimate the line count cheaply before the full collect below.
        _fold_hint = "▸ answer (0000 lines) — Enter or click to expand"
        if (autofold_threshold > 0 and _mi != (len(messages) - 1)
                and (collapsed is None or _mi not in collapsed)
                and (expanded is None or _mi not in expanded)
                and len(wrap_lines(text, max(1, width - 4))) > autofold_threshold):
            bw = max(bw, len(_fold_hint) + 4)
        _emit((_box_top(label, bw), role))

        # Collect content lines first for collapse support. Each entry is a
        # (text, style, spans) triple; spans carry intra-line syntax highlighting.
        # `content_raw` is a PARALLEL list holding the raw diff text (with the +/- sign)
        # for single-line diff rows, else None — used by the word-diff post-pass below.
        from .markdown import highlight_code_spans
        content_lines: list = []
        content_raw: list = []
        in_code = False
        code_lang = ""
        for ln in text.split("\n"):
            stripped = ln.strip()
            if stripped.startswith("```"):
                lang = stripped[3:].strip()
                if not in_code:
                    in_code = True
                    code_lang = lang
                    _blk[0] += 1
                    # [n] index + a CLICKABLE copy affordance: clicking this header row copies the
                    # RAW block (no gutter/borders) to the clipboard. Fixes "drag-copy grabs the ▎
                    # gutter + │ borders" WITHOUT making people type /copy (they won't) — the TUI
                    # click handler recognizes a "⧉ copy" header row and copies block N. (#132)
                    _lang = lang or "code"
                    label2 = f"[{_blk[0]}] {_lang}   ⧉ copy"
                    content_lines.append((_box_row(label2, bw), "comment", []))
                    content_raw.append(None)
                else:
                    in_code = False
                    code_lang = ""
                continue
            line_role = "code" if in_code else role
            ls = ln.lstrip()
            if ls.startswith("@@"):
                line_role = "diff_hunk"
            elif ls.startswith("+") and not ls.startswith("+++"):
                line_role = "diff_add"
            elif ls.startswith("-") and not ls.startswith("---"):
                line_role = "diff_del"
            if in_code or line_role in ("code", "diff_add", "diff_del", "diff_hunk"):
                pieces = wrap_lines(ln, bw - 6)
                single = len(pieces) == 1
                for w in pieces:
                    # syntax-highlight the code text (M1); spans shift right by the
                    # "│ ▎ " box+marker prefix (4 cols) to align to the rendered line.
                    sp = ([(s + 4, e + 4, st) for s, e, st in highlight_code_spans(w, code_lang)]
                          if in_code else [])
                    content_lines.append((_box_row("▎ " + w, bw), line_role, sp))
                    # only single-piece (unwrapped) diff rows are eligible for word-diff
                    content_raw.append(w if (single and line_role in ("diff_add", "diff_del")) else None)
            else:
                for w in wrap_lines(ln, bw - 4):
                    # M7: style URLs / file:line refs so they read as clickable; spans
                    # shift right by the "│ " box prefix (2 cols).
                    lsp = [(s + 2, e + 2, "accent") for s, e, _t in find_links(w)]
                    content_lines.append((_box_row(w, bw), line_role, lsp))
                    content_raw.append(None)

        # word-level diff (M1): for each adjacent (diff_del, diff_add) pair of unwrapped
        # rows, KEEP the line in its green/red diff colour (scannability) and mark just the
        # CHANGED chars with an emphasis span (rendered reverse-video) so a one-word edit
        # stands out without losing the at-a-glance add/del cue. Emphasis spans are
        # 4-tuples (start, end, style, emph=True); syntax spans stay 3-tuples.
        for k in range(len(content_lines) - 1):
            t0, s0, _ = content_lines[k]
            t1, s1, _ = content_lines[k + 1]
            r0, r1 = content_raw[k], content_raw[k + 1]
            if s0 == "diff_del" and s1 == "diff_add" and r0 and r1:
                dels, adds = word_diff_spans(r0[1:], r1[1:])   # compare content after the sign
                # rendered content-after-sign offset = "│ ▎ " (4) + sign (1) = 5
                content_lines[k] = (t0, s0, [(s + 5, e + 5, "diff_del", True) for s, e in dels])
                content_lines[k + 1] = (t1, s1, [(s + 5, e + 5, "diff_add", True) for s, e in adds])

        # Collapse long messages if in collapsed set
        is_collapsed = collapsed is not None and _mi in collapsed
        # AUTO-FOLD (user #5): a long body folds to ONE summary line unless the user
        # expanded it, it's manually collapsed (handled above), or it's the newest turn.
        _is_newest = _mi == (len(messages) - 1)
        _auto_fold = (autofold_threshold > 0
                      and not is_collapsed
                      and not _is_newest
                      and (expanded is None or _mi not in expanded)
                      and len(content_lines) > autofold_threshold)
        if _auto_fold:
            n = len(content_lines)
            _kind = ("code" if role == "code" or any(s == "code" for _, s, _ in content_lines)
                     else "output" if role in ("tool", "system")
                     else "message" if role == "user"       # a long USER turn is a "message", not an "answer"
                     else "answer")
            _emit((_box_row(f"▸ {_kind} ({n} lines) — Enter or click to expand", bw), "accent"))
        elif is_collapsed and len(content_lines) > 4:
            for _ct, _cs, _csp in content_lines[:3]:
                _emit((_ct, _cs), _csp)
            more = len(content_lines) - 3
            _emit((_box_row(f"▾ {more} more lines (click to expand)", bw), "dim"))
        else:
            for _ct, _cs, _csp in content_lines:
                _emit((_ct, _cs), _csp)
            # When an auto-foldable block IS expanded, offer a one-key re-fold affordance.
            if (autofold_threshold > 0 and expanded is not None and _mi in expanded
                    and len(content_lines) > autofold_threshold):
                _emit((_box_row("▾ collapse — Enter or click to fold", bw), "dim"))
        if _nav:
            _emit((_box_row(_nav, bw), "dim"))   # F31: ‹ n/N › variant toggle
        _emit((_box_bottom(bw), role))
        # Tighter threading: only add blank between consecutive same-role messages.
        # Between different roles (user→assistant), use a thin connector instead.
        if _di < len(disp) - 1:
            next_msg = disp[_di + 1]
            if next_msg.role in ("system", "tool", "trace_summary"):
                pass  # system/tool lines have their own compact rendering
            elif next_msg.role == role:
                _emit(("", role))  # blank between same-role messages
            else:
                _emit(("─" * min(bw, 20), "dim"))  # thin connector between turns
        else:
            _emit(("", role))  # trailing blank after last message
    # F25: line_msg_index currently holds POST-transform indices (freeze_thinking collapsed
    # runs). Consumers (scroll_to_message via the rail/msgs) index into the RAW transcript.messages,
    # so translate each entry back to its original index via _ft_orig before exporting. Internal
    # fold logic above keeps using the post-transform _mi untouched.
    line_msg_index = [(_ft_orig[mi] if 0 <= mi < len(_ft_orig) else mi) for mi in line_msg_index]
    # stash the per-line message index on the list so callers can read it
    # without changing the return type (backward compatible).
    try:
        out.line_msg_index = line_msg_index  # type: ignore[attr-defined]
        out.line_spans = line_spans  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - plain list can't hold attrs in some cases
        pass
    return out


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Syntra's "working" pulse for the window title — the brand mark, steady.
_PULSE_FRAMES = (BRAND_MARK,)

# Effort gauge: visual power indicator per reasoning level. Each level has a
# distinct fill (how much "power" is lit) AND a distinct color, so you can read
# the level at a glance. Higher levels animate more energetically (see effort_bar).
EFFORT_GAUGE = {
    "low":    "█░░░",
    "medium": "██░░",
    "high":   "███░",
    "xhigh":  "████",
    "max":    "████",
    "ultracode": "█████",
}
EFFORT_COLORS = {
    "auto":   "accent",     # adaptive
    "low":    "dim",        # muted grey — minimal power
    "medium": "string",     # green
    "high":   "accent",     # blue
    "xhigh":  "number",     # amber/orange
    "max":    "error",      # hot red — maximum
    "ultracode": "error",   # hottest — max effort + workflows
}
# Bolt animation frames: the ⚡ pulses; how fast scales with the level so a higher
# effort visibly "buzzes" harder.
_BOLT_FRAMES = ("⚡", "⚡", "✦", "⚡")
_EFFORT_SPEED = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5, "ultracode": 6}


def effort_bar(level: str, *, tick: int = 0) -> tuple[str, str]:
    """Animated effort gauge -> (text, style). `tick` (e.g. int(time*4)) drives a
    bolt pulse whose rate rises with the level; the fill + color encode the level."""
    level = (level or "").lower()
    gauge = EFFORT_GAUGE.get(level, "░░░░")
    style = EFFORT_COLORS.get(level, "dim")
    speed = _EFFORT_SPEED.get(level, 1)
    bolt = _frame(_BOLT_FRAMES, tick * speed) if tick else "⚡"
    # the lit cells shimmer left-to-right at higher levels for a sense of motion
    lit = gauge.count("█")
    if tick and lit > 1 and motion_enabled():
        # rotate a brighter cell through the lit region
        cells = list(gauge)
        bright_at = (tick * speed) % max(1, lit)
        seen = 0
        for i, c in enumerate(cells):
            if c == "█":
                cells[i] = "▓" if seen == bright_at else "█"
                seen += 1
        gauge = "".join(cells)
    return f"{bolt}{gauge} {level}", style


# Effort slider (P19): the Faster↔Smarter ladder. `auto` is the adaptive far-left;
# `ultracode` is the far-right (max effort + multi-agent workflows), past a divider.
# This is the FULL ladder (a reasoning model that supports the top level shows all of
# it). Per #13 the slider is now MODEL-AWARE: effort_slider_levels_for() trims this to
# exactly what the selected model supports (or just "auto" / "n/a" for a non-reasoning
# model) so the UI never offers a level the model silently ignores.
EFFORT_SLIDER_LEVELS = ["auto", "low", "medium", "high", "xhigh", "max", "ultracode"]


def effort_slider_levels_for(model) -> list[str]:
    """The slider levels to OFFER for `model` (#13 / req I1: data-driven, not hardcoded).

    Built from the engine's capability view (`reasoning.effort_levels_for`):
      • non-reasoning model  -> ["auto"]  (the adaptive setting is always valid; the UI
        shows it alone + an "n/a — model has no reasoning levels" note);
      • reasoning model      -> ["auto", <its supported low…xhigh>], plus "max" and
        "ultracode" only when the model reaches the top of the ladder (xhigh). "max" is
        the slider's name for the top reasoning level; "ultracode" = max + workflows.
    `model` may be None (no selection yet) -> the full ladder (we can't gate what we
    don't know, and auto stays safe)."""
    if model is None:
        return list(EFFORT_SLIDER_LEVELS)
    try:
        from .reasoning import effort_levels_for
        supported = list(effort_levels_for(model))
    except Exception:  # noqa: BLE001 - never let a capability lookup break the slider
        return list(EFFORT_SLIDER_LEVELS)
    if not supported:
        return ["auto"]                       # non-reasoning model: only the adaptive setting
    out = ["auto"] + supported
    if "xhigh" in supported:                  # tops the ladder → expose max + ultracode
        out += ["max", "ultracode"]
    return out


def effort_slider_box(idx: int, width: int = 60, levels: list | None = None,
                      tick: int = 0) -> list:
    """Render the effort ladder as a Faster↔Smarter slider.

    Returns a list of rows that are either ``(text, style)`` or ``(text, style, spans)``
    where `spans` is ``[(start, end, style, emph?)]`` — the TUI overpaints them so each
    level shows its OWN gradient color (low=dim … max=red, ultracode=purple) and the
    SELECTED level is emphasized (#14: the gauge is colored + the ⚡ bolt pulses with
    `tick`). `idx` indexes `levels` (defaults to the full ladder; the TUI passes the
    MODEL-AWARE subset). Pure given (idx, levels, tick) -> unit-tested.

    TODO(effort-animation): PENDING — the user wants a richer, eye-catching per-level
    animation/effect here (effort = more INTENSITY/RICHNESS, calmer not faster; the design
    explored a half-block pixel framebuffer / dense-organic "fire"-class effect). Design
    pass done (workflow wf_7ea384d1-26e); live prototypes were in .anim_demo/ but none were
    approved yet. The MODEL-AWARE level gating below is DONE; only the visual is pending.
    """
    levels = list(levels) if levels else EFFORT_SLIDER_LEVELS
    # animated bolt that pulses with tick (#14 — "this block should have animations")
    bolt = _frame(_BOLT_FRAMES, tick) if (tick and motion_enabled()) else "⚡"
    # A non-reasoning model offers only "auto" → show an explicit n/a note, no ladder.
    if len(levels) <= 1:
        only = levels[0] if levels else "auto"
        return [
            ("", "default"),
            (f"  {bolt} Effort", "accent"),
            ("", "default"),
            (f"  ▸ {only}", EFFORT_COLORS.get(only, "accent")),
            ("  n/a — this model has no selectable reasoning levels", "dim"),
            ("", "default"),
            ("  Enter to confirm · Esc to cancel", "dim"),
        ]
    idx = max(0, min(int(idx), len(levels) - 1))

    # lay out the labels on one row, recording each label's (start,end) span so each
    # level can be painted in its own gradient color. ultracode sits past a dotted
    # divider to read as a separate "gear".
    label_row = ""
    centers: list[int] = []
    spans: list = []
    for i, lv in enumerate(levels):
        sep = "" if i == 0 else ("  ┊  " if lv == "ultracode" else "   ")
        label_row += sep
        start = len(label_row)
        centers.append(start + len(lv) // 2)
        label_row += lv
        # each level painted in its intensity color; the SELECTED one is emphasized
        spans.append((start, start + len(lv), EFFORT_COLORS.get(lv, "default"), i == idx))

    # keep the marker inside the rendered width so it stays visible after the
    # caller truncates to `width` on narrow terminals (the "  " prefix costs 2 cols).
    marker = [" "] * (len(label_row) + 1)
    mpos = min(centers[idx], max(0, int(width) - 3))
    marker[mpos] = "▲"
    marker_row = "".join(marker)

    cur = levels[idx]
    hint = {
        "auto": "adaptive — scales low→max with task risk (capability-gated)",
        "ultracode": "max effort + multi-agent workflows",
    }.get(cur, f"force {cur} reasoning on every step")
    style = EFFORT_COLORS.get(cur, "accent")
    gap = max(1, len(label_row) - len("Faster") - len("Smarter"))
    # the "  " left pad on the label/marker rows shifts the spans +2 columns
    _PAD = 2
    label_spans = [(s + _PAD, e + _PAD, st, em) for (s, e, st, em) in spans]
    marker_span = [(mpos + _PAD, mpos + _PAD + 1, style, True)]

    return [
        ("", "default"),
        (f"  {bolt} Effort", "accent"),
        ("", "default"),
        ("  Faster" + " " * gap + "Smarter", "dim"),
        ("  " + label_row, "default", label_spans),   # per-level gradient colors (#14)
        ("  " + marker_row, "accent", marker_span),    # ▲ marker in the level's color
        ("", "default"),
        (f"  ▸ {cur}  —  {hint}", style),
        ("", "default"),
        ("  ←/→ to adjust · Enter to confirm · Esc to cancel", "dim"),
    ]


def minimap_wheel_box(rows, focus_pos: int, focused_full: str, width: int):
    """Render the expanded minimap as a ROLLING WHEEL (iOS-picker feel): the focused row is
    centered + bright with ▶…◀ marks; neighbors fade by distance (default → dim) so the
    column reads as a drum that rolls as focus moves; the focused message's FULL text sits
    in a bar at the bottom. `rows` = MinimapRail.expanded() output (RailRow list); `focus_pos`
    = index WITHIN rows of the focused row. Returns [(text, style)] box lines. Pure."""
    w = max(20, int(width))
    inner = w - 4
    out = [("╭─ messages " + "─" * max(0, w - 13) + "╮", "accent")]
    for i, r in enumerate(rows):
        dist = abs(i - focus_pos)
        label = (r.label or "")[:inner - 4]
        if getattr(r, "focused", False) or i == focus_pos:
            body = f"▶ {label}".ljust(inner - 2) + "◀"
            style = "accent"
        else:
            body = f"  {label}"
            style = "default" if dist == 1 else "dim"   # nearest neighbors brighter than far
        out.append(("│ " + body.ljust(inner)[:inner] + " │", style))
    out.append(("├" + "─" * (w - 2) + "┤", "dim"))
    full = (focused_full or "").strip()
    full = full if len(full) <= inner else full[: inner - 1] + "…"
    out.append(("│ " + full.ljust(inner)[:inner] + " │", "string"))
    out.append(("│ " + "↑↓ roll · ⏎ go · esc close".ljust(inner)[:inner] + " │", "dim"))
    out.append(("╰" + "─" * (w - 2) + "╯", "accent"))
    return out


# Shimmer animation: a bright band sweeps across a bar of block characters.
# Each frame shifts the bright position. Width = 8 chars, 16 frames.
_SHIMMER_BLOCKS = "░▒▓█▓▒░ "
_SHIMMER_WIDTH = 8

def shimmer_bar(tick: int) -> str:
    pos = (tick % (_SHIMMER_WIDTH * 2)) if motion_enabled() else 0   # freeze if reduced-motion
    chars = []
    for i in range(_SHIMMER_WIDTH):
        dist = abs(i - (pos % _SHIMMER_WIDTH))
        if dist == 0:
            chars.append("█")
        elif dist == 1:
            chars.append("▓")
        elif dist == 2:
            chars.append("▒")
        else:
            chars.append("░")
    return "".join(chars)


def spinner_frame(tick: int) -> str:
    """Return the spinner glyph for a given tick (use time.time()*10 as tick).
    Freezes to a single frame under reduced-motion."""
    return _frame(_SPINNER_FRAMES, tick)


def pulse_frame(tick: int) -> str:
    """Syntra's brand-mark pulse for the 'working with <model>' window title."""
    return _PULSE_FRAMES[int(tick) % len(_PULSE_FRAMES)]


def permission_status_lines(*, auto_approve: bool, sandbox: str = "",
                            session_grants=None, always_grants=None) -> list:
    """Describe the CURRENT permission posture from LIVE state — not a hardcoded blurb.

    Every line is derived from a real fact so it can't drift into a false claim:
      - `auto_approve`: the live toggle (run_goal.get_toggle('auto_approve')).
      - `sandbox`: the active OS sandbox name ("bubblewrap"/"Seatbelt") or "" when NONE is
        present — the "shell isolated" vs "shell runs on the HOST" line is chosen by this,
        so it only promises isolation when a sandbox actually exists.
      - `session_grants` / `always_grants`: the PermissionStore's real granted tool-name sets,
        so the user sees exactly which tools are currently waved through (and that grants are
        per-TOOL, not per-command).

    Returns [(text, style)] for the caller to paint. Pure + unit-tested."""
    sess = sorted(session_grants or [])
    always = sorted(always_grants or [])
    out: list = []
    if auto_approve:
        out.append(("🔓 auto-approve — writes & shell commands RUN without a prompt", "string"))
    else:
        out.append(("🔒 ask — you approve each write / edit / shell command", "accent"))
        out.append(("   allow once · session (this tool, whole run) · reject", "dim"))
    # Always-true guarantees, stated plainly (these hold in BOTH modes):
    out.append(("· always enforced: reads/writes confined to the workspace "
                "(.. / symlink / absolute-path escapes blocked)", "dim"))
    out.append(("· always enforced: hard command blocklist (rm -rf /, curl|sh, sudo, …)", "dim"))
    # THE FLOOR — true in BOTH modes, including auto. Secrets are never silently auto-approved
    # (permissions.PermissionStore forces a fresh prompt on .env / .git / keys even under auto).
    out.append(("· always enforced: secrets (.env / .git / keys) ALWAYS ask — even on auto", "dim"))
    # Sandbox posture — chosen by the live probe, so it never over-promises:
    if sandbox:
        out.append((f"· shell isolated by {sandbox} (network off, secrets masked)", "dim"))
    else:
        out.append(("⚠ no OS sandbox (bubblewrap/Seatbelt) — shell commands run on the HOST", "diff_del"))
    # What auto DOES skip — stated truthfully (non-secret in-workspace writes only):
    if auto_approve:
        out.append(("· auto: non-secret in-workspace writes run without a prompt", "dim"))
    # Live grants, if any are active this run:
    if sess:
        out.append((f"· allowed this session: {', '.join(sess)}", "dim"))
    if always:
        out.append((f"· always-allowed (saved): {', '.join(always)}", "dim"))
    out.append(("  /permissions ask  ·  /permissions auto", "comment"))
    return out
