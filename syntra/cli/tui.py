"""Full-screen curses TUI (Track T1) — modern full-screen style.

This is the real full-screen terminal UI (alt-screen), NOT the line-based REPL.
Layout:

  +-------------------------------------------+------------------+
  |  transcript (wrapping, scroll, follow)    |  right panel:    |
  |                                           |  plan / usage /  |
  |                                           |  status          |
  +-------------------------------------------+------------------+
  |  status line: model · cost · task                            |
  +--------------------------------------------------------------+
  |  > input box (single Enter=send, esc esc=interrupt)          |
  +--------------------------------------------------------------+

The renderable STATE lives in core/tui_model.py (pure, tested). curses here is a
thin draw + key loop over it. Layout math (pane sizes) is factored into a pure
helper so it is unit-testable without a terminal.

Keybindings (F4): Enter = send, Ctrl-J = newline, PgUp/PgDn/Home/End = scroll,
Esc Esc = interrupt, Ctrl-C / Ctrl-D = quit. Ctrl-T = file picker, Ctrl-P or "/"
= command palette, Ctrl-F = find in transcript. Slash commands (/help /tasks
/verbose /exit ...) reuse the session dispatcher.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass


_RUN_DONE = object()  # sentinel pushed to the output queue when a run finishes


class _Quit(Exception):
    """Internal: raised inside the curses loop to break out (e.g. /exit)."""


class RunController:
    """Runs a task on a background thread so the TUI input stays live, and routes
    mid-run steering into the run's SteeringInbox (F5). Concurrency coordination
    lives here (testable); the curses loop just polls + forwards keys.

    run_goal(goal, on_line, steering) -> result : executes the task, streaming
        output via on_line(text, role="assistant") and polling `steering` between
        steps. on_line accepts an optional role so progress telemetry can stream
        as "tool" while the final answer streams as "assistant".
    """

    def __init__(self, run_goal):
        self._run_goal = run_goal
        self._out: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._inbox = None
        self._cancelled = False
        self.result = None
        self.error: Exception | None = None
        self.finished = False
        self.active_model = ""          # latest routed model, shown in the spinner

    def start(self, goal: str) -> None:
        from ..core.steering import SteeringInbox
        inbox = SteeringInbox()
        out: "queue.Queue" = queue.Queue()   # PER-RUN queue: an abandoned run can't
        self._inbox = inbox                  # leak output into the next one.
        self._out = out
        self.result = None
        self.error = None
        self.finished = False
        self.active_model = ""
        self._cancelled = False

        def _emit(text, role: str = "assistant"):
            if role == "tool" and isinstance(text, str) and "-> " in text and text.startswith("[route]"):
                try:
                    self.active_model = text.split("-> ", 1)[1].split(" via", 1)[0].strip()
                except Exception:  # noqa: BLE001
                    pass
            out.put((role, text))            # capture local `out`, not self._out

        def _work():
            try:
                res = self._run_goal(goal, _emit, inbox)
                if not self._cancelled:      # a detached run's result is discarded
                    self.result = res
            except Exception as e:  # noqa: BLE001
                if not self._cancelled:
                    self.error = e
            finally:
                out.put(_RUN_DONE)

        self._thread = threading.Thread(target=_work, name="syntra-tui-run", daemon=True)
        self._thread.start()

    def hard_stop(self) -> bool:
        """INSTANT interrupt: detach from the run NOW so the UI is freed immediately,
        and signal the cooperative stop so the orphaned daemon thread dies at its next
        boundary (≤1 more model call, whose output is ignored). The model can't be
        aborted mid-socket, but the user regains control instantly. Returns True if a
        run was live."""
        if self._thread is None or not self._thread.is_alive():
            return False
        if self._inbox is not None:
            self._inbox.request_stop()       # orphan stops at its next step boundary
        self._cancelled = True
        self._thread = None                  # running() -> False, UI freed this frame
        self._out = queue.Queue()            # ignore anything the orphan still emits
        self.finished = False
        self.result = None
        return True

    def poll(self) -> list[tuple[str, str]]:
        """Drain newly-streamed (role, text) items (non-blocking). Sets finished."""
        items: list[tuple[str, str]] = []
        while True:
            try:
                item = self._out.get_nowait()
            except queue.Empty:
                break
            if item is _RUN_DONE:
                self.finished = True
            else:
                items.append(item)
        return items

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def steer_now(self, text: str) -> bool:
        return bool(self._inbox and self._inbox.steer(text))

    def steer_queue(self, text: str) -> bool:
        return bool(self._inbox and self._inbox.queue(text))

    def request_stop(self) -> bool:
        """Ask the running task to interrupt itself. The loop checks should_stop
        at its next step boundary. Returns True if a run was live to signal."""
        if self._inbox is not None:
            self._inbox.request_stop()
            return True
        return False


@dataclass(frozen=True)
class Layout:
    """Computed pane geometry. Pure -> unit-tested without curses."""

    rows: int
    cols: int
    panel_w: int          # right panel width (0 if too narrow)
    transcript_w: int     # transcript pane width
    transcript_h: int     # transcript pane height (lines)
    status_row: int       # y of the status/input-border line
    input_row: int        # y of the input text


def compute_layout(rows: int, cols: int, *, want_panel: bool = True,
                   min_panel: int = 22, min_main: int = 30) -> Layout:
    """Split the screen into transcript + (optional) right panel + status + input.

    The panel is dropped on narrow terminals so the transcript stays usable.
    Reserves 2 bottom rows (status + input) and 1 separator column for the panel.
    """
    rows = max(4, int(rows))
    cols = max(10, int(cols))
    body_h = rows - 2                      # leave status + input rows
    panel_w = 0
    if want_panel and cols >= (min_main + min_panel + 1):
        panel_w = min_panel
    transcript_w = cols - (panel_w + 1 if panel_w else 0)
    return Layout(
        rows=rows, cols=cols, panel_w=panel_w,
        transcript_w=transcript_w, transcript_h=body_h,
        status_row=rows - 2, input_row=rows - 1,
    )


def input_on_enter(buf: str, *, double_enter_send: bool = True) -> tuple[str, str]:
    """Decide what Enter does (F4). Pure + testable.

    Default: **single Enter = newline, double Enter =
    send** (press Enter on a blank trailing line to send). Set
    double_enter_send=False for the alternative single-Enter-sends / backslash-
    newline style. Returns (action, new_buffer), action in {send, newline, noop}.
    """
    if double_enter_send:
        if buf.strip() == "":
            return ("noop", "")
        if buf.endswith("\n"):           # a second Enter on a blank line -> SEND
            return ("send", buf.rstrip("\n"))
        return ("newline", buf + "\n")   # single Enter -> newline
    # alternative: single Enter sends; trailing backslash inserts a newline
    if buf.endswith("\\"):
        return ("newline", buf[:-1] + "\n")
    return ("send", buf)


def esc_action(*, double: bool, run_active: bool, queued: int) -> str:
    """Decide what Esc does in the TUI (pure + testable).

    A SINGLE Esc flushes any queued messages into the running
    task and keeps it going; a DOUBLE Esc (esc-esc) interrupts the whole run — or
    quits the app when nothing is running.

    Returns one of:
      'flush' — send the queued messages into the active run, then continue
      'stop'  — interrupt the whole run
      'quit'  — exit the app (idle double-Esc)
      'arm'   — single Esc with nothing to flush: arm so a following Esc acts
    """
    if double:
        return "stop" if run_active else "quit"
    if run_active:
        # single Esc: queue present -> send the queued messages together; otherwise
        # (just the one running task) -> interrupt it instantly. Double Esc always
        # hard-stops + discards the queue.
        return "flush" if queued > 0 else "stop"
    return "arm"


def input_display(buf: str, width: int) -> str:
    """How the input box renders a (possibly multi-line) buffer. Pure."""
    if "\n" in buf:
        lines = buf.split("\n")
        shown = f"[{len(lines)} lines] {lines[-1]}"
    else:
        shown = buf
    return ("❯ " + shown)[: max(0, width - 1)]


def input_view(text: str, cursor: int, width: int) -> tuple[str, int]:
    """Render the prompt box with a horizontally-scrolled window + cursor column.

    Pure + testable horizontal-scroll math. Multi-line
    buffers collapse to `[N lines] <cursor-line>` so the single input row always
    shows the line being edited, scrolled to keep the cursor visible.

    Returns (display_string, cursor_col) where cursor_col is the screen column of
    the cursor within display_string.
    """
    prompt = "❯ "
    # Locate the line containing the cursor and the cursor's column within it.
    line_start = text.rfind("\n", 0, cursor) + 1
    nl = text.find("\n", cursor)
    line_end = len(text) if nl < 0 else nl
    line = text[line_start:line_end]
    col = cursor - line_start

    lead = prompt
    if "\n" in text:
        lead = f"{prompt}[{text.count(chr(10)) + 1} lines] "
    avail = max(1, width - len(lead) - 1)   # -1 leaves room for the cursor cell

    start = 0
    if len(line) > avail:
        if col < avail // 2:
            start = 0
        elif col > len(line) - avail // 2:
            start = max(0, len(line) - avail)
        else:
            start = max(0, col - avail // 2)
    window = line[start:start + avail]
    # F27: the cursor's SCREEN column is the display width of the window text before it, not the
    # code-point offset — otherwise wide/zero-width glyphs drift the caret (same class of bug
    # tui2 fixed via tui_model.input_rows). Fall back to the offset if display_width is unavailable.
    try:
        from ..core.tui_model import display_width
        cursor_col = len(lead) + display_width(window[: max(0, col - start)])
    except Exception:  # noqa: BLE001
        cursor_col = len(lead) + (col - start)
    return (lead + window, cursor_col)


def panel_lines(*, plan: list | None = None, cost_usd: float = 0.0,
                tokens: tuple[int, int] = (0, 0), task_id: str = "",
                model: str = "", theme: str = "", workspace: str = "",
                mode: str = "build", lsp: str = "", mcp_count: int = 0,
                width: int = 22, height: int = 20) -> list[str]:
    """Right-panel content: workspace context, model, usage. Pure."""
    w = max(6, width)
    def clip(s: str) -> str:
        return s[:w]
    out: list[str] = []
    bottom: list[str] = []

    # ── top: branding + workspace ──
    if workspace:
        import os
        short = workspace.replace(os.path.expanduser("~"), "~")
        if len(short) > w - 2:
            parts = short.rstrip("/").split("/")
            short = parts[-1] if parts else "."
        out.append(clip(f" {short}"))
    else:
        out.append(clip(" ."))

    # model + mode on same density
    if model:
        short_model = model.split("/")[-1] if "/" in model else model
        if len(short_model) > w - 2:
            short_model = short_model[:w - 5] + ".."
        out.append(clip(f" {short_model}"))
    out.append(clip(f" {mode}"))

    # cost/tokens — compact, only when active
    in_t, out_t = tokens
    if cost_usd > 0 or in_t or out_t:
        out.append("")
        out.append(clip(f" ${cost_usd:.4f}  {in_t}+{out_t}"[:w]))
        if plan:
            done = sum(1 for s in plan if getattr(s, "status", "") == "done")
            out.append(clip(f" {done}/{len(plan)} steps"))

    # integrations — only if connected
    if lsp or mcp_count:
        out.append("")
    if lsp:
        out.append(clip(f" lsp  {lsp}"))
    if mcp_count:
        out.append(clip(f" mcp  {mcp_count}"))

    # ── bottom: version + theme + one keybind hint ──
    bottom.append(clip(f" {theme}" if theme else ""))
    bottom.append(clip(" ◆ syntra 0.1"))
    bottom.append(clip(" ctrl+p cmds  / help"))

    # pad middle
    remaining = height - len(out) - len(bottom)
    if remaining > 0:
        out.extend([""] * remaining)
    out.extend(bottom)

    return out[:height]


# ----------------------------------------------------------------- curses loop


def picker_overlay(query: str, visible: list[str], selected_row: int,
                   width: int, *, title: str = "find file") -> list[str]:
    """File/command overlay lines (pure + testable). Header + marked rows.

    selected_row is the index of the highlighted item WITHIN `visible` (-1 = none).
    """
    def clip(s: str) -> str:
        return s[: max(0, width)]
    lines = [clip(f"{title}> {query}")]
    if not visible:
        lines.append(clip("  (no match)"))
    for i, item in enumerate(visible):
        prefix = "> " if i == selected_row else "  "
        lines.append(clip(prefix + item))
    return lines


def find_status(query: str, idx: int, count: int, width: int) -> str:
    """Status line for find-in-transcript mode (F3). Pure."""
    pos = f" [{idx + 1}/{count}]" if count else " (no match)"
    return (f"find: {query}{pos}  · Enter=next · Esc=close")[: max(0, width - 1)]


def select_status(messages: list, idx: int, width: int) -> str:
    """Status line for message-select mode (F8). Pure.

    Shows position, role, a snippet, and the available actions. `messages` is the
    transcript message list; idx is the selected index.
    """
    if not messages:
        return "select: (no messages)"[: max(0, width - 1)]
    i = max(0, min(idx, len(messages) - 1))
    m = messages[i]
    role = getattr(m, "role", "?")
    snippet = " ".join((getattr(m, "text", "") or "").split())[:24]
    return (f"select {i + 1}/{len(messages)} [{role}] {snippet!r}  "
            f"· ↑↓ move · c=copy · r=retry · Esc=exit")[: max(0, width - 1)]


def select_clamp(idx: int, count: int) -> int:
    """Clamp a message-select index into [0, count-1] (0 when empty). Pure."""
    if count <= 0:
        return 0
    return max(0, min(idx, count - 1))


def run_tui(run_goal, *, startup_note_fn=None, **_unused) -> int:
    """Launch the full-screen curses TUI.

    run_goal(goal, on_event) -> result   : callback that executes a task; should
        stream lines via on_event(text). Kept injectable so the loop is testable
        and so cli/main wires the real Loop here.
    startup_note_fn : optional callable -> one-line health summary, invoked AFTER
        the tty check so a no-tty launch falls back cleanly without side effects.
    Returns an exit code. Requires a real terminal; raises RuntimeError if curses
    is unavailable (caller should fall back to the line REPL).
    """
    try:
        import curses
    except Exception as e:  # pragma: no cover - platform without curses
        raise RuntimeError(f"curses unavailable: {e}") from e

    import sys as _sys
    if not (_sys.stdin.isatty() and _sys.stdout.isatty()):
        raise RuntimeError("not a real terminal (stdin/stdout not a tty)")

    from ..core.tui_model import Transcript
    from .main import session_dispatch  # reuse the slash-command classifier

    def _main(stdscr):
        curses.curs_set(1)
        stdscr.keypad(True)
        # Enable mouse wheel scrolling (best-effort; ignored if unsupported).
        try:
            curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED |
                             getattr(curses, "BUTTON2_PRESSED", 0))
        except curses.error:
            pass
        # Theme colors -> curses color pairs (one per semantic role). xterm-256
        # when available; degrade gracefully when the terminal can't do colors.
        # Re-runnable so `/themes <name>` recolors the live screen instantly.
        from ..core.themes import theme_colors
        role_attrs: dict[str, int] = {}

        def _load_theme_colors() -> None:
            role_attrs.clear()
            try:
                curses.start_color()
                try: curses.use_default_colors()
                except curses.error: pass
                if curses.has_colors():
                    colors = theme_colors()
                    maxc = getattr(curses, "COLORS", 8) or 8
                    for i, (role, fg) in enumerate(colors.items(), start=1):
                        try:
                            curses.init_pair(i, fg if fg < maxc else -1, -1)
                            role_attrs[role] = curses.color_pair(i)
                        except curses.error:
                            pass
            except curses.error:
                pass

        _load_theme_colors()

        def _attr_for(role: str) -> int:
            if role in role_attrs: return role_attrs[role]
            if role == "user":     return role_attrs.get("user", 0) | curses.A_BOLD
            if role == "system":   return role_attrs.get("system", 0) | curses.A_DIM
            if role == "tool":     return role_attrs.get("tool", 0) | curses.A_DIM
            return role_attrs.get("assistant", 0)

        def _paint_code(scr, y, line, maxw):
            """Paint a code box-row '│ <code> │' — borders in box color, the code
            content highlighted token-by-token."""
            from ..core.highlight import highlight_line
            box_attr = role_attrs.get("assistant", 0)
            # Separate the box frame from the inner code if present.
            if line.startswith("│ ") and line.rstrip().endswith("│"):
                inner = line[2:line.rstrip().rfind("│")].rstrip()
                trail = line[2 + len(inner):]            # padding + " │"
                try:
                    scr.addnstr(y, 0, "│ ", maxw, box_attr)
                except curses.error:
                    pass
                x = 2
                for text, trole in highlight_line(inner):
                    if x >= maxw:
                        break
                    seg = text[: maxw - x]
                    attr = role_attrs.get(trole, role_attrs.get("code", 0))
                    if trole == "comment":
                        attr |= curses.A_DIM
                    elif trole == "keyword":
                        attr |= curses.A_BOLD
                    try:
                        scr.addnstr(y, x, seg, maxw - x, attr)
                    except curses.error:
                        pass
                    x += len(seg)
                try:
                    scr.addnstr(y, x, trail, max(0, maxw - x), box_attr)
                except curses.error:
                    pass
                return
            # No frame -> highlight the whole line.
            x = 0
            for text, trole in highlight_line(line):
                if x >= maxw:
                    break
                seg = text[: maxw - x]
                attr = role_attrs.get(trole, role_attrs.get("code", 0))
                if trole == "comment":
                    attr |= curses.A_DIM
                elif trole == "keyword":
                    attr |= curses.A_BOLD
                try:
                    scr.addnstr(y, x, seg, maxw - x, attr)
                except curses.error:
                    pass
                x += len(seg)
        transcript = Transcript()
        transcript.add("system", "type a goal and press enter.  /help for commands.")
        if startup_note_fn is not None:
            try:
                note = startup_note_fn()
            except Exception:  # noqa: BLE001 - never block the TUI on a health probe
                note = None
            if note:
                transcript.add("system", note)
        cost = 0.0
        last_esc = False
        controller = RunController(run_goal)
        active = False  # a run is in flight (started, not yet finalized)
        plan: list = []                  # latest plan steps, shown in the panel
        tokens = (0, 0)                  # latest (input, output) token totals
        last_task_id = ""                # id of the most recent run (for /resume etc.)

        from ..core.input_editor import InputEditor, PasteScanner
        from ..core.chips import BRACKETED_PASTE_END
        from ..core.select_list import SelectList
        from ..core.keymap import Keymap
        from ..core.keys import ctrl_token
        from pathlib import Path as _Path
        import sys as _s
        editor = InputEditor()
        scanner = PasteScanner()
        keymap = Keymap.load(_Path(".syntra/keymap.json"))   # F4: configurable, collision-checked
        _special_tokens = {curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown"}
        picker: SelectList | None = None        # overlay list (None = closed)
        picker_kind = ""                         # "file" | "command"
        find_query: str | None = None            # not None => find-in-transcript mode
        find_matches: list[int] = []
        find_idx = 0
        select_idx: int | None = None             # not None => message-select mode (F8)

        def _open_picker() -> None:
            nonlocal picker, picker_kind
            from ..core.files import list_workspace_files
            try:
                files = list_workspace_files(".")
            except Exception:  # noqa: BLE001
                files = []
            rows, _ = stdscr.getmaxyx()
            picker = SelectList(files, height=max(3, min(10, rows - 6)))
            picker_kind = "file"

        def _open_palette() -> None:
            nonlocal picker, picker_kind
            from ..core.commands import command_labels
            rows, _ = stdscr.getmaxyx()
            picker = SelectList(command_labels(), height=max(3, min(10, rows - 6)))
            picker_kind = "command"

        def _set_bracketed(on: bool) -> None:
            try:
                _s.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
                _s.stdout.flush()
            except Exception:  # noqa: BLE001
                pass

        def _esc_burst() -> str:
            """After an ESC, grab the rest of the escape burst (for paste detection)."""
            seq = "\x1b"
            stdscr.timeout(0)                      # non-blocking read of the burst
            try:
                while True:
                    c = stdscr.getch()
                    if c == -1:
                        break
                    if 0 <= c <= 0x10FFFF:
                        seq += chr(c)
                    if seq.endswith(BRACKETED_PASTE_END) or len(seq) > 2_000_000:
                        break
            finally:
                stdscr.timeout(80)
            return seq

        def _draw():
            rows, cols = stdscr.getmaxyx()
            lay = compute_layout(rows, cols)
            transcript.width = lay.transcript_w
            stdscr.erase()
            # Render bubble-framed messages (rounded borders + role badges) so the
            # transcript looks like a real coding-agent TUI, not a plain log.
            from ..core.tui_model import render_bubbles, pulse_frame
            bubbles = render_bubbles(transcript.messages, lay.transcript_w)
            # apply the same scroll/follow math the line view uses
            if transcript._follow:
                transcript.scroll = max(0, len(bubbles) - lay.transcript_h)
            transcript.scroll = max(0, min(transcript.scroll, max(0, len(bubbles) - lay.transcript_h)))
            view = bubbles[transcript.scroll:transcript.scroll + lay.transcript_h]
            for y, (line, role) in enumerate(view):
                try:
                    if role == "code" and "```" not in line:
                        _paint_code(stdscr, y, line, lay.transcript_w)
                    elif line[:1] in ("╭", "╰"):
                        # Box frame lines tint to the accent color (signature ◆)
                        # — subtle, NOT bold, so it reads as a calm glow not glare.
                        accent = role_attrs.get("accent", _attr_for(role))
                        stdscr.addnstr(y, 0, line, lay.transcript_w, accent)
                    else:
                        stdscr.addnstr(y, 0, line, lay.transcript_w, _attr_for(role))
                except curses.error:
                    pass
            if lay.panel_w:
                dim = role_attrs.get("dim", 0)
                for y in range(lay.transcript_h):
                    try:
                        stdscr.addstr(y, lay.transcript_w, "│", dim)
                    except curses.error:
                        pass
                from ..core.themes import current_theme
                import os as _os
                _panel_model = getattr(controller, "active_model", "") or ""
                _panel_ws = _os.getcwd()
                for y, line in enumerate(panel_lines(
                        plan=plan, cost_usd=cost, tokens=tokens, task_id="",
                        model=_panel_model, theme=current_theme(),
                        workspace=_panel_ws, mode="build",
                        width=lay.panel_w - 1, height=lay.transcript_h)):
                    try:
                        # branding line gets accent color
                        attr = role_attrs.get("accent", dim) if "◆" in line else dim
                        stdscr.addnstr(y, lay.transcript_w + 1, line, lay.panel_w - 1, attr)
                    except curses.error:
                        pass
            # ── status bar: thin ─ border with info embedded ──
            if find_query is not None:
                bar_text = find_status(find_query, find_idx, len(find_matches), cols)
            elif select_idx is not None:
                bar_text = select_status(transcript.messages, select_idx, cols)
            else:
                if controller.running():
                    import time as _t
                    frame = pulse_frame(int(_t.time() * 4))
                    model_hint = getattr(controller, "active_model", "") or ""
                    short_m = model_hint.split("/")[-1] if "/" in model_hint else model_hint
                    bar_text = f" {frame} {short_m} "
                elif cost > 0:
                    bar_text = f" ${cost:.4f} "
                else:
                    bar_text = ""
                # build the ─── info ─── line
                if bar_text:
                    pad = max(0, cols - len(bar_text) - 2)
                    left_pad = 2
                    right_pad = max(0, pad - left_pad)
                    bar_text = "─" * left_pad + bar_text + "─" * right_pad
                else:
                    bar_text = "─" * (cols - 1)
            dim = role_attrs.get("dim", 0)
            try:
                stdscr.addnstr(lay.status_row, 0, bar_text[:cols - 1], cols - 1, dim)
            except curses.error:
                pass

            # ── input box: prompt glyph in accent, text in user color ──
            disp, curcol = input_view(editor.display(), editor.cursor, cols)
            accent_attr = role_attrs.get("accent", 0)
            user_attr = role_attrs.get("user", 0)
            try:
                # draw the "❯ " in accent
                stdscr.addstr(lay.input_row, 0, disp[:2], accent_attr)
                # draw the rest in user color
                if len(disp) > 2:
                    stdscr.addnstr(lay.input_row, 2, disp[2:], cols - 3, user_attr)
            except curses.error:
                pass
            if picker is not None:
                over = picker_overlay(picker.query, picker.visible(),
                                      picker.visible_selected(), cols - 1,
                                      title=("command" if picker_kind == "command" else "find file"))
                top = max(0, lay.input_row - len(over))
                for i, line in enumerate(over):
                    try:
                        stdscr.addnstr(top + i, 0, line.ljust(cols - 1), cols - 1)
                    except curses.error:
                        pass
            stdscr.move(lay.input_row, min(curcol, cols - 1))
            stdscr.refresh()

        def _drain():
            nonlocal active, cost, plan, tokens
            for role, line in controller.poll():
                if role == "stream":
                    transcript.append_stream(line)      # live token chunk
                else:
                    transcript.end_stream()             # finalize any live stream first
                    transcript.add(role, line)
            if active and controller.finished:
                active = False
                transcript.end_stream()
                if controller.error is not None:
                    transcript.add("system", f"error: {controller.error}")
                elif controller.result is not None:
                    try:
                        st = controller.result.state
                        cost = st.total_cost_usd()
                        tokens = st.total_tokens()
                        plan = list(getattr(st, "plan", []) or [])
                        verdict = getattr(controller.result, "verdict", "?")
                        transcript.add("system", f"[{verdict}]  cost ${cost:.4f}")
                    except Exception:  # noqa: BLE001
                        pass

        def _submit():
            """Send the current editor contents (expanding paste markers)."""
            nonlocal active, last_task_id
            display_line = editor.display().strip()
            sent = editor.expand().strip()
            editor.clear()
            if not sent:
                return
            if controller.running():                 # mid-run -> steer (F5)
                controller.steer_now(sent)
                transcript.add("system", f"[steer] injected: {display_line}")
                return
            action, arg = session_dispatch(sent)
            if action == "exit":
                raise _Quit()
            if action in ("goal", "proof-goal", "agent"):
                # All three kick off a run. (Agent-mode specialization is handled
                # by the run callback the host passes in; the TUI just starts it.)
                transcript.add("user", display_line)
                active = True
                controller.start(arg if action != "agent" else (arg or display_line))
                return
            # Every other slash-command: execute it for real and SHOW its output
            # in the transcript (captured so it isn't lost behind curses).
            from .main import _handle_session_action, capture_output
            transcript.add("user", display_line)
            if action == "empty":
                _draw(); return
            with capture_output() as out_lines:
                handled = _handle_session_action(action, arg, last_task_id)
            body = "\n".join(out_lines).rstrip()
            if not handled:
                transcript.add("system", f"unknown command: {display_line}")
            elif body:
                transcript.add("system", body)
            else:
                transcript.add("system", f"{action}: done")
            # /themes <name> just changed the active theme -> recolor live now.
            if action == "themes" and arg:
                _load_theme_colors()
            _draw()

        _set_bracketed(True)
        stdscr.timeout(80)  # non-blocking-ish: getch returns -1 every 80ms
        _draw()
        try:
            while True:
                _drain()
                try:
                    ch = stdscr.getch()
                except KeyboardInterrupt:
                    break
                if ch == -1:  # timeout tick: keep the screen live while a run streams
                    if controller.running() or controller.finished or active:
                        _drain()      # pull any queued run output even with no keypress
                        _draw()
                    continue
                # ---- find-in-transcript mode intercepts keys while active (F3) ----
                if find_query is not None:
                    rows, cols = stdscr.getmaxyx(); lay = compute_layout(rows, cols)
                    def _recompute_find():
                        nonlocal find_matches, find_idx
                        find_matches = transcript.find(find_query)
                        find_idx = 0
                        if find_matches:
                            transcript.scroll_to(find_matches[0], lay.transcript_h)
                    if ch == 27:                          # ESC closes find
                        burst = _esc_burst()
                        if burst == "\x1b":
                            find_query = None
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13):  # jump to next match
                        if find_matches:
                            find_idx = (find_idx + 1) % len(find_matches)
                            transcript.scroll_to(find_matches[find_idx], lay.transcript_h)
                        _draw(); continue
                    if ch in (curses.KEY_BACKSPACE, 127, 8):
                        find_query = find_query[:-1]; _recompute_find(); _draw(); continue
                    if 32 <= ch < 127:
                        find_query += chr(ch); _recompute_find(); _draw(); continue
                    _draw(); continue
                # ---- message-select mode intercepts keys while active (F8) ----
                if select_idx is not None:
                    msgs = transcript.messages
                    if ch == 27:                          # ESC exits select mode
                        burst = _esc_burst()
                        if burst == "\x1b":
                            select_idx = None
                        _draw(); continue
                    if not msgs:
                        select_idx = None; _draw(); continue
                    if ch in (curses.KEY_UP, ord("k")):
                        select_idx = select_clamp(select_idx - 1, len(msgs)); _draw(); continue
                    if ch in (curses.KEY_DOWN, ord("j")):
                        select_idx = select_clamp(select_idx + 1, len(msgs)); _draw(); continue
                    if ch in (ord("c"), ord("C")):        # copy selected message
                        from ..core.clipboard import osc52
                        sel = msgs[select_clamp(select_idx, len(msgs))]
                        try:
                            _s.stdout.write(osc52(getattr(sel, "text", "") or "")); _s.stdout.flush()
                        except Exception:  # noqa: BLE001
                            pass
                        transcript.add("system", "[copied to clipboard]")
                        select_idx = None; _draw(); continue
                    if ch in (ord("r"), ord("R")):        # retry: resubmit a user message
                        sel = msgs[select_clamp(select_idx, len(msgs))]
                        select_idx = None
                        if getattr(sel, "role", "") == "user" and not controller.running():
                            goal = (getattr(sel, "text", "") or "").strip()
                            if goal:
                                transcript.add("user", goal)
                                active = True
                                controller.start(goal)
                        _draw(); continue
                    _draw(); continue
                # ---- file picker / command palette intercepts keys while open ----
                if picker is not None:
                    if ch == 27:                      # ESC closes the overlay
                        burst = _esc_burst()
                        if burst == "\x1b":
                            picker = None
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13):
                        chosen = picker.current()
                        kind = picker_kind
                        picker = None
                        if chosen and kind == "command":
                            from ..core.commands import name_from_label, command_for
                            name = name_from_label(chosen)
                            editor.clear()
                            editor.insert(name + (" " if (command_for(name) and command_for(name).takes_arg) else ""))
                        elif chosen:                  # file path
                            # pad a path with a leading space if it
                            # would butt against a preceding word character.
                            pre = editor.text[:editor.cursor]
                            if pre and pre[-1].isalnum():
                                editor.insert(" ")
                            editor.insert(chosen)
                        _draw(); continue
                    if ch in (curses.KEY_BACKSPACE, 127, 8):
                        picker.backspace(); _draw(); continue
                    if ch == curses.KEY_UP:
                        picker.move(-1); _draw(); continue
                    if ch == curses.KEY_DOWN:
                        picker.move(1); _draw(); continue
                    if 32 <= ch < 127:
                        picker.type_char(chr(ch)); _draw(); continue
                    _draw(); continue
                if ch == 27:  # ESC: could be a bracketed paste, an escape seq, or esc-esc
                    burst = _esc_burst()
                    if burst == "\x1b":             # lone ESC
                        if last_esc:
                            break                   # esc esc = quit/interrupt
                        last_esc = True
                        continue
                    # Shift+Enter (and Alt/Meta+Enter) arrive as an escape burst on
                    # modern terminals: kitty "\x1b[13;2u" / "\x1b[27;2;13~", or a
                    # meta-prefixed "\x1b\r" / "\x1b\n". Treat all as INSERT NEWLINE
                    # (Enter alone sends; this is the multi-line key the user wants).
                    if (burst in ("\x1b\r", "\x1b\n")
                            or burst.endswith("[13;2u") or burst.endswith("[27;2;13~")
                            or burst.endswith("OM")):
                        editor.newline(); last_esc = False; _draw(); continue
                    handled_paste = False
                    for kind, payload in scanner.feed(burst):
                        if kind == "paste":
                            editor.paste(payload)
                            handled_paste = True
                    last_esc = False
                    if handled_paste:
                        _draw()
                    continue
                last_esc = False
                if ch == curses.KEY_RESIZE:
                    _draw(); continue
                if ch == curses.KEY_MOUSE:
                    # Mouse wheel scrolls the transcript (wheel-up = button 4,
                    # wheel-down = button 5). Other mouse events are ignored.
                    try:
                        _id, _mx, _my, _mz, bstate = curses.getmouse()
                    except curses.error:
                        _draw(); continue
                    rows, cols = stdscr.getmaxyx(); lay = compute_layout(rows, cols)
                    if bstate & curses.BUTTON4_PRESSED:
                        transcript.scroll_up(3); _draw()
                    elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                        transcript.scroll_down(3, lay.transcript_h); _draw()
                    continue
                if ch in (curses.KEY_ENTER, 10, 13):
                    # Enter ALWAYS sends. (Shift+Enter is handled in the ESC burst
                    # above as a newline.) Ctrl-O also inserts a newline as a
                    # fallback for terminals that can't distinguish Shift+Enter.
                    if not editor.display().strip():
                        _draw(); continue
                    _submit(); _draw()
                elif ch == 15:  # Ctrl-O = insert a newline (fallback for newline)
                    editor.newline(); _draw()
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    editor.backspace(); _draw()
                elif ch == curses.KEY_DC:
                    editor.delete_forward(); _draw()
                elif ch == curses.KEY_LEFT:
                    editor.left(); _draw()
                elif ch == curses.KEY_RIGHT:
                    editor.right(); _draw()
                elif ch == curses.KEY_UP:
                    editor.up(); _draw()
                elif ch == curses.KEY_DOWN:
                    editor.down(); _draw()
                elif ch == curses.KEY_HOME:
                    editor.home(); _draw()
                elif ch == curses.KEY_END:
                    editor.end(); _draw()
                elif 32 <= ch < 127:
                    ch_s = chr(ch)
                    # Shell-mode hotkey: ":" on an empty input opens the command
                    # palette (vim-style), so you can run a slash-command without
                    # typing "/" first. "/" still works as before.
                    if ch_s in ("/", ":") and editor.is_empty():
                        _open_palette()
                    else:
                        editor.insert_char(ch_s)
                    _draw()
                else:
                    # Configurable command keys (F4): consult the Keymap by token.
                    tok = ctrl_token(ch) or _special_tokens.get(ch)
                    act = keymap.action_for(tok) if tok else None
                    rows, cols = stdscr.getmaxyx(); lay = compute_layout(rows, cols)
                    if act == "file_picker":
                        _open_picker(); _draw()
                    elif act == "command_palette":
                        _open_palette(); _draw()
                    elif act == "find":
                        find_query = ""; find_matches = []; find_idx = 0; _draw()
                    elif act == "message_select":
                        select_idx = len(transcript.messages) - 1 if transcript.messages else None
                        _draw()
                    elif act == "scroll_up":
                        transcript.scroll_up(lay.transcript_h - 1); _draw()
                    elif act == "scroll_down":
                        transcript.scroll_down(lay.transcript_h - 1, lay.transcript_h); _draw()
                    elif act == "quit":
                        break
                    elif act == "steer_now" and controller.running() and not editor.is_empty():
                        controller.steer_now(editor.expand().strip())
                        transcript.add("system", "[steer] injected"); editor.clear(); _draw()
                    elif act == "steer_queue" and controller.running() and not editor.is_empty():
                        controller.steer_queue(editor.expand().strip())
                        transcript.add("system", "[steer] queued"); editor.clear(); _draw()
        except _Quit:
            pass
        finally:
            _set_bracketed(False)
        return last_task_id

    result = curses.wrapper(_main)
    # After curses restores the terminal, show a resume hint if there was a task.
    if result and isinstance(result, str):
        import sys as _sys2
        _sys2.stdout.write(f"\nresume later: syntra resume {result}\n")
        _sys2.stdout.flush()
    return 0
