"""Inline-mode native-scrollback rendering primitives (L2, Phase 0).

The PURE, terminal-free core of an opt-in "inline mode" where finalized conversation
turns are committed to the terminal's OWN scrollback (so native mouse-wheel, the
terminal's find, and native copy work on history) while a live input/status region stays
pinned at the bottom. The proven technique (a well-known inline-viewport approach used by
mature terminal UIs): a TOP-ANCHORED DEC scroll region (DECSTBM) on the MAIN screen, scrolled
up so finalized rows spill into native scrollback.

MAKE-OR-BREAK RULE (verified against the VT/DEC scroll-region behavior): native scrollback
is fed ONLY when the scroll region includes row 0 / line 1. A region starting below the
top line DISCARDS scrolled-off lines instead of saving them. So every history region here
is anchored at top=1.

This module emits byte sequences only; it never touches a real terminal — so it is fully
unit-tested. The renderer (Phase 1) writes these to the tty via raw os.write.
"""

from __future__ import annotations

from .tui_model import clip_to_width, wrap_lines

ESC = b"\x1b"
CSI = b"\x1b["


def reserve_region_seq(screen_rows: int, live_rows: int) -> bytes:
    """DECSTBM that pins the HISTORY region to the top of the screen, reserving the
    bottom ``live_rows`` for the live (composer/status/in-progress) region. Returns
    ``CSI 1 ; <hist_bottom> r``. Top is ALWAYS 1 (the scrollback rule)."""
    screen_rows = max(2, int(screen_rows))
    live_rows = max(1, int(live_rows))
    hist_bottom = max(1, screen_rows - live_rows)
    return CSI + f"1;{hist_bottom}r".encode()


def reset_region_seq() -> bytes:
    """``CSI r`` — reset the scroll region to the whole screen. Always emitted after a
    commit so the live region (and teardown) can address the full screen."""
    return CSI + b"r"


def commit_rows_seq(rows, hist_bottom: int, width: int, *, hyperlinks: bool = False) -> bytes:
    """Byte sequence that commits already-wrapped ``rows`` into native scrollback:
    set a top-anchored region (rows 1..hist_bottom), move to the bottom of it, emit
    ``CR LF`` + the (width-clipped) row text for each — each LF at the region bottom
    scrolls the topmost line up into the terminal's scrollback — then reset the region.
    Empty input is a no-op. Pure -> unit-tested.

    ``hyperlinks`` (#176): after clipping each row to width, wrap any URL/file-path span
    in a real OSC-8 hyperlink. Applied POST-clip so the escape's zero display-width can't
    disturb the width math. Inline mode writes raw bytes to the tty, so the links are
    genuinely Cmd/Ctrl-clickable and survive copy-paste."""
    rows = list((rows or []))
    if not rows:
        return b""
    hist_bottom = max(1, int(hist_bottom))
    out = bytearray()
    out += CSI + f"1;{hist_bottom}r".encode()          # region top=1 -> scrollback rule
    out += CSI + f"{hist_bottom};1H".encode()          # cursor to bottom-left of region
    for r in rows:
        vis = clip_to_width(r, max(1, int(width)))
        if hyperlinks:
            from .tui_model import linkify_osc8
            vis = linkify_osc8(vis)
        out += b"\r\n" + vis.encode("utf-8", "replace")
    out += CSI + b"r"                                   # reset region to whole screen
    return bytes(out)


def prewrap_turn(text: str, width: int) -> list:
    """Pre-wrap a finalized turn's text to fixed-width rows BEFORE committing, so the
    rows stay put (no terminal soft-wrap surprises, no re-wrap on resize for v1).
    Pure -> unit-tested."""
    width = max(1, int(width))
    rows: list = []
    for ln in (text or "").split("\n"):
        rows.extend(wrap_lines(ln, width) or [""])
    return rows


def split_committable(buf: str) -> tuple:
    """Split a streaming buffer into (complete_lines, partial_tail). Only COMPLETE lines
    (terminated by '\\n') may be committed to native scrollback — the partial last line
    stays in the live region until its newline arrives, so the terminal never shows a
    half-line that later changes (the "never commit a partial line" rule). Pure."""
    s = buf or ""
    if "\n" not in s:
        return ([], s)
    *lines, tail = s.split("\n")
    return (lines, tail)


