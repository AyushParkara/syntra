"""Chat widget — conversation stream + input box.

The primary panel. Shows message bubbles with tool calls, code blocks,
timestamps. Has its own scrollable transcript and input editor.
"""

from __future__ import annotations

from typing import ClassVar

from ..widget import Widget, RenderLine
from ..tui_model import Transcript, render_bubbles, clip_to_width
from ..input_editor import InputEditor


def at_mention_token(text: str, cursor: int):
    """If the cursor sits within an '@'-mention being typed (a leading '@' followed by
    non-space chars), return ``(at_start, word_end, partial)`` where `partial` is the
    chars after '@' up to the cursor. Else None. Pure -> unit-tested (M3)."""
    if not (0 <= cursor <= len(text or "")):
        return None
    i = cursor
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    if i >= len(text) or text[i] != "@":
        return None
    j = cursor
    while j < len(text) and not text[j].isspace():
        j += 1
    return (i, j, text[i + 1:cursor])


class ChatWidget(Widget):
    kind = "chat"
    focusable = True

    def __init__(self, *, title: str = "chat", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self.transcript = Transcript()
        self.editor = InputEditor()
        self._w = 80
        self._h = 20
        self.tab = "chat"  # "chat" | "plan" | "search" | "memory"
        # #216: real in-transcript incremental search — TranscriptSearch finds every
        # occurrence, tracks a current match for n/N nav + an X/total counter, over a
        # snapshot of the transcript's rendered lines (rebuilt as the query changes).
        from ..tui_model import TranscriptSearch as _TS
        self.search = _TS()
        self._search_query = ""          # last query the engine was built for (dedup rebuilds)
        self.plan_steps: list = []      # set by the TUI on each run
        self.memory_items: list = []    # durable constraints
        self.last_row_to_msg: dict = {} # screen-row -> message index (for clicks)
        self.last_row_to_text: dict = {} # BUG2: screen-row -> rendered row text (click acts on the DRAWN affordance)
        # Live "working" cue (P5): the TUI sets these while a run is active so the
        # chat never looks frozen — an animated spinner rides the separator line.
        self.working: bool = False
        self.working_tick: int = 0
        self.working_label: str = "working"
        self.working_model: str = ""     # active role's model; shown by a fan-out worker status
        self.working_elapsed: str = ""   # set by the loop; shown in the working cue
        self.working_tokens: int = 0     # F53: live token count for the animated cue
        self.working_effort: str = ""    # F53: active reasoning effort for the cue
        self.stall_seconds: float = 0.0  # #233: seconds since the last token/tool activity
                                         # (tui2 sets it each frame); drives fade-to-red
        from ..tui_model import HeightRatchet as _HR
        self._feed_ratchet = _HR()       # #230a: grow-only reserve for the live feed zone so
                                         # the transcript doesn't reflow-jump as tools come/go
        self.running_agents: int = 0     # #15: live "N agents" count near the chatbox
        self.running_tools: int = 0      # #15: live "N tools" count near the chatbox
        self.action_feed = None          # optional ActionFeed (set by tui2); the rolling
                                         # Tool(arg) play-by-play, painted just above the input
        self.plan_ribbon = None          # optional PlanRibbon (set by tui2); plan-mode banner
                                         # painted at the top during analyze/plan phases
        # Minimap rail (msg navigator): the widget builds + draws the collapsed ticks in
        # its right-edge column during render, and stashes the model + geometry here so the
        # TUI layer (tui2) can hover/click-test it without recomputing (no drift).
        self._rail = None                # the live MinimapRail model (or None when empty)
        self._rail_col: int = -1         # widget-local column the rail draws in
        self._rail_top: int = 1          # widget-local row where rail row 0 sits (below tabs)
        self._rail_content_h: int = 0    # number of content rows the rail spans
        self._rail_band_top: int = 0     # offset of the centered compact-rail band within content
        # Draggable scrollbar geometry (stashed at render so the mouse layer can drag the thumb
        # without recomputing): column, top row, content height, thumb position+size, total lines.
        self._sb_col: int = -1
        self._sb_top: int = 1
        self._sb_content_h: int = 0
        self._sb_thumb_pos: int = 0
        self._sb_thumb_size: int = 0
        self._sb_total: int = 0
        # #20: cache the rendered bubbles so a 100+-message transcript isn't re-wrapped +
        # re-syntax-highlighted on EVERY frame (the draw loop redraws each tick for the
        # working glyph). Busted by a cheap signature; the live '✶ thinking' twinkle still
        # animates because we only cache when nothing is streaming.
        self._bub_cache = None
        self._bub_sig = None
        self.effort_level: str = ""     # active reasoning effort (P19b input gradient)
        self.anim_tick: int = 0         # frame counter for idle animations
        self._sep_row: int = -1         # widget-local row of the separator (P4 click)
        # multi-row input layout (P27), recomputed each render
        self._input_lines: list = ["❯ "]
        self.input_height: int = 1
        self._cursor_row: int = 0       # row within the input block
        self._cursor_col: int = 2       # screen column of the cursor
        # navigable "/" command palette (user [158]: list all + toggle inline)
        self._slash_idx: int = 0
        self._slash_matches: list = []
        self._slash_prefix: str = ""
        # When a modal/overlay owns input (model picker, effort, wizard…), the chat must
        # NOT also render its "/" palette behind it — that leaked through the overlay box
        # (e.g. "…/ routing"). The TUI sets this each frame while a modal is open.
        self.suppress_palette: bool = False
        # inline "@" file-mention autocomplete (M3): popup while typing, mid-line
        self._at_idx: int = 0
        self._at_matches: list = []
        self._at_prefix = None
        self._wsfiles = None        # cached workspace file list (lazy)

    _TABS: ClassVar[list[str]] = ["chat", "plan", "search", "memory"]
    # Max ticks in the message-navigator rail: a short centered strip, NOT a full-height
    # column. Few messages → exactly that many; many → a sampled subset across the same band.
    RAIL_MAX_TICKS = 9

    def _slash_menu(self):
        """Matches for the open '/' palette, or [] if not typing a bare slash command.
        A lone '/' lists ALL commands; typing narrows. Selection resets when the
        prefix changes so a fresh list starts at the top."""
        raw = (self.editor.text or "")
        if not raw.startswith("/") or " " in raw:
            self._slash_matches = []
            return []
        # F33: use _all_commands() (built-ins + plugin/optional) — the same source /help uses —
        # so the in-chat '/' palette also lists plugin-registered commands, not just built-ins.
        from ..commands import _all_commands
        ms = [c for c in _all_commands() if c.name.startswith(raw.lower())]
        if raw != self._slash_prefix:
            self._slash_idx = 0
            self._slash_prefix = raw
        if ms:
            self._slash_idx = max(0, min(self._slash_idx, len(ms) - 1))
        self._slash_matches = ms
        return ms

    def _slash_rows(self):
        """Render rows for the open palette: a windowed, navigable list with the
        selected row marked. Returns (text, style) tuples (empty if closed)."""
        ms = self._slash_menu()
        if not ms:
            return []
        from ..commands import command_usage
        cap = max(1, min(7, self._h - 5))
        n = len(ms)
        show = min(cap, n)
        start = 0 if n <= show else max(0, min(self._slash_idx - show + 1, n - show))
        rows = []
        for i in range(start, start + show):
            c = ms[i]
            u = command_usage(c)
            sel = i == self._slash_idx
            label = f" {'▌' if sel else ' '}{c.name}  — {c.desc}" + (f"  · {u}" if u else "")
            rows.append((label, "accent" if sel else "dim"))
        rows.append((f"   {self._slash_idx + 1}/{n} · ↑↓ select · Tab fill · Enter run", "dim"))
        return rows

    def fill_slash(self) -> bool:
        """Tab-completion: replace the input with the currently-SELECTED slash command (+ a
        trailing space for args). Returns True if it filled (the palette was open with a match);
        the trailing space then auto-closes the palette (the menu requires a bare, space-free '/x').
        For a lone '/' or a unique prefix, this completes to the highlighted command."""
        ms = self._slash_menu()
        if not ms:
            return False
        name = (getattr(ms[self._slash_idx], "name", "") or "").strip()
        if not name:
            return False
        self.editor.clear()
        self.editor.insert(name + " ")
        return True

    # ---- inline @ file-mention autocomplete (M3) ----------------------------
    def _workspace_files(self) -> list:
        """Lazily-cached workspace file list for the @-mention popup."""
        if self._wsfiles is None:
            try:
                import os
                from ..files import list_workspace_files
                self._wsfiles = list_workspace_files(os.getcwd())
            except Exception:  # noqa: BLE001 - popup must never crash the input
                self._wsfiles = []
        return self._wsfiles

    def _at_menu(self) -> list:
        """Matches for an open inline '@' file-mention popup, or [] when the cursor is
        not inside an @-mention. Fuzzy-filters workspace files by what's typed."""
        tok = at_mention_token(self.editor.text or "", self.editor.cursor)
        if tok is None:
            self._at_matches = []
            self._at_prefix = None
            return []
        start, _end, partial = tok
        files = self._workspace_files()
        if partial:
            words = partial.lower().split()
            if len(words) == 1:
                import difflib
                ms = difflib.get_close_matches(partial, files, n=20, cutoff=0.3)
            else:
                ms = [f for f in files if all(w in f.lower() for w in words)][:20]
        else:
            ms = list(files)
        ms = ms[:50]
        key = (start, partial)
        if key != self._at_prefix:
            self._at_idx = 0
            self._at_prefix = key
        if ms:
            self._at_idx = max(0, min(self._at_idx, len(ms) - 1))
        self._at_matches = ms
        return ms

    def _at_rows(self) -> list:
        """Windowed, navigable rows for the open @-mention popup (empty if closed)."""
        ms = self._at_menu()
        if not ms:
            return []
        cap = max(1, min(7, self._h - 5))
        n = len(ms)
        show = min(cap, n)
        start = 0 if n <= show else max(0, min(self._at_idx - show + 1, n - show))
        rows = []
        for i in range(start, start + show):
            sel = i == self._at_idx
            rows.append((f" {'▌' if sel else ' '}@ {ms[i]}", "accent" if sel else "dim"))
        rows.append((f"   {self._at_idx + 1}/{n} · ↑↓ select · Tab/Enter insert · #L1-9 ranges",
                     "dim"))
        return rows

    def fill_at(self) -> bool:
        """Replace the active @token span with the selected file path (+ trailing space).
        Mid-line safe: only the @token is replaced, surrounding text is preserved."""
        ms = self._at_menu()
        if not ms:
            return False
        tok = at_mention_token(self.editor.text or "", self.editor.cursor)
        if tok is None:
            return False
        start, end, _ = tok
        path = ms[self._at_idx % len(ms)]
        self.editor._snapshot()
        self.editor.text = self.editor.text[:start] + path + " " + self.editor.text[end:]
        self.editor.cursor = start + len(path) + 1
        self._at_matches = []
        return True

    def next_tab(self) -> None:
        i = self._TABS.index(self.tab) if self.tab in self._TABS else 0
        self.tab = self._TABS[(i + 1) % len(self._TABS)]

    def prev_tab(self) -> None:
        i = self._TABS.index(self.tab) if self.tab in self._TABS else 0
        self.tab = self._TABS[(i - 1) % len(self._TABS)]

    def add(self, role: str, text: str) -> None:
        self.transcript.add(role, text)

    def append_stream(self, chunk: str) -> None:
        self.transcript.append_stream(chunk)

    def end_stream(self) -> None:
        self.transcript.end_stream()
        self._bub_cache = None      # force a fresh render when a stream finalizes

    # ── message navigator (the /msgs overlay + the right-edge minimap rail) ──
    def user_msg_rail_index(self, label_width: int = 60):
        """The rail's data: each USER message as (transcript_index, label). Source is the live
        transcript (the bubbles actually on screen), so a click maps straight to a scroll via
        scroll_to_message. Whitespace-collapsed + width-truncated labels."""
        import re as _re
        out = []
        for i, m in enumerate(self.transcript.messages):
            if getattr(m, "role", "") != "user":
                continue
            text = _re.sub(r"\s+", " ", str(getattr(m, "text", "") or "")).strip()
            if not text:
                continue
            label = text if len(text) <= label_width else text[: label_width - 1].rstrip() + "…"
            out.append((i, label))
        return out

    def scroll_to_message(self, msg_index: int) -> bool:
        """Scroll the transcript so the message at transcript index `msg_index` is at the top of
        the viewport (read-only navigation, used by the rail + /msgs). Disables follow so it
        stays put. Returns True if it found a line for that message. Uses the same line→message
        map (line_msg_index) the click-handler already relies on."""
        bubbles = self._render_bubbles_cached()
        idx_map = getattr(bubbles, "line_msg_index", []) or []
        target_line = next((ln for ln, mi in enumerate(idx_map) if mi == msg_index), None)
        if target_line is None:
            return False
        self.transcript._follow = False
        self.transcript.scroll = max(0, min(target_line, self.transcript.max_scroll()))
        return True

    # ── #216: in-transcript incremental search ──
    def sync_search(self, query: str) -> None:
        """Rebuild the search over the CURRENT rendered transcript for `query` (only when it
        changed — the engine re-finds and resets to the first match). Cheap to call every
        keystroke: the rendered-line snapshot is the same one the paint already builds."""
        q = query or ""
        if q == self._search_query:
            return
        self._search_query = q
        self.search.set_lines(self.transcript.rendered_lines())
        self.search.set_query(q)
        self._scroll_to_current_match()

    def search_next(self) -> bool:
        """Advance to the next match (wraps) and scroll it into view. Returns True if there
        were matches to move through."""
        if self.search.total() == 0:
            return False
        self.search.next()
        self._scroll_to_current_match()
        return True

    def search_prev(self) -> bool:
        if self.search.total() == 0:
            return False
        self.search.prev()
        self._scroll_to_current_match()
        return True

    def _scroll_to_current_match(self) -> None:
        """Scroll the transcript so the current match line is visible (centered). No-op when
        there's no match. Uses the transcript's own height-aware scroll_to."""
        line = self.search.current_line()
        if line is None:
            return
        self.transcript._follow = False
        self.transcript.scroll_to(int(line), max(1, self._h - 2))

    def _render_bubbles_cached(self):
        """render_bubbles() memoized on a cheap signature (#20: kill the per-frame re-wrap
        of the whole transcript). A live '✶ thinking' streaming line still animates — when
        anything is streaming we render fresh with the tick; otherwise we reuse the cache."""
        tr = self.transcript
        msgs = tr.messages
        streaming = bool(msgs and msgs[-1].role == "assistant_stream")
        # signature of everything that changes the COMMITTED render (not the tick)
        sig = (len(msgs), self._w, tr.trace_collapsed,
               frozenset(tr.collapsed), frozenset(tr.expanded),
               # last message identity+length so a growing/edited tail busts the cache
               (msgs[-1].role, len(msgs[-1].text)) if msgs else None,
               getattr(msgs[-1], "variant_idx", 0) if msgs else 0)
        if streaming:
            # animate live: render fresh with the current tick (the streaming tail is small)
            self._bub_cache = None
            self._bub_sig = None
            return render_bubbles(msgs, self._w, collapsed=tr.collapsed,
                                  trace_collapsed=tr.trace_collapsed, tick=self.working_tick,
                                  expanded=tr.expanded, autofold_threshold=tr.AUTOFOLD_THRESHOLD)
        if sig != self._bub_sig or self._bub_cache is None:
            self._bub_cache = render_bubbles(msgs, self._w, collapsed=tr.collapsed,
                                             trace_collapsed=tr.trace_collapsed, tick=0,
                                             expanded=tr.expanded,
                                             autofold_threshold=tr.AUTOFOLD_THRESHOLD)
            self._bub_sig = sig
        return self._bub_cache

    def render(self, width: int, height: int) -> list[RenderLine]:
        self._w = max(10, width)
        self._h = max(4, height)

        # Live agent/tool zone — render its pure models ONCE up-front so the heights can be
        # reserved out of the transcript budget. This zone sits JUST ABOVE the input box (so
        # the input ❯ stays the bottom-most row / cursor at the very bottom): a status line
        # (plan-mode + active agent) then the live Tool(arg) action feed. Empty when idle.
        self._feed_cache = (self.action_feed.render(self._w, tick=self.working_tick)
                            if self.action_feed is not None else [])
        self._status_cache = None
        _rib = getattr(self, "plan_ribbon", None)
        if _rib is not None:
            # Only show a real FAN-OUT WORKER ('·' lane marker: sub·1 / agent·4 / plan·grok)
            # as the active agent — the pipeline roles (planner/executor/reviewer) ARE the
            # phase, already shown by plan_ribbon. Elapsed lives on the working line, not here
            # (no duplication).
            _lbl = self.working_label or ""
            _worker = _lbl if (self.working and "·" in _lbl) else ""
            self._status_cache = _rib.status_line(
                agent=_worker, model=getattr(self, "working_model", ""),
                tick=self.working_tick)

        lines: list[RenderLine] = []

        # tab bar: CHAT | PLAN | SEARCH | MEMORY — LIVE: the active tab's glyph twinkles
        # and each tab is colored (active bright/bold, others dim) via spans (#9).
        from ..tui_model import tab_bar_row
        tab_line, _tab_spans = tab_bar_row(
            list(self._TABS), self.tab, tick=self.anim_tick, width=self._w)
        # clip spans to the painted width so none paint past the pane edge
        _spans = [(s, min(e, self._w)) + tuple(sp[2:])
                  for sp in _tab_spans for s, e in [(sp[0], sp[1])] if s < self._w]
        lines.append(RenderLine(clip_to_width(tab_line, self._w), "accent", spans=_spans))

        # NOTE: the plan-mode phase + active agent are NOT shown at the top — they render in
        # the live agent/tool zone just above the input (status line), see the end of render().

        # Lay out the input box first — it GROWS up to a few rows for long/multi-line
        # messages (P27), borrowing height from the transcript so nothing overlaps.
        from ..tui_model import input_rows as _input_rows
        in_cap = max(1, min(6, self._h - 4))  # keep tab+sep+>=1 content visible
        self._input_lines, self._cursor_row, self._cursor_col = _input_rows(
            self.editor.display(), self.editor.cursor, self._w, max_rows=in_cap)
        self.input_height = len(self._input_lines)

        # navigable "/" palette rows (user [158]) and inline "@" file-mention rows (M3)
        # — reserve their height so the transcript shrinks and nothing overlaps. They're
        # mutually exclusive (slash needs a leading '/', @ needs a mid-line mention).
        slash_rows = [] if self.suppress_palette else self._slash_rows()
        at_rows = [] if self.suppress_palette else (self._at_rows() if not slash_rows else [])
        # the live agent/tool zone (status line + Tool(arg) feed, rendered just above the
        # input) reserves transcript height so it never pushes the input off-screen.
        _extra = len(getattr(self, "_feed_cache", []) or [])
        if getattr(self, "_status_cache", None):
            _extra += 1
        # #230a: hold the live-zone reserve at its high-water mark WHILE a run is active so
        # the transcript's content height stays stable — a feed that shrinks then re-grows
        # (tool ends, next starts) no longer reflows + jitters the bubbles/spinner. Released
        # to the real count once idle (the reserve collapses so the idle layout is unchanged).
        _extra = self._feed_ratchet.height(_extra, active=bool(self.working))
        # tab bar + separator + input box (+ palette/mention rows + below-input zone)
        content_h = max(1, self._h - 2 - self.input_height - len(slash_rows)
                        - len(at_rows) - _extra)

        if self.tab == "chat":
            # transcript
            self.transcript.width = self._w
            bubbles = self._render_bubbles_cached()
            idx_map = getattr(bubbles, "line_msg_index", []) or []
            # Keep all scroll math in the SAME line space we actually paint
            # (render_bubbles count + content height). The old code clamped against
            # transcript._rendered(), a different count, so scroll/pin felt broken.
            self.transcript.sync_view(len(bubbles), content_h)
            if self.transcript._follow:
                self.transcript.scroll = self.transcript.max_scroll()
            self.transcript.scroll = max(0, min(
                self.transcript.scroll, self.transcript.max_scroll()))
            start = self.transcript.scroll
            view = bubbles[start:start + content_h]
            total = len(bubbles)
            # Right edge = TWO ADJACENT (non-overlapping) columns:
            #   • the SCROLLBAR at the absolute right column (_w-1): █ thumb / │ track,
            #     sized to the viewport — drag it / wheel over it to scroll the chat. Shown
            #     whenever the chat overflows.
            #   • the message RAIL one column inboard (_w-2): one tick per user message
            #     (━ current / ─ a message / · track) — click/wheel it for the navigator.
            #     Shown whenever the user has sent messages.
            # They never share a column (the earlier merge removed the scrollbar — wrong;
            # the user needs BOTH: a real scroll AND the msg navigator).
            from .minimap import MinimapRail
            _rail_index = self.user_msg_rail_index()
            show_rail = bool(_rail_index) and content_h > 2
            show_sb = total > content_h and content_h > 2
            _sb_col = (self._w - 1) if show_sb else -1
            # rail sits inboard of the scrollbar when both show; else at the right edge.
            _rail_col = (self._w - 2 if show_sb else self._w - 1) if show_rail else -1
            thumb_pos = thumb_size = 0
            if show_sb:
                thumb_size = max(1, content_h * content_h // total)
                span = max(1, total - content_h)
                thumb_pos = min(content_h - thumb_size,
                                start * (content_h - thumb_size) // span)
            self._sb_col = _sb_col
            self._sb_top = 1
            self._sb_content_h = content_h
            self._sb_thumb_pos = thumb_pos
            self._sb_thumb_size = thumb_size
            self._sb_total = total
            self._rail = None
            self._rail_col = -1
            _tick_by_row = {}
            if show_rail:
                self._rail = MinimapRail(_rail_index, pane_height=content_h,
                                         pane_right=_rail_col)
                self._rail_col = _rail_col
                self._rail_top = 1
                self._rail_content_h = content_h
                _cur_ti = idx_map[start] if start < len(idx_map) else -1
                # COMPACT, vertically-CENTERED band — CAPPED so it never stretches the whole
                # pane top-to-bottom as the chat grows (user). At most RAIL_MAX_TICKS dots: with
                # fewer messages the band is exactly that many (tight, contiguous, centered);
                # with more, the band samples a subset across the same short strip.
                _cap = min(content_h, self.RAIL_MAX_TICKS)
                _cticks, self._rail_band_top = self._rail.compact_ticks(
                    current_turn_index=_cur_ti, max_band=_cap)
                for _tk in _cticks:
                    _tick_by_row[_tk.row] = _tk
            # reserve one column per visible strip, on the LEFT of the content.
            _reserved = (1 if show_sb else 0) + (1 if show_rail else 0)
            # build screen-row -> message-index map (row 0 = first content line,
            # i.e. just below the tab bar). The tab bar occupies render-line 0.
            self.last_row_to_msg = {}
            self.last_row_to_text = {}   # BUG2: row → drawn text, so a click acts on the shown affordance
            all_spans = getattr(bubbles, "line_spans", []) or []
            for vi, (text, role) in enumerate(view):
                src = start + vi
                paint_w = max(1, self._w - _reserved)
                t = text[:paint_w]
                _rstyle = ""
                _extra_spans = []
                if _reserved:
                    t = t[:paint_w].ljust(paint_w)
                    # rail glyph (inboard column) — styled via a span so it keeps its own
                    # color independent of the row content.
                    if show_rail:
                        _tk = _tick_by_row.get(vi)
                        if _tk is not None and _tk.current:
                            _g, _gs = "━", "accent"
                        elif _tk is not None:
                            _g, _gs = "─", "default"
                        else:
                            _g, _gs = "·", "dim"
                        t += _g
                        _extra_spans.append((len(t) - 1, len(t), _gs))
                    # scrollbar (absolute right column) — █ thumb / │ track via rstyle.
                    if show_sb:
                        _is_thumb = thumb_pos <= vi < thumb_pos + thumb_size
                        t += "█" if _is_thumb else "│"
                        _rstyle = "accent" if _is_thumb else "dim"
                # clip intra-line syntax spans to the painted content width so they never
                # paint onto the rail/scrollbar columns or past the pane edge (M1).
                _raw = all_spans[src] if src < len(all_spans) else []
                _spans = []
                for _sp in _raw:
                    s, e, st = _sp[0], _sp[1], _sp[2]
                    e = min(e, paint_w)
                    if s < paint_w and s < e:
                        _spans.append((s, e, st) + tuple(_sp[3:]))   # preserve emph flag
                _spans.extend(_extra_spans)
                lines.append(RenderLine(t, role, rstyle=_rstyle, spans=_spans))
                if src < len(idx_map):
                    # +1 because the tab bar is render-line 0 inside this widget
                    self.last_row_to_msg[vi + 1] = idx_map[src]
                    self.last_row_to_text[vi + 1] = text   # BUG2: the raw drawn text for this row
        elif self.tab == "plan":
            lines.append(RenderLine(" PLAN", "dim"))
            if self.plan_steps:
                # checklist glyphs (non-diamond squares): ▣ in-progress, ☐ pending, ✓ done
                for s in self.plan_steps:
                    mark = {"done": "✓", "running": "▣", "failed": "✗",
                            "pending": "☐", "skipped": "·"}.get(
                        getattr(s, "status", ""), "☐")
                    desc = getattr(s, "description", "")
                    style = "diff_add" if mark == "✓" else (
                        "accent" if mark == "▣" else "default")
                    lines.append(RenderLine(f"  {mark} {desc}"[:self._w], style))
                # tally line (reference: "… +N pending, M completed")
                _done = sum(1 for s in self.plan_steps if getattr(s, "status", "") == "done")
                _pending = len(self.plan_steps) - _done
                lines.append(RenderLine(
                    f"  … +{_pending} pending, {_done} completed"[:self._w], "dim"))
            else:
                lines.append(RenderLine("  (no active plan)", "dim"))
        elif self.tab == "memory":
            lines.append(RenderLine(" DURABLE MEMORY", "dim"))
            if self.memory_items:
                lines.extend(RenderLine(f"  • {m}"[:self._w], "default") for m in self.memory_items)
            else:
                lines.append(RenderLine("  (no durable constraints)", "dim"))
            lines.append(RenderLine("", "default"))
            lines.append(RenderLine("  /memory-update <text> to add", "dim"))
        elif self.tab == "search":
            # #216: real in-transcript incremental search — live X/total counter + n/N nav
            # (upgrades the old static message-filter list). Rebuild the engine for the
            # current query, then show the matched lines with the CURRENT one marked.
            query = self.editor.text.strip()
            self.sync_search(query)
            if query:
                total = self.search.total()
                lines.append(RenderLine(f" SEARCH: {query}   [{self.search.label()}]  "
                                        f"· n/N next/prev · Esc close", "accent"))
                if total == 0:
                    lines.append(RenderLine("  (no matches)", "dim"))
                else:
                    rendered = self.transcript.rendered_lines()
                    matches = self.search.matches()
                    cur = self.search.current_index()
                    # a window of matches centered on the current one, so long result sets
                    # stay navigable (the current match is always shown + marked).
                    budget = max(1, content_h - 2)
                    lo = max(0, min(cur - budget // 2, max(0, total - budget)))
                    for mi in range(lo, min(total, lo + budget)):
                        li, a, _b = matches[mi]
                        src = rendered[li] if 0 <= li < len(rendered) else ""
                        # a small context slice around the hit so the row is meaningful
                        ctx0 = max(0, a - 12)
                        snippet = src[ctx0:ctx0 + max(20, self._w - 8)].replace("\n", " ")
                        marker = "▶" if mi == cur else " "
                        style = "accent" if mi == cur else "default"
                        lines.append(RenderLine(f" {marker} {snippet}", style))
            else:
                lines.append(RenderLine(" SEARCH", "dim"))
                lines.append(RenderLine("  type to search the transcript · n/N to step matches",
                                        "dim"))

        while len(lines) < content_h + 1:  # +1 for tab bar
            lines.append(RenderLine("", "default"))

        # (the action feed used to paint here, above the working line; it now lives in the
        # C-hybrid zone BELOW the input box — see the end of render().)

        # separator: working pulse (P5) > jump-to-end (P4) > plain. The separator
        # is at this widget-local row — recorded so a click on it jumps to latest.
        self._sep_row = len(lines)
        sep_style = "dim"
        at_bottom = self.transcript.at_bottom() if self.tab == "chat" else True
        if self.working:
            # F53: the ONE animated live line — twinkling glyph + label · elapsed · ↓tokens
            # · effort, riding the separator just above the chatbox (user: 'text + shape
            # should animate', 'keep the working thing near the chatbox').
            from ..tui_model import activity_working_line, working_verb
            # a real role (planner/executor/…) shows its own name; a generic "working"
            # rotates through Syntra's own playful verbs so the line has life (#15).
            _lbl = self.working_label or "working"
            if _lbl in ("working", "", None):
                _lbl = working_verb(self.working_tick)
            # NOTE: the plan PHASE + active agent are shown in the status line of the live
            # agent/tool zone just ABOVE the input (see end of render), not duplicated here —
            # the working line stays the generic "alive" pulse (verb · elapsed · tokens · esc).
            core = activity_working_line(_lbl, self.working_elapsed, self.working_tokens,
                                         self.working_effort, tick=self.working_tick)
            # surface the live agent/tool counters right here when a fan-out is active (#15)
            _ag = getattr(self, "running_agents", 0) or 0
            _tl = getattr(self, "running_tools", 0) or 0
            _suffix = ""
            if _ag:
                _suffix = f"  ·  {_ag} agent{'s' if _ag != 1 else ''}"
                if _tl:
                    _suffix += f" · {_tl} tool{'s' if _tl != 1 else ''}"
            ind = f" {core}{_suffix}  ·  esc to interrupt "
            sep = "─" * max(0, self._w - len(ind)) + ind
            # #233: if no tokens/tool for a few seconds, the working line eases to red so a
            # possibly-hung run reads as "looks stuck" without a hard error. Resets the moment
            # activity resumes (tui2 zeroes stall_seconds on new tokens).
            from ..tui_model import stall_fade_style
            sep_style = stall_fade_style(self.stall_seconds)
        elif self.tab == "chat" and not at_bottom:
            total = self.transcript._view_total
            below = max(0, total - content_h - self.transcript.scroll)
            pct = min(100, int((self.transcript.scroll + content_h) / max(1, total) * 100))
            ind = f" ▼ {below} below · End to jump to latest ({pct}%) "
            sep = "─" * max(0, self._w - len(ind)) + ind
            sep_style = "accent"
        elif self.effort_level:
            # animated effort "gradient" strip above the input, colored + pulsing
            # per reasoning level (P19b). The gauge's bright cell sweeps with the tick.
            from ..tui_model import effort_bar
            gauge, sep_style = effort_bar(self.effort_level, tick=self.anim_tick)
            ind = f" {gauge} effort "   # gauge already carries the level name
            sep = "─" * max(0, self._w - len(ind)) + ind
        else:
            sep = "─" * self._w
        lines.append(RenderLine(sep[:self._w], sep_style))

        # navigable "/" command palette: a real list (↑↓ select, Tab fill, Enter run),
        # not just a one-line hint (user [158]: "list all + toggle inline").
        for row_text, row_style in slash_rows:
            lines.append(RenderLine(row_text[:self._w], row_style))
        # inline @ file-mention popup rows (M3)
        for row_text, row_style in at_rows:
            lines.append(RenderLine(row_text[:self._w], row_style))

        # ── live agent/tool zone ── the status line (plan-mode + active agent) and the live
        # Tool(arg) action feed render JUST ABOVE the input box, so the input ❯ stays the
        # LAST row and the cursor sits at the very bottom (user: rendering this below the
        # input made it unclear you could still type). The feed carries its own fade-by-age
        # + ▸ fold; _feed_row0 records where it starts so the mouse layer can map a click on
        # a ▸ fold line back to the model.
        if getattr(self, "_status_cache", None):
            _stext, _sstyle = self._status_cache
            lines.append(RenderLine(_stext[:self._w], _sstyle))
        self._feed_row0 = -1
        _feed_rows = getattr(self, "_feed_cache", None) or []
        if _feed_rows:
            self._feed_row0 = len(lines)
            for _ftext, _fstyle, _fspans in _feed_rows:
                # feed rows are (text, style, spans) — a running tool line carries a shimmer span
                # sweeping the filename (#135); other rows have spans=[]. Pass spans through so
                # the paint layer highlights the swept band.
                lines.append(RenderLine(_ftext[:self._w], _fstyle, spans=_fspans))

        # input box — one or more rows (grows/scrolls for long messages, P27), kept LAST so
        # the cursor row is the bottom-most line. The box ITSELF tints to the current EFFORT
        # level (P19b) and stays tinted during a run / while scrolled.
        from ..tui_model import EFFORT_COLORS
        in_style = EFFORT_COLORS.get(self.effort_level, "user") if self.effort_level else "user"
        lines.extend(RenderLine(row[:self._w], in_style) for row in self._input_lines)

        return lines

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        # ── #216: in-transcript search tab — navigate matches while typing edits the query.
        #   Enter / Ctrl-F / ↓  → next match      Shift-Enter equiv (↑) / Ctrl-P → previous
        #   Esc                 → leave search, back to the chat tab
        # Printable chars + Backspace fall through to the normal editor path (which edits the
        # query; render() re-syncs the engine). This keeps search INCREMENTAL as you type.
        if self.tab == "search":
            if ch == 27:                                   # Esc → back to chat
                self.tab = "chat"; return True
            if ch in (curses.KEY_ENTER, 10, 13, 6, curses.KEY_DOWN):   # Enter / Ctrl-F / ↓ → next
                self.sync_search(self.editor.text.strip())
                self.search_next(); return True
            if ch in (16, curses.KEY_UP):                  # Ctrl-P / ↑ → previous
                self.sync_search(self.editor.text.strip())
                self.search_prev(); return True
            # everything else (typing, backspace, etc.) falls through to editing the query
        # ── navigable "/" palette (user [158]): when typing /cmd, ↑↓ move the
        # selection, Tab fills it, Enter runs it. Intercept BEFORE history/submit.
        # NOTE: when history recall (↑) PLACES a past "/cmd" into the box, the text now starts
        # with "/" — but the user is browsing history, NOT composing a new command. If the palette
        # captured ↑/↓ here it would trap recall on that entry forever (#136: "arrow history breaks
        # after a / command"). So the palette only owns the arrows while actively TYPING a slash
        # command — i.e. NOT mid history-recall (_history_idx < 0).
        sm = self._slash_matches
        _typing_slash = (self.editor.text.startswith("/") and " " not in self.editor.text
                         and self.editor._history_idx < 0)
        if sm and _typing_slash:
            if ch == curses.KEY_UP:
                self._slash_idx = (self._slash_idx - 1) % len(sm); return True
            if ch == curses.KEY_DOWN:
                self._slash_idx = (self._slash_idx + 1) % len(sm); return True
            if ch == 9:  # Tab = fill the selected command into the input
                c = sm[self._slash_idx % len(sm)]
                self.editor.text = c.name + (" " if getattr(c, "takes_arg", False) else "")
                self.editor.cursor = len(self.editor.text)
                return True
            if ch in (curses.KEY_ENTER, 10, 13):
                c = sm[self._slash_idx % len(sm)]
                # If the user has ALREADY typed the exact command name (e.g. "/effort"),
                # Enter SUBMITS it as-is — even for takes_arg commands, since many take an
                # OPTIONAL arg and submitting bare opens their modal (/effort, /models, …).
                # Only fill-and-wait when Enter is selecting a DIFFERENT (still-prefix) match.
                _typed = self.editor.text.strip()
                if getattr(c, "takes_arg", False) and _typed != c.name:
                    self.editor.text = c.name + " "          # selecting a match → fill + wait
                    self.editor.cursor = len(self.editor.text)
                    return True
                _cmd = _typed if _typed.startswith(c.name) else c.name
                self.editor.history_add(_cmd)               # submit (bare command runs)
                self.editor.clear()
                self._slash_matches = []
                self.transcript.to_bottom()
                self.emit("submit", _cmd)
                return True
        # ── inline "@" file-mention popup (M3): when typing an @mention, ↑↓ move the
        # selection, Tab/Enter insert the chosen path. Recomputed fresh so it can't act
        # on a stale list. Intercepts BEFORE submit so Enter inserts (not sends).
        am = self._at_menu()
        if am:
            if ch == curses.KEY_UP:
                self._at_idx = (self._at_idx - 1) % len(am); return True
            if ch == curses.KEY_DOWN:
                self._at_idx = (self._at_idx + 1) % len(am); return True
            if ch in (9, curses.KEY_ENTER, 10, 13):   # Tab / Enter insert the path
                self.fill_at(); return True
        # Ctrl+Right / Ctrl+Left: on an EMPTY input they switch tabs; while TYPING they
        # move the cursor by WORD like a real terminal (user: 'ctrl arrows should move by
        # word, not 1 letter'). The editor already owns word_left/word_right.
        _CTRL_RIGHT = (getattr(curses, "KEY_SRIGHT", -99), 561)
        _CTRL_LEFT = (getattr(curses, "KEY_SLEFT", -98), 546)
        if ch in _CTRL_RIGHT:
            if self.editor.is_empty():
                self.next_tab()
            else:
                self.editor.word_right()
            return True
        if ch in _CTRL_LEFT:
            if self.editor.is_empty():
                self.prev_tab()
            else:
                self.editor.word_left()
            return True
        # Delete / KEY_DC = forward-delete (today only Backspace deleted). Curses code 330.
        if ch in (getattr(curses, "KEY_DC", 330), 330):
            self.editor.delete_forward(); return True
        # Ctrl+Home / Ctrl+End = jump to the very start / end of the input buffer.
        if ch == 536:   # Ctrl+Home
            self.editor.doc_start(); return True
        if ch == 531:   # Ctrl+End — when typing, doc-end; when empty, jump transcript to latest
            if self.editor.is_empty():
                self.transcript.to_bottom()
            else:
                self.editor.doc_end()
            return True
        if ch in (curses.KEY_ENTER, 10, 13):
            text = self.editor.expand().strip()
            if text:
                self.editor.history_add(text)
                self.editor.clear()
                self.transcript.to_bottom()   # sending re-pins to the latest (P3)
                self.emit("submit", text)
            else:
                # Empty Enter still fires submit("") — the app uses it to APPROVE a
                # pending plan ("press Enter to start"). Was a dead no-op before.
                self.emit("submit", "")
            return True
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.editor.backspace(); return True
        if ch == curses.KEY_UP and (self.editor.is_empty() or self.editor._history_idx >= 0):
            self.editor.history_up(); return True
        if ch == curses.KEY_DOWN and self.editor._history_idx >= 0:
            self.editor.history_down(); return True
        # ↑/↓ with text typed (and NOT in history recall, e.g. "/models" in the box) SCROLLS
        # the transcript so you can look back at previous messages while composing — instead
        # of being swallowed (user: scrolling up got "stuck on /models"). History recall stays
        # ↑ on an EMPTY box; PgUp/PgDn still page.
        if ch == curses.KEY_UP and not self.editor.is_empty() and self.editor._history_idx < 0:
            self.transcript.scroll_up(2); return True
        if ch == curses.KEY_DOWN and not self.editor.is_empty() and self.editor._history_idx < 0:
            self.transcript.scroll_down(2, max(1, self._h - 3)); return True
        if ch == curses.KEY_LEFT:
            self.editor.left(); return True
        if ch == curses.KEY_RIGHT:
            self.editor.right(); return True
        if ch == 7:  # Ctrl-G = jump to the latest message (re-enable follow)
            self.transcript.to_bottom(); return True
        if ch == curses.KEY_HOME or ch == 1:  # Home or Ctrl-A
            self.editor.cursor = 0; return True
        if ch == curses.KEY_END or ch == 5:  # End / Ctrl-E
            # When the input is empty, End jumps the TRANSCRIPT to the bottom
            # (latest message). While typing, it's cursor-to-line-end as usual.
            if not self.editor.text:
                self.transcript.to_bottom(); return True
            self.editor.cursor = len(self.editor.text); return True
        if ch == curses.KEY_PPAGE:
            page = max(1, self._h - 3)
            self.transcript.scroll_up(page); return True
        if ch == curses.KEY_NPAGE:
            page = max(1, self._h - 3)
            self.transcript.scroll_down(page, page); return True
        if ch == 21:  # Ctrl-U = kill from line start to cursor (readline)
            self.editor.kill_to_start(); return True
        if ch == 11:  # Ctrl-K = kill from cursor to line end (readline)
            self.editor.kill_to_end(); return True
        if ch == 15:  # Ctrl-O = newline
            self.editor.newline(); return True
        if ch == 23:  # Ctrl-W = delete word backward (editor owns the boundary logic)
            self.editor.delete_word_left(); return True
        if ch == 26:  # Ctrl-Z = undo (#232 — was unwired; the model had undo() but no key)
            self.editor.undo(); return True
        if ch == 25:  # Ctrl-Y = yank the kill-ring here (#231). tui2 routes Ctrl-Y to the
            self.editor.yank(); return True   # editor only WHEN typing (empty box → copy-last)
        if ch == 6:  # Ctrl-F = search transcript
            self.tab = "search"
            return True
        if ch == 18:  # Ctrl-R = reverse search history (step to the next distinct past entry)
            if self.editor._history:
                start = self.editor._history_idx if self.editor._history_idx >= 0 else len(self.editor._history)
                for i in range(start - 1, -1, -1):
                    h = self.editor._history[i]
                    if h != self.editor.text:
                        self.editor.text = h
                        self.editor.cursor = len(h)
                        self.editor._history_idx = i
                        break
            return True
        if ch == ord("@") and self.editor.is_empty():
            self.emit("open_filepicker", ""); return True
        if ch == 9:  # Tab = complete file path after @
            text = self.editor.text
            at_idx = text.rfind("@")
            if at_idx >= 0:
                partial = text[at_idx + 1:]
                if partial:
                    import glob as _glob
                    matches = _glob.glob(partial + "*")[:1]
                    if matches:
                        completed = matches[0]
                        self.editor.text = text[:at_idx + 1] + completed
                        self.editor.cursor = len(self.editor.text)
                        return True
            return True
        if 32 <= ch < 127:
            self.editor.insert_char(chr(ch)); return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        # click on the tab bar (row 0) switches tabs. Mirror tab_bar_row's layout EXACTLY:
        # each tab is "<icon> <label>" (icon 1 cell + space + label), joined by "  ".
        if y == 0 and (button & curses.BUTTON1_CLICKED or button & curses.BUTTON1_PRESSED):
            pos = 0
            for i, t in enumerate(self._TABS):
                if i:
                    pos += 2  # the "  " join
                label = t.upper() if t == self.tab else t
                seg_len = 2 + len(label)  # icon + space + label
                if pos <= x < pos + seg_len:
                    self.tab = t
                    return True
                pos += seg_len
            return True
        # click the separator's "jump to latest" affordance (P4)
        if (self._sep_row > 0 and y == self._sep_row
                and (button & curses.BUTTON1_CLICKED or button & curses.BUTTON1_PRESSED)):
            self.transcript.to_bottom(); return True
        # Wheel = fine-grained 2-line steps (snappy, no overshoot on short chats). Each
        # tick repaints immediately at the tui2 layer, so scrolling stays smooth.
        if button & curses.BUTTON4_PRESSED:
            self.transcript.scroll_up(2); return True
        if button & getattr(curses, "BUTTON5_PRESSED", 0):
            self.transcript.scroll_down(2); return True
        return False
