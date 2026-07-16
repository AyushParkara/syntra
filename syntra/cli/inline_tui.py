"""Inline-mode TUI driver (L2, opt-in) — finalized turns go to the terminal's NATIVE
scrollback (native wheel / find / copy work on history), a small live region stays pinned
at the bottom. Separate from the full-screen curses cockpit (tui2.py), which is untouched.

This is the terminal-glue layer; the rendering math + escape sequences live in
core/inline_render.py (fully unit-tested), and the run machinery reuses RunController
(cli/tui.py) + InputEditor + Transcript. The full transcript is RETAINED in the model, so
model/keyboard features stay reachable; only the VISUAL history is delegated to the
terminal. v1: committed scrollback is fixed on resize. See
[[project-syntra-tui-improvements]].
"""

from __future__ import annotations

import os
import select
import sys
import time

from ..core.inline_render import InlineSession, prewrap_turn, split_committable


def _make_transcript():
    from ..core.tui_model import Transcript as _T
    return _T()


def run_inline(run_goal, *, startup_note_fn=None, live_rows: int = 3) -> int:
    """Drive Syntra in inline mode. Returns an exit code. Reuses RunController so the run
    executes on a background thread while input stays live."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("not a real terminal")

    from .tui import RunController
    from ..core.input_editor import InputEditor

    fd_out = sys.stdout.fileno()
    fd_in = sys.stdin.fileno()

    sess = InlineSession(live_rows=max(2, live_rows), fd=fd_out)
    editor = InputEditor()
    controller = RunController(run_goal)
    transcript = _make_transcript()

    _stream = {"buf": ""}            # in-progress assistant text not yet line-committed
    _run = {"active": False, "start": 0.0}
    # When plan-review is ON (default), a run can finish with verdict "plan_pending": the plan
    # is proposed and we wait for the user to approve it. Inline mode is minimal, so the approval
    # is a one-line prompt (Enter = approve & run · d = discard · anything else = re-plan with
    # your change as the new goal). `_pending` is True while a plan awaits approval.
    _pending = {"active": False}

    def _cols():
        return sess.size()[1]

    def _commit_text(text: str, prefix: str = "") -> None:
        """Pre-wrap `text` to the current width and commit each row to native scrollback,
        retaining it in the model too."""
        cols = _cols()
        body = (prefix + text) if prefix else text
        rows = prewrap_turn(body, cols)
        for r in rows:
            sess.commit_turn(r)

    def _flush_stream(final: bool = False) -> None:
        """Commit any COMPLETE streamed lines to scrollback; keep the partial tail live."""
        done, tail = split_committable(_stream["buf"])
        for line in done:
            _commit_text(line)
        _stream["buf"] = tail
        if final and tail:
            _commit_text(tail)
            _stream["buf"] = ""

    def _draw_live() -> None:
        lines = []
        # row 0: in-progress streamed tail (so the user sees tokens land before the
        # newline commits the line to scrollback)
        if _stream["buf"]:
            lines.append(_stream["buf"])
        # status row
        if _run["active"]:
            el = time.time() - _run["start"]
            model = controller.active_model or ""
            lines.append(f"  … working {el:0.0f}s" + (f" · {model}" if model else ""))
        elif _pending["active"]:
            lines.append("  ❯ Enter approve & run · d discard · type a change to re-plan")
        else:
            lines.append("  enter a goal · Ctrl+C interrupt · Ctrl+D quit")
        # prompt row
        lines.append("❯ " + editor.display())
        sess.draw_live(lines)

    def _start(goal: str) -> None:
        _commit_text(goal, prefix="❯ ")
        transcript.add("user", goal)
        _stream["buf"] = ""
        _run["active"] = True
        _run["start"] = time.time()
        controller.start(goal)

    sess.start(raw=True)
    if startup_note_fn:
        try:
            note = startup_note_fn()
            if note:
                _commit_text(str(note))
        except Exception:  # noqa: BLE001 - a startup note must never block the session
            pass
    _draw_live()

    rc = 0
    try:
        while True:
            # poll the run for streamed output
            if _run["active"]:
                for role, text in controller.poll():
                    if role in ("assistant", "assistant_stream", "stream"):
                        # F14: the engine also emits role "stream" (tui.py/tui2.py handle it);
                        # without it here every token committed as its own dim line → garbled.
                        _stream["buf"] += text
                        _flush_stream()
                    else:
                        # discrete status/tool/route lines commit immediately (dim)
                        _flush_stream()
                        _commit_text(str(text))
                if controller.finished and not controller.running():
                    _flush_stream(final=True)
                    if controller.error is not None:
                        _commit_text(f"error: {controller.error}")
                    elif controller.result is not None:
                        transcript.add("assistant", "")  # retained boundary
                    # Plan-review ON (default): the run proposed a plan and is waiting. Don't
                    # close the turn with a blank separator — arm the inline approval prompt.
                    if getattr(controller.result, "verdict", "") == "plan_pending":
                        _pending["active"] = True
                        _commit_text("  plan ready — Enter to approve & run · d to discard · "
                                     "or type a change")
                    else:
                        _commit_text("")  # blank separator between turns
                    _run["active"] = False

            _draw_live()

            # non-blocking input read (short timeout so streaming stays smooth)
            r, _, _ = select.select([fd_in], [], [], 0.05 if _run["active"] else 0.25)
            if not r:
                continue
            try:
                data = os.read(fd_in, 4096)
            except OSError:
                break
            if not data:
                break
            for ch in data.decode("utf-8", "replace"):
                o = ord(ch)
                if o == 4:                       # Ctrl-D
                    if not editor.text and not _run["active"]:
                        raise _InlineQuit()
                elif o == 3:                     # Ctrl-C / interrupt
                    if _run["active"]:
                        controller.hard_stop()
                        _flush_stream(final=True)
                        _commit_text("⊘ interrupted")
                        _commit_text("")
                        _run["active"] = False
                    elif editor.text:
                        editor.text = ""; editor.cursor = 0
                    else:
                        raise _InlineQuit()
                elif o in (10, 13):              # Enter
                    goal = editor.expand().strip() if hasattr(editor, "expand") else editor.text.strip()
                    editor.clear()
                    if _pending["active"] and not _run["active"]:
                        # Acting on a pending plan: empty Enter or "y" → approve & run; "d"/
                        # "discard" → drop it; anything else → re-plan with that as the new goal.
                        _pending["active"] = False
                        low = goal.lower()
                        if not goal or low in ("y", "yes", "approve", "run"):
                            _commit_text("✓ plan approved — running")
                            _start("/resume")
                        elif low in ("d", "discard", "n", "no"):
                            # Drop the pending plan: just clear the prompt. The next goal starts
                            # fresh (run_goal supersedes the un-resumed plan), so there's nothing
                            # to send — sending "/clear" here would reach run_goal, which doesn't
                            # handle it (that's a session-dispatch command).
                            _commit_text("plan discarded")
                            _commit_text("")
                        else:
                            _commit_text(f"✎ modifying the plan: {goal}")
                            _start(goal)
                    elif goal and not _run["active"]:
                        # /attach in inline mode: stage an image for the next message (path arg,
                        # or no arg = clipboard image). The engine reads it as initial_images and
                        # auto-routes to a vision model. Mirrors the TUI; minimal inline feedback.
                        if goal == "/attach" or goal.startswith("/attach "):
                            arg = goal[len("/attach"):].strip().strip('"').strip("'")
                            fn = getattr(controller._run_goal, "attach_image", None)
                            if fn is None:
                                _commit_text("attachments not available")
                            elif arg in ("clear", "none", "drop"):
                                cf = getattr(controller._run_goal, "clear_attachments", None)
                                if cf:
                                    cf()
                                _commit_text("attachments cleared")
                            else:
                                src = arg
                                if not arg:                       # no path → clipboard image
                                    try:
                                        from ..core.clipboard import read_image
                                        src = read_image()
                                    except Exception:  # noqa: BLE001
                                        src = None
                                    if src is None:
                                        _commit_text("no image on the clipboard (or no clipboard tool)")
                                        src = False
                                if src is not False:
                                    ok, label = fn(src)
                                    _commit_text(f"📎 attached {label} — sends with your next message"
                                                 if ok else f"couldn't attach: {label}")
                        else:
                            _start(goal)
                elif o in (127, 8):              # Backspace
                    editor.backspace()
                elif o == 27:                    # Esc -> interrupt a run
                    if _run["active"]:
                        controller.hard_stop()
                        _flush_stream(final=True)
                        _commit_text("⊘ interrupted")
                        _commit_text("")
                        _run["active"] = False
                elif 32 <= o:                    # printable
                    editor.insert(ch)
            _draw_live()
    except _InlineQuit:
        rc = 0
    finally:
        try:
            if controller.running():
                controller.hard_stop()
        except Exception:  # noqa: BLE001
            pass
        sess.stop(raw=True)
    return rc


class _InlineQuit(Exception):
    pass