class InlineSession:
    """Drives inline mode on the MAIN screen (never the alternate screen): reserves the
    bottom ``live_rows`` for a live region, commits finalized turns up into the terminal's
    NATIVE scrollback, and repaints the live region in place. The full transcript is
    retained by the caller's model (this only renders) — so model+keyboard features
    (search/copy/retry) still reach all history; see [[project-syntra-tui-improvements]].

    ``writer`` (bytes->None) and ``size_fn`` (->(rows,cols)) are injected so the byte
    emission is fully unit-testable; the raw-termios setup is isolated behind ``raw=`` and
    only runs against a real tty."""

    def __init__(self, *, live_rows: int = 3, writer=None, size_fn=None, fd: int = 1,
                 hyperlinks: bool = True):
        self.live_rows = max(1, int(live_rows))
        self._fd = fd
        self._writer = writer or (lambda b: __import__("os").write(fd, b))
        self._size_fn = size_fn or self._default_size
        self._saved_termios = None
        self._started = False
        # #176: emit real OSC-8 hyperlinks for URLs/file paths in committed turns. On by
        # default in inline mode (raw byte stream → genuinely clickable + copy-safe).
        self.hyperlinks = bool(hyperlinks)

    def _default_size(self):
        import shutil
        sz = shutil.get_terminal_size((80, 24))
        return (sz.lines, sz.columns)

    def _w(self, b: bytes) -> None:
        try:
            self._writer(b)
        except Exception:  # noqa: BLE001 - a write must never crash the session
            pass

    def size(self):
        rows, cols = self._size_fn()
        return (max(2, int(rows)), max(1, int(cols)))

    def hist_bottom(self):
        rows, _ = self.size()
        return max(1, rows - self.live_rows)

    def live_top(self):
        rows, _ = self.size()
        return max(1, rows - self.live_rows + 1)

    # ---- lifecycle ----------------------------------------------------------
    def start(self, *, raw: bool = True) -> None:
        """Enter inline mode: put the tty in cbreak/no-echo (raw) WITHOUT switching to the
        alternate screen, then reserve the top history region. ``raw=False`` skips termios
        (for tests / non-tty)."""
        if raw:
            self._enter_raw()
        rows, _ = self.size()
        self._w(reserve_region_seq(rows, self.live_rows))
        self._started = True

    def stop(self, *, raw: bool = True) -> None:
        """Leave inline mode cleanly: reset the scroll region, move below the live region,
        restore termios. NEVER emits rmcup/alt-screen-exit (we never entered it)."""
        self._w(reset_region_seq())
        rows, _ = self.size()
        self._w(CSI + f"{rows};1H".encode())   # park cursor at the bottom
        self._w(b"\r\n")
        if raw:
            self._restore_raw()
        self._started = False

    # ---- rendering ----------------------------------------------------------
    def commit_turn(self, text: str) -> None:
        """Pre-wrap a finalized turn to the current width and scroll it up into NATIVE
        scrollback (above the live region). The caller keeps it in its transcript model."""
        _, cols = self.size()
        rows = prewrap_turn(text, cols)
        if rows:
            self._w(commit_rows_seq(rows, self.hist_bottom(), cols,
                                    hyperlinks=self.hyperlinks))

    def draw_live(self, lines) -> None:
        """Repaint the pinned bottom live region (composer + status + in-progress turn) in
        place: position at the live top, clear-to-EOL + write each line. Cleared rows keep
        the region from showing stale text when content shrinks."""
        _, cols = self.size()
        top = self.live_top()
        lines = list(lines or [])[: self.live_rows]
        for i in range(self.live_rows):
            text = lines[i] if i < len(lines) else ""
            self._w(CSI + f"{top + i};1H".encode())          # absolute position
            self._w(CSI + b"K")                              # clear to end of line
            if text:
                self._w(clip_to_width(text, cols).encode("utf-8", "replace"))

    def on_resize(self) -> None:
        """SIGWINCH: re-reserve the region for the new size (committed scrollback stays
        fixed at its old width — v1 accepts this, the common inline-viewport tradeoff)."""
        rows, _ = self.size()
        self._w(reset_region_seq())
        self._w(reserve_region_seq(rows, self.live_rows))

    # ---- raw tty (isolated; only against a real terminal) -------------------
    def _enter_raw(self) -> None:
        try:
            import termios, tty
            self._saved_termios = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001 - non-tty / unsupported -> run without raw
            self._saved_termios = None

    def _restore_raw(self) -> None:
        if self._saved_termios is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_termios)
            except Exception:  # noqa: BLE001
                pass
            self._saved_termios = None
