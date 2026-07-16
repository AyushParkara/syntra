"""Input editor model (Track T1) — the prompt box state.

A full editor would add (kill-ring, undo, word-nav, grapheme
segmentation, kitty protocol, autocomplete, history). We port the CORE that an
editor must have and that our `cli/tui.py` was missing entirely (it only did
append + backspace, no cursor):

- flat text buffer + a cursor OFFSET (newlines are literal; multi-line prompt);
- insert-at-cursor, backspace, forward-delete;
- cursor left/right, line home/end, line up/down, doc start/end;
- bracketed-paste -> large pastes become an ATOMIC `[paste #N ...]` marker backed
  by a `pastes` id->content map, expanded on send;
- paste markers behave as single units for cursor movement + deletion, exactly
  treating each marker as one atomic unit.

Deliberately SKIPPED (documented bloat, reference/snippets/tui-patterns.md):
kill-ring, undo stack, word navigation, grapheme segmentation, kitty CSI-u,
autocomplete, input history. Pure + deterministic -> unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from syntra.core.chips import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    PASTE_CHIP_MIN_CHARS,
    PASTE_CHIP_MIN_LINES,
    filter_printable,
)

# Paste marker format: "[paste #1 +123 lines]" or "[paste #2 1234 chars]".
PASTE_MARKER_RE = re.compile(r"\[paste #(\d+)(?: (?:\+\d+ lines|\d+ chars))?\]")


@dataclass
class InputEditor:
    text: str = ""
    cursor: int = 0
    pastes: dict[int, str] = field(default_factory=dict)
    _paste_counter: int = 0
    _undo: list = field(default_factory=list)      # stack of (text, cursor) snapshots
    _redo: list = field(default_factory=list)      # #232: undone states, for redo
    _kill_ring: list = field(default_factory=list)  # #231: killed text (Ctrl-K/U/W) for yank
    _coalescing: bool = False                       # batch consecutive char inserts into one undo
    _history: list = field(default_factory=list)    # sent messages history
    _history_idx: int = -1                          # -1 = current input, 0+ = history position
    _history_saved: str = ""                        # saved current input when browsing history

    _UNDO_LIMIT = 200
    _HISTORY_LIMIT = 500

    _KILL_RING_LIMIT = 60

    def _snapshot(self) -> None:
        """Push the current state for undo, capped. A fresh edit invalidates the redo stack
        (#232) — standard editor semantics: you can't redo past a new change."""
        self._undo.append((self.text, self.cursor))
        if len(self._undo) > self._UNDO_LIMIT:
            self._undo.pop(0)
        if self._redo:
            self._redo = []

    def _kill(self, text: str) -> None:
        """Record killed text on the kill-ring for yank (#231). Empty kills are ignored."""
        if text:
            self._kill_ring.append(text)
            if len(self._kill_ring) > self._KILL_RING_LIMIT:
                self._kill_ring.pop(0)

    # ---- marker awareness ---------------------------------------------------
    def _marker_spans(self) -> list[tuple[int, int, int]]:
        """(start, end, paste_id) for every valid paste marker present in text."""
        spans: list[tuple[int, int, int]] = []
        for m in PASTE_MARKER_RE.finditer(self.text):
            pid = int(m.group(1))
            if pid in self.pastes:
                spans.append((m.start(), m.end(), pid))
        return spans

    def _span_covering(self, pos: int) -> tuple[int, int, int] | None:
        """The marker span strictly containing offset `pos` (start < pos < end)."""
        for start, end, pid in self._marker_spans():
            if start < pos < end:
                return (start, end, pid)
        return None

    def _span_ending_at(self, pos: int) -> tuple[int, int, int] | None:
        for start, end, pid in self._marker_spans():
            if end == pos:
                return (start, end, pid)
        return None

    def _span_starting_at(self, pos: int) -> tuple[int, int, int] | None:
        for start, end, pid in self._marker_spans():
            if start == pos:
                return (start, end, pid)
        return None

    # ---- editing ------------------------------------------------------------
    def insert(self, s: str) -> None:
        self.text = self.text[: self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)
        # editing exits history-recall mode: whatever we recalled is now being changed into a
        # fresh composition (#136). Without this, a recalled "/cmd" would keep _history_idx>=0 and
        # the slash palette would stay suppressed while the user types a new command over it.
        self._history_idx = -1

    def insert_char(self, ch: str) -> None:
        """Insert a single printable char (control chars are ignored)."""
        if ch and (ch == "\n" or ord(ch[0]) >= 32):
            # Coalesce a run of typed chars into ONE undo unit; break the run on
            # whitespace/newline so undo lands on word boundaries.
            if not self._coalescing or ch in (" ", "\n", "\t"):
                self._snapshot()
            self._coalescing = ch not in (" ", "\n", "\t")
            self.insert(ch)

    def newline(self) -> None:
        self._snapshot()
        self._coalescing = False
        self.insert("\n")

    def backspace(self) -> None:
        if self.cursor <= 0:
            return
        self._snapshot()
        self._coalescing = False
        self._history_idx = -1   # editing exits history-recall mode (#136)
        span = self._span_ending_at(self.cursor)
        if span:                                  # delete the whole marker atomically
            start, end, pid = span
            self.text = self.text[:start] + self.text[end:]
            self.cursor = start
            self.pastes.pop(pid, None)
            return
        self.text = self.text[: self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1

    def delete_forward(self) -> None:
        if self.cursor >= len(self.text):
            return
        self._snapshot()
        self._coalescing = False
        self._history_idx = -1   # editing (fwd-delete) exits history-recall mode
        span = self._span_starting_at(self.cursor)
        if span:
            start, end, pid = span
            self.text = self.text[:start] + self.text[end:]
            self.pastes.pop(pid, None)
            return
        self.text = self.text[: self.cursor] + self.text[self.cursor + 1:]

    # ---- cursor movement (markers are atomic) -------------------------------
    def left(self) -> None:
        if self.cursor <= 0:
            return
        new = self.cursor - 1
        cov = self._span_covering(new + 1) or self._span_covering(new)
        if cov and cov[0] < self.cursor:
            new = cov[0]
        self.cursor = max(0, new)

    def right(self) -> None:
        if self.cursor >= len(self.text):
            return
        new = self.cursor + 1
        cov = self._span_covering(self.cursor) or self._span_covering(new - 1)
        if cov and cov[1] > self.cursor:
            new = cov[1]
        self.cursor = min(len(self.text), new)

    @staticmethod
    def _is_word(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    def word_left(self) -> None:
        """Move to the start of the previous word (skip whitespace, then word chars)."""
        i = self.cursor
        while i > 0 and not self._is_word(self.text[i - 1]):
            i -= 1
        while i > 0 and self._is_word(self.text[i - 1]):
            i -= 1
        self.cursor = i

    def word_right(self) -> None:
        """Move to the end of the next word (skip whitespace, then word chars)."""
        n = len(self.text)
        i = self.cursor
        while i < n and not self._is_word(self.text[i]):
            i += 1
        while i < n and self._is_word(self.text[i]):
            i += 1
        self.cursor = i

    def delete_word_left(self) -> None:
        """Delete from the previous word boundary to the cursor (Ctrl+W / Ctrl+Backspace)."""
        start = self.cursor
        self.word_left()
        wb = self.cursor
        if wb < start:
            self._snapshot()
            self._coalescing = False
            self._kill(self.text[wb:start])         # #231: killed word → ring
            self.text = self.text[:wb] + self.text[start:]
            self.cursor = wb
        else:
            self.cursor = start

    def delete_word_right(self) -> None:
        """Delete from the cursor to the next word boundary (Ctrl+Delete). Mirror of
        delete_word_left: cursor stays put, the word AHEAD is removed."""
        start = self.cursor
        self.word_right()
        we = self.cursor
        if we > start:
            self._snapshot()
            self._coalescing = False
            self._kill(self.text[start:we])         # #231: killed word → ring
            self.text = self.text[:start] + self.text[we:]
            self.cursor = start
        else:
            self.cursor = start

    def kill_to_end(self) -> None:
        """Ctrl+K: delete from the cursor to the end of the current line."""
        _, end = self._line_bounds(self.cursor)
        if end > self.cursor:
            self._snapshot()
            self._coalescing = False
            self._kill(self.text[self.cursor:end])  # #231: killed tail → ring
            self.text = self.text[:self.cursor] + self.text[end:]

    def kill_to_start(self) -> None:
        """Ctrl+U: delete from the start of the current line to the cursor."""
        start, _ = self._line_bounds(self.cursor)
        if self.cursor > start:
            self._snapshot()
            self._coalescing = False
            self._kill(self.text[start:self.cursor])  # #231: killed head → ring
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start

    def yank(self) -> None:
        """#231: insert the most recently killed text at the cursor (readline Ctrl-Y). A
        no-op when nothing has been killed. Records an undo snapshot so it's reversible."""
        if not self._kill_ring:
            return
        self._snapshot()
        self._coalescing = False
        chunk = self._kill_ring[-1]
        self.text = self.text[:self.cursor] + chunk + self.text[self.cursor:]
        self.cursor += len(chunk)
        self._history_idx = -1

    def undo(self) -> None:
        """Restore the most recent pre-edit snapshot. Saves the current state to the redo
        stack (#232) so `redo()` can step forward again."""
        if self._undo:
            self._redo.append((self.text, self.cursor))
            self.text, self.cursor = self._undo.pop()
            self._coalescing = False

    def redo(self) -> None:
        """#232: reverse the most recent undo. Pushes the current state back onto the undo
        stack (WITHOUT clearing redo — only a fresh edit does that, via _snapshot)."""
        if self._redo:
            self._undo.append((self.text, self.cursor))
            self.text, self.cursor = self._redo.pop()
            self._coalescing = False

    def _line_bounds(self, pos: int) -> tuple[int, int]:
        start = self.text.rfind("\n", 0, pos) + 1
        nl = self.text.find("\n", pos)
        end = len(self.text) if nl < 0 else nl
        return start, end

    def home(self) -> None:
        self.cursor = self._line_bounds(self.cursor)[0]

    def end(self) -> None:
        self.cursor = self._line_bounds(self.cursor)[1]

    def doc_start(self) -> None:
        self.cursor = 0

    def doc_end(self) -> None:
        self.cursor = len(self.text)

    def up(self) -> None:
        start, _ = self._line_bounds(self.cursor)
        if start == 0:
            return
        col = self.cursor - start
        prev_start, prev_end = self._line_bounds(start - 1)
        self.cursor = min(prev_start + col, prev_end)

    def down(self) -> None:
        start, end = self._line_bounds(self.cursor)
        if end >= len(self.text):
            return
        col = self.cursor - start
        next_start, next_end = self._line_bounds(end + 1)
        self.cursor = min(next_start + col, next_end)

    # ---- paste (bracketed) --------------------------------------------------
    def paste(self, payload: str) -> None:
        """Handle a completed bracketed-paste payload."""
        text = filter_printable(payload or "")
        if not text:
            return
        # F34: snapshot BEFORE inserting so a paste is one undoable unit (every other edit op
        # snapshots; paste was the exception — Ctrl+Z couldn't undo a paste).
        self._snapshot()
        self._coalescing = False   # a paste is its own undo unit, not part of a typing run
        lines = text.split("\n")
        if len(lines) > PASTE_CHIP_MIN_LINES or len(text) > PASTE_CHIP_MIN_CHARS:
            self._paste_counter += 1
            pid = self._paste_counter
            self.pastes[pid] = text
            marker = (f"[paste #{pid} +{len(lines)} lines]"
                      if len(lines) > PASTE_CHIP_MIN_LINES
                      else f"[paste #{pid} {len(text)} chars]")
            self.insert(marker)
        else:
            self.insert(text)               # small paste inlined

    # ---- output -------------------------------------------------------------
    def display(self) -> str:
        """What the box shows (markers stay compact)."""
        return self.text

    def expand(self) -> str:
        """What actually gets sent: paste markers replaced by full content."""
        def repl(m: re.Match) -> str:
            pid = int(m.group(1))
            return self.pastes.get(pid, m.group(0))
        return PASTE_MARKER_RE.sub(repl, self.text)

    def is_empty(self) -> bool:
        return self.text.strip() == ""

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0
        self.pastes = {}
        self._paste_counter = 0
        self._undo = []
        self._redo = []          # #232: a cleared box has nothing to redo into
        self._coalescing = False
        self._history_idx = -1
        self._history_saved = ""
        # NOTE: the kill-ring deliberately SURVIVES clear() — like a real shell, you can
        # yank previously-killed text into a fresh line.

    def history_add(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if self._history and self._history[-1] == t:
            return
        self._history.append(t)
        if len(self._history) > self._HISTORY_LIMIT:
            self._history = self._history[-self._HISTORY_LIMIT:]
        self._history_idx = -1

    def save_history(self, path) -> None:
        """#232: persist the input history to `path` as JSONL (one entry per line) so it
        survives a restart. Bounded to _HISTORY_LIMIT. Best-effort (never raises)."""
        import json
        from pathlib import Path
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(h, ensure_ascii=False) for h in self._history[-self._HISTORY_LIMIT:]]
            p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except Exception:  # noqa: BLE001 - persistence is best-effort
            pass

    def load_history(self, path) -> None:
        """#232: load persisted input history from a JSONL `path` (missing/corrupt → no-op,
        keeping the current in-memory history). Bounded + resets the recall index."""
        import json
        from pathlib import Path
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return
        loaded = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                v = json.loads(ln)
            except ValueError:
                continue
            if isinstance(v, str) and v:
                loaded.append(v)
        if loaded:
            self._history = loaded[-self._HISTORY_LIMIT:]
            self._history_idx = -1

    def history_up(self) -> bool:
        if not self._history:
            return False
        if self._history_idx == -1:
            self._history_saved = self.text
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        else:
            return False
        self.text = self._history[self._history_idx]
        self.cursor = len(self.text)
        return True

    def history_down(self) -> bool:
        if self._history_idx == -1:
            return False
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self.text = self._history[self._history_idx]
        else:
            self._history_idx = -1
            self.text = self._history_saved
        self.cursor = len(self.text)
        return True


@dataclass
class PasteScanner:
    """Bracketed-paste state machine over input chunks.

    The curses loop assembles input into chunks (a normal keypress is one char; an
    escape burst is read with a short timeout) and calls feed(chunk). We mirror
    chunk algorithm: detect `\\x1b[200~`, buffer until `\\x1b[201~`,
    emit the payload, then re-process any trailing data after the end marker.

    feed(chunk) -> list of events, each ("text", s) for ordinary input or
    ("paste", payload) for a completed paste. Pure -> unit-tested.
    """

    _in_paste: bool = False
    _buffer: str = ""
    _MAX_BUFFER: int = 8 * 1024 * 1024     # 8 MiB hard cap -> flush, so we never OOM/freeze

    def feed(self, data: str) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if data == "":
            return events
        # Start of a bracketed paste (data contains "\x1b[200~")
        if BRACKETED_PASTE_START in data:
            self._in_paste = True
            self._buffer = ""
            data = data.replace(BRACKETED_PASTE_START, "", 1)
        if self._in_paste:
            self._buffer += data
            end = self._buffer.find(BRACKETED_PASTE_END)
            if end != -1:
                payload = self._buffer[:end]
                events.append(("paste", payload))
                self._in_paste = False
                remaining = self._buffer[end + len(BRACKETED_PASTE_END):]
                self._buffer = ""
                if remaining:
                    events.extend(self.feed(remaining))
            elif len(self._buffer) > self._MAX_BUFFER:
                # Runaway paste with no end marker yet (a terminal whose 201~ was lost, or a
                # paste past the burst cap): flush what we have as a completed paste and reset,
                # so the input box can't freeze and memory can't grow without bound.
                events.append(("paste", self._buffer))
                self._in_paste = False
                self._buffer = ""
            return events
        events.append(("text", data))
        return events

    def flush(self) -> list[tuple[str, str]]:
        """Force-emit any buffered (unterminated) paste + reset. The TUI can call this on an
        input-idle deadline so a terminal that sends \\x1b[200~ but never \\x1b[201~ can't
        freeze the input box. Same event shape as feed(); idempotent (empty if nothing buffered)."""
        buf, self._buffer, self._in_paste = self._buffer, "", False
        return [("paste", buf)] if buf else []
