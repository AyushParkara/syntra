"""Widget-based curses TUI (Track T3).

Modular widget-driven layout. Every panel is a Widget in a split tree.
Users can toggle, resize, and rearrange panels. Keyboard-first.

Key bindings:
    Enter       = send
    Shift+Enter = newline (or Ctrl-O)
    Esc Esc     = quit
    Tab         = cycle focus
    Ctrl+B      = toggle left sidebar
    Ctrl+`      = toggle bottom terminal
    Ctrl+D      = quit
    Ctrl+K      = kill running task
    Ctrl+L      = clear chat
    Ctrl+P      = command palette (via chat slash)
    PgUp/PgDn   = scroll focused widget
    Mouse wheel = scroll
"""

from __future__ import annotations

import json
import os
import sys as _sys
import time as _time
from pathlib import Path as _Path

from ..core.widget import (
    Rect, WidgetRegistry,
    LayoutSplit, LayoutLeaf, resolve_layout,
    layout_to_dict, layout_from_dict,
)


def _state_root() -> _Path:
    explicit = os.environ.get("SYNTRA_STATE_DIR")
    if explicit:
        return _Path(explicit).expanduser().resolve()
    p = _Path.home() / ".config" / "syntra" / "state"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return (_Path.cwd() / ".syntra" / "state").resolve()


def _state_json(name: str, default):
    paths = [_state_root() / name]
    legacy = _Path(os.environ.get("SYNTRA_STATE_DIR", ".syntra")) / name
    if legacy not in paths:
        paths.append(legacy)
    for p in paths:
        try:
            return json.loads(p.read_text())
        except (FileNotFoundError, ValueError, OSError):
            continue
    return default


def _register_all(reg: WidgetRegistry, *, workspace: str = "") -> None:
    """Register all built-in widget classes and create default instances."""
    from ..core.widgets.chat import ChatWidget
    from ..core.widgets.file_tree import FileTreeWidget
    from ..core.widgets.git_status import GitStatusWidget
    from ..core.widgets.diff_viewer import DiffViewerWidget
    from ..core.widgets.activity_log import ActivityLogWidget
    from ..core.widgets.token_monitor import TokenMonitorWidget
    from ..core.widgets.run_output import RunOutputWidget
    from ..core.widgets.shortcuts import ShortcutsWidget
    from ..core.widgets.model_router import ModelRouterWidget
    from ..core.widgets.agent_status import AgentStatusWidget
    from ..core.widgets.file_viewer import FileViewerWidget
    from ..core.widgets.workspace_overview import WorkspaceOverviewWidget
    from ..core.widgets.activity_tree_widget import ActivityTreeWidget

    reg.register("chat", ChatWidget)
    reg.register("file_tree", FileTreeWidget)
    reg.register("git_status", GitStatusWidget)
    reg.register("diff_viewer", DiffViewerWidget)
    reg.register("activity_log", ActivityLogWidget)
    reg.register("token_monitor", TokenMonitorWidget)
    reg.register("run_output", RunOutputWidget)
    reg.register("shortcuts", ShortcutsWidget)
    reg.register("model_router", ModelRouterWidget)
    reg.register("agent_status", AgentStatusWidget)
    reg.register("file_viewer", FileViewerWidget)
    reg.register("workspace_overview", WorkspaceOverviewWidget)
    reg.register("activity_tree", ActivityTreeWidget)

    reg.create("chat", "chat")
    reg.create("file_tree", "file_tree", root=workspace or os.getcwd())
    reg.create("git_status", "git_status")
    reg.create("diff_viewer", "diff_viewer")
    reg.create("activity_log", "activity_log")
    reg.create("token_monitor", "token_monitor")
    reg.create("run_output", "run_output")
    reg.create("shortcuts", "shortcuts")
    reg.create("model_router", "model_router")
    reg.create("agent_status", "agent_status")
    reg.create("file_viewer", "file_viewer")
    reg.create("workspace_overview", "workspace_overview")
    reg.create("activity_tree", "activity_tree")


# ────────────────────────── Layouts ──────────────────────────

def _full_layout() -> LayoutSplit:
    """Full layout matching the reference image."""
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutSplit("v", [
                LayoutLeaf("file_tree", weight=3.0),
                LayoutLeaf("git_status", weight=2.0),
                LayoutLeaf("shortcuts", weight=2.0),
            ], weight=0.9),
            LayoutLeaf("chat", weight=3.0),
            LayoutSplit("v", [
                LayoutLeaf("diff_viewer", weight=2.5),
                LayoutLeaf("run_output", weight=2.0),
            ], weight=1.5),
            LayoutSplit("v", [
                LayoutLeaf("activity_log", weight=2.0),
                LayoutLeaf("token_monitor", weight=1.0),
            ], weight=1.0),
        ], weight=1.0),
    ])


def _router_layout() -> LayoutSplit:
    """Routing-focused layout: chat + model router + agent status."""
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutSplit("v", [
                LayoutLeaf("agent_status", weight=1.0),
                LayoutLeaf("model_router", weight=2.0),
            ], weight=1.0),
            LayoutLeaf("chat", weight=3.0),
            LayoutSplit("v", [
                LayoutLeaf("activity_log", weight=2.0),
                LayoutLeaf("token_monitor", weight=1.0),
            ], weight=1.0),
        ], weight=1.0),
    ])


def _focus_layout() -> LayoutSplit:
    """Just chat."""
    return LayoutSplit("v", [
        LayoutLeaf("chat", weight=1.0),
    ])


def _coding_layout() -> LayoutSplit:
    """Chat + file tree + diff."""
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutLeaf("file_tree", weight=0.8),
            LayoutLeaf("chat", weight=3.0),
            LayoutSplit("v", [
                LayoutLeaf("diff_viewer", weight=1.0),
                LayoutLeaf("run_output", weight=1.0),
            ], weight=1.5),
        ], weight=1.0),
    ])


def _review_layout() -> LayoutSplit:
    """Chat + diff + activity."""
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutLeaf("chat", weight=1.5),
            LayoutSplit("v", [
                LayoutLeaf("diff_viewer", weight=1.0),
                LayoutLeaf("activity_log", weight=1.0),
            ], weight=1.0),
        ], weight=1.0),
    ])


def _default_catalog_path() -> str:
    from ..core.paths import default_catalog_path
    return str(default_catalog_path())


def _plugin_lines(rows: list[dict]) -> list[str]:
    """Format the /plugins info-popup body. Pure so it is unit-testable and so the old inline
    version's `state` local can't shadow the outer TUI state dict (that shadow made `state` a
    function-local for the whole command handler → `/image` and other branches that read the outer
    state dict raised UnboundLocalError)."""
    lines = ["Installed plugins — toggle from the CLI:",
             "  syntra plugins --disable <name> / --enable <name>", ""]
    for r in rows:
        mark = "●" if r["enabled"] else "○"
        suffix = "" if r["enabled"] else "  (disabled)"
        lines.append(f"  {mark} {r['name']}{suffix}")
        lines.append(f"      {r['summary']}")
    return lines


def _clean_layout() -> LayoutSplit:
    """Default: just the conversation, full width. Panels summoned on demand."""
    return LayoutSplit("v", [LayoutLeaf("chat", weight=1.0)])


_LAYOUTS = {
    "default": _clean_layout,   # clean single column is the default now
    "clean": _clean_layout,
    "cockpit": _full_layout,    # the 8-panel view is opt-in
    "full": _full_layout,
    "focus": _focus_layout,
    "coding": _coding_layout,
    "review": _review_layout,
    "router": _router_layout,
}


# ── layout-tree helpers for summoning/dropping panels ──

def _layout_has(node, name: str) -> bool:
    if isinstance(node, LayoutLeaf):
        return node.widget_name == name
    return any(_layout_has(c, name) for c in node.children)


def _strip_outer(node):
    """If the root is a 1-child vertical wrapper, unwrap it for cleaner nesting."""
    if isinstance(node, LayoutSplit) and len(node.children) == 1:
        return node.children[0]
    return node


def _find_main_hsplit(node):
    """Walk down single-child vertical wrappers to find the main h-split."""
    if isinstance(node, LayoutSplit):
        if node.direction == "h":
            return node
        if len(node.children) == 1:
            return _find_main_hsplit(node.children[0])
    return None


def _drop_from_layout(node, name: str):
    """Return a new tree with the named leaf removed; collapse empties."""
    if isinstance(node, LayoutLeaf):
        return node
    kept = []
    for c in node.children:
        if isinstance(c, LayoutLeaf):
            if c.widget_name == name:
                continue
            kept.append(c)
        else:
            pruned = _drop_from_layout(c, name)
            if isinstance(pruned, LayoutSplit) and not pruned.children:
                continue
            kept.append(pruned)
    if not kept:
        return LayoutLeaf("chat", 1.0)  # never end up empty
    if len(kept) == 1:
        return kept[0]
    return LayoutSplit(node.direction, kept, node.weight)


# ────────────────────────── Layout persistence ──────────────────────────

def _layouts_path() -> str:
    return str(_state_root() / "layouts.json")


def _save_layout(name: str, node) -> None:
    path = _layouts_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            data = json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data[name] = layout_to_dict(node)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _load_saved_layout(name: str):
    path = _layouts_path()
    try:
        with open(path) as f:
            data = json.loads(f.read())
        if name in data:
            return layout_from_dict(data[name])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


# ────────────────────────── TUI driver ──────────────────────────

def run_tui2(run_goal, *, startup_note_fn=None) -> int:
    """Launch the widget-based full-screen TUI."""
    try:
        import curses
    except Exception as e:
        raise RuntimeError(f"curses unavailable: {e}") from e

    if not (_sys.stdin.isatty() and _sys.stdout.isatty()):
        raise RuntimeError("not a real terminal")

    # Adopt the environment's UTF-8 locale BEFORE curses initializes (#137). Python's curses
    # advances the cursor using the C library's wide-char width; until the locale is set to the
    # terminal's UTF-8, libc reports the wrong width for astral emoji (📎 U+1F4CE / 🔒), so the
    # cursor under-advances and the NEXT glyph over-paints the emoji's second cell — which shows
    # up as a doubled trailing character (e.g. an attach chip "photo.jpeg" rendering "photo.jpegg").
    # setlocale("") makes ncurses measure wide glyphs correctly, so the doubling disappears.
    try:
        import locale as _locale
        _locale.setlocale(_locale.LC_ALL, "")
    except Exception:  # noqa: BLE001 - a locale that won't set must never block launch
        pass

    from ..core.themes import theme_colors, current_theme

    # Graphics capability: probe the REAL terminal ONCE, BEFORE curses grabs the tty (the probe
    # needs cbreak + to read the reply). The terminal reports its truth (kitty/sixel/none) instead
    # of env guessing — so images render sharply where supported and fall back cleanly where not.
    # Cached on `state` and reused at every image paint site (never re-probe per image).
    try:
        from ..core import terminal_image as _ti_probe
        _img_caps = _ti_probe.caps_for_terminal()
    except Exception:  # noqa: BLE001 - probing must never block launch
        _img_caps = None

    registry = WidgetRegistry()
    _register_all(registry, workspace=os.getcwd())

    # mutable layout state
    state = {"layout": _clean_layout(), "sidebar": False, "terminal": False,
             "img_caps": _img_caps}   # cached graphics caps from the startup probe
    # session info for the resume hint — filled by _main, printed AFTER curses exits so
    # it shows on EVERY exit path (clean quit, Ctrl+C, even an exception).
    session = {"task_id": "", "cost": 0.0, "tokens": 0}

    class _Quit(Exception):
        pass

    def _main(stdscr):
        from ..core.tui_model import BRAND_MARK   # one constant → swap the brand glyph anywhere
        curses.curs_set(0)
        stdscr.keypad(True)
        # #232: deliver Ctrl+Z to the app as a keystroke (0x1a → editor UNDO) instead of the
        # tty's SIGTSTP job-suspend. We clear ONLY VSUSP (leave VINTR/Ctrl+C, VQUIT intact),
        # so Ctrl+C etc. behave exactly as before. Best-effort: skipped where there's no tty.
        try:
            import termios as _termios
            _tfd = _sys.stdin.fileno()
            _tattr = _termios.tcgetattr(_tfd)
            if _tattr[6][_termios.VSUSP] != b"\x00":
                _tattr[6][_termios.VSUSP] = b"\x00"    # disable ^Z suspend
                _termios.tcsetattr(_tfd, _termios.TCSANOW, _tattr)
        except Exception:  # noqa: BLE001 - no tty / unsupported → Ctrl+Z just won't undo
            pass
        # Recognize a lone Esc fast. Default ESCDELAY is 1000ms, which makes pressing
        # Esc (to cancel a modal / interrupt) feel laggy or unresponsive — a real bug.
        # Configurable via SYNTRA_ESCDELAY (ms) for laggy SSH links where split escape
        # sequences need a longer window; clamped to a sane 10–1000ms.
        try:
            _esc_ms = int(os.environ.get("SYNTRA_ESCDELAY", "25"))
        except ValueError:
            _esc_ms = 25
        _esc_ms = max(10, min(1000, _esc_ms))
        try:
            curses.set_escdelay(_esc_ms)
        except (AttributeError, curses.error):
            pass
        # MOUSE MODE (research-grounded — mature terminal UIs enable ZERO motion tracking and let the
        # TERMINAL own text selection; any-motion ?1003h was the root cause of lag +
        # auto-select + flaky clicks here). Default = CLICK-ONLY (button press/release +
        # wheel, NO motion): in-app clicks (collapse, tabs, agents line, menu/permission
        # rows) + scroll still work, and because we never capture motion the terminal's
        # OWN drag-select/copy works normally (rock-solid, every terminal). SYNTRA_MOUSE_MODE
        # overrides: "click" (default) · "drag" (?1002 button-event motion) · "all" (?1003).
        _mouse_mode = (os.environ.get("SYNTRA_MOUSE_MODE", "click") or "click").lower()
        _any_motion = False
        try:
            if _mouse_mode in ("drag", "1002", "all", "1003", "motion"):
                # opt-in: capture motion (for in-app drag-select / hover features)
                curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
                _seq = "1003" if _mouse_mode in ("all", "1003", "motion") else "1002"
                try:
                    _sys.stdout.write(f"\x1b[?{_seq}h"); _sys.stdout.flush()
                    _any_motion = True
                except Exception:  # noqa: BLE001
                    pass
            else:
                # DEFAULT: click + wheel only, NO motion reporting → no firehose, and the
                # terminal keeps native text selection. ncurses ?1000/?1006 (no ?1003).
                curses.mousemask(curses.ALL_MOUSE_EVENTS & ~curses.REPORT_MOUSE_POSITION)
        except (curses.error, AttributeError):
            try:
                curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
            except curses.error:
                pass

        # ── theme colors ──
        role_attrs: dict[str, int] = {}

        def _load_colors():
            role_attrs.clear()
            from ..core.themes import color_enabled
            if not color_enabled():
                return   # NO_COLOR: leave role_attrs empty -> _attr() returns 0 (monochrome)
            try:
                curses.start_color()
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
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

        _load_colors()

        def _attr(style: str) -> int:
            return role_attrs.get(style, role_attrs.get("default", 0))

        # ── terminal title (tab name) ──
        _title_state = {"last": None, "name": ""}

        def _set_title(title: str):
            # Compose the OS tab/window title. When the session has a user-set name
            # (/title sets _title_state["name"]), it LEADS so the tab reads
            # "<name> — <status>" instead of just "Syntra…" (Bug H1). Only writes when the
            # text actually changed (the draw loop calls this every frame; the OSC churn
            # was pointless + flickery).
            name = (_title_state.get("name") or "").strip()
            full = f"{name} — {title}" if name else title
            if full == _title_state["last"]:
                return
            _title_state["last"] = full
            # #250(a): the session name can be MODEL-DERIVED (the analyzer titles the session
            # from the goal), so strip control chars before writing it into the OSC-0 title —
            # else a prompt-injected BEL/ESC breaks out of the sequence and injects terminal
            # control codes. Sanitize the composed string (covers both name + status).
            from ..core.tui_model import sanitize_terminal_title
            try:
                _sys.stdout.write(f"\x1b]0;{sanitize_terminal_title(full)}\x07")
                _sys.stdout.flush()
            except Exception:
                pass

        # ── OSC 9;4 taskbar/tab progress (#178) ── a cheap ambient "agent working / done /
        # failed" signal OUTSIDE the TUI: the terminal's taskbar button or tab fills/colors.
        # Deduped like the title so the draw loop can call it freely. Safe no-op where the
        # terminal ignores the sequence.
        _progress_state = {"last": None}

        def _set_progress(state: str, percent: int = 0):
            key = (state, int(percent))
            if key == _progress_state["last"]:
                return
            _progress_state["last"] = key
            try:
                from ..core.tui_model import osc9_4_progress
                _sys.stdout.write(osc9_4_progress(state, percent))
                _sys.stdout.flush()
            except Exception:  # noqa: BLE001 - an ambient signal must never break the TUI
                pass

        _set_title(f"Syntra {BRAND_MARK}".strip())

        # ── Kitty keyboard protocol (enhanced key detection) ──
        _kitty_protocol = False
        try:
            _sys.stdout.write("\x1b[>1u")  # request kitty protocol level 1
            _sys.stdout.flush()
            _kitty_protocol = True
        except Exception:
            pass

        # ── bracketed paste ──
        def _set_bracketed(on: bool):
            try:
                _sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
                _sys.stdout.flush()
            except Exception:
                pass

        last_esc = False
        _esc_time = 0.0
        # SYNTRA_KEY_DEBUG=1 shows the last keypress (code + curses keyname) in the status bar —
        # a dev diagnostic for keymap/terminal issues. Off by default; zero cost when off.
        _key_debug = (os.environ.get("SYNTRA_KEY_DEBUG", "") or "").strip().lower() in ("1", "true", "yes", "on")
        _last_key = {"code": -999, "name": ""}

        def _esc_burst() -> str:
            seq = "\x1b"
            # Arrow keys and other CSI sequences can arrive a few milliseconds after the
            # initial ESC (especially with a tight ESCDELAY). If we poll with timeout=0 and
            # break on the first -1, we misclassify arrows as a bare Esc and swallow them.
            # Use a tiny, bounded wait to reliably slurp the rest of the sequence.
            stdscr.timeout(5)
            try:
                quiet = 0
                start = _time.time()
                while True:
                    c = stdscr.getch()
                    if c == -1:
                        quiet += 1
                        # Bound the total wait, but tie it to the configured ESCDELAY so a laggy
                        # SSH link (large SYNTRA_ESCDELAY) still has time to deliver the CSI tail of
                        # a split arrow burst — otherwise an arrow is misread as a lone Esc.
                        if quiet >= 2 or (_time.time() - start) > max(0.06, (_esc_ms / 1000.0) * 2.0):
                            break
                        continue
                    quiet = 0
                    if 0 <= c <= 0x10FFFF:
                        seq += chr(c)
                    if len(seq) > 200_000:
                        break
            finally:
                stdscr.timeout(80)
            return seq

        # Map a raw CSI/SS3 escape burst → the curses keycode the rest of the loop expects.
        # ncurses sometimes hands us a bare ESC + the raw "[D"/"[3~" tail (split-sequence
        # under a tight ESCDELAY, or a terminal whose terminfo lacks the cap) instead of the
        # assembled KEY_* code. Without this, plain arrows / Home / End / Delete are SWALLOWED
        # by the esc-burst path and the cursor never moves (confirmed live). We normalize the
        # common xterm sequences (incl. the modified ctrl/alt/shift forms) back to a keycode so
        # one code path handles every terminal. Returns None when the burst isn't a known key.
        _CTRL_DEL = 0x7F0001   # private sentinel for Ctrl+Delete (delete word ahead); not a curses code
        _CSI_BASE = {
            "A": curses.KEY_UP, "B": curses.KEY_DOWN, "C": curses.KEY_RIGHT, "D": curses.KEY_LEFT,
            "H": curses.KEY_HOME, "F": curses.KEY_END,
            "5~": curses.KEY_PPAGE, "6~": curses.KEY_NPAGE, "3~": curses.KEY_DC,
            "2~": getattr(curses, "KEY_IC", 331),
        }
        # ctrl (modifier 5) variants → the SLEFT/SRIGHT-style codes the editor handlers use.
        # Ctrl+Delete (3~) maps to a DISTINCT sentinel (not plain KEY_DC=330) so the editor can
        # delete the word AHEAD, not just one char — matching Ctrl+Backspace deleting the word behind.
        _CSI_CTRL = {
            "C": getattr(curses, "KEY_SRIGHT", 561), "D": getattr(curses, "KEY_SLEFT", 546),
            "A": 567, "B": 526, "H": 536, "F": 531, "3~": _CTRL_DEL,
        }

        def _decode_one(seq_with_esc: str):
            """(keycode, alt) for ONE recognized cursor/edit escape sequence, else (None, alt).
            `alt` is True for Alt/Meta-modified forms (ESC ESC [X or ESC [1;3X)."""
            b = seq_with_esc
            if not b.startswith("\x1b"):
                return None, False
            alt = False
            if b.startswith("\x1b\x1b"):        # Alt+arrow as a doubled escape: ESC ESC [ X
                alt = True
                b = b[1:]
            body = b[1:]
            if body.startswith("O"):            # SS3 (application cursor): ESC O A/B/C/D/H/F
                return _CSI_BASE.get(body[1:]), alt
            if not body.startswith("["):
                return None, alt
            seq = body[1:]                       # after "["
            if seq.startswith("1;") and len(seq) >= 4:   # modified: 1;5D ctrl / 1;3D alt / 1;2D shift
                mod, final = seq[2], seq[3:]
                if mod == "5":
                    return _CSI_CTRL.get(final), alt
                if mod == "3":
                    return _CSI_BASE.get(final), True
                return _CSI_BASE.get(final), alt
            return _CSI_BASE.get(seq), alt        # plain: "D", "3~", "5~", …

        def _split_csi(burst: str):
            """A fast input burst can carry SEVERAL escape sequences glued together
            (autorepeat / quick presses deliver e.g. '\\x1b[D\\x1b[D' in one read). Split on
            the ESC boundaries and decode each, so every keypress lands. Returns a list of
            (keycode, alt); empty if NOTHING in the burst is a recognized key."""
            if not burst.startswith("\x1b"):
                return []
            # split into chunks each beginning with ESC (keep a leading ESC ESC together so
            # an Alt-modified sequence isn't torn apart).
            parts, i, n = [], 0, len(burst)
            while i < n:
                if burst[i] != "\x1b":
                    break
                j = i + 1
                if j < n and burst[j] == "\x1b":   # doubled ESC (Alt) — consume the pair start
                    j += 1
                # consume up to (not including) the next lone ESC that starts a new sequence
                while j < n and burst[j] != "\x1b":
                    j += 1
                parts.append(burst[i:j])
                i = j
            out = []
            for p in parts:
                key, alt = _decode_one(p)
                if key is not None:
                    out.append((key, alt))
            return out

        # ── focus management ──
        focusable_names = [n for n, w in registry.all().items() if w.focusable]
        focused = "chat"
        # active modal overlay (command palette / file picker / model board). `stack` holds
        # PARENT overlays for nested drill-downs (roleboard → model picker) so ←/Backspace
        # returns to the parent instead of Esc closing the whole thing (user).
        overlay = {"current": None, "stack": []}
        msg_menu = {"current": None, "x": 0, "y": 0}   # per-message popup (Copy/Revert/...)
        panel_menu = {"current": None}                 # Ctrl+E panel checklist
        cmd_menu = {"stack": None, "query": ""}        # chained command menu (Ctrl+P)
        effort_modal = {"active": False, "idx": 0}     # /effort slider (P19)
        wizard_modal = {"current": None, "paused": None}  # question wizard (P11)
        plan_modal = {"current": None}                  # plan-approval modal (interactive + scrollable)
        key_modal = {"current": None}                   # API-key entry popup (masked form)
        access_modal = {"current": None}                 # access-modes popup (file/command permissions)
        perm_modal = {"req": None, "geom": None, "hover": None, "reason": None, "explain": False}  # interactive tool-permission prompt (geom/hover for clickable+hover; reason=str while typing deny+tell guidance; explain=#222 Ctrl+E risk-explainer toggle)
        compare_modal = {"view": None}                  # /compare side-by-side cards (A-fix 5)
        # Inline terminal image (view/preview): when set, _sync_refresh paints the image's
        # graphics escape into a reserved region AFTER the text frame. {path, caps, id, drawn}.
        image_overlay = {"path": None, "caps": None, "id": 0, "drawn_id": None}
        # Minimap rail expanded panel (hover the right-edge rail → rolling-wheel msg list).
        # `focus` = index into the rail's message index; `geom` = (panel_top_row, left_col)
        # recorded at draw so a click maps to a row. Read-only navigation (scroll only).
        minimap_modal = {"active": False, "focus": 0, "geom": None}
        # configurable keymap (A1): translate a user's rebound key (from
        # ~/.config/syntra/keymap.json) to the canonical default code the handlers below
        # check, so rebinding works without touching any handler. Empty for the default map.
        try:
            from ..core.keymap import Keymap as _Keymap, remap_table as _remap_table
            _kpath = os.path.expanduser("~/.config/syntra/keymap.json")
            _keymap = _Keymap.load(_kpath if os.path.exists(_kpath) else None)
            _key_remap = _remap_table(_keymap)
        except Exception:  # noqa: BLE001 - a bad keymap must never stop the TUI booting
            from ..core.keymap import Keymap as _Keymap
            _keymap = _Keymap()
            _key_remap = {}
        # #173: user-bound /commands on keys (a SEPARATE layer from the action keymap above).
        # keycode -> /command; only free (non-reserved) single keys survive. Fail-open to {}.
        _cmd_binds_path = os.path.expanduser("~/.config/syntra/keybinds.json")
        try:
            from ..core.keymap import load_command_binds as _lcb, command_bind_keycode_map as _cbkm
            _cmd_binds = _cbkm(_lcb(_cmd_binds_path))
        except Exception:  # noqa: BLE001 - a bad keybinds file must never stop boot
            _cmd_binds = {}
        # M4: usage-based (frecency) ranking for pickers — most-used models/files/commands
        # float to the top of an empty-query list. Persisted across sessions.
        from ..core.frecency import Frecency as _Frecency
        _frec_path = str(_state_root() / "frecency.json")
        _frec = _Frecency.load(_frec_path)

        def _frec_key(kind: str, row: str) -> str:
            """Stable frecency key for a picker row: model id / session id is the first
            token; file paths and command labels are the row itself."""
            if kind in ("model", "session"):
                return (row.split() or [""])[0]
            return row
        # status-bar context-window + rate-limit data (A1): the active model's context
        # window (catalog) and a transient cooldown signal (route health). Loaded once;
        # per-frame use is a cheap by_id/dict lookup. Best-effort — never block boot.
        try:
            from ..core.catalog import Catalog as _Catalog
            _status_catalog = _Catalog.load(os.environ.get("SYNTRA_CATALOG_PATH") or _default_catalog_path())
        except Exception:  # noqa: BLE001
            _status_catalog = None
        try:
            from .main import _route_health as _rh_loader
            _status_route_health = _rh_loader()
        except Exception:  # noqa: BLE001
            _status_route_health = None
        from ..core.toast import ToastManager
        toasts = ToastManager()
        _pending: list = []                            # queued messages while a run is active
        # drag selection. "active" = mouse currently held; "show" = render the
        # highlight (stays on briefly after release so you see what got copied).
        _sel = {"active": False, "show": False, "x0": 0, "y0": 0, "x1": 0, "y1": 0,
                "cx0": 0, "cx1": 9999,   # cx0/cx1 = column bounds of the box the drag started in (F23)
                # CONTENT-anchored selection for the CHAT (scroll-to-extend copy, #89): a chat drag
                # anchors to content-LINE indices + columns so it survives scrolling and copy can
                # reach off-screen lines. mode "screen" (default, every other panel) keeps the
                # screen-coord x0/y0/x1/y1 path; "content" uses cl0/ccol0..cl1/ccol1 instead.
                "mode": "screen", "cl0": 0, "ccol0": 0, "cl1": 0, "ccol1": 0,
                # #122: while a content drag is held PAST the top/bottom edge, the cursor can't
                # move further so motion events STOP — scrolling would stall (the "bottom-to-top
                # is buggy/slow" bug). We record the last drag point + its edge direction so the
                # IDLE tick keeps auto-scrolling smoothly until the button is released.
                "edge": 0, "last_mx": 0, "last_my": 0}   # edge: -1 up, +1 down, 0 not at an edge
        # U1 (#8 drag-resize): when a press lands on a vertical divider between two side
        # panels, we enter a resize drag — motion adjusts the LEFT panel's layout weight.
        _resize_drag = {"active": False, "left": "", "anchor_x": 0, "w0": 1.0, "px": 0}
        # Chat scrollbar drag: a press on the chat's scrollbar column starts a drag; motion maps
        # the cursor's y within the track → an absolute scroll position (transcript fraction).
        _sb_drag = {"active": False}
        # Last hovered cell — so a pure mouse MOVE only forces a redraw when it actually
        # lands somewhere new (any-motion tracking fires dozens of events per drag of the
        # hand; redrawing each was the lag). Hover features (perm-button highlight, the
        # minimap rail) update through here.
        _hover = {"x": -1, "y": -1}
        _screen_text: list[str] = []                   # last painted rows (for copy)
        _awaiting_input = {"active": False, "message": ""}  # set when loop needs user input
        _active_role = {"role": "", "model": ""}             # current planner/executor/reviewer stage
        _active_mode = {"mode": ""}                          # current pipeline-phase chip for the status bar
        _access_chip = {"mode": "", "tweaks": 0}             # run-mode chip (Plan/Ask/Edit/Auto) for the status bar
        try:
            from ..core.access_modes import load_access_state as _las
            from .main import _state_root as _sr_access
            _st0 = _las(_sr_access() / "access.json")
            _access_chip["mode"] = _st0.mode
            _access_chip["tweaks"] = len(_st0.overrides)
        except Exception:  # noqa: BLE001 - a missing access file just leaves the chip blank
            pass
        # Images the user has attached for the NEXT message (drag-drop / paste / /attach /
        # clipboard probe). Each entry is {"label": str}. They're staged on the engine side
        # (run_goal.attach_image) and ride the next submit; this list drives the 📎 status chip
        # and the count shown in the user bubble. Cleared on submit/discard.
        _attached = {"items": []}                            # list[{"label": str}]
        _copy_highlight = {"msg_idx": -1, "until": 0.0}     # flash copied message

        # ── controller ──
        from .tui import RunController, esc_action as _esc_action
        controller = RunController(run_goal)

        # ── interactive tool-permission prompt (cross-thread) ──
        # Called on the AGENT background thread when a tool wants to run; it parks the
        # request in perm_modal and BLOCKS until the main UI loop renders the modal and
        # the user picks once/session/reject (or /auto-approve skips this entirely).
        def _permission_ask(name, danger, args):
            import threading as _th
            from ..core.permissions import ALLOW_ONCE, ALLOW_SESSION, REJECT  # noqa: F401
            detail = ""
            if name == "bash":
                detail = "$ " + str((args or {}).get("command", ""))[:140]
            elif name in ("write", "edit"):
                # #222: show the ACTUAL change, not just `path:`. For write, a preview of the
                # content; for edit, the old→new so you approve what you can SEE.
                _p = str((args or {}).get("path", ""))
                if name == "write":
                    _c = str((args or {}).get("content", "") or "")
                    _prev = _c[:400] + (" …" if len(_c) > 400 else "")
                    detail = f"write {_p}:\n{_prev}" if _c else f"write {_p} (empty)"
                else:
                    _old = str((args or {}).get("old_string", "") or "")[:200]
                    _new = str((args or {}).get("new_string", "") or "")[:200]
                    detail = f"edit {_p}:\n- {_old}\n+ {_new}"
            elif name == "websearch":
                detail = "search: " + str((args or {}).get("query", ""))[:90]
            # #222: attach a deterministic plain-English what/why/risk (shown lazily via Ctrl+E).
            try:
                from ..core.tui_model import explain_permission as _explain_perm
                _explain = _explain_perm(name, danger, args or {})
            except Exception:  # noqa: BLE001 - the explainer must never block a prompt
                _explain = None
            ev = _th.Event()
            perm_modal["req"] = {"name": name, "danger": danger, "detail": detail,
                                 "explain": _explain, "event": ev, "answer": REJECT}
            perm_modal["explain"] = False    # each new prompt starts collapsed (lazy)
            ev.wait(180)                 # block this tool until the user answers (3min -> reject)
            req = perm_modal["req"]
            perm_modal["req"] = None
            return req["answer"] if req else REJECT
        if hasattr(run_goal, "set_permission_ask"):
            run_goal.set_permission_ask(_permission_ask)

        # Resolve the pending permission prompt. ONE place so the keyboard (1/2/3/a) and a
        # mouse click on the action row do the IDENTICAL thing — the modal is clickable now.
        def _perm_answer(action: str, reason: str = "") -> None:
            from ..core.permissions import (ALLOW_ONCE, ALLOW_SESSION, ALLOW_ALWAYS,
                                            DENY_ONCE, DENY_ALL, deny_guide)
            req = perm_modal["req"]
            if req is None:
                return
            if action == "once":
                req["answer"] = ALLOW_ONCE
                toasts.show("allowed once", "success", ttl=0.8)
            elif action == "session":
                req["answer"] = ALLOW_SESSION
                toasts.show("allowed — this action, for the session", "success", ttl=1.0)
            elif action == "always":          # remember across sessions (durable)
                req["answer"] = ALLOW_ALWAYS
                toasts.show("allowed always — remembered", "success", ttl=1.0)
            elif action == "deny_all":        # stop asking for this action this session
                req["answer"] = DENY_ALL
                toasts.show("denied — won't ask again this session", "info", ttl=1.0)
            elif action == "deny_guide":      # #83: deny + hand the agent a reason/suggestion
                req["answer"] = deny_guide(reason)
                toasts.show("denied — guidance sent to the agent", "info", ttl=1.2)
            else:                              # deny once (default)
                req["answer"] = DENY_ONCE
                toasts.show("denied", "info", ttl=0.8)
            req["event"].set()

        # Test/demo hook (OFF by default): when SYNTRA_DEMO_PERM is set, pop the
        # permission modal once via the REAL _permission_ask path so the PTY harness can
        # verify it renders. A daemon thread waits on the modal's Event like the engine
        # would; the user's choice (e.g. reject) releases it. No-op without the env var.
        if os.environ.get("SYNTRA_DEMO_PERM"):
            import threading as _demo_th
            import time as _demo_t

            def _demo_perm():
                _demo_t.sleep(0.6)
                try:
                    _permission_ask("bash", "destructive", {"command": "rm -rf /tmp/demo"})
                except Exception:  # noqa: BLE001
                    pass
            _demo_th.Thread(target=_demo_perm, daemon=True).start()

        def _mark_queued_sent(verb: str = "sent") -> None:
            """#124: rewrite the '(queued · Esc to send)' annotations in the chat to show the
            queued messages actually WENT (e.g. '✓ sent' / '↪ steered'), so the box reflects the
            state instead of saying 'queued' forever. Also drops any trailing 'queued' system
            notice. Pure text substitution over the transcript — safe, no model calls."""
            if not chat_w:
                return
            for m in chat_w.transcript.messages:
                if getattr(m, "role", "") == "user" and "(queued · Esc to send)" in getattr(m, "text", ""):
                    m.text = m.text.replace("(queued · Esc to send)", f"✓ {verb}")

        def _flush_pending_to_run() -> int:
            """Send all HELD queued messages into the CURRENT run as instant steers, so it picks
            them up at its next step and keeps going (#124). Steers the full run_text (with any
            attached file context), not just the display text. Marks the bubble '✓ steered' and
            returns the count."""
            n = 0
            while _pending:
                _disp, _rt = _pending.pop(0)
                if controller.steer_now(_rt or _disp):   # #124: send run_text (keeps file context)
                    n += 1
            if n:
                _mark_queued_sent("steered")
            return n

        def _interrupt_merge_resend() -> int:
            """Single-Esc (P30): INTERRUPT the current run, MERGE all queued messages
            into one, and RESEND them together as a fresh run. We collapse the queue
            into a single merged entry and request_stop(); the run-finished handler
            then starts that merged run (so we never run two at once). Returns the
            number of messages merged."""
            if not _pending:
                return 0
            disps = [d for d, _rt in _pending]
            runs = [rt for _d, rt in _pending]
            _pending.clear()
            _pending.append((" / ".join(disps), "\n\n".join(runs)))
            controller.request_stop()
            return len(disps)

        def _do_complete_stop(reason: str = "stopped") -> bool:
            """COMPLETE STOP (user's double-Esc spec): kill EVERYTHING — detach the run, signal the
            cooperative stop so the orphaned engine thread + its tool loop + any sub-agents die at
            their next boundary, clear the queue, and — CRUCIALLY — release any pending permission
            prompt (denied) so a thread blocked on `ev.wait` wakes up, sees the stop, and exits
            instead of hanging. Nothing keeps running in the background. Returns True if a run was
            live. ONE place both the main Esc handler and the permission-prompt Esc call."""
            # If a permission prompt is blocking the engine thread, answer it DENY so ev.wait
            # returns; the thread then hits the stop flag (run_agent/_execute_and_review) and ends.
            _req = perm_modal.get("req")
            if _req is not None:
                from ..core.permissions import DENY_ALL
                _req["answer"] = DENY_ALL
                try:
                    _req["event"].set()
                except Exception:  # noqa: BLE001
                    pass
                perm_modal["req"] = None
            stopped = controller.hard_stop()
            _pending.clear()
            if chat_w:
                chat_w.working = False
                chat_w.add("system", "⏹ Stopped." if stopped else "Nothing running.")
                # BUG3: settle any in-flight action-feed item so no live "Editing…" line
                # survives the stop (the run is over — freeze, don't leave it shimmering).
                if getattr(chat_w, "action_feed", None) is not None:
                    chat_w.action_feed.freeze_running()
            if agent_w:
                agent_w.finalize_running()
            if activity_w:
                activity_w.log("interrupted by user", "error")
            toasts.show(reason, "info", ttl=1.0)
            return stopped
        active = False
        cost = 0.0
        tokens_in = tokens_out = 0
        last_task_id = ""
        session_title = ""   # user-set session name (/title); shown in the header
        # True once the USER explicitly set a title via /title — auto-derivation (submit-time
        # or the post-run analyzer upgrade) must then never overwrite their choice.
        # "user" = the user set the title via /title (always wins). "auto" = we've already
        # adopted the analyzer's understood title ONCE — after that the title is STABLE and
        # is NOT re-derived every turn (user: the title kept changing on each message; it
        # should read like one whole-conversation summary, set once).
        _title_locked = {"user": False, "auto": False}
        pending_title = ""   # title set BEFORE any run exists yet → applied to the
                             # real task_id the moment a run mints one (no orphan task)
        speed_tps = 0.0
        _run_start = 0.0
        _run_tokens = 0
        _last_activity_ts = {"t": 0.0}   # #233: wall-clock of the last token/tool event;
                                         # drives the working line's fade-to-red stall cue
        # #129: the answer is STREAMED live (role "stream" → append_stream → one bubble), then the
        # post-run replay re-emits the SAME text as role "assistant" → a DUPLICATE bubble. Track
        # what was streamed this turn so the drain can skip the redundant replay-add. Reset per run.
        _streamed = {"buf": ""}
        # #141: capture the whole-TURN diff the engine emits (loop.py → the shared bridge
        # renders "⎿ changes: <summary>" + the unified-diff lines into the feed). The pure
        # TurnDiffCapture reconstructs the coherent turn diff from those feed lines so bare
        # `/diff` shows THIS turn's changes (not just the git working tree) + a card.
        from ..core.tui_model import TurnDiffCapture as _TDC
        _turn_diff = _TDC()
        # #221: a browsable history of finished-turn diffs — `/diff turns` steps ◀ older ▶.
        from ..core.tui_model import TurnDiffHistory as _TDH
        _turn_diff_hist = _TDH()

        # ── widget refs ──
        chat_w = registry.get("chat")
        # #232: load the persisted input history so ↑/Ctrl-R recall survives restarts.
        _history_path = str(_state_root() / "history.jsonl")
        if chat_w:
            try:
                chat_w.editor.load_history(_history_path)
            except Exception:  # noqa: BLE001 - a bad history file must not stop boot
                pass
        activity_w = registry.get("activity_log")
        token_w = registry.get("token_monitor")
        diff_w = registry.get("diff_viewer")
        run_out_w = registry.get("run_output")
        agent_w = registry.get("agent_status")
        tree_w = registry.get("activity_tree")   # live working-tree (P24/P25)

        # rolling action feed (inline play-by-play) + plan-mode phase ribbon — both pure
        # models, attached to the chat widget which paints them (feed above the working
        # line, ribbon at the top during a plan phase). Fed from the structured event
        # stream in _drain; cleared per run.
        from ..core.action_feed import ActionFeed
        from ..core.plan_ribbon import PlanRibbon
        action_feed = ActionFeed()
        plan_ribbon = PlanRibbon()
        if chat_w is not None:
            chat_w.action_feed = action_feed
            chat_w.plan_ribbon = plan_ribbon

        if chat_w and startup_note_fn:
            try:
                note = startup_note_fn()
                if note:
                    chat_w.add("system", note)
            except Exception:
                pass
        if chat_w:
            from ..core.tui_model import BRANDING as _BR
            _hint = "type a goal · /help · ^E panels · ^Y copy"
            if _BR == "full":
                _inner = 25  # inner width between the borders
                _l1 = "S Y N T R A  v0.2"
                _l2 = "smart model coordinator"
                chat_w.add("system",
                    "╭" + "─" * _inner + "╮\n"
                    "│ " + _l1.ljust(_inner - 1) + "│\n"
                    "│ " + _l2.ljust(_inner - 1) + "│\n"
                    "╰" + "─" * _inner + "╯\n"
                    + _hint)
            elif _BR == "minimal":
                # one quiet line, no boxed hero (user: less branding)
                chat_w.add("system", "syntra · smart model coordinator\n" + _hint)
            else:  # off
                chat_w.add("system", _hint)
            # Resume: preload the prior conversation so `syntra resume` REOPENS the chat
            # (not a CLI re-run). The runner already seeded its history to match.
            _seed = getattr(run_goal, "_initial_messages", None) or []
            if _seed:
                _rid = getattr(run_goal, "_resume_id", "")
                _from = ""
                if _rid:
                    try:
                        from ..core.state import TaskStore
                        _from = TaskStore(_state_root()).branched_from(_rid)
                    except Exception:  # noqa: BLE001 - banner is best-effort
                        _from = ""
                if _from:
                    chat_w.add("system",
                        f"↶ forked from {_from[:8]} — this is an independent copy; "
                        "the original is untouched. Continue below:")
                else:
                    chat_w.add("system", "↩ resumed your previous conversation — continue below:")
                for _role, _txt in _seed:
                    chat_w.add(_role, _txt)
                if _rid:
                    last_task_id = _rid
                    # restore a user-set title so a resumed session keeps its name
                    try:
                        from .main import _state_root as _sr
                        from ..core.state import TaskStore as _TS
                        _meta = _TS(_sr()).load(_rid)
                        session_title = getattr(_meta, "title", "") or ""
                        if session_title:
                            _title_state["name"] = session_title   # tab title on resume too
                    except Exception:  # noqa: BLE001 - missing title just shows the id
                        pass
                    # Bug A / T3: seed the header's $ + token totals from the RESUMED task's
                    # accumulated spend (the engine computes these as run_goal._resume_*).
                    # Without this the header showed $0.00 / 0 tok on resume until a NEW run
                    # finished — the prior spend looked "gone".
                    cost = float(getattr(run_goal, "_resume_cost", 0.0) or 0.0)
                    tokens_in = int(getattr(run_goal, "_resume_tokens_in", 0) or 0)
                    tokens_out = int(getattr(run_goal, "_resume_tokens_out", 0) or 0)
                    if token_w and (cost or tokens_in or tokens_out):
                        token_w.update(input_tokens=tokens_in, output_tokens=tokens_out, cost=cost)
            # load durable memory for the MEMORY tab
            try:
                data = _state_json("session-memory.json", {})
                chat_w.memory_items = data.get("constraints", []) if isinstance(data, dict) else []
            except (ValueError, OSError):
                chat_w.memory_items = []

        # ── L1: synchronized output (DEC mode 2026) ── bracket each frame's terminal
        # write (curses.refresh) with BSU/ESU so supporting terminals present the whole
        # delta ATOMICALLY — eliminates mid-frame flicker/tearing. Ignored (harmless) on
        # terminals that don't support it; gated by SYNTRA_SYNC_OUTPUT (default on) so it
        # can be disabled if a terminal misbehaves. erase()/addnstr only touch the in-
        # memory screen; refresh() is the sole terminal write, so wrapping it suffices.
        _sync_on = os.environ.get("SYNTRA_SYNC_OUTPUT", "1") not in ("0", "false", "no", "")
        try:
            _tty_fd = _sys.stdout.fileno()
        except Exception:  # noqa: BLE001
            _tty_fd = 1

        def _paint_image_overlay():
            """Paint the active inline image AFTER the text frame: curses owns the cells, we
            write the graphics escape straight to the tty into a reserved region at the bottom of
            the chat area. Idempotent per image id (don't re-transmit the same image every frame);
            deletes a previously-drawn Kitty image when the overlay changes/clears (else it smears
            on scroll). No-op when there's no overlay or the terminal can't do graphics."""
            ov = image_overlay
            cur_id = ov.get("id") or 0
            # nothing to show, or already transmitted this exact image → leave it.
            if not ov.get("path"):
                # clear a previously-drawn kitty image so it doesn't linger.
                if ov.get("drawn_id"):
                    try:
                        from ..core.terminal_image import kitty_delete
                        os.write(_tty_fd, kitty_delete(ov["drawn_id"]).encode())
                    except Exception:  # noqa: BLE001
                        pass
                    ov["drawn_id"] = None
                return
            if ov.get("drawn_id") == cur_id:
                return                              # already on screen, don't re-blast it
            try:
                from ..core import terminal_image as _ti
                from pathlib import Path as _P
                data = _P(ov["path"]).read_bytes()
                caps = ov.get("caps") or state.get("img_caps") or _ti.detect_caps()
                rows, cols = stdscr.getmaxyx()
                # reserve a box: ~60% of the height, full chat width-ish, near the bottom.
                box_rows = max(3, min(rows - 4, int(rows * 0.55)))
                box_cols = max(10, cols - 4)
                seq, used = _ti.render_image(data, caps, cols=box_cols, rows=box_rows,
                                             image_id=cur_id, label=_P(ov["path"]).name,
                                             image_path=str(ov["path"]))
                if caps.protocol == _ti.NONE:
                    return                          # placeholder is shown as a chat line instead
                # position cursor at the top of the reserved region (a few rows above the input).
                y = max(1, rows - 2 - box_rows)
                os.write(_tty_fd, f"\x1b[{y};1H".encode())   # move cursor (1-based)
                os.write(_tty_fd, seq.encode() if isinstance(seq, str) else seq)
                ov["drawn_id"] = cur_id
            except Exception:  # noqa: BLE001 - image paint must never break the TUI
                pass

        def _open_image_viewer(path: str) -> bool:
            """Open `path` full-size in the OS image viewer (xdg-open/open/start). Detached, output
            silenced so it never disturbs curses. Returns True if launched. The guaranteed-readable
            path on any terminal — and (per the user's rule) auto-fired when the terminal can't
            render readable inline. Skipped over SSH with no display (nothing to open onto)."""
            try:
                if _sys.platform != "win32" and not (os.environ.get("DISPLAY")
                                                     or os.environ.get("WAYLAND_DISPLAY")):
                    return False   # headless / SSH-no-X: no display to open onto
                import subprocess
                opener = {"darwin": "open", "win32": "start"}.get(_sys.platform, "xdg-open")
                subprocess.Popen([opener, path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                return True
            except Exception:  # noqa: BLE001
                return False

        def _show_image(path: str, *, note: str = "") -> None:
            """Show an image the SELF-MANAGING way (user's rule), using the cached startup probe:
              • terminal CAN render (kitty/iterm/sixel) → paint sharp inline; user opens full-size
                manually (:open) since inline is already readable.
              • terminal CANNOT (no graphics) → still show the best inline preview, AND AUTO-open the
                full image in the OS viewer, because inline here isn't readable.
            One place so every trigger (engine show_image, /view, click, generated image) behaves the
            same."""
            from ..core import terminal_image as _ti
            from pathlib import Path as _P
            p = _P(path)
            if not p.is_file():
                if chat_w:
                    chat_w.add("system", f"no such image: {path}")
                return
            caps = state.get("img_caps") or _ti.detect_caps()
            can_render = caps.protocol in (_ti.KITTY, _ti.ITERM2, _ti.SIXEL)
            if can_render:
                # terminal has a real graphics engine → paint sharp inline; open is MANUAL (:open).
                image_overlay["path"] = str(p)
                image_overlay["caps"] = caps
                image_overlay["id"] = (image_overlay.get("id") or 0) + 1
                image_overlay["drawn_id"] = None
                if chat_w:
                    chat_w.add("system", f"🖼 {p.name} (rendered inline · :open for full-size)"
                               + (f" — {note}" if note else ""))
            else:
                # NO graphics engine → do NOT paint a blurry, useless inline render (that was the
                # "still tried to render + looked bad" noise). Just a clean note + AUTO-open the
                # real viewer, which is the readable path here.
                image_overlay["path"] = None            # clear any prior inline image
                image_overlay["id"] = (image_overlay.get("id") or 0) + 1
                image_overlay["drawn_id"] = None
                opened = _open_image_viewer(str(p))
                if chat_w:
                    if opened:
                        chat_w.add("system", f"🖼 {p.name} — opened full-size in your image viewer "
                                   f"(this terminal can't render images inline; run Syntra in "
                                   f"kitty/Ghostty/WezTerm for sharp inline images)")
                    else:
                        chat_w.add("system", f"🖼 {p.name} — /open to view full-size "
                                   f"(no display to auto-open onto)")

        def _preview_inline(path: str, label: str = "") -> None:
            """Paint a browser-preview PNG INLINE in the terminal — HALF-BLOCKS included. Separate
            from _show_image ON PURPOSE: a rendered web page (flat bg + text/boxes) is readable in
            half-blocks, whereas photos look bad — so images stay on the external-viewer path while
            /preview shows the page right here (the user's explicit ask). Uses the terminal's real
            detected caps: kitty/iterm2/sixel → sharp; truecolor → half-block; only a genuinely
            colorless terminal falls back to a note + external open."""
            from ..core import terminal_image as _ti
            from pathlib import Path as _P
            p = _P(path)
            if not p.is_file():
                if chat_w: chat_w.add("system", f"preview render missing: {path}")
                return
            caps = state.get("img_caps") or _ti.detect_caps()
            if caps.protocol == _ti.NONE:
                # no truecolor/graphics at all → can't render readably here; open externally.
                opened = _open_image_viewer(str(p))
                if chat_w:
                    chat_w.add("system", (f"🖼 preview of {label or p.name} — opened in your image "
                                          f"viewer (this terminal has no color rendering)") if opened
                               else f"🖼 preview saved to {p.name} — /open to view")
                return
            image_overlay["path"] = str(p)
            image_overlay["caps"] = caps          # real caps → half-block on a truecolor VTE
            image_overlay["id"] = (image_overlay.get("id") or 0) + 1
            image_overlay["drawn_id"] = None
            if chat_w:
                chat_w.add("system", f"🖼 preview of {label or p.name} (rendered inline · via "
                           f"{caps.protocol})")

        def _sync_refresh():
            if _sync_on:
                try:
                    os.write(_tty_fd, b"\x1b[?2026h")
                except OSError:
                    pass
            try:
                stdscr.refresh()
                _paint_image_overlay()
            finally:
                if _sync_on:
                    try:
                        os.write(_tty_fd, b"\x1b[?2026l")
                    except OSError:
                        pass

        # ── draw ──
        # _draw() paints SYNCHRONOUSLY (every UI action repaints now — modals/overlays must
        # appear the instant they open). The lag came from the HIGH-FREQUENCY mouse-motion
        # path repainting on every move; that path alone uses _draw_coalesced(), which caps
        # repaints to one per frame interval (a burst of 50 moves → ~3 paints, not 50).
        # frame-throttle approach: throttle only the firehose, keep everything else immediate.
        try:
            _FRAME_MIN_S = 1.0 / max(15, min(120, int(os.environ.get("SYNTRA_MAX_FPS", "60"))))
        except ValueError:
            _FRAME_MIN_S = 1.0 / 60
        _paint = {"last": 0.0}

        def _draw_coalesced():
            """Paint at most once per frame interval — for the mouse-motion firehose only.
            Drops only REDUNDANT intra-frame repaints; the next non-motion event (or the
            motion that crosses the frame boundary) paints, so motion still looks live."""
            now = _time.time()
            if (now - _paint["last"]) < _FRAME_MIN_S:
                return
            _paint["last"] = now
            _draw()

        def _draw():
            nonlocal speed_tps
            _paint["last"] = _time.time()
            rows, cols = stdscr.getmaxyx()
            if rows < 5 or cols < 20:
                return

            # resolve body (between top bar and bottom bar)
            body_rect = Rect(0, 1, cols, rows - 2)
            # responsive: on narrow terminals, collapse to chat-only so panels
            # don't get crushed into unreadable slivers.
            active_layout = state["layout"]
            if cols < 90:
                active_layout = _focus_layout()
            rects = resolve_layout(active_layout, body_rect)

            # reset the screen-text grid used by drag-select copy
            _screen_text[:] = [" " * cols for _ in range(rows)]

            def _record(y: int, x: int, text: str):
                if 0 <= y < len(_screen_text):
                    row = _screen_text[y]
                    end = min(len(row), x + len(text))
                    if x < len(row):
                        _screen_text[y] = row[:x] + text[: end - x] + row[end:]

            stdscr.erase()

            # ── top bar ──
            model_name = getattr(controller, "active_model", "") or ""
            short_model = model_name.split("/")[-1] if "/" in model_name else model_name
            ctx_total = tokens_in + tokens_out

            if _awaiting_input["active"]:
                mode_badge = "⏸ INPUT NEEDED"
                mode_short = "⏸ input"
                mode_style = "error"
                _set_title("Syntra ⏸ input needed")
            elif controller.running():
                from ..core.tui_model import pulse_frame, spinner_frame
                pulse = pulse_frame(int(_time.time() * 4))
                role_label = _active_role["role"].upper()
                elapsed = _time.time() - _run_start
                elapsed_str = f"{int(elapsed)}s" if elapsed < 60 else f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
                _spin = spinner_frame(int(_time.time() * 8))
                _lbl = role_label.lower() if role_label else "working"
                # Minimal, clean top indicator (the ugly ▓█▒░ shimmer is gone). The
                # PROMINENT working cue lives near the chatbox (the chat separator).
                mode_badge = f"{_spin} {_lbl} · {elapsed_str}"
                mode_short = f"{_spin} {_lbl} {elapsed_str}"
                _set_title(f"Syntra {pulse} {_lbl}...")
                mode_style = "dim"
            else:
                # In full mode the READY badge carries the brand glyph; in minimal/off it's
                # just "READY" so the header doesn't show a doubled separator dot.
                from ..core.tui_model import BRANDING as _BR_READY
                _ready = (f"{BRAND_MARK} READY" if _BR_READY == "full" else "READY")
                mode_badge = _ready
                mode_short = _ready
                mode_style = "accent"
                _set_title(f"Syntra {BRAND_MARK}".strip())

            # live "working" cue for the chat separator so it never looks frozen (P5)
            if chat_w is not None:
                # suppress the "/" palette while ANY modal owns input, so it can't leak
                # through an overlay box drawn on top (#12/#16 row-bleed).
                chat_w.suppress_palette = bool(
                    overlay["current"] is not None or effort_modal["active"]
                    or perm_modal["req"] is not None or wizard_modal["current"] is not None
                    or plan_modal["current"] is not None or key_modal["current"] is not None
                    or access_modal["current"] is not None
                    or panel_menu["current"] is not None
                    or (cmd_menu["stack"] is not None and cmd_menu["stack"].is_open)
                    or msg_menu["current"] is not None or compare_modal["view"] is not None)
                chat_w.working = bool(controller.running() or active)
                chat_w.working_tick = int(_time.time() * 10)
                chat_w.working_label = (_active_role["role"] or "working") if chat_w.working else "working"
                # model of the active role → shown beside a fan-out worker in the status line
                chat_w.working_model = _active_role.get("model", "") if chat_w.working else ""
                if chat_w.working and _run_start:
                    _we = _time.time() - _run_start
                    chat_w.working_elapsed = (f"{int(_we)}s" if _we < 60
                                              else f"{int(_we // 60)}m {int(_we % 60):02d}s")
                else:
                    chat_w.working_elapsed = ""
                # effort gradient strip above the input (P19b)
                _eff = os.environ.get("SYNTRA_REASONING_EFFORT", "")
                if _eff == "max" and os.environ.get("SYNTRA_ULTRACODE"):
                    _eff = "ultracode"
                chat_w.effort_level = _eff
                # F53: feed the animated working cue its live token + effort stats
                chat_w.working_tokens = (_run_tokens // 4) if chat_w.working else 0
                chat_w.working_effort = (_eff or "auto") if chat_w.working else ""
                # #233: seconds since the last streamed activity → the working line fades to
                # red when a run goes quiet (looks-stuck cue). Zero when idle (no run).
                if chat_w.working:
                    if not _last_activity_ts["t"]:
                        _last_activity_ts["t"] = _time.time()   # seed on run start
                    chat_w.stall_seconds = _time.time() - _last_activity_ts["t"]
                else:
                    chat_w.stall_seconds = 0.0
                    _last_activity_ts["t"] = 0.0                # reset for the next run
                chat_w.anim_tick = int(_time.time() * 4)
                # #15: surface the live agent/tool counters right by the chatbox cue, so
                # "N agents · N tools" is visible without opening the agents panel.
                if agent_w is not None and chat_w.working:
                    chat_w.running_agents = agent_w.running_count()
                    chat_w.running_tools = agent_w.tool_total()
                else:
                    chat_w.running_agents = 0
                    chat_w.running_tools = 0
            # drive the live working-tree panel header (spinner/elapsed/tokens, P25)
            if tree_w is not None:
                tree_w.running = bool(controller.running() or active)
                if tree_w.running:
                    tree_w.elapsed_s = max(0.0, _time.time() - _run_start)
                    tree_w.tokens = _run_tokens
                tree_w.label = _active_role["role"] or "working"
                # F: advance the trace spinner EVERY frame (driven by wall-clock so it's
                # smooth + independent of draw cadence). Without this the ANALYZE/PLAN/
                # thinking spinner sat frozen at ⠋ — the "too static / should animate" bug.
                tree_w._tick = int(_time.time() * 8)

            active_model = _active_role["model"] if controller.running() else ""
            display_model = active_model or short_model or "(auto)"

            # ── responsive segmented status bar ── builds prioritized segments and
            # lets layout_status_bar() drop/abbreviate the lowest-priority ones first
            # so nothing is ever silently chopped mid-field, at any terminal width.
            from ..core.tui_model import (StatusSeg, abbrev_count, layout_status_bar,
                                           display_width, fit_to_width, toast_anchor_x)
            effort = os.environ.get("SYNTRA_REASONING_EFFORT", "")
            if effort == "max" and os.environ.get("SYNTRA_ULTRACODE"):
                effort = "ultracode"
            # Brand wordmark scales with SYNTRA_BRANDING (user: "i dont want this much
            # branding"). minimal (default) = a quiet lowercase "syntra"; full = the loud
            # "⌥ SYNTRA 0.1"; off = no brand segment at all (state badge leads instead).
            from ..core.tui_model import BRANDING as _BRANDING
            segs = []
            if _BRANDING == "full":
                segs.append(StatusSeg("brand", f"{BRAND_MARK} SYNTRA 0.1",
                                      f"{BRAND_MARK} SYNTRA", "accent", "left", 0, bold=True))
            elif _BRANDING == "minimal":
                segs.append(StatusSeg("brand", "syntra", "syntra", "accent", "left", 0, bold=True))
            # off → no brand segment
            segs.append(StatusSeg("state", mode_badge, mode_short, mode_style, "left", 1, bold=True))
            if _key_debug:
                _kn = _last_key.get("name") or ""
                _kc = _last_key.get("code", -999)
                segs.append(StatusSeg("key", f"key:{_kc} {_kn}"[:28], f"k:{_kc}", "dim", "left", 9))
            # Bug H3 fix: the user-set session title (/title) is STORED + loaded on resume but
            # was only ever shown in the bottom bar — never on the header. Surface it as a
            # prominent left header segment (low priority so it survives a narrow bar). Quotes
            # set it apart from the mode/model fields. Falls back to nothing when unnamed.
            if session_title:
                _t_full = f"“{session_title[:32]}”"
                _t_short = f"“{session_title[:14]}”"
                segs.append(StatusSeg("title", _t_full, _t_short, "accent", "left", 2, bold=True))
            try:
                _ga = getattr(controller._run_goal, "get_toggle", None)
                if _ga:
                    # The run MODE chip (below) now shows the approval posture, so we no longer
                    # show a separate 🔓auto/🔒ask chip (it duplicated + could disagree). Keep only
                    # the autopilot multiplier when it's actually on (a distinct feature).
                    autop = int(_ga("autopilot", 1) or 1)
                    if autop > 1:
                        segs.append(StatusSeg("autopilot", f"↻{autop}", f"↻{autop}",
                                              "number", "left", 2))
                # run-mode chip (B6): Plan/Ask/Edit/Auto, from persisted access.json. This is the
                # visible indicator for the Shift+Tab mode toggle. Glyph + style per mode so the
                # posture reads at a glance (plan=eye, ask=lock, edit=pencil, auto=bolt).
                _mode = _access_chip.get("mode") or ""
                if _mode:
                    _glyphs = {"plan": "👁", "ask": "🔒", "edit": "✎", "auto": "⚡"}
                    _styles = {"plan": "dim", "ask": "accent", "edit": "number", "auto": "diff_del"}
                    _names = {"plan": "Plan", "ask": "Ask", "edit": "Edit", "auto": "Auto"}
                    _g = _glyphs.get(_mode, "•")
                    _full = f"{_g} {_names.get(_mode, _mode)}"
                    if _access_chip.get("tweaks"):
                        _full += "*"
                    segs.append(StatusSeg("mode", _full, _g,
                                          _styles.get(_mode, "dim"), "left", 3))
                # 📎 attachment chip: N image(s) staged for the next message. Click it (or
                # /attach with no arg) to clear. Only shown when something is attached.
                _na = len(_attached["items"])
                if _na:
                    _alabel = (f"📎 {_attached['items'][0]['label']}" if _na == 1
                               else f"📎 {_na} files")
                    segs.append(StatusSeg("attach", _alabel, f"📎{_na}",
                                          "number", "left", 2))
            except Exception:  # noqa: BLE001 - the bar must never break on a badge
                pass
            if display_model and display_model != "(auto)":
                _m = display_model
                segs.append(StatusSeg("model", _m, (_m[:13] + "…") if len(_m) > 14 else _m,
                                      "assistant", "right", 3))   # P31: readable (not dim 239)
            if effort:
                from ..core.tui_model import effort_bar
                gauge, estyle = effort_bar(effort, tick=int(_time.time() * 4))
                segs.append(StatusSeg("effort", gauge, f"⚡{effort}", estyle, "left", 4, bold=True))
            if ctx_total > 0:
                # show used/window when the active model's context window is known (A1)
                _win = 0
                if _status_catalog is not None and model_name:
                    _cm = _status_catalog.by_id(model_name)
                    if _cm is not None:
                        _win = getattr(_cm, "context_window", 0) or 0
                from ..core.tui_model import context_window_label
                segs.append(StatusSeg("ctx", f"context {context_window_label(ctx_total, _win)} tok",
                                      f"⊙{abbrev_count(ctx_total)}", "assistant", "right", 5))
            # provider rate-limit / cooldown indicator (A1): when the active route is
            # cooling from a recent 429/quota error, surface WHY it's slow.
            if _status_route_health is not None and _status_catalog is not None and model_name:
                try:
                    _cm2 = _status_catalog.by_id(model_name)
                    _prov = getattr(_cm2, "provider", "") if _cm2 else ""
                    if _prov and _status_route_health.is_cooled(_prov, model_name):
                        segs.append(StatusSeg("cooldown", "⚠ rate-limited", "⚠rl",
                                              "error", "right", 1, bold=True))
                except Exception:  # noqa: BLE001 - the bar must never break on a badge
                    pass
            if speed_tps > 0:
                segs.append(StatusSeg("speed", f"{speed_tps:.0f} tok/s", f"{speed_tps:.0f}t/s",
                                      "assistant", "right", 6))
            if last_task_id:
                segs.append(StatusSeg("task", f"task {last_task_id[:8]}", last_task_id[:6],
                                      "assistant", "right", 7))
            # paint the top row, then fill the gaps so the bar reads as one strip
            try:
                stdscr.addnstr(0, 0, " " * (cols - 1), cols - 1, _attr("accent"))
            except curses.error:
                pass
            for col, text, style, bold in layout_status_bar(segs, cols - 1):
                attr = _attr(style) | (curses.A_BOLD if bold else 0)
                try:
                    stdscr.addnstr(0, col, text, max(0, cols - 1 - col), attr)
                except curses.error:
                    pass
                _record(0, col, text)

            # ── widgets ──
            # first pass: collect all rects by x-column to detect horizontal splits
            # a widget whose y > body_rect.y and has a neighbor above = needs ─ separator
            sorted_rects = sorted(rects.items(), key=lambda kv: (kv[1].x, kv[1].y))

            for name, rect in sorted_rects:
                widget = registry.get(name)
                if not widget or not rect or rect.w < 2 or rect.h < 1:
                    continue

                is_focused = (name == focused)
                is_chat = (name == "chat")
                border_style = "focus_border" if is_focused else "system"

                if is_chat:
                    # Chat gets minimal chrome: just 1-col left padding
                    draw_rect = Rect(rect.x + 1, rect.y, max(1, rect.w - 1), rect.h)
                else:
                    # Non-chat panels get btop-style bordered boxes:
                    # ╭─ TITLE ─────╮
                    # │ content      │
                    # ╰──────────────╯
                    bx, by, bw, bh = rect.x, rect.y, rect.w, rect.h
                    if bw < 4 or bh < 3:
                        draw_rect = rect
                    else:
                        ba = _attr(border_style)
                        title = f" {widget.title.upper()} " if hasattr(widget, "title") else ""
                        # top border
                        top = "╭" + title + "─" * max(0, bw - len(title) - 2) + "╮"
                        try:
                            stdscr.addnstr(by, bx, top[:bw], bw, ba)
                        except curses.error:
                            pass
                        # bottom border
                        bot = "╰" + "─" * max(0, bw - 2) + "╯"
                        try:
                            stdscr.addnstr(by + bh - 1, bx, bot[:bw], bw, ba)
                        except curses.error:
                            pass
                        # left and right borders with scrollbar
                        content_h = bh - 2
                        total_lines = getattr(widget, "_scroll_total", 0)
                        scroll_pos = getattr(widget, "_scroll", 0)
                        thumb_y = -1
                        if total_lines > content_h and content_h > 2:
                            frac = scroll_pos / max(1, total_lines - content_h)
                            thumb_y = int(frac * (content_h - 1))
                        for yi, y in enumerate(range(by + 1, by + bh - 1)):
                            try:
                                stdscr.addstr(y, bx, "│", ba)
                                if yi == thumb_y:
                                    stdscr.addstr(y, bx + bw - 1, "┃", _attr("accent"))
                                else:
                                    stdscr.addstr(y, bx + bw - 1, "│", ba)
                            except curses.error:
                                pass
                        # content area inside the box
                        draw_rect = Rect(bx + 2, by + 1, max(1, bw - 3), max(1, bh - 2))

                if draw_rect.w < 2 or draw_rect.h < 1:
                    continue

                lines = widget.render(draw_rect.w, draw_rect.h)
                # Copy highlight: flash the copied message with A_REVERSE
                if _copy_highlight["msg_idx"] >= 0 and _time.time() >= _copy_highlight["until"]:
                    _copy_highlight["msg_idx"] = -1  # expired — clear
                highlight_active = is_chat and _copy_highlight["msg_idx"] >= 0
                highlight_idx = _copy_highlight["msg_idx"] if highlight_active else -1

                row_msg_map = getattr(widget, "last_row_to_msg", {}) if is_chat else {}

                for i, rl in enumerate(lines[:draw_rect.h]):
                    y = draw_rect.y + i
                    attr = _attr(rl.style)
                    if i == 0 and is_focused and is_chat:
                        attr |= curses.A_BOLD
                    # Highlight the copied message
                    if highlight_active and row_msg_map.get(i) == highlight_idx:
                        attr = _attr("accent") | curses.A_REVERSE
                    seg = rl.text[:draw_rect.w]
                    try:
                        stdscr.addnstr(y, draw_rect.x, seg, draw_rect.w, attr)
                    except curses.error:
                        pass
                    # intra-line styled spans (code syntax-highlight / word-level diff):
                    # overpaint each (start,end,style) run ON TOP of the base line so each
                    # token shows its own colour (M1). Skipped while the row is reverse-
                    # highlighted (copy) so the highlight stays uniform.
                    _spans = getattr(rl, "spans", None)
                    if _spans and not (highlight_active and row_msg_map.get(i) == highlight_idx):
                        for _sp in _spans:
                            _ss, _se, _sst = _sp[0], _sp[1], _sp[2]
                            # 4th element (emph) -> reverse-video, for word-level diff
                            _emph = curses.A_REVERSE if (len(_sp) > 3 and _sp[3]) else 0
                            _se = min(int(_se), draw_rect.w)
                            if _ss < 0 or _ss >= _se:
                                continue
                            _sub = seg[_ss:_se]
                            if _sub:
                                try:
                                    stdscr.addnstr(y, draw_rect.x + _ss, _sub, _se - _ss,
                                                   _attr(_sst) | _emph)
                                except curses.error:
                                    pass
                    # A line's optional right-edge cell (e.g. the chat scrollbar) repaints
                    # in its OWN style so it doesn't inherit the row's content color.
                    _rstyle = getattr(rl, "rstyle", "")
                    if _rstyle and seg:
                        _lx = draw_rect.x + len(seg) - 1
                        try:
                            stdscr.addnstr(y, _lx, seg[-1], 1, _attr(_rstyle))
                        except curses.error:
                            pass
                    _record(y, draw_rect.x, seg)

            # ── bottom bar ── keybinds (left) + session/amounts (right). Measure the
            # amounts FIRST so the keybinds stop before them: the two halves then can't
            # collide or get crammed at any width (was reserving a fixed 24 cols for a
            # ~50-col amounts string → overlap). P31/P32 + the amount-bar overlap fix.
            bottom_y = rows - 1
            # rebindable entries derive their key label from the LIVE keymap so a custom
            # keymap.json shows the user's key, not a hardcoded glyph (Q7). ^E/^L/^K are
            # fixed TUI chrome (not in the rebindable keymap).
            from ..core.keymap import key_label as _kl
            def _lbl(_action, _fallback):
                _ks = _keymap.keys_for(_action)
                return _kl(_ks[0]) if _ks else _kl(_fallback)
            binds = [
                (_lbl("command_palette", "ctrl+p"), "menu"), ("^E", "panels"),
                (_lbl("message_select", "ctrl+y"), "copy reply"),
                ("^L", "clear"), ("^K", "stop"), (_lbl("interrupt", "esc esc"), "quit"),
            ]
            right_parts = []
            # Running-agents summary (user [211]): a compact "▸ N/M agents · Xk tok" when a
            # multi-agent run is live — Syntra's own take on the bottom agent/workflow strip.
            if agent_w:
                _ag = agent_w.agents
                _run = sum(1 for a in _ag.values() if a.get("status") == "running")
                if _run:
                    _done = sum(1 for a in _ag.values() if a.get("status") == "done")
                    _atok = sum(int(a.get("agent_tokens", 0) or 0) for a in _ag.values())
                    seg = f"▸ {_done}/{_done + _run} agents"
                    if _atok:
                        seg += f" · {abbrev_count(_atok)} tok"
                    right_parts.append(seg)
            if session_title:
                # a named session reads by its name, not the opaque id
                right_parts.append(session_title[:28])
            elif last_task_id:
                right_parts.append(f"session {last_task_id[:6]}")
            if cost > 0:
                right_parts.append(f"cost ${cost:.4f}")
            right_parts.append(f"context {abbrev_count(ctx_total)} tok")  # P32: spelled out + abbreviated
            right = "  ·  ".join(right_parts)
            rw = display_width(right)   # cell-accurate (▸/· glyphs) so the anchor math is right
            # clear the row first
            try:
                stdscr.addnstr(bottom_y, 0, " " * (cols - 1), cols - 1, _attr("default"))
            except curses.error:
                pass
            x = 1
            for k, v in binds:
                if x + len(k) + len(v) + 3 >= cols - rw - 4:   # stop before the amounts
                    break
                try:
                    stdscr.addnstr(bottom_y, x, k, len(k), _attr("accent") | curses.A_BOLD)
                    stdscr.addnstr(bottom_y, x + len(k) + 1, v, len(v), _attr("default"))
                except curses.error:
                    pass
                x += len(k) + len(v) + 3
            # right-align the amounts with a clean >=2-col gap after the keybinds, so
            # they never overprint and never get truncated unless truly no room.
            rx = max(x + 2, cols - rw - 2)
            maxlen = max(0, cols - rx - 1)
            if maxlen > 0:
                try:
                    # User [186]: these were dim/hard to read. 'assistant' (252) is the
                    # brightest neutral across every theme -> readable session/cost/context.
                    # fit_to_width: cell-accurate clip + an ellipsis so a squeezed amount
                    # field reads as truncated instead of a silent mid-glyph chop.
                    fitted = fit_to_width(right, maxlen)
                    stdscr.addnstr(bottom_y, rx, fitted, len(fitted),
                                   _attr("assistant") | curses.A_BOLD)
                except curses.error:
                    pass

            # ── cursor in chat input ──
            if focused == "chat" and chat_w:
                # find chat rect and put cursor at the input line.
                # Chat content is always drawn at rect.x + 1 (1-col left padding),
                # matching the is_chat draw_rect above — the cursor must too.
                chat_rect = rects.get("chat")
                if chat_rect:
                    content_x = chat_rect.x + 1
                    content_w = max(1, chat_rect.w - 1)
                    # input box is the last `input_height` rows of the chat rect;
                    # the widget reports the cursor's row/col within it (P27).
                    in_h = getattr(chat_w, "input_height", 1)
                    in_top = chat_rect.y + chat_rect.h - in_h
                    cur_row = getattr(chat_w, "_cursor_row", 0)
                    cur_col = getattr(chat_w, "_cursor_col", 2)
                    # clamp into the rect AND the (possibly just-shrunk) screen so the
                    # hardware cursor is never placed off-screen on resize (Q4).
                    from ..core.tui_model import clamp_cursor
                    cursor_y, cursor_x = clamp_cursor(
                        in_top=in_top, cur_row=cur_row, content_x=content_x, cur_col=cur_col,
                        rect_y=chat_rect.y, rect_h=chat_rect.h, content_w=content_w,
                        screen_rows=rows, screen_cols=cols)
                    try:
                        curses.curs_set(1)
                        stdscr.move(cursor_y, cursor_x)
                    except curses.error:
                        pass
                else:
                    try:
                        curses.curs_set(0)
                    except curses.error:
                        pass
            else:
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── drag-selection highlight (P1) ── one source of truth with copy (P2):
            # selection_spans gives the exact (row, col0, col1) cells, reverse-video'd
            # over the painted text. Visible while dragging AND briefly after release.
            if _sel["active"] or _sel["show"]:
                # F23: keep the selection INSIDE the panel box where the drag started
                # (recorded as cx0/cx1 on press) — it must not bleed across the screen.
                _cx0 = _sel.get("cx0", 0)
                _cx1 = _sel.get("cx1", cols)
                # #89: a CONTENT-anchored chat selection projects its content lines onto the
                # CURRENT scroll position (so it re-lands on the right text after a scroll);
                # off-screen parts clip. Everything else uses the screen-coord path.
                if _sel["mode"] == "content" and chat_rect is not None and chat_w is not None:
                    from ..core.tui_model import content_selection_spans
                    _ctop = chat_rect.y + getattr(chat_w, "_sb_top", 1)
                    _chh = max(1, getattr(chat_w, "_sb_content_h", 1))
                    _cx_off = chat_rect.x + 1                 # content starts 1 col in (left pad)
                    _cw = max(1, chat_rect.w - 1)             # content width (cols are 0-based here)
                    # the helper returns CONTENT columns (0 = content start); shift to SCREEN cols.
                    _spans = [(sy, a + _cx_off, b + _cx_off) for (sy, a, b) in
                              content_selection_spans(
                                  _sel["cl0"], _sel["ccol0"], _sel["cl1"], _sel["ccol1"],
                                  chat_w.transcript.scroll, _ctop, _chh, _cw)]
                else:
                    from ..core.tui_model import selection_spans
                    _spans = selection_spans(_sel["y0"], _sel["x0"],
                                             _sel["y1"], _sel["x1"], cols)
                for sy, a, b in _spans:
                    if not (0 <= sy < rows):
                        continue
                    a = max(a, _cx0); b = min(b, _cx1)        # clamp to the box
                    if b <= a:
                        continue
                    row = _screen_text[sy] if sy < len(_screen_text) else ""
                    # Highlight ONLY the real text, not trailing padding (matches copy).
                    seg = (row[a:b] if a < len(row) else "").rstrip()
                    if not seg:
                        continue
                    try:
                        stdscr.addnstr(sy, a, seg, len(seg), curses.A_REVERSE)
                    except curses.error:
                        pass

            # ── modal overlay (command palette / file picker) ──
            if overlay["current"] is not None:
                from ..core.overlay import overlay_box
                from ..core.tui_model import modal_geometry
                ov = overlay["current"]
                # M4: attach frecency ranking once per picker instance (single chokepoint
                # covering every open path); empty-query lists then float most-used to top.
                if ov.kind in ("model", "file", "command", "session") and ov.select.score_fn is None:
                    ov.select.score_fn = (lambda row, _k=ov.kind:
                                          _frec.score(_frec_key(_k, row), _time.time()))
                # Model/role/info rows are long (model · provider · …key); give them
                # a wide box so names aren't clipped on the right edge (P15). modal_geometry
                # clamps to the screen + a usable floor so the box never overflows nor
                # collapses to nothing on tiny terminals.
                _wide = ov.kind in ("model", "roleboard", "info", "command", "file",
                                    "session", "backtrack")
                box_w, box_h, ox, _oy = modal_geometry(
                    cols, rows, 96 if _wide else 60, 20 if _wide else 16,
                    min_w=24, min_h=6)
                box = overlay_box(ov, box_w, box_h)
                # center vertically on the ACTUAL box height, clamped >=0 (a tall box on a
                # short terminal must not start at a negative y -> top rows vanished).
                oy = max(0, (rows - len(box)) // 2)
                from ..core.tui_model import display_width as _dw
                for i, (text, style) in enumerate(box):
                    # First BLANK the full box-width row so NONE of the chat/palette behind
                    # the overlay shows through (the bordered box content can be narrower in
                    # cells than box_w when it holds wide glyphs — that gap leaked the
                    # background, e.g. "…-miniog / routing"). Then paint the box row. (#16/#12)
                    try:
                        stdscr.addnstr(oy + i, ox, " " * box_w, box_w, _attr("default"))
                    except curses.error:
                        pass
                    _pad = max(0, box_w - _dw(text))
                    try:
                        stdscr.addnstr(oy + i, ox, text + " " * _pad, box_w, _attr(style))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── per-message action popup (Copy/Revert/Fork/Edit/Retry) ──
            if msg_menu["current"] is not None:
                from ..core.msg_menu import menu_box
                mb = menu_box(msg_menu["current"], 22)
                mx = min(msg_menu["x"], cols - 24)
                my = min(msg_menu["y"], rows - len(mb) - 1)
                mx = max(0, mx); my = max(0, my)
                for i, (text, style) in enumerate(mb):
                    try:
                        stdscr.addnstr(my + i, mx, text[:22], 22, _attr(style))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── panel toggle checklist (Ctrl+E) ──
            if panel_menu["current"] is not None:
                from ..core.panel_menu import panel_menu_box
                pb = panel_menu_box(panel_menu["current"], 32)
                px = (cols - 32) // 2
                py = (rows - len(pb)) // 2
                px = max(0, px); py = max(0, py)
                for i, (text, style) in enumerate(pb):
                    try:
                        stdscr.addnstr(py + i, px, text[:32], 32, _attr(style))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── chained command menu (Ctrl+P) ──
            if cmd_menu["stack"] is not None and cmd_menu["stack"].is_open:
                from ..core.menu import menu_render
                mw = min(46, cols - 4)
                mb = menu_render(cmd_menu["stack"], mw, min(18, rows - 4))
                mxx = max(0, (cols - mw) // 2)
                myy = max(1, min((rows - len(mb)) // 2, rows - len(mb) - 1))
                # blank the box rectangle first so nothing underneath bleeds through
                for i in range(len(mb)):
                    try:
                        stdscr.addnstr(myy + i, mxx, " " * mw, mw, _attr("default"))
                    except curses.error:
                        pass
                for i, (text, style) in enumerate(mb):
                    try:
                        stdscr.addnstr(myy + i, mxx, text, mw, _attr(style))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── minimap rail expanded panel (hover) — rolling-wheel message navigator ──
            if minimap_modal["active"] and chat_w and chat_w._rail is not None:
                from ..core.tui_model import minimap_wheel_box
                rail = chat_w._rail
                cr = rects.get("chat")
                if cr and rail:
                    n = len(rail.index)
                    focus = max(0, min(minimap_modal["focus"], n - 1))
                    mbw = min(48, max(24, cr.w - 4))
                    # Keep the drum COMPACT (≤7 message rows) so the whole box is short enough to
                    # sit centered on the click — a full-pane-height box would always clamp to the
                    # top (the bug). It still rolls: expanded() windows around focus, so off-screen
                    # messages scroll into view as ↑↓ moves focus.
                    win = max(3, min(n, 7, cr.h - 6))
                    erows = rail.expanded(focus, max_rows=win)
                    # focus position WITHIN the returned window (expanded() centers it)
                    fpos = next((i for i, r in enumerate(erows)
                                 if getattr(r, "focused", False)), len(erows) // 2)
                    full = erows[fpos].label if erows else ""
                    box = minimap_wheel_box(erows, fpos, full, mbw)
                    # anchor to the chat's RIGHT edge, expanding leftward, and VERTICALLY
                    # CENTER the box on where the rail was clicked (anchor_y) so it opens
                    # beside the cursor, not pinned at the top (user). Clamp into the pane.
                    mx0 = max(cr.x, cr.x + cr.w - mbw)
                    _ay = minimap_modal.get("anchor_y", cr.y + cr.h // 2)
                    my0 = _ay - len(box) // 2                # center the box on the cursor row
                    my0 = max(cr.y + 1, min(my0, cr.y + cr.h - len(box) - 1))
                    my0 = max(1, my0)
                    _bottom = cr.y + cr.h            # don't paint past the chat pane
                    for i, row in enumerate(box):
                        if my0 + i >= _bottom:
                            break
                        text, style = row[0], row[1]
                        try:
                            stdscr.addnstr(my0 + i, mx0, text[:mbw], mbw, _attr(style))
                        except curses.error:
                            pass
                    # record geometry (top row, left col, height) for click hit-testing
                    minimap_modal["geom"] = (my0, mx0, len(box))

            # ── effort slider (P19) ── model-aware levels (#13) + animated gradient (#14)
            if effort_modal["active"]:
                from ..core.tui_model import effort_slider_box
                ebw = min(74, cols - 6)
                _etick = int(_time.time() * 4)   # drives the pulsing ⚡ bolt
                content = effort_slider_box(effort_modal["idx"], ebw - 4,
                                            levels=effort_modal.get("levels"), tick=_etick)
                # box rows carry an optional 3rd element: per-level color spans. The "│ "
                # frame prefix shifts a row's spans +2 columns when we overpaint them.
                box = [("╭─ effort " + "─" * max(0, ebw - 12) + "╮", "accent", None)]
                for row in content:
                    text, style = row[0], row[1]
                    rspans = row[2] if len(row) > 2 else None
                    box.append(("│ " + text.ljust(ebw - 4)[:ebw - 4] + " │", style, rspans))
                box.append(("╰" + "─" * (ebw - 2) + "╯", "accent", None))
                ex = max(0, (cols - ebw) // 2)
                ey = max(1, (rows - len(box)) // 2)
                for i, (t, s, rspans) in enumerate(box):
                    try:
                        stdscr.addnstr(ey + i, ex, t[:ebw], ebw, _attr(s))
                    except curses.error:
                        pass
                    # overpaint per-level gradient spans (the "│ " prefix adds +2 cols)
                    if rspans:
                        for _sp in rspans:
                            _ss, _se, _sst = _sp[0] + 2, _sp[1] + 2, _sp[2]
                            _emph = curses.A_BOLD | curses.A_REVERSE if (len(_sp) > 3 and _sp[3]) else 0
                            _sub = t[_ss:_se]
                            if _sub and _ss < ebw:
                                try:
                                    stdscr.addnstr(ey + i, ex + _ss, _sub,
                                                   max(0, min(_se, ebw) - _ss), _attr(_sst) | _emph)
                                except curses.error:
                                    pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── question wizard (P11) ──
            if wizard_modal["current"] is not None:
                from ..core.question_wizard import wizard_box
                wz = wizard_modal["current"]
                wbw = min(82, cols - 6)
                content = wizard_box(wz, wbw - 4)
                title = wz.title[:wbw - 6]
                box = [("╭─ " + title + " " + "─" * max(0, wbw - len(title) - 5) + "╮", "accent")]
                for text, style in content:
                    box.append(("│ " + text.ljust(wbw - 4)[:wbw - 4] + " │", style))
                box.append(("╰" + "─" * (wbw - 2) + "╯", "accent"))
                wx = max(0, (cols - wbw) // 2)
                wy = max(1, (rows - len(box)) // 2)
                # record geometry so a mouse click maps to a wizard content-row (P11):
                # content row 0 is painted at wy+1 (just below the top border).
                wz._screen_y0 = wy + 1
                for i, (t, s) in enumerate(box):
                    try:
                        stdscr.addnstr(wy + i, wx, t[:wbw], wbw, _attr(s))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── plan-approval modal ── interactive + scrollable plan review (replaces the
            # old plain "press Enter" text box). The plan body scrolls so a big plan is fully
            # visible; the three actions (Approve/Modify/Discard) are chosen like a question.
            if plan_modal["current"] is not None:
                from ..core.plan_approval import plan_box
                pa = plan_modal["current"]
                pbw = min(86, cols - 6)
                # size the scrollable plan body to the available height (leave room for the
                # header, the FIVE action rows + 2 hint lines, borders + a little margin).
                pa.body_height = max(3, rows - 16)
                content = plan_box(pa, pbw - 4)
                title = pa.title[:pbw - 6]
                box = [("╭─ " + title + " " + "─" * max(0, pbw - len(title) - 5) + "╮", "accent")]
                for text, style in content:
                    box.append(("│ " + text.ljust(pbw - 4)[:pbw - 4] + " │", style))
                box.append(("╰" + "─" * (pbw - 2) + "╯", "accent"))
                bx = max(0, (cols - pbw) // 2)
                by = max(1, (rows - len(box)) // 2)
                pa._screen_y0 = by + 1            # content row 0 is just below the top border
                for i, (t, s) in enumerate(box):
                    try:
                        stdscr.addnstr(by + i, bx, t[:pbw], pbw, _attr(s))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── API-key entry popup ── a masked form (provider / key / base_url) so the user
            # adds a key WITHOUT typing the secret on the command line or into chat history.
            if key_modal["current"] is not None:
                from ..core.key_entry import key_box
                kf = key_modal["current"]
                kbw = min(72, cols - 6)
                content = key_box(kf, kbw - 4)
                box = [("╭─ API key " + "─" * max(0, kbw - 12) + "╮", "accent")]
                for text, style in content:
                    box.append(("│ " + text.ljust(kbw - 4)[:kbw - 4] + " │", style))
                box.append(("╰" + "─" * (kbw - 2) + "╯", "accent"))
                bx = max(0, (cols - kbw) // 2)
                by = max(1, (rows - len(box)) // 2)
                kf._screen_y0 = by + 1
                for i, (t, s) in enumerate(box):
                    try:
                        stdscr.addnstr(by + i, bx, t[:kbw], kbw, _attr(s))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── access-modes popup ── the file/command permission system: pick a mode
            # (normal/locked) and tune any individual permission (auto/ask/off). The sandbox is
            # always on underneath; secrets (.env/.git/keys) always at least ask.
            if access_modal["current"] is not None:
                from ..core.access_modes import access_box
                af = access_modal["current"]
                abw = min(76, cols - 6)
                content = access_box(af, abw - 4)
                box = [("╭─ access " + "─" * max(0, abw - 11) + "╮", "accent")]
                for text, style in content:
                    box.append(("│ " + text.ljust(abw - 4)[:abw - 4] + " │", style))
                box.append(("╰" + "─" * (abw - 2) + "╯", "accent"))
                bx = max(0, (cols - abw) // 2)
                by = max(1, (rows - len(box)) // 2)
                af._screen_y0 = by + 1
                for i, (t, s) in enumerate(box):
                    try:
                        stdscr.addnstr(by + i, bx, t[:abw], abw, _attr(s))
                    except curses.error:
                        pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── tool-permission prompt (A5) ── a tool wants to run and the engine is
            # BLOCKED on _permission_ask until the user picks; render the box so they
            # can. Keys (1/2/3/a) are handled in the event loop's perm intercept.
            if perm_modal["req"] is not None:
                from ..core.tui_model import permission_box
                pbw = min(72, cols - 6)
                inner = max(8, pbw - 4)
                content = permission_box(perm_modal["req"], inner, reason=perm_modal.get("reason"),
                                         show_explain=bool(perm_modal.get("explain")))
                box = [("╭─ permission " + "─" * max(0, pbw - 15) + "╮", "accent")]
                for text, style in content:
                    box.append(("│ " + text.ljust(inner)[:inner] + " │", style))
                box.append(("╰" + "─" * (pbw - 2) + "╯", "accent"))
                px = max(0, (cols - pbw) // 2)
                py = max(1, (rows - len(box)) // 2)
                # Record the clickable action row's geometry so the mouse handler maps a
                # click to the SAME action as the keys (no recomputed coordinates). LOCATE the
                # action line by its `[1]` marker (do NOT assume it's the last content row —
                # #222 can append a "Ctrl+E explain" hint below it). `line` is the raw action
                # string for hit-testing. In reason-capture mode there's no button row.
                _in_reason = perm_modal.get("reason") is not None
                _act_idx = next((i for i, (t, _s) in enumerate(content) if "[1]" in t), len(content) - 1)
                _act_line = content[_act_idx][0]
                # content row i maps to box row i+1 (box[0] is the top border) → screen y.
                perm_modal["geom"] = (None if _in_reason else
                                      {"y": py + 1 + _act_idx, "x0": px + 2, "line": _act_line})
                for i, (t, s) in enumerate(box):
                    try:
                        stdscr.addnstr(py + i, px, t[:pbw], pbw, _attr(s))
                    except curses.error:
                        pass
                # Overpaint each option so it reads as a live, clickable button: the option
                # under the cursor is reverse-video, the rest are bold accent (not dead text).
                # (Skipped while typing a deny+tell reason — the button row isn't shown then.)
                if not _in_reason:
                    from ..core.tui_model import permission_action_spans
                    _arow, _aline = py + 1 + _act_idx, _act_line
                    _hover = perm_modal.get("hover")
                    for _s, _e, _act in permission_action_spans(_aline):
                        _seg = _aline[_s:_e].rstrip()        # skip the inter-option padding
                        if not _seg:
                            continue
                        _sx = px + 2 + _s
                        _btn = _attr("accent") | (curses.A_REVERSE if _act == _hover else curses.A_BOLD)
                        try:
                            stdscr.addnstr(_arow, _sx, _seg, max(0, (px + pbw - 1) - _sx), _btn)
                        except curses.error:
                            pass
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── /compare side-by-side cards (A-fix 5) — a large modal on top ──
            if compare_modal["view"] is not None:
                cv = compare_modal["view"]
                cbw = min(100, cols - 4)
                cbh = min(max(14, rows - 4), rows - 2)
                cbox = cv.render(cbw, cbh)
                cx = max(0, (cols - cbw) // 2)
                cy = max(0, (rows - len(cbox)) // 2)
                for i, (t, s) in enumerate(cbox):
                    try:
                        stdscr.addnstr(cy + i, cx, t[:cbw], cbw, _attr(s))
                    except curses.error:
                        pass
                # remember geometry so a click maps to a tab row (set on the view for the
                # mouse handler — the tab rows start after the title (+question) lines).
                compare_modal["geom"] = (cx, cy, cbw, len(cbox))
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

            # ── toast notification (bottom-right, above the keybind bar) ──
            tn = toasts.render(min(50, cols - 4))
            if tn:
                ttext, tstyle = tn
                ty = rows - 2
                tx = toast_anchor_x(ttext, cols)   # cell-accurate so emoji toasts don't overflow
                try:
                    stdscr.addnstr(ty, tx, ttext, len(ttext),
                                   _attr(tstyle) | curses.A_BOLD)
                except curses.error:
                    pass

            _sync_refresh()   # L1: atomic frame present (sync output) — no flicker

        # ── live rollout persistence (A2.4) ──
        def _persist_rollout(tid: str) -> None:
            """Snapshot the user/assistant turns to the append-only rollout so the
            session can be resumed, forked, or backtracked later. ``save_rollout`` is
            atomic + idempotent, so re-writing the same stream after every run never
            duplicates lines. Best-effort: a persistence error never breaks the UI."""
            if not tid or chat_w is None:
                return
            records = [{"role": getattr(m, "role", ""), "text": getattr(m, "text", "")}
                       for m in chat_w.transcript.messages
                       if getattr(m, "role", "") in ("user", "assistant")
                       and (getattr(m, "text", "") or "").strip()]
            if not records:
                return
            try:
                from ..core.state import TaskStore
                TaskStore(_state_root()).save_rollout(tid, records)
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass

        # ── drain run output ──
        def _drain():
            nonlocal active, cost, tokens_in, tokens_out, speed_tps, _run_tokens, last_task_id, _run_start, pending_title, session_title
            if _drain_bg():               # ran a bg-thread UI op → must repaint even at idle
                _bg_dirty["v"] = True     # (else a pasted-image chip/toast is applied but unseen)
            _items = controller.poll()
            # #233: any new streamed item = the run is ALIVE → reset the stall clock so the
            # working line stays its normal color; silence for >3s eases it toward red.
            if _items:
                _last_activity_ts["t"] = _time.time()
            # New content shifts the chat, so a screen-anchored selection would now sit on
            # the WRONG text (user: "the selection stays and the text moves behind it").
            # Drop it the moment anything streams in. A CONTENT-anchored selection (#89) is kept
            # WHILE you're actively dragging (a token streaming in mid-drag mustn't kill the drag —
            # appended lines don't shift the indices you're selecting); once released we still drop
            # it, since folding/re-render above the anchor could shift content-line indices.
            if _items and (_sel["active"] or _sel["show"]):
                if not (_sel["mode"] == "content" and _sel["active"]):
                    _sel["active"] = False
                    _sel["show"] = False
            for role, line in _items:
                if role == "show_image":         # engine asked to render an image inline
                    try:
                        _show_image(line)            # self-managing: sharp inline OR preview+auto-open
                    except Exception:  # noqa: BLE001 - never break the drain on a bad image
                        pass
                    continue
                if role == "agent_evt":          # structured agent lifecycle -> AGENTS panel + feed
                    try:                         # (council plan·X / panel review·X / sub·N)
                        import json as _json
                        d = _json.loads(line)
                        _k = d.get("k", "")
                        if agent_w:
                            agent_w.feed(_k, d)
                        action_feed.ingest(_k, d)          # rolling inline action feed
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if role == "stream":
                    if chat_w:
                        chat_w.append_stream(line)
                    _streamed["buf"] += line          # remember what streamed (dedupe the replay)
                    _run_tokens += len(line)
                    # Live token monitor update during streaming
                    if token_w and _run_tokens > 0:
                        est_out = _run_tokens // 4  # rough chars-to-tokens estimate
                        token_w.update(input_tokens=tokens_in,
                                       output_tokens=tokens_out + est_out,
                                       cost=cost)
                else:
                    # #129: if the answer was already STREAMED live, finalize that bubble but DON'T
                    # add a second identical one from the post-run replay. Compare trimmed text so
                    # whitespace differences don't defeat the match.
                    _dup_of_stream = (role == "assistant" and _streamed["buf"].strip()
                                      and line.strip() == _streamed["buf"].strip())
                    if chat_w:
                        chat_w.end_stream()
                        if not _dup_of_stream:
                            chat_w.add(role, line)
                    if role == "assistant":
                        _streamed["buf"] = ""          # consumed; next answer starts fresh
                    # feed the live working-tree panel. This binds off the formatted trace;
                    # structured events can instead route via tree_w.feed(kind, payload)
                    # for a richer tree.
                    if tree_w and role in ("mode", "tool", "thinking", "ok", "error"):
                        tree_w.feed_line(role, line)
                    # route tool output to activity log
                    if activity_w and role in ("tool", "mode"):
                        status = "running"
                        short = line.strip()[:60]
                        if "error" in line.lower() or line.lstrip().startswith("✗"):
                            status = "error"
                        elif role == "mode":
                            status = "info"
                        activity_w.log(short, status)
                    # update agent status from the mode header (▶ EXECUTE · model)
                    # and the route child (┄ role → model). The mode chip drives the
                    # current-mode display; the route line carries the model.
                    if role == "mode":
                        ml = line.lower()
                        # action feed: a phase change is a milestone line + a fold-group boundary
                        _phname = line.strip().split("·")[0].strip().lstrip("▶▸◎⊙↻✓✗● ").strip()
                        if _phname:
                            action_feed.ingest("phase", {"phase": _phname})
                        # plan-mode ribbon: light up while ANALYZING/PLANNING, off otherwise
                        if any(w in ml for w in ("analyz", "plan")):
                            plan_ribbon.set_phase("plan" if "plan" in ml else "analyze")
                            plan_ribbon.active = True
                        else:
                            plan_ribbon.active = False
                        mode_map = {"plan": "planner", "execute": "executor", "review": "reviewer"}
                        for chip, r in mode_map.items():
                            if chip in ml:
                                _active_role["role"] = r
                                if "·" in line:
                                    _active_role["model"] = line.split("·")[-1].strip()
                                _active_mode["mode"] = line.strip().split("·")[0].strip()
                                if agent_w:
                                    agent_w.set_status(r, "running", model=_active_role.get("model", ""))
                                break
                    elif role == "tool" and "→" in line and ("┄" in line or "├" in line):
                        for r in ("planner", "executor", "reviewer"):
                            if r in line.lower():
                                _active_role["role"] = r
                                _active_role["model"] = line.split("→")[1].split("via")[0].strip()
                                if agent_w:
                                    agent_w.set_status(r, "running", model=_active_role.get("model", ""))
                    # structured question from the engine: "[question] {json spec}"
                    # opens the interactive wizard (P11). Engine side is B's hook;
                    # this is the UI receiver.
                    if role == "tool" and line.startswith("[question]"):
                        try:
                            import json as _json
                            spec = _json.loads(line[len("[question]"):].strip())
                            _open_wizard(spec)
                            _awaiting_input["active"] = True
                        except Exception:  # noqa: BLE001 - bad spec must not crash the UI
                            pass
                    # detect "awaiting input" signals
                    elif role == "tool" and line.startswith("[waiting]"):
                        _awaiting_input["active"] = True
                        _awaiting_input["message"] = line[10:].strip()
                        toasts.show(line[10:].strip() or "input needed", "info", ttl=5.0)
                    elif role == "tool" and line.startswith("[resumed]"):
                        _awaiting_input["active"] = False
                    # #141: reconstruct the whole-TURN diff from the feed lines the bridge
                    # renders ("⎿ changes: <summary>" + unified-diff body). The pure capture
                    # owns the state machine; we just forward each (role, line) and fire an
                    # action-feed milestone when a new turn diff opens.
                    if _turn_diff.feed(role, line) and role == "tool" and "⎿ changes:" in line:
                        action_feed.ingest("tool_call", {"name": "turn diff"})   # milestone
                    # route edits to diff viewer
                    if diff_w and role == "tool" and ("edit" in line.lower() or "write" in line.lower()):
                        # try to extract a filename
                        parts = line.split()
                        if len(parts) >= 2:
                            diff_w.set_diff(parts[-1], line)
                    # route command output to run_output
                    if run_out_w and role == "tool" and (line.startswith("$") or line.startswith(">")):
                        run_out_w.append(line)

            if active and controller.finished:
                active = False
                _active_role["role"] = ""
                _active_role["model"] = ""
                _awaiting_input["active"] = False
                # #221: this turn is done — archive its diff into the browsable history (a
                # no-op when the turn changed nothing; record() ignores empty diffs).
                if _turn_diff.has_diff():
                    _turn_diff_hist.record(_turn_diff.summary, _turn_diff.lines)
                # #178: run finished → taskbar/tab shows error (red) on failure, else clears.
                _set_progress("error" if controller.error is not None else "clear")
                elapsed = max(0.01, _time.time() - _run_start)
                if _run_tokens > 0:
                    speed_tps = _run_tokens / elapsed
                if agent_w:
                    for r in ("planner", "executor", "reviewer"):
                        agent_w.set_status(r, "done")
                if chat_w:
                    chat_w.end_stream()
                if controller.error is not None:
                    if chat_w:
                        chat_w.add("system", f"error: {controller.error}")
                    if activity_w:
                        activity_w.log(str(controller.error)[:40], "error")
                elif controller.result is not None:
                    try:
                        st = controller.result.state
                        cost = st.total_cost_usd()
                        tokens_in, tokens_out = st.total_tokens()
                        if token_w:
                            token_w.update(input_tokens=tokens_in,
                                           output_tokens=tokens_out, cost=cost)
                        # populate plan steps for the PLAN tab + the live plan-mode ribbon
                        if chat_w:
                            _plan_list = list(getattr(st, "plan", []) or [])
                            chat_w.plan_steps = _plan_list
                            plan_ribbon.set_steps(
                                [str(getattr(_s, "description", _s) or "") for _s in _plan_list])
                            # a real MULTI-step plan → drop the click-to-expand plan card (naming
                            # the plan FILE). Skipped for a trivial 1-step/no-plan direct answer.
                            if len(_plan_list) > 1:
                                _show_plan_card(st)
                        tid = getattr(st, "task_id", "")
                        if tid:
                            last_task_id = tid
                            # apply a title the user set BEFORE this run existed (no orphan
                            # task was minted; the name was stashed in pending_title).
                            if pending_title:
                                try:
                                    from ..core.state import TaskStore as _TS
                                    from .main import _state_root as _sr
                                    _TS(_sr()).set_title(tid, pending_title)
                                    session_title = pending_title
                                    _title_state["name"] = pending_title   # tab title (H1)
                                except Exception:  # noqa: BLE001
                                    pass
                                pending_title = ""
                            # UPGRADE the auto-derived title to the analyzer's UNDERSTOOD title
                            # ONCE: the submit-time guess was distilled from the raw first
                            # message; the analyzer read the goal and named the topic. After we
                            # adopt it once we LOCK it (auto) so the title stays STABLE and is
                            # not re-derived every turn. The user's own /title always wins.
                            _smart = (getattr(controller.result, "title", "") or "").strip()
                            if (_smart and not _title_locked["user"] and not _title_locked["auto"]
                                    and _smart != session_title):
                                try:
                                    from ..core.state import TaskStore as _TS
                                    from .main import _state_root as _sr
                                    _TS(_sr()).set_title(tid, _smart)
                                    session_title = _smart
                                    _title_state["name"] = _smart
                                    _title_locked["auto"] = True   # lock — don't churn next turn
                                except Exception:  # noqa: BLE001
                                    pass
                            # tag the user turn that produced this run with its task_id
                            # so "fork" branches the RIGHT task at a valid coordinate
                            # (fixes the fork-coordinate / wrong-task bug).
                            if chat_w:
                                for _m in reversed(chat_w.transcript.messages):
                                    if getattr(_m, "role", "") == "user":
                                        _m.task_id = tid
                                        break
                            # durably record the conversation so it's resumable (A2.4)
                            _persist_rollout(tid)
                        verdict = getattr(controller.result, "verdict", "")
                        if verdict == "plan_pending":
                            # Open the INTERACTIVE plan-approval modal (scrollable, never
                            # trimmed) instead of a plain "press Enter" text dump — the user
                            # answers it like a question: Approve / Modify / Discard.
                            from ..core.plan_approval import PlanApproval
                            _plan = list(getattr(st, "plan", []) or [])
                            _steps = [str(getattr(_s, "description", _s) or "") for _s in _plan]
                            plan_modal["current"] = PlanApproval(steps=_steps, title="Plan ready")
                            plan_ribbon.clear()   # ribbon hands off to the approval modal
                            # also drop a click-to-expand plan card (naming the plan FILE) into the
                            # chat, so the plan stays reachable after the modal is answered.
                            _show_plan_card(st)
                            if activity_w:
                                activity_w.log("plan ready — awaiting approval", "info")
                        else:
                            elapsed_total = max(0.1, _time.time() - _run_start)
                            if elapsed_total < 60:
                                et = f"{elapsed_total:.1f}s"
                            else:
                                et = f"{int(elapsed_total // 60)}m {int(elapsed_total % 60):02d}s"
                            if chat_w:
                                chat_w.add("system", f"done  {et}  ${cost:.4f}")
                            if activity_w:
                                activity_w.log(f"completed {et} ${cost:.4f}", "ok")
                            # Completion notification: terminal bell + an OSC 9 desktop
                            # notification (fires a real OS notice when the run finishes in
                            # a background terminal — Q6). Both degrade safely if unsupported.
                            try:
                                from ..core.tui_model import osc9_notify
                                _sys.stdout.write("\a" + osc9_notify(f"Syntra ✓ done · {et} · ${cost:.4f}"))
                                _sys.stdout.flush()
                            except Exception:
                                pass
                            _set_title(f"Syntra ✓ done ${cost:.4f}")
                    except Exception as _e:  # noqa: BLE001
                        # F35: completion handling is best-effort (must not crash the UI), but
                        # don't swallow it SILENTLY — surface a short note so a broken
                        # cost/plan/title update is visible instead of vanishing.
                        if chat_w:
                            chat_w.add("system", f"(note: run finished but a completion-panel update failed: {str(_e)[:80]})")
                # run finished -> freeze any still-'running' agent timer (e.g. the
                # librarian) so the panel never shows a ghost ticking forever.
                if agent_w and not active:
                    agent_w.finalize_running()
                # run finished -> collapse the rolling action feed + plan ribbon to nothing
                # (design: idle shows no feed). They reappear on the next run. This also keeps
                # the idle chat layout identical to pre-feed, so nothing else shifts when idle.
                if not active:
                    action_feed.clear()
                    plan_ribbon.clear()
                # start the next queued message, if any (keeps turns ordered)
                # Drain the queue as ONE combined turn (user choice: coalesce → a single
                # reply for everything queued, not N separate full-pipeline runs). All held
                # messages merge into one run_text so the model answers them together with
                # shared context — queue 100, get 1 coherent reply, not 100. Ordered: this
                # only fires once the prior run is fully finished (not active).
                if _pending and not active:
                    disps = [d for d, _rt in _pending]
                    runs = [rt for _d, rt in _pending]
                    _pending.clear()
                    disp = " / ".join(disps)
                    run_text = ("\n\n".join(runs) if len(runs) == 1 else
                                "Please address ALL of the following messages together:\n\n"
                                + "\n\n".join(f"{i}. {r}" for i, r in enumerate(runs, 1)))
                    active = True
                    _run_start = _time.time()
                    _run_tokens = 0
                    _streamed["buf"] = ""   # #129: fresh stream buffer per run (dedupe replay)
                    controller.start(run_text)
                    _mark_queued_sent("sent")   # #124: the bubble now reflects it actually went
                    _n_sent = len(disps)
                    toasts.show(f"sent {_n_sent} queued message{'s' if _n_sent != 1 else ''}",
                                "success", ttl=1.6)   # #124: notification on send
                    if activity_w:
                        _note = (f"started {len(disps)} queued msgs as one turn" if len(disps) > 1
                                 else f"started: {disp[:40]}")
                        activity_w.log(_note, "running")

            # #178: reflect the live run in the terminal taskbar/tab. Indeterminate while a
            # run is active (we don't have a reliable global %), cleared when idle. Deduped
            # in _set_progress, so calling every drain is cheap (writes only on change) and
            # covers every run-start site uniformly (typed goal / queued / resume).
            _set_progress("indeterminate" if (active or controller.running()) else "clear")

        # ── show command output in a clean scrollable popup (not the chat) ──
        def _info_popup(title: str, text: str):
            from ..core.overlay import make_info_overlay
            # wrap to the box's real inner width so content isn't re-clipped on
            # terminals narrower than the 96-col cap (matches the overlay box geometry).
            try:
                _r, _c = stdscr.getmaxyx()
            except Exception:  # noqa: BLE001
                _c = 100
            inner = max(20, min(96, _c - 4) - 4)
            overlay["current"] = make_info_overlay(title, text, wrap_width=inner)

        def _preview_theme(ov):
            # Q3: live-preview the highlighted theme by applying it for real (re-inits
            # the curses color pairs). Esc restores ov.preview_orig; Enter keeps it.
            sel = ov.select.current()
            if not sel:
                return
            from ..core.themes import set_theme as _st
            if _st(sel):
                _load_colors()

        # ── run a blocking command body off the main loop so the UI never freezes
        # (P5). The worker computes on its thread but routes every UI mutation back
        # to the MAIN thread via _post()/_bg_q, so widget/overlay state is only ever
        # mutated on the draw thread (no data races, SYNTRA-BUG-001).
        import queue as _queue
        _bg_q: "_queue.Queue" = _queue.Queue()
        _bg_dirty = {"v": False}   # set when a drained bg op mutated UI → force a redraw at idle

        def _post(fn):
            """Schedule fn() to run on the main loop (thread-safe)."""
            _bg_q.put(fn)

        def _drain_bg() -> bool:
            """Apply queued background-thread UI ops on the main thread. Returns True if any ran,
            so the caller can force a redraw — at IDLE the loop otherwise wouldn't repaint, and a
            bg result (e.g. a pasted-image 📎 chip / toast) would be applied to state but never
            drawn (the Ctrl+V 'nothing happened' bug)."""
            ran = False
            while True:
                try:
                    op = _bg_q.get_nowait()
                except _queue.Empty:
                    break
                ran = True
                try:
                    op()
                except Exception:  # noqa: BLE001 - a UI op must not kill the loop
                    pass
            return ran

        def _run_bg(label: str, fn):
            """fn() runs on a daemon thread; it should call _post(...) for any UI
            update (chat_w.add / diff_w.set_diff / _info_popup)."""
            import threading as _thr
            def _work():
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    _post(lambda e=e: chat_w and chat_w.add("system", f"{label}: {e}"))
            _thr.Thread(target=_work, name=f"syntra-cmd-{label}", daemon=True).start()
            toasts.show(f"⏳ {label}…", "info", ttl=1.5)

        def _detect_models_bg(provider: str):
            """Run provider model-discovery on a background thread, streaming each probed
            model into the chat via _post. Used by /key detect AND auto after a key is added.
            Degrades quietly if the engine doesn't expose the seam."""
            seam = getattr(controller._run_goal, "detect_provider_models", None)
            if seam is None:
                if chat_w:
                    chat_w.add("system", "model detection unavailable (no provider runner)")
                return

            def _work():
                def _ev(kind, p):
                    if kind == "discovery_model":
                        _tu = p.get("tool_use")
                        _suffix = f" · tools={_tu}" if _tu not in (None, "unknown") else ""
                        _post(lambda p=dict(p), s=_suffix: chat_w and chat_w.add(
                            "system", f"  {p.get('id','?')}: {p.get('status','?')}{s}"))
                ok, msg = seam(provider, on_event=_ev)
                _post(lambda ok=ok, msg=msg: chat_w and chat_w.add(
                    "system", ("✓ " if ok else "✗ ") + msg))
            _run_bg(f"detect {provider} models", _work)

        # ── apply a reasoning-effort level (from the /effort slider or arg) ──
        def _effort_levels() -> list:
            """#13: the effort levels the SELECTED model actually supports (model-aware, never
            the hardcoded 7). Resolves the model the user will actually run with — even BEFORE a
            run starts — so /effort only offers what that model/provider provides:
              1. the currently-RUNNING model (controller.active_model), else
              2. the model PINNED to the executor (the reasoning role) via /models, else
                 the planner pin, else
              3. $SYNTRA_MODEL, else None → full ladder (auto stays safe).
            Then asks the engine's capability view (effort_levels_for) through the catalog."""
            from ..core.tui_model import effort_slider_levels_for
            mdl = None
            try:
                _mid = (getattr(controller, "active_model", "") or "").strip()
                if not _mid:
                    # no run active → use the model the user PINNED (what they'll run with)
                    try:
                        from ..core.overrides import Overrides
                        _ov = Overrides.load()
                        _mid = (_ov.pinned_model_for("executor")
                                or _ov.pinned_model_for("planner") or "").strip()
                    except Exception:  # noqa: BLE001
                        _mid = ""
                if not _mid:
                    _mid = (os.environ.get("SYNTRA_MODEL", "") or "").strip()
                if _status_catalog is not None and _mid:
                    mdl = _status_catalog.by_id(_mid)
            except Exception:  # noqa: BLE001
                mdl = None
            return effort_slider_levels_for(mdl)

        def _apply_effort(level: str):
            level = (level or "auto").lower()
            if level == "auto":
                os.environ.pop("SYNTRA_REASONING_EFFORT", None)
                os.environ.pop("SYNTRA_ULTRACODE", None)
                toasts.show("⚡ effort → auto (adaptive)", "success")
                if chat_w:
                    chat_w.add("system", "reasoning effort → auto  (adaptive: scales low→max with task risk)")
            elif level == "ultracode":
                os.environ["SYNTRA_REASONING_EFFORT"] = "max"
                os.environ["SYNTRA_ULTRACODE"] = "1"
                toasts.show("⚡ effort → ultracode", "success")
                if chat_w:
                    chat_w.add("system", "reasoning effort → ultracode  (max effort + multi-agent workflows)")
            else:
                os.environ["SYNTRA_REASONING_EFFORT"] = level
                os.environ.pop("SYNTRA_ULTRACODE", None)
                toasts.show(f"⚡ effort → {level}", "success")
                if chat_w:
                    chat_w.add("system", f"reasoning effort → {level} (forced every step)")

        # ── question wizard (P11) ──
        def _open_wizard(spec: dict):
            from ..core.question_wizard import wizard_from_spec
            effort_modal["active"] = False     # only one modal captures keys at a time
            plan_modal["current"] = None       # …and never behind/over an open plan modal
            wizard_modal["current"] = wizard_from_spec(spec)
            wizard_modal["paused"] = None

        def _access_path():
            return _state_root() / "access.json"

        def _open_access():
            """Open the access-modes popup, seeded from the persisted state."""
            from ..core.access_modes import load_access_state, AccessModeForm
            effort_modal["active"] = False
            plan_modal["current"] = None
            wizard_modal["current"] = None
            key_modal["current"] = None
            access_modal["current"] = AccessModeForm(state=load_access_state(_access_path()))

        def _apply_access(st):
            """Persist the chosen access state. The runner reads access.json fresh at the start of
            each run (main.py run_goal → load_access_state), so persistence IS the live wiring —
            the next run honors the new mode/permissions."""
            from ..core.access_modes import save_access_state
            save_access_state(_access_path(), st)
            _access_chip["mode"] = st.mode                  # refresh the status-bar chip now
            _access_chip["tweaks"] = len(st.overrides)
            if chat_w:
                chat_w.add("system", f"mode: {st.summary()} — sandbox always on; secrets always ask")

        def _cycle_mode(direction=1):
            """Shift+Tab: advance the run mode (Plan→Ask→Edit→Auto), persist, toast, refresh chip."""
            from ..core.access_modes import load_access_state
            st = load_access_state(_access_path())
            st.cycle_mode(direction)
            _apply_access(st)
            toasts.show(f"mode: {st.mode_label()}", "info", ttl=1.6)

        def _wizard_signal(sig, wz):
            """Handle a wizard activate() result: chat-about / done / cancel."""
            if not sig:
                return
            kind, payload = sig
            if kind == "chat":
                wizard_modal["paused"] = wz
                wizard_modal["current"] = None
                if chat_w:
                    chat_w.add("system", "↶ paused the question — chat freely, then /resume-question to continue")
            elif kind == "done":
                wizard_modal["current"] = None
                wizard_modal["paused"] = None
                import json as _json
                if chat_w:
                    echo = ["✓ answers submitted:"]
                    for k, v in payload.items():
                        vv = ", ".join(v) if isinstance(v, list) else str(v)
                        echo.append(f"  {k}: {vv}")
                    chat_w.add("system", "\n".join(echo))
                # contract: a run awaiting input receives the answers as a steer
                if controller.running():
                    controller.steer_now(f"[answers] {_json.dumps(payload)}")
            elif kind == "cancel":
                wizard_modal["current"] = None
                _wizard_dismissed()
                if chat_w:
                    chat_w.add("system", "question cancelled")

        def _wizard_dismissed():
            """The wizard was closed WITHOUT submitting (Esc / backed-out / Cancel). If a run's
            clarify-question callback is BLOCKED waiting on the steering inbox for an answer, send
            an empty answer so it unblocks immediately and the engine proceeds with its assumption
            (instead of waiting out the 600s deadline)."""
            if controller.running():
                controller.steer_now("[answers] null")

        def _drop_pending_plan():
            """Forget a pending (un-approved) plan WITHOUT touching the conversation. Clears the
            engine-side pending-task pointer so a later /resume won't re-run the dropped plan, and
            clears the live plan-step list. Does NOT call /clear (that wipes the whole chat — the
            bug the user hit when Esc-discarding a plan)."""
            try:
                _st = getattr(controller._run_goal, "set_toggle", None)
                if _st:
                    _st("_pending_task", "")
            except Exception:  # noqa: BLE001
                pass
            if chat_w:
                chat_w.plan_steps = []
            plan_ribbon.clear()

        def _plan_signal(sig):
            """Act on the plan-approval modal's result: approve <scope> → resume + run the plan
            (and, per scope, stop pausing for future tasks); modify <text> → send the change as
            the next turn; discard → drop the plan.

            Approve scope (how the user turns plan-review OFF, right from the modal):
              • "once"    — run this plan; keep asking next time (default, nothing persisted).
              • "session" — run, and stop pausing for the rest of THIS app session (in-memory toggle).
              • "always"  — run, and stop pausing for good (persisted to disk via /plan-review off)."""
            if not sig:
                return
            kind, payload = sig
            # ("modify", "") is NOT a final action — it's the "I just opened the text box, wait
            # for the user to type their change" signal. Keep the modal open (in text mode) and do
            # nothing; only a non-empty modify payload (from submit_text) actually runs anything.
            # (Closing here + submitting empty was the "executor started before I edited" bug.)
            if kind == "modify" and not (payload or "").strip():
                return
            plan_modal["current"] = None
            if kind == "approve":
                scope = payload or "once"
                if scope in ("session", "always"):
                    # Turn plan-review OFF at the chosen scope so future tasks don't pause.
                    _st = getattr(controller._run_goal, "set_toggle", None)
                    if _st:
                        _st("_plan_review", False)            # in-memory: covers this session
                    if scope == "always":
                        try:                                  # persist across restarts
                            from ..cli.main import _save_plan_review
                            _save_plan_review(False)
                        except Exception:  # noqa: BLE001
                            pass
                _msg = {"once": "✓ plan approved — running",
                        "session": "✓ approved — plan review off for this session",
                        "always": "✓ approved — plan review off (saved)"}.get(scope, "✓ plan approved — running")
                if chat_w:
                    chat_w.add("system", _msg)
                _submit("/resume")
            elif kind == "modify":
                if chat_w:
                    chat_w.add("system", f"✎ modifying the plan: {payload}")
                # re-plan with the user's change as the goal (the engine folds in the prior
                # plan via conversation context); a fresh run supersedes the pending plan.
                _submit(payload)
            elif kind == "discard":
                # Drop ONLY the pending plan — do NOT wipe the conversation. (Bug: discard used to
                # run /clear, which erased the whole chat; the user just wanted to dismiss the plan.)
                _drop_pending_plan()
                if chat_w:
                    chat_w.add("system", "plan discarded")

        # ── backtrack (A2): step back to a past user turn, edit it, re-run as a branch ──
        # `pending` arms the next submit to fork a lineage-tagged branch (source
        # untouched) instead of just appending. `records`/`turn_index` snapshot the
        # conversation at the moment the picker opened.
        _backtrack = {"pending": False, "from_id": "", "turn_index": 0, "records": []}

        def _open_backtrack():
            """Open a picker of the prior user turns; choosing one loads it into the
            editor for editing and arms a branch on the next Enter."""
            if not chat_w:
                return
            records = [{"role": getattr(m, "role", ""), "text": getattr(m, "text", "")}
                       for m in chat_w.transcript.messages
                       if getattr(m, "role", "") in ("user", "assistant")
                       and (getattr(m, "text", "") or "").strip()]
            user_turns = [(i, r["text"]) for i, r in enumerate(records) if r["role"] == "user"]
            if not user_turns:
                toasts.show("no earlier turn to step back to", "info", ttl=1.2)
                return
            from ..core.select_list import SelectList
            from ..core.overlay import Overlay
            # newest first; row begins with the rollout index so confirm() recovers it
            rows = [f"{i}  {t.replace(chr(10), ' ')[:60]}" for i, t in reversed(user_turns)]
            overlay["current"] = Overlay(kind="backtrack",
                                         title="backtrack · edit a past turn → re-run as branch",
                                         select=SelectList(rows, height=12))
            _backtrack["records"] = records
            _backtrack["from_id"] = last_task_id

        # ── image attachment (drag-drop / paste / /attach / clipboard probe) ──
        def _attach_src(src):
            """Stage an image for the next message via run_goal.attach_image (path or
            (bytes,mime)). Updates the 📎 chip + a toast. Returns True on success."""
            fn = getattr(controller._run_goal, "attach_image", None)
            if fn is None:
                toasts.show("attachments not available", "warn", ttl=1.4)
                return False
            ok, label = fn(src)
            if ok:
                _attached["items"].append({"label": label})
                toasts.show(f"📎 attached {label}", "success", ttl=1.6)
                if chat_w:
                    chat_w.add("system", f"📎 attached {label} — sends with your next message")
            else:
                toasts.show(f"couldn't attach: {label}", "warn", ttl=2.2)
            return ok

        def _attach_clear():
            """Drop all staged attachments (and clear the engine-side queue)."""
            _attached["items"] = []
            fn = getattr(controller._run_goal, "clear_attachments", None)
            if fn:
                fn()

        def _attach_remove(index):
            """Remove ONE staged attachment by 0-based index (#128), keeping the TUI chip list and
            the engine's ordered queue in lockstep."""
            fn = getattr(controller._run_goal, "remove_attachment", None)
            if not (0 <= index < len(_attached["items"])):
                toasts.show(f"no attachment #{index + 1}", "warn", ttl=1.6)
                return
            label = _attached["items"][index].get("label", "")
            if fn:
                fn(index)                       # drop it engine-side (same order)
            _attached["items"].pop(index)       # mirror in the chip list
            toasts.show(f"removed {label}", "info", ttl=1.4)

        def _attach_clipboard():
            """Pull an image off the system clipboard and stage it. read_image() tries every
            reader this OS supports (wl-paste/xclip/CopyQ/GTK on Linux, pngpaste/osascript on mac,
            PowerShell on Windows). Some readers shell out / spawn a short-lived helper, which can
            take a few seconds when the clipboard holds NO image — so we run it on a BACKGROUND
            thread and post the result back, NEVER freezing the UI on Ctrl+V (the bug: read_image
            blocked the main loop ~5s). If no reader can read an image here,
            image_paste_unavailable_reason points at /attach (no install pitch)."""
            def _work():
                try:
                    from ..core.clipboard import read_image, image_paste_unavailable_reason
                    got = read_image()
                except Exception:  # noqa: BLE001
                    got = None
                if got is not None:
                    _post(lambda g=got: _attach_src(g))   # (bytes, mime) → 📎 chip on the UI thread
                    return
                try:
                    _why = image_paste_unavailable_reason()
                except Exception:  # noqa: BLE001
                    _why = ""
                msg = _why or "no image on the clipboard — copy an image first, or /attach <path>"
                _post(lambda m=msg, w=_why: toasts.show(m, "info", ttl=4.0 if w else 2.0))
            _run_bg("clipboard image", _work)
            return True

        # ── plan card ──
        # Show the plan as ONE system message: a header naming the written plan FILE + the full
        # numbered body. Because it's a long `system` message it AUTO-FOLDS to a one-line
        # "▸ … click to expand" card and click-toggles (the existing fold mechanism) — exactly the
        # "click to see the full plan, click again to collapse" behavior the user asked for.
        _plan_card = {"last_sig": ""}     # dedup so resume/re-render doesn't double-add the card

        def _show_plan_card(st, *, force=False):
            if not (chat_w and st is not None):
                return
            steps = list(getattr(st, "plan", []) or [])
            if not steps:
                if force:
                    chat_w.add("system", "no plan yet — give me a task first.")
                return
            pf = getattr(st, "plan_file", "") or ""
            if not pf:
                # state loaded from disk doesn't carry the runtime plan_file — reconstruct the
                # conventional path the engine writes to, if it exists.
                try:
                    from pathlib import Path as _P
                    _cand = _P(getattr(st, "workspace_root", "") or ".") / ".syntra" / "plans" \
                        / f"plan-{getattr(st, 'task_id', '')}.md"
                    if _cand.is_file():
                        pf = str(_cand)
                except Exception:  # noqa: BLE001
                    pf = ""
            # a relative path reads cleaner in the header (and stays clickable)
            rel = pf
            try:
                import os as _os
                rel = _os.path.relpath(pf) if pf else ""
            except Exception:  # noqa: BLE001
                rel = pf
            sig = f"{getattr(st, 'task_id', '')}:{len(steps)}:{rel}"
            if not force and sig == _plan_card["last_sig"]:
                return                       # already showed this exact plan card
            _plan_card["last_sig"] = sig
            head = f"📋 plan · {len(steps)} step{'s' if len(steps) != 1 else ''}"
            if rel:
                head += f" · {rel}"
            body = [head, ""]
            for i, s in enumerate(steps, 1):
                tag = "" if getattr(s, "priority", "must") == "must" else "  (nice-to-have)"
                body.append(f"{i}. {str(getattr(s, 'description', s) or '')}{tag}")
            chat_w.add("system", "\n".join(body))

        # ── submit ──
        def _submit(text: str):
            nonlocal active, last_task_id, _run_start, _run_tokens, session_title, pending_title
            nonlocal _cmd_binds  # #173: /keymap bind|unbind re-applies the live command-key map
            from .main import session_dispatch, _handle_session_action, capture_output

            # /help (or /?) — open the searchable command palette (readable, lists
            # every command with its description + arg hint) instead of dumping dim
            # text into the chat (P37/P20).
            if text.strip() in ("/help", "/?", "/commands"):
                from ..core.overlay import make_command_overlay
                overlay["current"] = make_command_overlay()
                return

            # /attach [path|clear|list|remove N] — stage a file (ANY type: image→vision,
            # text/code→context, #127) to send with the next message. No arg → pull an image off
            # the clipboard. "clear" drops all; "list" shows staged; "remove N" drops one (#128).
            # The 📎 chip shows what's queued.
            _ts = text.strip()
            if _ts == "/attach" or _ts.startswith("/attach "):
                arg = _ts[len("/attach"):].strip().strip('"').strip("'")
                if arg in ("clear", "none", "drop"):
                    _attach_clear()
                    toasts.show("attachments cleared", "info", ttl=1.2)
                elif arg == "list":
                    _names = [i["label"] for i in _attached["items"]]
                    if _names:
                        chat_w.add("system", "📎 staged: " + " · ".join(
                            f"[{i+1}] {n}" for i, n in enumerate(_names))) if chat_w else None
                    else:
                        toasts.show("nothing attached", "info", ttl=1.2)
                elif arg.startswith("remove"):
                    _rest = arg[len("remove"):].strip()
                    if _rest.isdigit():
                        _attach_remove(int(_rest) - 1)   # 1-based for the user
                    else:
                        toasts.show("usage: /attach remove <number>", "warn", ttl=1.8)
                elif arg:
                    _attach_src(arg)            # a file path (any type)
                else:
                    _attach_clipboard()         # no arg → clipboard image
                return

            # /plan — reopen the current plan as a click-to-expand card. Reads the live result's
            # state if present, else loads the last task from disk.
            if _ts == "/plan":
                _st = getattr(getattr(controller, "result", None), "state", None)
                if _st is None and last_task_id:
                    try:
                        from ..core.state import TaskStore as _TS
                        from .main import _state_root as _sr
                        _st = _TS(_sr()).load(last_task_id)
                    except Exception:  # noqa: BLE001
                        _st = None
                _show_plan_card(_st, force=True)
                return

            # /wizard — open a demo question wizard (P11). The engine raises real
            # ones via a "[question] {json}" line (see _drain); this lets you try it.
            if text.strip() == "/wizard":
                _open_wizard({
                    "title": "Clarify",
                    "steps": [
                        {"id": "surface", "prompt": "How should Syntra surface a clarifying question?",
                         "kind": "single",
                         "options": ["Popup overlay (like this)", "Inline numbered list", "Side panel"]},
                        {"id": "inputs", "prompt": "Which inputs should the widget support?",
                         "kind": "multi",
                         "options": ["Arrow keys + Enter", "Mouse click", "Number hotkeys", "Free-text 'Other'"]},
                        {"id": "fallback", "prompt": "Anything else?", "kind": "text"},
                    ],
                })
                return

            # /copy-chat — copy the WHOLE conversation to the clipboard (no drag needed)
            if text.strip() in ("/copy-chat", "/copy-all"):
                _copy_whole_chat()
                return

            # /resume-question — reopen a wizard the user paused via "Chat about this"
            if text.strip() == "/resume-question":
                if wizard_modal["paused"] is not None:
                    wizard_modal["paused"].resume_from_chat()
                    wizard_modal["current"] = wizard_modal["paused"]
                    wizard_modal["paused"] = None
                elif chat_w:
                    chat_w.add("system", "no paused question to resume")
                return

            # /trace — fold/unfold the background activity trace in the chat (P29)
            if text.strip().split() and text.strip().split()[0] == "/trace" and chat_w:
                tc = not chat_w.transcript.trace_collapsed
                chat_w.transcript.trace_collapsed = tc
                toasts.show(f"background trace {'collapsed' if tc else 'expanded'}", "info")
                return

            # F37: two related verbs, disambiguated by arg:
            #   /backtrack  (preferred) or bare /rewind  → the picker: edit a past turn → re-run
            #   /rewind N                                 → delete the last N messages (handled below)
            # `/backtrack` is the clear name for the picker; bare `/rewind` stays as a habit alias.
            if text.strip() in ("/backtrack", "/rewind"):
                _open_backtrack()
                return

            # Backtrack resend (A2): a past turn was loaded for editing — fork a
            # lineage-tagged branch from the current session FIRST (history left
            # untouched), then let the edited text run as the branch's new turn.
            if _backtrack.get("pending"):
                _backtrack["pending"] = False
                if text.strip() and not text.strip().startswith("/"):
                    try:
                        from ..core.state import TaskStore
                        _store = TaskStore(_state_root())
                        _src = _backtrack.get("from_id") or last_task_id
                        if _src:
                            _store.save_rollout(_src, _backtrack.get("records", []))
                            _new = _store.backtrack_branch(
                                _src, _backtrack.get("turn_index", 0), edited_text=text)
                            if chat_w:
                                chat_w.add("system",
                                           f"↳ branched {_new[:8]} from {_src[:8]} — re-running edited turn")
                    except Exception as e:  # noqa: BLE001 - branch is best-effort; run still proceeds
                        if chat_w:
                            chat_w.add("system", f"backtrack branch failed: {e}")

            # /fork with no id — open the fork picker (A2). With an id it falls
            # through to the normal /fork <id> branch handler below.
            if text.strip() in ("/fork", "/branch"):
                from ..core.overlay import make_session_overlay
                from ..core.state import TaskStore
                try:
                    _sess = TaskStore(_state_root()).recent_sessions(30)
                except Exception:  # noqa: BLE001
                    _sess = []
                if _sess:
                    overlay["current"] = make_session_overlay(
                        _sess, title="fork a session", intent="fork")
                elif chat_w:
                    chat_w.add("system", "no saved sessions to fork")
                return

            action, arg = session_dispatch(text)

            if action == "exit":
                raise _Quit()

            # /mcp (no sub-command) — #227: open the MCP management screen as a styled overlay
            # (servers + live status + tool count). `/mcp add|remove …` still goes to the CLI
            # handler below. A read-only vet-your-servers view; strong for a security tool.
            if action == "mcp" and (arg or "").strip().split()[:1] not in (["add"], ["remove"]):
                from ..core.tui_model import render_mcp_screen
                from ..core import mcp_config
                try:
                    _servers = mcp_config.load_servers(_state_root())
                except Exception:  # noqa: BLE001
                    _servers = []
                # live client tools, if the run exposes connected MCP clients.
                _live = {}
                try:
                    _clients = getattr(controller._run_goal, "mcp_clients", None) or []
                    for _cl in _clients:
                        _nm = getattr(_cl, "name", "") or ""
                        _tools = [t.get("name", "") for t in (getattr(_cl, "list_tools", lambda: [])() or [])]
                        _live[_nm] = {"connected": True, "tools": _tools}
                except Exception:  # noqa: BLE001
                    _live = {}
                try:
                    _sr, _sc = stdscr.getmaxyx()
                except Exception:  # noqa: BLE001
                    _sc = 100
                _rows = render_mcp_screen(servers=_servers, live=_live,
                                          width=min(84, max(40, _sc - 8)))
                from ..core.overlay import make_styled_overlay
                overlay["current"] = make_styled_overlay("mcp", _rows)
                return

            # ! shell commands — run and show output in chat
            if action == "shell":
                if chat_w:
                    chat_w.add("user", f"! {arg}")
                def _sh(cmd=arg):
                    import subprocess
                    try:
                        r = subprocess.run(cmd, shell=True, capture_output=True,
                                           text=True, timeout=30, cwd=os.getcwd())
                        out = (r.stdout + r.stderr).strip()
                        msg = f"```\n{out}\n```" if out else f"(exit {r.returncode})"
                    except subprocess.TimeoutExpired:
                        msg = "command timed out (30s limit)"
                    except Exception as _e:  # noqa: BLE001 - friendly message for any failure
                        msg = f"shell error: {_e}"
                    _post(lambda: chat_w and chat_w.add("system", msg))
                _run_bg("shell", _sh)   # off the main loop — no 30s freeze (P5)
                return

            # /copy [n] — copy the last answer, or fenced code block #n, to the clipboard (M5)
            if action == "copy":
                from ..core.clipboard import copy as _clip_copy
                from ..core.tui_model import extract_code_blocks
                _blocks = extract_code_blocks(chat_w.transcript.messages) if chat_w else []
                if arg.strip().isdigit():
                    _n = int(arg.strip())
                    if 1 <= _n <= len(_blocks):
                        _clip_copy(_blocks[_n - 1])
                        toasts.show(f"copied block [{_n}]", "success")
                    else:
                        toasts.show(f"no block [{_n}] — {len(_blocks)} available", "error")
                else:
                    _last = ""
                    if chat_w:
                        _last = next((m.text for m in reversed(chat_w.transcript.messages)
                                      if m.role in ("assistant", "assistant_stream")), "")
                    if _last:
                        _clip_copy(_last); toasts.show("copied last answer", "success")
                    else:
                        toasts.show("nothing to copy yet", "info")
                return

            # /blocks — list the indexed code blocks for /copy <n> (M5)
            if action == "blocks":
                from ..core.tui_model import extract_code_blocks
                _blocks = extract_code_blocks(chat_w.transcript.messages) if chat_w else []
                if not _blocks:
                    _info_popup("blocks", "No code blocks yet.\n\nWhen the assistant emits ``` "
                                "fenced code, each is indexed —\ncopy one with /copy <n>.")
                else:
                    _out = ["Code blocks — copy one with /copy <n>:", ""]
                    for _i, _b in enumerate(_blocks, 1):
                        _first = ((_b.splitlines() or [""])[0]).strip()[:54]
                        _out.append(f"  [{_i}]  {_first}   ({len(_b.splitlines())} lines)")
                    _info_popup("blocks", "\n".join(_out))
                return

            # /spin [N] — F24: an on-demand DEMO so you can SEE the multi-agent panel
            # populate + work live (Syntra running its OWN visible agents, not writing a
            # script). N mock agents start, show updating activity + tool/token counts +
            # ticking elapsed, then finish. Pairs with the now-animated trace spinner.
            if action == "spin":
                try:
                    n = max(1, min(8, int(arg.strip()))) if arg.strip() else 5
                except ValueError:
                    n = 5
                _summon_panel("agent_status")
                if not agent_w:
                    if chat_w:
                        chat_w.add("system", "agents panel unavailable")
                    return
                agent_w.reset()
                # Reuse the real role slots first so they show as RUNNING (not idle ○) —
                # otherwise the demo renders 3 stray idle rows beside the live agents.
                _roles = (["planner", "executor", "reviewer"]
                          + [f"agent-{i + 1}" for i in range(3, n)])[:n]
                # realistic "{tool}: {arg}" activities (same shape as loop._tool_activity)
                # so the demo exercises the real Tool(arg) feed path, not a fake verb.
                _verbs = ["read_file: core/loop.py", "exec_command: pytest -q",
                          "search: TODO markers", "edit_file: router.py",
                          "web_fetch: docs.example.com", "read_file: tui2.py",
                          "exec_command: ruff check ."]
                action_feed.clear()                     # fresh feed for the demo
                for _r in _roles:                       # populate immediately (visible)
                    agent_w.feed("agent_start", {"role": _r, "model": "demo"})
                    action_feed.ingest("agent_start", {"role": _r})   # also the inline feed

                def _spin_demo(roles=_roles, verbs=_verbs, count=n):
                    import time as _t
                    for _step in range(12):
                        for _i, _r in enumerate(roles):
                            _act = verbs[(_step + _i) % len(verbs)]
                            _post(lambda r=_r, a=_act, s=_step: (
                                agent_w.feed("agent_activity",
                                             {"role": r, "tools": s + 1, "tokens": (s + 1) * 137,
                                              "activity": a}),
                                action_feed.ingest("agent_activity", {"role": r, "activity": a})))
                        _t.sleep(0.45)
                    for _r in roles:
                        _post(lambda r=_r: (agent_w.feed("agent_done", {"role": r}),
                                            action_feed.ingest("agent_done", {"role": r})))
                    _post(lambda c=count: chat_w and chat_w.add(
                        "system", f"✓ demo done — that's the multi-agent panel ({c} agents)."))
                _run_bg("spin", _spin_demo)
                if chat_w:
                    chat_w.add("system",
                               f"▶ spinning up {n} demo agents — watch the AGENTS panel "
                               "(they read / think / run, then finish).")
                return

            # /models — bare opens the Auto/Manual toggle board; with a role, the picker
            if action == "models":
                from ..core.overlay import make_model_overlay, make_roleboard_overlay
                role = ""
                parts = arg.split() if arg else []
                _pinnable = ("planner", "executor", "reviewer", "subagent")
                if parts and parts[0] == "pin" and len(parts) >= 2 and parts[1] in _pinnable:
                    role = parts[1]
                elif parts and parts[0] in _pinnable:
                    role = parts[0]
                overlay["stack"] = []   # fresh /models flow — no stale parent
                overlay["current"] = make_model_overlay(role) if role else make_roleboard_overlay()
                return

            # /layout
            if action == "layout":
                _handle_layout(arg)
                return

            # /diff — populate the diff viewer with the real git diff
            if action == "diff":
                # #141: bare `/diff` prefers THIS TURN's coherent diff (captured from the
                # engine's turn_diff event) so you see what the agent just changed, not the
                # whole git working tree. `/diff git` (or --staged) forces the git view.
                _arg_l = (arg or "").strip().lower()
                # #221: `/diff turns` opens the per-turn diff BROWSER (step ◀ older/newer ▶
                # through every past turn's changes), vs bare `/diff` = just this turn.
                if _arg_l in ("turns", "history", "browse"):
                    if _turn_diff_hist.count() == 0 and _turn_diff.has_diff():
                        _turn_diff_hist.record(_turn_diff.summary, _turn_diff.lines)  # seed current
                    try:
                        _sr, _sc = stdscr.getmaxyx()
                    except Exception:  # noqa: BLE001
                        _sc = 100
                    from ..core.overlay import make_styled_overlay
                    _ov = make_styled_overlay("turn diffs", _turn_diff_hist.render(min(88, max(40, _sc - 8))))
                    _ov.turn_diff_hist = _turn_diff_hist   # type: ignore[attr-defined]  (←/→ steps it)
                    overlay["current"] = _ov
                    return
                if _turn_diff.has_diff() and _arg_l not in ("git", "--staged", "staged", "worktree"):
                    _added, _removed = _turn_diff.stats()
                    if diff_w:
                        diff_w.set_diff(_turn_diff.summary or "this turn", _turn_diff.text())
                    if chat_w:
                        chat_w.add("system", f"turn diff: {_turn_diff.summary} "
                                   f"(+{_added} -{_removed}) — `/diff git` for the working tree")
                    return
                import shutil
                if not shutil.which("git"):
                    if chat_w: chat_w.add("system", "git not installed")
                    return
                def _diff(staged_arg=arg):
                    import subprocess
                    staged = staged_arg.strip() == "--staged"
                    if staged:
                        r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=5)
                    else:
                        r = subprocess.run(["git", "diff"], capture_output=True, text=True, timeout=5)
                        if not r.stdout.strip():
                            r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=5)
                            staged = True
                    if r.returncode == 0 and r.stdout.strip():
                        fname = "staged" if staged else "working tree"
                        for ln in r.stdout.split("\n"):
                            if ln.startswith("+++ b/"):
                                fname = ln[6:]
                                break
                        added = r.stdout.count("\n+") - r.stdout.count("\n+++")
                        removed = r.stdout.count("\n-") - r.stdout.count("\n---")
                        out = r.stdout
                        def _apply(fname=fname, out=out, added=added, removed=removed):
                            if diff_w:
                                diff_w.set_diff(fname, out)
                            if chat_w:
                                chat_w.add("system", f"diff loaded: {fname} (+{added} -{removed})")
                        _post(_apply)
                    else:
                        _post(lambda: chat_w and chat_w.add("system", "no changes (working tree + staging clean)"))
                _run_bg("diff", _diff)   # off the main loop (P5)
                return

            # /themes
            if action == "themes" and arg:
                from ..core.themes import set_theme as _st
                if _st(arg):
                    _load_colors()
                    if chat_w:
                        chat_w.add("system", f"theme: {arg}")
                else:
                    if chat_w:
                        chat_w.add("system", f"unknown theme: {arg}")
                return
            if action == "themes" and not arg:
                # no name -> open the live-preview picker (browse by seeing the real UI)
                from ..core.overlay import make_theme_overlay
                from ..core.themes import list_themes as _lt, current_theme as _ct
                overlay["current"] = make_theme_overlay(_lt(), _ct())
                return

            # /bg — background task (E1: drive the REAL engine scheduler, not a private dict).
            # The engine's run_bg() runs the goal non-blocking, learns from it (T12), and fires
            # task_started/task_done/task_error/monitor events we render here.
            if action == "bg":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /bg <goal>")
                    return
                _bg_run = getattr(run_goal, "run_bg", None)
                if _bg_run is None:
                    if chat_w: chat_w.add("system", "background scheduler unavailable")
                    return
                def _on_sched(kind, payload):
                    # marshal scheduler events back onto the UI thread
                    def _show():
                        if not chat_w:
                            return
                        tid = payload.get("task_id", "?")
                        if kind == "task_done":
                            chat_w.add("system", f"✓ background {tid} done · {payload.get('elapsed_s', 0)}s "
                                                 f"(waiting {payload.get('waiting', 0)})")
                        elif kind == "task_error":
                            chat_w.add("system", f"✗ background {tid} failed: {payload.get('error', '')[:120]}")
                    _post(_show)
                task = _bg_run(arg, _on_sched)
                _label = getattr(task, "label", arg[:48])
                _tid = getattr(task, "task_id", "bg")
                if chat_w:
                    chat_w.add("system", f"▸ started background {_tid}: {_label}\n  /jobs to check status")
                toasts.show(f"bg {_tid} started", "info")
                return

            # /jobs — list background tasks straight from the engine scheduler's live status.
            if action == "jobs":
                _sched = getattr(run_goal, "scheduler", None)
                rows = _sched.all_status() if _sched is not None else []
                if not rows:
                    if chat_w: chat_w.add("system", "no background tasks")
                else:
                    icon = {"running": "▸", "done": "✓", "error": "✗", "pending": "·"}
                    out = [f"background tasks (running {sum(1 for r in rows if r['status'] == 'running')}"
                           f" · {len(rows)} total):"]
                    for r in rows:
                        line = f"  {icon.get(r['status'], '?')} {r['task_id']}  {r['status']}  {r.get('elapsed_s', 0)}s"
                        if r.get("label"):
                            line += f"  — {r['label'][:40]}"
                        out.append(line)
                    if chat_w: chat_w.add("system", "\n".join(out))
                return

            # /map — show repo map (scan can be slow → off the main loop, P5)
            if action == "map":
                def _map():
                    from ..core.repo_map import build_repo_map
                    summary = build_repo_map(os.getcwd()).summary(40)
                    _post(lambda: _info_popup("repo map", summary))
                _run_bg("map", _map)
                return

            # /symbols — find symbol
            if action == "symbols":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /symbols <name>")
                    return
                def _symbols(name=arg):
                    from ..core.repo_map import build_repo_map
                    found = build_repo_map(os.getcwd()).find_symbol(name)
                    if found:
                        msg = "\n".join([f"symbol '{name}' found in:"] + [f"  {e.path}" for e in found[:10]])
                    else:
                        msg = f"symbol '{name}' not found"
                    _post(lambda: chat_w and chat_w.add("system", msg))
                _run_bg("symbols", _symbols)
                return

            # /deep-research — multi-source research
            if action == "deep-research":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /deep-research <topic>")
                    return
                # P22: run as a real research investigation (researcher prompt + web tools +
                # cross-family citation auditor) via a one-shot toggle the runner reads.
                _st = getattr(controller._run_goal, "set_toggle", None)
                if _st:
                    _st("_research_next", True)
                _submit(f"Research and write a cited report on: {arg}")
                return

            # /council — run a goal with SEVERAL agents in parallel (plan council +
            # review panel) so you can WATCH real agents work in the panel (user F24/F30).
            if action == "council":
                if not arg.strip():
                    if chat_w:
                        chat_w.add("system",
                            "usage: /council <goal>\n"
                            "  Runs it with several planner + reviewer agents in parallel —\n"
                            "  open the agents panel (click the bottom bar) to watch them work.")
                    return
                _st = getattr(controller._run_goal, "set_toggle", None)
                if _st:
                    _st("_council_next", 3)
                if chat_w:
                    chat_w.add("system", "● running with 3 agents in parallel — watch the agents panel")
                _submit(arg)
                return

            # /key — add an API key for a provider FROM the TUI. BARE /key opens an interactive
            # masked POPUP FORM (so the secret is never typed on the command line or into chat
            # history). /key <provider> <key> [base_url] is the quick direct path. Either way the
            # key is persisted chmod-600 and NEVER echoed.
            if action == "key":
                parts = (arg or "").split()
                # /key detect <provider> — probe a configured provider's /models with its key,
                # validate each model, and make the working ones routable (allowed_models +
                # minimal catalog overlay). Runs in the background so the UI never blocks.
                if parts and parts[0] == "detect":
                    if len(parts) < 2:
                        if chat_w: chat_w.add("system", "usage: /key detect <provider>")
                        return
                    _detect_models_bg(parts[1])
                    return
                if len(parts) < 2:
                    # open the popup form (pre-fill provider if one bare word was given)
                    from ..core.key_entry import KeyEntryForm
                    _f = KeyEntryForm()
                    if len(parts) == 1:
                        _f.provider = parts[0]; _f.focus = 1   # provider given → jump to key field
                    key_modal["current"] = _f
                    return
                _prov, _key = parts[0], parts[1]
                _burl = parts[2] if len(parts) > 2 else ""
                _adder = getattr(controller._run_goal, "add_provider_key", None)
                if _adder is None:
                    if chat_w:
                        chat_w.add("system", "key entry unavailable (no provider runner)")
                    return
                ok, msg = _adder(_prov, _key, _burl)
                if chat_w:
                    chat_w.add("system", ("✓ " if ok else "✗ ") + msg)
                toasts.show(("✓ key added" if ok else "key not added"),
                            "success" if ok else "error", ttl=2.0)
                if ok:
                    _detect_models_bg(_prov)   # auto-discover what this key unlocks
                return

            # /compare — ask N models the SAME question, show every answer side by side
            # in a clickable card view, highlight the judge's pick, synthesize the best,
            # and let the user accept any candidate (manual override). The engine streams
            # compare_* events; CompareView (pure) holds the state + render.
            if action == "compare":
                q = arg.strip()
                if not q:
                    if chat_w:
                        chat_w.add("system",
                            "usage: /compare <question>\n"
                            "  Asks several models the same thing — compare side by side,\n"
                            "  ⏎ accepts the highlighted answer (or the synthesized best).")
                    return
                _runner = getattr(controller._run_goal, "run_compare", None)
                if _runner is None:
                    if chat_w:
                        chat_w.add("system", "compare unavailable (no model runner)")
                    return
                from ..core.compare_view import CompareView
                view = CompareView(question=q)
                compare_modal["view"] = view

                def _compare_work(_q=q, _view=view, _run=_runner):
                    def _on_event(kind, payload):
                        # only the compare_* events drive the cards; route them to the
                        # main thread so the view mutates on the draw thread (no races).
                        if kind in ("compare_start", "compare_candidate", "compare_result"):
                            _post(lambda k=kind, p=dict(payload): _view.on_event(k, p))
                    try:
                        _run(_q, 3, _on_event)
                    except Exception as e:  # noqa: BLE001
                        _post(lambda e=e: _view.on_event(
                            "compare_result",
                            {"best_index": -1, "rationale": f"compare failed: {e}", "synthesis": ""}))
                _run_bg("compare", _compare_work)
                return

            # /plan-review — toggle whether tasks pause for plan approval (default ON: the plan
            # is shown and vetted before it runs). The setting PERSISTS across restarts, and the
            # session toggle seeds from it. Turn OFF so tasks you give just run.
            if action == "plan-review":
                from ..cli.main import _load_plan_review, _save_plan_review
                _get = getattr(controller._run_goal, "get_toggle", None)
                _st = getattr(controller._run_goal, "set_toggle", None)
                _cur = _get("_plan_review", _load_plan_review()) if _get else _load_plan_review()
                _new = not _cur
                if _st:
                    _st("_plan_review", _new)
                _save_plan_review(_new)              # persist across restarts
                if chat_w:
                    chat_w.add("system", "plan review ON — I'll show the plan and wait for your "
                               "Enter before running" if _new else
                               "plan review OFF — tasks you give just run (risky tools still ask)")
                toasts.show(f"plan review {'on' if _new else 'off'}", "info")
                return

            # /providers — show providers in chat
            if action == "providers":
                try:
                    from ..core.registry import ProviderRegistry
                    reg = ProviderRegistry.load()
                    lines = []
                    for ep in reg.endpoints:
                        key_ok = "✓" if ep.credential_state != "missing" else "✗"
                        key_label = {"keyed": "configured", "no-auth": "no-auth",
                                     "missing": "missing"}.get(ep.credential_state, "missing")
                        n_models = len(ep.allowed_models) if ep.allowed_models else 0
                        models_str = f"{n_models} models" if n_models else "wildcard"
                        lines.append(f"{key_ok} {ep.name:16s}  {key_label:10s}  {models_str:12s}  {ep.base_url[:34]}")
                    _info_popup("providers", "\n".join(lines) or "no providers configured")
                except Exception as e:
                    _info_popup("providers", f"error: {e}")
                return

            # /code-review — send git diff to the reviewer
            # /review and /code-review both run a real diff review in the TUI (parity with
            # /simplify). /review = a SKEPTICAL reviewer pass; /code-review = a bug/issue pass.
            # (Without an inline handler /review fell through to the engine's hint-only text —
            # inconsistent with its two siblings; audit flagged it.)
            if action in ("review", "code-review"):
                import subprocess, shutil
                if not shutil.which("git"):
                    if chat_w: chat_w.add("system", "git not installed")
                    return
                r = subprocess.run(["git", "diff"], capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    if chat_w: chat_w.add("system", "no changes to review")
                    return
                effort = arg if arg in ("low", "medium", "high") else "medium"
                diff_preview = r.stdout[:6000]
                if action == "review":
                    goal = ("Act as a SKEPTICAL code reviewer over this git diff. Challenge the "
                            "approach, hunt for correctness/security/edge-case problems, and call "
                            "out anything risky. Only raise issues you're ≥80% confident are real.\n\n"
                            f"```diff\n{diff_preview}\n```")
                else:
                    goal = (f"Review this git diff for bugs, issues, and improvements. "
                            f"Effort level: {effort}. Focus on correctness issues ≥80% confidence.\n\n"
                            f"```diff\n{diff_preview}\n```")
                _submit(goal)
                return

            # /browse — open a URL in a headless browser, show page text
            if action == "browse":
                from ..core.browser import playwright_available
                if not playwright_available():
                    _info_popup("browse", "Playwright not installed.\n\nRun:\n  pip install playwright\n  playwright install chromium")
                    return
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /browse <url>")
                    return
                import threading as _thr
                def _browse_work(url):
                    # F5: this runs on a worker thread — route every chat_w.add through _post()
                    # so it doesn't mutate transcript.messages while the main thread renders it.
                    try:
                        from ..core.browser import get_browser
                        b = get_browser()
                        nav = b.navigate(url)
                        text = b.text(2000)
                        if chat_w:
                            _post(lambda: chat_w.add("system", nav))
                            _post(lambda: chat_w.add("system", f"```\n{text}\n```"))
                    except Exception as e:
                        if chat_w: _post(lambda e=e: chat_w.add("system", f"browse error: {e}"))
                _thr.Thread(target=_browse_work, args=(arg,), daemon=True).start()
                if chat_w: chat_w.add("system", f"browsing {arg}...")
                return

            # /image — attach an image for the model to see next message
            if action == "image":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /image <path>")
                    return
                from pathlib import Path as _P
                p = _P(arg).expanduser()
                if not p.exists():
                    if chat_w: chat_w.add("system", f"file not found: {arg}")
                    return
                # F7: route through the real attach path (run_goal.attach_image) — the old code
                # stashed a data URL in state["_pending_images"], which nothing ever read, so the
                # image was never actually sent. _attach_src stages it for the next message.
                try:
                    if _attach_src(str(p)):
                        if chat_w:
                            chat_w.add("system", f"📎 image attached: {p.name} — describe what you want in your next message")
                    else:
                        if chat_w: chat_w.add("system", "image attach unavailable")
                except Exception as e:
                    if chat_w: chat_w.add("system", f"image error: {e}")
                return

            # /view <path> — render an image INLINE in the terminal (kitty/iterm2/sixel), or
            # bare /view to clear the current one.
            if action == "preview":
                # Render a URL / HTML file / html string IN the terminal via a headless browser →
                # PNG → HALF-BLOCK inline. UNLIKE images (photos look bad in half-blocks, so
                # _show_image sends those to the external viewer), a rendered PAGE is mostly flat
                # bg + text/boxes → half-blocks are readable, and the user explicitly wants the page
                # shown IN the terminal. So /preview uses the overlay with the REAL detected caps
                # (HALFBLOCK on a truecolor VTE) directly, NOT the image _show_image gate.
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /preview <url or html file>")
                    return
                import hashlib as _hl
                from pathlib import Path as _P
                _od = _P.cwd() / ".syntra" / "preview"
                _op = _od / (_hl.sha1(arg.encode("utf-8", "replace")).hexdigest()[:12] + ".png")
                def _prev_work(target=arg, out=_op, od=_od):
                    from ..core import browser_preview as _bp
                    ok, msg = _bp.render_to_png(target, out, scratch_dir=od)
                    if ok:
                        _post(lambda p=str(out): _preview_inline(p, target))
                    else:
                        _post(lambda m=msg: chat_w and chat_w.add("system", f"could not preview: {m}"))
                _run_bg("preview", _prev_work)
                return

            if action == "view":
                from pathlib import Path as _P
                if not arg:
                    image_overlay["path"] = None       # clear; _paint_image_overlay deletes it
                    if chat_w: chat_w.add("system", "cleared inline image")
                    _draw()
                    return
                p = _P(arg).expanduser()
                if not p.is_file():
                    if chat_w: chat_w.add("system", f"file not found: {arg}")
                    return
                _show_image(str(p))    # self-managing: sharp inline where supported, else preview+auto-open
                _draw()
                return

            # /open <path> — open an image FULL-SIZE in the OS image viewer (the always-readable
            # path, on any terminal). Bare /open re-opens the currently-shown inline image.
            if action == "open":
                from pathlib import Path as _P
                _tgt = arg or (image_overlay.get("path") or "")
                if not _tgt:
                    if chat_w: chat_w.add("system", "usage: /open <image path>")
                    return
                p = _P(_tgt).expanduser()
                if not p.is_file():
                    if chat_w: chat_w.add("system", f"file not found: {_tgt}")
                    return
                ok = _open_image_viewer(str(p))
                if chat_w:
                    chat_w.add("system", f"opened {p.name} in your image viewer" if ok
                               else f"couldn't open a viewer (no display?) — path: {p}")
                return

            # /simplify — cleanup-only review of the diff (no bug hunt)
            if action == "simplify":
                import subprocess, shutil
                if not shutil.which("git"):
                    if chat_w: chat_w.add("system", "git not installed")
                    return
                r = subprocess.run(["git", "diff"], capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=10)
                if not r.stdout.strip():
                    if chat_w: chat_w.add("system", "no changes to simplify")
                    return
                diff_preview = r.stdout[:6000]
                goal = ("Review this diff for SIMPLIFICATION only (not bugs): reduce duplication, "
                        "flatten nesting, clarify names, remove dead code. Behavior must stay identical.\n\n"
                        f"```diff\n{diff_preview}\n```")
                _submit(goal)
                return

            # /compact — summarize conversation context
            if action == "compact":
                if chat_w:
                    msgs = chat_w.transcript.messages
                    before = len(msgs)
                    # Keep last 10 messages, summarize the rest
                    if before > 15:
                        kept = msgs[-10:]
                        summary = f"[compacted {before - 10} messages]"
                        chat_w.transcript.messages = [type(msgs[0])(role="system", text=summary)] + list(kept)
                        chat_w.transcript.collapsed.clear()   # F26: indices changed → drop stale fold state
                        chat_w.transcript.expanded.clear()
                        chat_w.transcript._follow = True
                        chat_w.add("system", f"compacted: {before} → {len(chat_w.transcript.messages)} messages")
                    else:
                        chat_w.add("system", f"context small ({before} messages), no compaction needed")
                return

            # /feature — multi-agent feature workflow (explore→architect→review)
            if action == "feature":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /feature <goal>")
                    return
                if active or controller.running():
                    if chat_w: chat_w.add("system",
                        "a task is running — press Esc Esc to stop it first, then /feature")
                    return
                if chat_w:
                    chat_w.add("user", f"/feature {arg}")
                    chat_w.add("system", "● Multi-agent workflow: explore → architect → review")
                import threading as _thr
                def _feature_work(goal_text):
                    # F6: worker thread — every chat_w.add (here and in _prog) goes through _post()
                    # so UI mutation happens on the main loop, not concurrently with rendering.
                    try:
                        from ..core.orchestrator import Orchestrator
                        # call_model bridges to a single direct run per agent
                        def _call(role, system, user):
                            res = controller._run_goal(f"{system}\n\n{user}",
                                                       lambda t, r="tool": None, None)
                            for s in getattr(res, "state", type("X", (), {"plan": []})).plan:
                                if s.status == "done" and (s.result or "").strip():
                                    return s.result.strip()
                            return ""
                        def _prog(kind, payload):
                            if kind == "agent_start" and chat_w:
                                _post(lambda p=dict(payload): chat_w.add("system", f"  ▸ {p.get('role','?')}: {p.get('id','')}"))
                            elif kind == "phase" and chat_w:
                                _post(lambda p=dict(payload): chat_w.add("system", f"● {p.get('phase','?').capitalize()}"))
                        orch = Orchestrator(call_model=_call, progress=_prog, max_parallel=2)
                        plan = orch.plan_feature_dev(goal_text)
                        result = orch.execute(plan)
                        if chat_w:
                            done = sum(1 for p in result.phases for t in p.tasks if t.status == "done")
                            total = sum(len(p.tasks) for p in result.phases)
                            _post(lambda: chat_w.add("system", f"● workflow complete: {done}/{total} agents finished"))
                            # show the architect's output as the main result
                            for p in result.phases:
                                if p.name == "architect":
                                    for t in p.tasks:
                                        if t.result:
                                            _post(lambda r=t.result: chat_w.add("assistant", r[:1500]))
                    except Exception as e:
                        if chat_w: _post(lambda e=e: chat_w.add("system", f"feature workflow failed: {e}"))
                t = _thr.Thread(target=_feature_work, args=(arg,), daemon=True)
                t.start()
                toasts.show("feature workflow started", "info")
                return

            # /todo — session todo tracking
            if action == "todo":
                import json as _json
                from pathlib import Path as _P
                todo_path = _state_root() / "todos.json"
                try:
                    todos = _json.loads(todo_path.read_text()) if todo_path.exists() else []
                except (ValueError, OSError):
                    todos = []
                parts = (arg.split(None, 1) or [""]) if arg.strip() else [""]
                sub = parts[0].lower()
                rest = parts[1] if len(parts) > 1 else ""
                if sub == "add" and rest:
                    todos.append({"text": rest, "done": False})
                    todo_path.parent.mkdir(parents=True, exist_ok=True)
                    todo_path.write_text(_json.dumps(todos))
                    if chat_w: chat_w.add("system", f"todo added: {rest}")
                    toasts.show("todo added", "success")
                elif sub == "done" and rest.isdigit():
                    idx = int(rest) - 1
                    if 0 <= idx < len(todos):
                        todos[idx]["done"] = True
                        todo_path.write_text(_json.dumps(todos))
                        if chat_w: chat_w.add("system", f"done: {todos[idx]['text']}")
                else:
                    if todos:
                        lines = ["todos:"]
                        for i, t in enumerate(todos, 1):
                            mark = "■" if t["done"] else "□"
                            lines.append(f"  {mark} {i}. {t['text']}")
                        if chat_w: chat_w.add("system", "\n".join(lines))
                    else:
                        if chat_w: chat_w.add("system", "no todos. /todo add <text>")
                return

            # /grep — search workspace (off the main loop; can be slow, P5)
            if action == "grep":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /grep <pattern>")
                    return
                import shutil
                tool = "rg" if shutil.which("rg") else "grep"
                def _grep(pat=arg, tool=tool):
                    import subprocess
                    try:
                        if tool == "rg":
                            r = subprocess.run(["rg", "-n", "--max-count", "2", pat, "."],
                                               capture_output=True, text=True, timeout=15)
                        else:
                            r = subprocess.run(["grep", "-rn", "--max-count=2", pat, "."],
                                               capture_output=True, text=True, timeout=15)
                        out = r.stdout.strip()
                        if out:
                            lines = out.split("\n")[:20]
                            msg = f"grep '{pat}':\n" + "\n".join(f"  {ln}" for ln in lines)
                        else:
                            msg = f"no matches for '{pat}'"
                    except subprocess.TimeoutExpired:
                        msg = "search timed out"
                    _post(lambda: chat_w and chat_w.add("system", msg))
                _run_bg("grep", _grep)
                return

            # /search <pattern> — #217 workspace search overlay: streaming ripgrep across the
            # workspace → a navigable picker (start-truncated path + matched text). Enter inserts
            # an @path#Lnn mention so the agent can read that exact spot. Off the main loop (P5).
            if action == "search":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /search <pattern>")
                    return
                import shutil as _sh
                _tool = "rg" if _sh.which("rg") else "grep"
                def _ws_search(pat=arg, tool=_tool):
                    import subprocess
                    from ..core.tui_model import parse_rg_lines, search_result_label
                    try:
                        if tool == "rg":
                            r = subprocess.run(["rg", "-n", "--max-count", "3", "--", pat, "."],
                                               capture_output=True, text=True, timeout=20)
                        else:
                            r = subprocess.run(["grep", "-rn", "--max-count=3", "--", pat, "."],
                                               capture_output=True, text=True, timeout=20)
                        results = parse_rg_lines(r.stdout)[:200]
                    except subprocess.TimeoutExpired:
                        results = []
                    def _apply(results=results, pat=pat):
                        if not results:
                            if chat_w: chat_w.add("system", f"no matches for '{pat}'")
                            return
                        try:
                            _r, _c = stdscr.getmaxyx()
                        except Exception:  # noqa: BLE001
                            _c = 100
                        _w = min(88, max(40, _c - 10))
                        from ..core.overlay import Overlay
                        from ..core.select_list import SelectList
                        labels = [search_result_label(res, _w - 6) for res in results]
                        ov = Overlay(kind="search", title=f"search · {pat}",
                                     select=SelectList(labels, height=16))
                        ov.search_results = results   # type: ignore[attr-defined]  (row → result)
                        overlay["current"] = ov
                    _post(_apply)
                _run_bg("search", _ws_search)
                return

            # /watch <youtube-url> — "watch" a video: pull its full transcript + description,
            # explain what it teaches, and honestly flag when the core is VISUAL (so you should
            # actually watch it). A bare path (no YT url) keeps the old session-marker behavior.
            if action == "watch":
                from ..core.youtube import video_id as _yt_id
                if arg and _yt_id(arg):
                    if chat_w: chat_w.add("system", f"● watching (reading transcript of) {arg}")
                    def _watch_work(url=arg):
                        try:
                            from ..core.youtube import fetch_video
                            from ..core.video_understand import understand_video
                            v = fetch_video(url)
                            if not v.ok:
                                from ..core.youtube import innertube_key_help
                                msg = {"no_captions": "this video has no captions/transcript.",
                                       "potoken_gated": "this video's transcript is gated (needs a browser token) — can't read it here.",
                                       "missing_innertube_key": "YouTube transcript access is not configured; " + innertube_key_help() + "."}.get(
                                       v.status, f"couldn't get a transcript ({v.status}).")
                                _post(lambda m=msg, t=v.title: chat_w and chat_w.add("system", f"⚠ {t or url}: {m}\n  I can't 'watch' it — try opening it yourself."))
                                return
                            # explain via the engine's one-shot model call (same seam /feature uses)
                            def _caller(msgs):
                                sys = msgs[0].content; usr = msgs[1].content
                                res = controller._run_goal(f"{sys}\n\n{usr}", lambda _t, _r="tool": None, None)
                                for s in getattr(res, "state", type("X", (), {"plan": []})).plan:
                                    if getattr(s, "status", "") == "done" and (getattr(s, "result", "") or "").strip():
                                        return type("R", (), {"text": s.result.strip()})
                                return type("R", (), {"text": ""})
                            u = understand_video(v, caller=_caller)
                            hdr = (f"📺 {v.title}  ·  {v.author}  ·  {v.length_s//60}m{v.length_s%60:02d}s\n"
                                   f"   transcript: {v.lang or '?'} · {'auto-captions' if v.kind=='asr' else 'human captions'} · {v.word_count} words")
                            parts = [hdr, "", u.explanation]
                            if v.kind == "asr":
                                parts.append("\n⚠ auto-generated captions — names/technical terms may be mistranscribed.")
                            if u.watch_advice:
                                parts.append("\n⚠ " + u.watch_advice)
                            parts.append("\nℹ this is the SPOKEN transcript only — on-screen text/code/slides/visuals aren't captured.")
                            _post(lambda p=parts: chat_w and chat_w.add("assistant", "\n".join(p)))
                        except Exception as e:  # noqa: BLE001
                            _post(lambda e=e: chat_w and chat_w.add("system", f"watch failed: {e}"))
                    _run_bg("watch video", _watch_work)
                    return
                # F36: bare /watch records a path of interest for THIS session (a hint you can
                # reference). NOT a live filesystem watcher.
                state["watch_path"] = arg or os.getcwd()
                if chat_w: chat_w.add("system", f"noted path of interest: {state['watch_path']}\n  (a session marker — not live file monitoring)")
                toasts.show("path noted", "info")
                return
            if action == "unwatch":
                state.pop("watch_path", None)
                if chat_w: chat_w.add("system", "cleared the noted path")
                return

            # /git — git operations in chat (run off the main loop; push can be slow)
            if action == "git":
                import shutil
                if not shutil.which("git"):
                    if chat_w: chat_w.add("system", "git not installed")
                    return
                sub = (arg.split(None, 1) or [""]) if arg.strip() else [""]
                cmd = sub[0].lower()
                rest = sub[1] if len(sub) > 1 else ""
                def _git(cmd=cmd, rest=rest):
                    import subprocess
                    if cmd == "commit":
                        m = rest or "syntra: auto-commit"
                        r = subprocess.run(["git", "commit", "-m", m], capture_output=True, text=True, timeout=10)
                        msg = r.stdout.strip() or r.stderr.strip() or "committed"
                    elif cmd == "push":
                        r = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=30)
                        msg = r.stdout.strip() or r.stderr.strip() or "pushed"
                    elif cmd == "log":
                        r = subprocess.run(["git", "log", "--oneline", "-8", "--no-color"],
                                           capture_output=True, text=True, timeout=5)
                        msg = f"recent commits:\n{r.stdout.strip()}"
                    elif cmd == "stash":
                        r = subprocess.run(["git", "stash"] + (rest.split() if rest else []),
                                           capture_output=True, text=True, timeout=10)
                        msg = r.stdout.strip() or r.stderr.strip() or "stashed"
                    else:
                        msg = "/git commit [msg] | push | log | stash"
                    _post(lambda: chat_w and chat_w.add("system", msg))
                _run_bg("git", _git)
                return

            # /history — session browser
            if action == "history":
                from pathlib import Path as _P
                from ..core.state import TaskStore
                tasks_dir = _state_root() / "tasks"
                if tasks_dir.exists():
                    ts = TaskStore(_state_root())
                    lines = []
                    for td in sorted(tasks_dir.iterdir(), reverse=True)[:30]:
                        try:
                            st = ts.load(td.name)
                            c = st.total_cost_usd()
                            goal = (st.goal or "")[:35]
                            status = st.status or "?"
                            lines.append(f"{td.name[:10]}  {status:6s}  ${c:.4f}  {goal}")
                        except Exception:
                            pass
                    lines.append("")
                    lines.append("/resume <id> to continue a session")
                    _info_popup("session history", "\n".join(lines) or "no sessions")
                else:
                    _info_popup("session history", "no session history")
                return

            # /btw — quick side question (direct call, doesn't affect main task)
            if action == "btw":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /btw <question>")
                    return
                if chat_w:
                    chat_w.add("user", f"btw: {arg}")
                goal = f"Quick question (no context needed, answer briefly): {arg}"
                if not active and not controller.running():
                    active = True
                    _run_start = _time.time()
                    _run_tokens = 0
                    _streamed["buf"] = ""   # #129: fresh stream buffer per run (dedupe replay)
                    controller.start(goal)
                else:
                    _submit(f"/bg {goal}")
                return

            # /context — context-window occupancy dashboard (#220): per-source token
            # attribution (conversation / files / memory / system) + a proportion bar +
            # ranked reduction advice (biggest droppable source + duplicate file reads).
            # A rich styled overlay, upgraded from the old flat token bar.
            if action == "context":
                from ..core.tui_model import context_breakdown, render_context_dashboard
                from ..core.pricing import estimate_tokens as _est
                # window: prefer the live model's real context, else a safe default.
                _win = 0
                try:
                    _win = int(getattr(getattr(controller, "result", None), "context_window", 0) or 0)
                except Exception:  # noqa: BLE001
                    _win = 0
                if _win <= 0:
                    _win = 128000
                # attribute tokens to sources from the REAL transcript + memory + system.
                _msgs = chat_w.transcript.messages if chat_w else []
                _conv_tok = sum(_est(getattr(m, "text", "") or "") for m in _msgs
                                if getattr(m, "role", "").startswith(("user", "assistant")))
                _tool_tok = sum(_est(getattr(m, "text", "") or "") for m in _msgs
                                if getattr(m, "role", "") in ("tool", "system"))
                _mem_tok = sum(_est(str(x)) for x in (getattr(chat_w, "memory_items", []) or [])) if chat_w else 0
                # files read this session (for duplicate detection) — from the action feed trail
                _freads = list(state.get("_file_reads", []) or [])
                _sources = [("conversation", _conv_tok), ("tool output", _tool_tok),
                            ("memory", _mem_tok)]
                _bd = context_breakdown(_sources, window=_win,
                                        droppable={"conversation", "tool output"},
                                        file_reads=_freads)
                # If the engine reported real usage that exceeds our text estimate, trust it.
                _live_used = tokens_in + tokens_out
                if _live_used > _bd["used"]:
                    _bd["used"] = _live_used
                    _bd["pct"] = min(100, int(round(100.0 * _live_used / _win)))
                try:
                    _sr, _sc = stdscr.getmaxyx()
                except Exception:  # noqa: BLE001
                    _sc = 100
                _rows = render_context_dashboard(_bd, width=min(80, max(40, _sc - 8)))
                from ..core.overlay import make_styled_overlay
                overlay["current"] = make_styled_overlay("context", _rows)
                return

            # /debug — toggle verbose telemetry
            if action == "debug":
                state["debug"] = not state.get("debug", False)
                if chat_w:
                    chat_w.add("system", f"debug: {'ON — verbose telemetry enabled' if state['debug'] else 'OFF'}")
                toasts.show(f"debug {'on' if state['debug'] else 'off'}", "info")
                return

            # /commands — toggle whether EVERY shell command Syntra runs is announced (Gap 2).
            # Off (default): only commands that ran WITHOUT an OS sandbox are flagged. On: each
            # command is shown as it runs, so you always know what's executing.
            if action == "commands":
                _get = getattr(controller._run_goal, "get_toggle", None)
                _st = getattr(controller._run_goal, "set_toggle", None)
                _new = not (_get("verbose_commands", False) if _get else False)
                if _st:
                    _st("verbose_commands", _new)
                if chat_w:
                    chat_w.add("system",
                               "command info ON — I'll show each shell command as I run it"
                               if _new else
                               "command info OFF — I only flag commands that ran without a sandbox")
                toasts.show(f"command info {'on' if _new else 'off'}", "info")
                return

            # /usage — cost breakdown
            if action == "usage":
                if last_task_id:
                    from pathlib import Path as _P
                    from ..core.state import TaskStore
                    try:
                        ts = TaskStore(_state_root())
                        st = ts.load(last_task_id)
                        role_costs: dict[str, float] = {}
                        role_tokens: dict[str, int] = {}
                        for c in st.costs:
                            role_costs[c.role] = role_costs.get(c.role, 0.0) + c.cost_usd
                            role_tokens[c.role] = role_tokens.get(c.role, 0) + c.input_tokens + c.output_tokens
                        lines = []
                        for role in ("planner", "executor", "reviewer"):
                            c = role_costs.get(role, 0)
                            t = role_tokens.get(role, 0)
                            if c > 0 or t > 0:
                                lines.append(f"{role:10s}  ${c:.4f}  {t} tok")
                        lines.append(f"{'total':10s}  ${cost:.4f}  {tokens_in + tokens_out} tok")
                        _info_popup("usage breakdown", "\n".join(lines))
                    except Exception:
                        _info_popup("usage", f"session: ${cost:.4f}  {tokens_in + tokens_out} tokens")
                else:
                    _info_popup("usage", "no session to show usage for")
                return

            # /keymap — show the active, customizable keybindings (A1.5). Loads the
            # user's ~/.config/syntra/keymap.json merged over the defaults; the loader
            # auto-reverts any colliding action, so the shown map is always valid.
            if action == "keymap":
                from ..core.keymap import (Keymap, load_command_binds, save_command_binds,
                                           command_bind_conflicts, command_bind_keycode_map)
                from pathlib import Path as _P
                _parts = (arg or "").split(None, 2)
                # #173: /keymap bind <key> <command>  — bind a /command to a free single key.
                if _parts and _parts[0].lower() == "bind":
                    if len(_parts) < 3 or not _parts[2].strip().startswith("/"):
                        if chat_w: chat_w.add("system", "usage: /keymap bind <key> </command>   e.g. /keymap bind ctrl+g /stats")
                        return
                    _tok, _cmd = _parts[1].strip().lower(), _parts[2].strip()
                    _existing = load_command_binds(_cmd_binds_path)
                    _trial = dict(_existing); _trial[_tok] = _cmd
                    if command_bind_conflicts({_tok: _cmd}):
                        if chat_w: chat_w.add("system", f"'{_tok}' is a reserved key (a built-in shortcut) — pick another")
                        return
                    if not command_bind_keycode_map({_tok: _cmd}):
                        if chat_w: chat_w.add("system", f"'{_tok}' isn't a bindable single key (use e.g. ctrl+g, or a letter)")
                        return
                    save_command_binds(_cmd_binds_path, _trial)
                    _cmd_binds = command_bind_keycode_map(load_command_binds(_cmd_binds_path))  # live
                    if chat_w: chat_w.add("system", f"bound {_tok} → {_cmd}  (works on an empty prompt)")
                    toasts.show("key bound", "success", ttl=1.4)
                    return
                # /keymap unbind <key>
                if _parts and _parts[0].lower() == "unbind":
                    if len(_parts) < 2:
                        if chat_w: chat_w.add("system", "usage: /keymap unbind <key>")
                        return
                    _tok = _parts[1].strip().lower()
                    _existing = load_command_binds(_cmd_binds_path)
                    if _tok in _existing:
                        _existing.pop(_tok)
                        save_command_binds(_cmd_binds_path, _existing)
                        _cmd_binds = command_bind_keycode_map(load_command_binds(_cmd_binds_path))
                        if chat_w: chat_w.add("system", f"unbound {_tok}")
                        toasts.show("key unbound", "info", ttl=1.2)
                    else:
                        if chat_w: chat_w.add("system", f"no command bound to {_tok}")
                    return
                # bare /keymap → show action bindings + the command-binds
                _kpath = _P(os.path.expanduser("~/.config/syntra/keymap.json"))
                _custom = _kpath.exists()
                km = Keymap.load(_kpath if _custom else None)
                head = (f"Keybindings — customized via {_kpath}" if _custom
                        else f"Keybindings (defaults). Create {_kpath} to customize.")
                lines = [head, ""]
                for _action, _keys in km.bindings.items():
                    lines.append(f"  {_action:16} {'  ·  '.join(_keys)}")
                # #173: the user's command-key binds
                _cb = load_command_binds(_cmd_binds_path)
                lines.append("")
                if _cb:
                    lines.append("command binds (fire on an empty prompt):")
                    for _tok, _cmd in _cb.items():
                        lines.append(f"  {_tok:16} {_cmd}")
                else:
                    lines.append("no command binds — add one:  /keymap bind ctrl+g /stats")
                lines.append("")
                _live = len(_key_remap)
                lines.append(
                    (f"{_live} single-key rebinding(s) LIVE this session. " if _live
                     else "Default bindings (no custom keymap.json). ")
                    + "Collisions auto-revert to defaults; restart to reload edits. ✓")
                _info_popup("keymap", "\n".join(lines))
                return

            # /plugins — list installed plugins (agents/skills/commands) + enabled state (A4)
            if action == "plugins":
                from ..core import installer as _inst
                rows = _inst.list_installed()
                if not rows:
                    _info_popup("plugins",
                                "no plugins installed.\n\nInstall one from the CLI:\n"
                                "  syntra install <folder | file.md | git-url>")
                    return
                _info_popup("plugins", "\n".join(_plugin_lines(rows)))
                return

            # /skills — list available skills (or `/skills <query>` to rank by relevance)
            if action == "skills":
                from ..core.plugin_loader import bundled_skills, discover_plugins, match_skills
                _q = (arg or "").strip()
                if _q:   # implicit description matching, surfaced in the UI (A4)
                    _all = bundled_skills() + [s for p in discover_plugins() for s in p.skills]
                    hits = match_skills(_q, _all)
                    if hits:
                        ml = [f"skills matching “{_q}” (best first):", ""]
                        for s in hits:
                            ml.append(f"  ● {s.name}")
                            if getattr(s, "description", ""):
                                ml.append(f"      {s.description}")
                    else:
                        ml = [f"no skill matches “{_q}”.", "",
                              "Run /skills with no argument to see them all."]
                    _info_popup("skills", "\n".join(ml))
                    return
                # bare /skills -> a searchable, navigable PICKER (A4: upgrades the old
                # read-only popup). Type to filter; Enter shows the skill's detail.
                from ..core.overlay import make_skills_overlay
                overlay["current"] = make_skills_overlay()
                return

            # /agents — searchable picker of installed agents (A4), parallel to /skills.
            # (Was a dead command in the TUI — showed "unknown".)
            if action == "agents":
                from ..core.overlay import make_agents_overlay
                overlay["current"] = make_agents_overlay()
                return

            # /skill-create and /agent-create
            if action in ("skill-create", "agent-create"):
                if not arg:
                    if chat_w: chat_w.add("system", f"usage: /{action} <name>")
                    return
                from pathlib import Path as _P
                base = _P(os.path.expanduser("~/.config/syntra/plugins/user-skills"))
                if action == "skill-create":
                    d = base / "skills" / arg
                    d.mkdir(parents=True, exist_ok=True)
                    f = d / "SKILL.md"
                    if not f.exists():
                        f.write_text(f"---\nname: {arg}\ndescription: when this triggers\nmodel: inherit\n---\n\nYou are doing '{arg}'. Method here.\n")
                    if chat_w: chat_w.add("system", f"created skill: {f}\n  edit it, then /skills to verify")
                else:
                    # #228: build the agent .md via the validated serializer (safe frontmatter,
                    # can't be broken by a name with special chars) + a filesystem-safe slug.
                    from ..core.tui_model import (build_agent_markdown, agent_slug,
                                                  agent_synthesis_prompt, parse_agent_spec)
                    d = base / "agents"
                    d.mkdir(parents=True, exist_ok=True)
                    # #228: `/agent-create <name> | <one-sentence>` → let the MODEL synthesize
                    # the agent (name/description/tools/prompt) from the sentence, off the main
                    # loop. Bare `/agent-create <name>` keeps today's instant scaffold.
                    _name_part, _sep, _desc_part = arg.partition("|")
                    _slug = agent_slug(_name_part)
                    f = d / f"{_slug}.md"
                    if f.exists():
                        if chat_w: chat_w.add("system", f"agent '{_slug}' already exists: {f}\n  edit it, then /plugins to verify")
                        toasts.show("agent exists", "info")
                        return

                    def _scaffold():
                        """Deterministic fallback — never leave the user empty-handed."""
                        f.write_text(build_agent_markdown(
                            name=_slug, description="use when…",
                            tools=[], system_prompt=f"You are the '{_slug}' agent. Role here."))

                    _desc = _desc_part.strip()
                    if not _sep or not _desc:
                        # no description → instant deterministic scaffold (today's behavior)
                        _scaffold()
                        if chat_w: chat_w.add("system", f"created agent: {f}\n  edit it, then /plugins to verify")
                        toasts.show("agent created", "success")
                        return

                    # description present → one-shot model synthesis on a bg thread
                    def _synth_work(desc=_desc, slug=_slug, path=f):
                        spec = None
                        try:
                            res = controller._run_goal(
                                agent_synthesis_prompt(desc), lambda _t, _r="tool": None, None)
                            _text = ""
                            for _s in getattr(res, "state", type("X", (), {"plan": []})).plan:
                                if getattr(_s, "status", "") == "done" and (getattr(_s, "result", "") or "").strip():
                                    _text = _s.result.strip()
                            spec = parse_agent_spec(_text)
                        except Exception:  # noqa: BLE001 - offline/no-provider/parse → fall back
                            spec = None
                        if spec:
                            _md = build_agent_markdown(
                                name=slug, description=spec["description"],
                                tools=spec["tools"], system_prompt=spec["system_prompt"])
                            path.write_text(_md)
                            _post(lambda p=path: chat_w and chat_w.add(
                                "system", f"✓ synthesized agent: {p}\n  edit it, then /plugins to verify"))
                            _post(lambda: toasts.show("agent synthesized", "success"))
                        else:
                            _scaffold()
                            _post(lambda p=path: chat_w and chat_w.add(
                                "system", f"couldn't synthesize (offline or no clear answer) — wrote a blank scaffold: {p}\n  edit it, then /plugins to verify"))
                            _post(lambda: toasts.show("scaffold written", "info"))

                    _run_bg(f"synthesize {_slug}", _synth_work)
                    return
                # only the skill-create branch falls through here (agent branch always returns)
                toasts.show("skill created", "success")
                return

            # /plugins — list installed plugins
            if action == "plugins":
                from ..core.plugin_loader import discover_plugins, plugin_summary
                plugins = discover_plugins()
                _info_popup("plugins", plugin_summary(plugins))
                return

            # /loop — recurring task (/loop stop cancels)
            if action == "loop":
                import threading as _thr
                if arg.strip() == "stop":
                    ev = state.get("_loop_stop")
                    if ev:
                        ev.set()
                        state.pop("_loop_stop", None)
                        if chat_w: chat_w.add("system", "loop stopped")
                    else:
                        if chat_w: chat_w.add("system", "no loop running")
                    return
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /loop <interval> <command>  ·  /loop stop\n  e.g. /loop 5m /status")
                    return
                # Only one loop at a time — stop any existing one first.
                old = state.get("_loop_stop")
                if old:
                    old.set()
                parts = arg.split(None, 1)
                interval_str = parts[0] if parts else "60"
                cmd = parts[1] if len(parts) > 1 else ""
                if not cmd:
                    if chat_w: chat_w.add("system", "usage: /loop <interval> <command>")
                    return
                try:
                    if interval_str.endswith("m"):
                        interval = int(interval_str[:-1]) * 60
                    elif interval_str.endswith("h"):
                        interval = int(interval_str[:-1]) * 3600
                    elif interval_str.endswith("s"):
                        interval = int(interval_str[:-1])
                    else:
                        interval = int(interval_str)
                except ValueError:
                    interval = 60
                interval = max(5, interval)  # floor to avoid runaway tight loops
                stop_evt = _thr.Event()
                state["_loop_stop"] = stop_evt
                def _loop_worker(cmd_text, secs, evt):
                    # Event-based wait so it cancels promptly; exits cleanly on stop.
                    # F4: _submit mutates chat/overlay/controller + draws — curses isn't
                    # thread-safe, so marshal it onto the main loop via _post() rather than
                    # calling it from this worker thread.
                    while not evt.wait(secs):
                        try:
                            _post(lambda t=cmd_text: _submit(t))
                        except Exception:
                            break
                t = _thr.Thread(target=_loop_worker, args=(cmd, interval, stop_evt), daemon=True)
                t.start()
                if chat_w:
                    chat_w.add("system", f"loop started: '{cmd}' every {interval}s  ·  /loop stop to cancel")
                toasts.show(f"loop: {cmd} every {interval}s", "info")
                return

            # /batch — parallel worktree execution
            if action == "batch":
                if not arg:
                    if chat_w: chat_w.add("system", "usage: /batch <goal>\n  runs the goal in a git worktree")
                    return
                import subprocess, shutil
                if not shutil.which("git"):
                    if chat_w: chat_w.add("system", "git not available")
                    return
                if chat_w:
                    chat_w.add("system", f"batch: creating worktree for '{arg[:40]}'...")
                # Create a worktree, run the task there
                wt_name = f"syntra-batch-{int(_time.time()) % 10000}"
                try:
                    subprocess.run(["git", "worktree", "add", f"/tmp/{wt_name}", "-b", wt_name],
                                   capture_output=True, timeout=10)
                    if chat_w:
                        chat_w.add("system", f"worktree: /tmp/{wt_name}\n  running: {arg[:50]}")
                    _submit(f"/bg {arg}")
                except Exception as e:
                    if chat_w:
                        chat_w.add("system", f"batch failed: {e}")
                return

            # /export — save transcript as markdown
            if action == "export":
                if chat_w:
                    import time as _t
                    fname = arg.strip() or f"syntra-export-{int(_t.time())}.md"
                    lines_out = ["# Syntra Conversation Export\n"]
                    lines_out.append(f"Session: {last_task_id or 'none'}")
                    lines_out.append(f"Cost: ${cost:.4f}")
                    lines_out.append(f"Tokens: {tokens_in + tokens_out}\n")
                    lines_out.append("---\n")
                    for m in chat_w.transcript.messages:
                        role = getattr(m, "role", "?")
                        text = getattr(m, "text", "")
                        if role == "user":
                            lines_out.append(f"**You:** {text}\n")
                        elif role.startswith("assistant"):
                            lines_out.append(f"**Syntra:** {text}\n")
                        elif role == "system":
                            lines_out.append(f"*{text}*\n")
                        elif role == "tool":
                            lines_out.append(f"```\n{text}\n```\n")
                    try:
                        with open(fname, "w") as f:
                            f.write("\n".join(lines_out))
                        chat_w.add("system", f"exported to {fname}")
                        toasts.show(f"exported {fname}", "success")
                    except OSError as e:
                        chat_w.add("system", f"export failed: {e}")
                return

            # /rewind — rollback conversation
            if action == "rewind":
                if chat_w:
                    try:
                        n = int(arg) if arg else 1
                    except ValueError:
                        n = 1
                    msgs = chat_w.transcript.messages
                    if n <= 0:
                        chat_w.add("system", "rewound 0 messages (nothing to do)")
                        return
                    if n >= len(msgs):
                        msgs.clear()
                        chat_w.add("system", "rewound to start")
                    else:
                        del msgs[-n:]
                        chat_w.add("system", f"rewound {n} messages")
                    # deleting messages invalidates index-keyed fold state (F26) — reset it so a
                    # new message reusing an old index doesn't inherit stale collapse/expand.
                    chat_w.transcript.collapsed.clear()
                    chat_w.transcript.expanded.clear()
                    chat_w.transcript._follow = True
                    toasts.show(f"rewound {n}", "info")
                return

            # /sessions — list past sessions
            if action == "sessions":
                # Interactive resume picker (A2): pick a recent session and Enter
                # resumes it — replaces the old read-only list.
                from ..core.overlay import make_session_overlay
                from ..core.state import TaskStore
                try:
                    _sess = TaskStore(_state_root()).recent_sessions(30)
                except Exception:  # noqa: BLE001 - the picker still opens (empty) on failure
                    _sess = []
                if _sess:
                    overlay["current"] = make_session_overlay(_sess, intent="resume")
                else:
                    _info_popup("sessions", "no saved sessions yet")
                return

            # /msgs — message navigator: pick one of your past messages → scroll the chat to it.
            if action == "msgs":
                from ..core.overlay import make_message_overlay
                _idx = chat_w.user_msg_rail_index() if chat_w else []
                overlay["current"] = make_message_overlay(_idx)
                return

            # /status — rich session status
            if action == "status":
                lines = []
                lines.append(f"session: {last_task_id[:12] if last_task_id else '(none)'}")
                if session_title:
                    lines.append(f"title:   {session_title}")
                lines.append(f"tokens:  {tokens_in + tokens_out} (in: {tokens_in}, out: {tokens_out})")
                lines.append(f"cost:    ${cost:.4f}")
                if speed_tps > 0:
                    lines.append(f"speed:   {speed_tps:.0f} tok/s")
                effort_val = os.environ.get("SYNTRA_REASONING_EFFORT", "(auto)")
                lines.append(f"effort:  {effort_val}")
                lines.append(f"state:   {_state_root()}/")
                _info_popup("status", "\n".join(lines))
                return

            # /stats — cross-session usage dashboard (#218): activity heatmap + streaks +
            # per-day cost sparkline, from the spend ledger. A rich, styled popup (color
            # hierarchy), NOT a flat text dump. `/stats <days>` sets the window (default 30).
            if action == "stats":
                import time as _t
                from ..core.tui_model import usage_stats, render_stats_dashboard
                from ..core.spend import Ledger, _LEDGER_REL
                from .main import _state_root as _sr_stats
                try:
                    _days = int(arg.strip()) if arg.strip() else 30
                except ValueError:
                    _days = 30
                _days = max(7, min(365, _days))
                try:
                    _led = Ledger(_sr_stats().joinpath(*_LEDGER_REL))
                    _entries = _led._read()
                except Exception:  # noqa: BLE001 - a missing/corrupt ledger → empty state
                    _entries = []
                _stats = usage_stats(_entries, now=_t.time(), days=_days)
                try:
                    _sr, _sc = stdscr.getmaxyx()
                except Exception:  # noqa: BLE001
                    _sc = 100
                _rows = render_stats_dashboard(_stats, width=min(88, max(40, _sc - 8)))
                from ..core.overlay import make_styled_overlay
                overlay["current"] = make_styled_overlay(f"stats · {_days}d", _rows)
                return

            # /title — give the current session a human-readable name (shown in the
            # resume/fork pickers + the header). Empty arg clears it (falls back to goal).
            if action == "title":
                from ..core.state import TaskStore
                name = (arg or "").strip()
                # The user explicitly chose this → LOCK it so auto-derivation never overwrites it
                # (clearing the title with a bare /title unlocks, back to auto).
                _title_locked["user"] = bool(name)
                if not name:
                    _title_locked["auto"] = False   # cleared → let the analyzer re-title once
                # Show the name immediately (header) regardless of whether a task exists.
                session_title = name
                # H1: drive the OS terminal tab/window title from the name (was always
                # "Syntra"). _set_title composes "<name> — <status>" on every frame after.
                _title_state["name"] = name
                _set_title(f"Syntra {BRAND_MARK}".strip() if not name else "ready")
                if not last_task_id:
                    # No run has minted a task yet. Do NOT mint an orphan here — loop.run()
                    # always creates a fresh task, which would strand this title on a dead
                    # one. Stash it; it's applied to the REAL task_id when a run starts.
                    pending_title = name
                    if chat_w:
                        chat_w.add("system",
                                   f"session will be titled: {name}  (applied when you send a message)"
                                   if name else "session title cleared")
                    if name:
                        toasts.show(f"✎ {name}", "success", ttl=1.8)
                    return
                try:
                    ok = TaskStore(_state_root()).set_title(last_task_id, name)
                    if name:
                        session_title = name if ok else session_title
                        if chat_w:
                            chat_w.add("system", f"session titled: {name}" if ok else "could not set title")
                        toasts.show(f"✎ {name}", "success", ttl=1.8)
                    else:
                        session_title = ""
                        pending_title = ""
                        _title_state["name"] = ""    # clear tab title back to default (H1)
                        if chat_w:
                            chat_w.add("system", "session title cleared (using the goal)")
                except Exception as e:  # noqa: BLE001
                    if chat_w:
                        chat_w.add("system", f"title error: {e}")
                return

            # /resume — continue a plan-pending task
            if action == "resume":
                tid = arg.strip() or last_task_id
                if tid and not active:
                    # resuming a DIFFERENT session: clear the current chat + replay the
                    # resumed session's saved conversation, so the on-screen history AND
                    # the rollout we persist back belong ONLY to that session — never a
                    # mix of the two (which would corrupt the resumed session's rollout).
                    if chat_w and tid != last_task_id:
                        chat_w.transcript.messages.clear()
                        chat_w.transcript.scroll = 0
                        chat_w.transcript._follow = True
                        last_task_id = tid          # retarget persistence before the run
                        try:
                            from ..core.state import TaskStore
                            _roll = TaskStore(_state_root()).load_rollout(tid)
                        except Exception:  # noqa: BLE001
                            _roll = []
                        for _r in _roll:
                            _role, _txt = _r.get("role", ""), (_r.get("text", "") or "").strip()
                            if _role in ("user", "assistant") and _txt:
                                chat_w.add(_role, _txt)
                    if chat_w:
                        chat_w.add("system", f"resuming {tid[:12]}...")
                    active = True
                    _run_start = _time.time()
                    _run_tokens = 0
                    _streamed["buf"] = ""   # #129: fresh stream buffer per run (dedupe replay)
                    controller.start(f"/resume {tid}")
                    if activity_w:
                        activity_w.log(f"resuming {tid[:12]}", "running")
                else:
                    if chat_w:
                        chat_w.add("system", "nothing to resume" if not tid else
                                   "a task is running — press Esc Esc to stop it first, then /resume")
                return

            # /effort — set reasoning effort. With an explicit level, apply it; with
            # no/invalid arg, open the Faster↔Smarter slider (P19). auto = adaptive;
            # ultracode = max effort + multi-agent workflows (aliases max effort).
            if action == "effort":
                from ..core.tui_model import EFFORT_SLIDER_LEVELS as _ESL
                a = (arg or "").strip().lower()
                if a in _ESL:
                    _apply_effort(a)
                else:
                    cur = os.environ.get("SYNTRA_REASONING_EFFORT", "") or "auto"
                    if cur == "max" and os.environ.get("SYNTRA_ULTRACODE"):
                        cur = "ultracode"
                    # #13: open the slider on the levels THIS model supports, not all 7.
                    _levels = _effort_levels()
                    effort_modal["levels"] = _levels
                    effort_modal["idx"] = _levels.index(cur) if cur in _levels else 0
                    # enforce single-modal at open-time (mirror of _open_wizard)
                    wizard_modal["current"] = None
                    wizard_modal["paused"] = None
                    effort_modal["active"] = True
                return

            # /auto on|off|N — toggle auto-approve + autopilot for the next run
            if action == "auto":
                _set = getattr(controller._run_goal, "set_toggle", None)
                _get = getattr(controller._run_goal, "get_toggle", None)
                a = (arg or "").strip().lower()
                if not _set:
                    if chat_w:
                        chat_w.add("system", "auto-mode unavailable in this runner")
                    return
                if a in ("on", "yes", "true"):
                    _set("auto_approve", True)
                    toasts.show("🔓 auto-approve ON — tools run without asking", "info")
                elif a in ("off", "no", "false"):
                    _set("auto_approve", False); _set("autopilot", 1)
                    toasts.show("🔒 ask — auto-approve OFF", "success")
                elif a.isdigit():
                    n = max(1, int(a))
                    _set("autopilot", n); _set("auto_approve", True)
                    toasts.show(f"↻ autopilot ×{n} (auto-approve on)", "info")
                else:
                    ap = _get("auto_approve", False) if _get else False
                    n = _get("autopilot", 1) if _get else 1
                    if chat_w:
                        chat_w.add("system",
                            f"auto: {'on' if ap else 'off'} · autopilot ×{n}\n"
                            "  /auto on   approve tools automatically\n"
                            "  /auto off  ask before each risky tool (default)\n"
                            "  /auto N    keep working up to N passes until done")
                return

            # /permissions — show/set the permission posture
            if action == "permissions":
                _set = getattr(controller._run_goal, "set_toggle", None)
                _get = getattr(controller._run_goal, "get_toggle", None)
                a = (arg or "").strip().lower()
                from ..core.access_modes import MODES, _PERM_KEYS, _SETTINGS, load_access_state
                _parts = a.split()
                # /permissions <perm> <auto|ask|off> → tune one permission. Checked FIRST because
                # some names ("edit"/"auto"/"ask") are both a mode AND a permission/setting — a
                # 2-token form with a valid setting is unambiguously a per-permission set.
                if len(_parts) == 2 and _parts[0] in _PERM_KEYS and _parts[1] in _SETTINGS:
                    _st = load_access_state(_access_path()); _st.set_perm(_parts[0], _parts[1]); _apply_access(_st)
                    return
                # /permissions plan|ask|edit|auto → switch the run mode (persist immediately).
                # ("ask"/"auto" now mean the MODE, which also sets the approval posture — no
                # separate auto_approve toggle needed; the mode is the single source of truth.)
                if len(_parts) == 1 and _parts[0] in MODES:
                    _st = load_access_state(_access_path()); _st.set_mode(_parts[0]); _apply_access(_st)
                    return
                # /permissions rules|board → the #223 rule board: a read-only view of the
                # accumulated grants (durable / this-session / off) with plain-English meaning
                # + a shadowed-rule lint. Reads Part 1's PermissionStore accessors live.
                if _parts and _parts[0] in ("rules", "board", "grants"):
                    _pstore = getattr(controller._run_goal, "permission_store", None)
                    if _pstore is None:
                        _lp = getattr(controller._run_goal, "_loop", None)
                        _pstore = getattr(_lp, "permission_store", None) if _lp else None
                    _always = _session = set()
                    _off = set()
                    if _pstore is not None:
                        try:
                            _always = set(_pstore.always_allowed())
                            _session = set(_pstore.granted_for_session())
                        except Exception:  # noqa: BLE001
                            pass
                    # OFF tools: read the per-tool access settings (those set to "off").
                    try:
                        from ..core.access_modes import _PERM_KEYS, load_access_state
                        _ast = load_access_state(_access_path())
                        _off = {k for k in _PERM_KEYS if _ast.effective(k) == "off"}
                    except Exception:  # noqa: BLE001
                        _off = set()
                    from ..core.tui_model import render_rule_board
                    try:
                        _sr, _sc = stdscr.getmaxyx()
                    except Exception:  # noqa: BLE001
                        _sc = 100
                    _rows = render_rule_board(always=_always, session=_session, off=_off,
                                              width=min(84, max(40, _sc - 8)))
                    from ..core.overlay import make_styled_overlay
                    overlay["current"] = make_styled_overlay("permission rules", _rows)
                    return
                # bare /permissions (or /access) → open the proper popup box.
                if not a:
                    _open_access()
                    return
                if chat_w:
                    # DERIVE the posture from LIVE state (not a hardcoded blurb that can drift
                    # into a false claim): the auto-approve toggle + the actual OS-sandbox probe.
                    ap = _get("auto_approve", False) if _get else False
                    try:
                        from ..core.sandbox import bwrap_available, seatbelt_available
                        _sbx = ("bubblewrap" if bwrap_available()
                                else "Seatbelt" if seatbelt_available() else "")
                    except Exception:  # noqa: BLE001
                        _sbx = ""
                    # live per-tool grants if the engine exposes them (graceful if absent).
                    # Gap 5b: read the store LIVE through the loop (a snapshot would be None
                    # since the store is built when a run starts). Fall back to a direct attr.
                    _sess = _always = ()
                    _pstore = getattr(controller._run_goal, "permission_store", None)
                    if _pstore is None:
                        _lp = getattr(controller._run_goal, "_loop", None)
                        _pstore = getattr(_lp, "permission_store", None) if _lp else None
                    if _pstore is not None:
                        try:
                            _sess = _pstore.granted_for_session()
                            _always = _pstore.always_allowed()
                        except Exception:  # noqa: BLE001
                            pass
                    from ..core.tui_model import permission_status_lines
                    chat_w.add("system", "permissions:")
                    for _t, _st in permission_status_lines(
                            auto_approve=ap, sandbox=_sbx,
                            session_grants=_sess, always_grants=_always):
                        chat_w.add("system", "  " + _t)
                return

            # goal / agent
            if action in ("goal", "proof-goal", "agent"):
                run_text = arg if action != "agent" else (arg or text)
                # @ file mentions: extract @path references and inject file content
                import re as _re
                at_refs = _re.findall(r'@(\S+)', text)
                if at_refs:
                    file_context = []
                    for ref in at_refs:
                        fpath = os.path.join(os.getcwd(), ref)
                        try:
                            with open(fpath) as f:
                                content = f.read()[:8000]
                            file_context.append(f"[{ref}]\n```\n{content}\n```")
                        except (FileNotFoundError, OSError, PermissionError):
                            pass
                    if file_context:
                        run_text = run_text + "\n\nFILE CONTEXT:\n" + "\n\n".join(file_context)
                # If a run is already in flight, QUEUE this message instead of
                # starting a second run concurrently (that interleaves replies).
                # Queued messages are grouped — shown as a single "queued" notice
                # with all messages listed, not as separate boxes.
                if active or controller.running():
                    # A leading '!' = INSTANT steer: inject into the current step now.
                    # Everything else is HELD in the queue — press Esc to send all
                    # queued messages into the run, else they run as follow-ups when
                    # it ends. (Type while it works, Esc to send.)
                    if text.startswith("!"):
                        instant = text[1:].strip()
                        if instant and controller.steer_now(instant):
                            if chat_w:
                                msgs = chat_w.transcript.messages
                                for m in reversed(msgs):
                                    if getattr(m, "role", "") == "user":
                                        m.text += f"\n↪ {instant}"
                                        break
                                else:
                                    chat_w.add("user", f"↪ {instant}")
                            return
                    # Hold in the queue — merge into the user box as a grouped notice.
                    _pending.append((text, run_text))
                    if chat_w:
                        msgs = chat_w.transcript.messages
                        if msgs and getattr(msgs[-1], "role", "") == "system" and "queued" in getattr(msgs[-1], "text", ""):
                            msgs.pop()
                        appended = False
                        for m in reversed(msgs):
                            if getattr(m, "role", "") == "user":
                                m.text += f"\n→ {text} (queued · Esc to send)"
                                appended = True
                                break
                        if not appended:
                            chat_w.add("user", f"→ {text} (queued · Esc to send)")
                    return
                if chat_w:
                    # Show attached files in the user's bubble (they ride this message), then
                    # clear the 📎 chip — the engine reads + sends them this turn (images via
                    # initial_images, text/code injected as context; #127).
                    _na = len(_attached["items"])
                    _suffix = ""
                    if _na:
                        _names = ", ".join(i["label"] for i in _attached["items"])
                        _suffix = f"\n📎 {_na} file{'s' if _na != 1 else ''}: {_names}"
                    chat_w.add("user", text + _suffix)
                _attached["items"] = []   # chip clears; engine-side queue is consumed by the run
                active = True
                _run_start = _time.time()
                _run_tokens = 0
                if tree_w:
                    tree_w.reset()        # fresh working-tree per run (P25)
                if agent_w:
                    agent_w.reset()
                action_feed.clear()       # fresh rolling action feed per run
                plan_ribbon.clear()       # and a fresh plan-mode ribbon
                # Auto-derive the session/tab title when the user hasn't set one with /title.
                # At submit time we don't have the analysis yet, so use derive_title()'s SMART
                # distillation of the message (drops "can you please help me…" filler, keeps the
                # topic, Title-Cased) — NOT a raw first-N-chars trim. Once the run finishes, the
                # run-finished handler UPGRADES this to the analyzer's understood title if better.
                if not _title_state.get("name") and not session_title and not _title_locked["user"]:
                    try:
                        from ..core.task_analyzer import derive_title
                        _auto = derive_title(text or "")
                    except Exception:  # noqa: BLE001 - title is cosmetic, never break submit
                        _auto = " ".join((text or "").split())[:40].strip()
                    if _auto:
                        _title_state["name"] = _auto
                        pending_title = _auto
                _set_title("working")
                controller.start(run_text)
                if activity_w:
                    activity_w.log(f"started: {text[:40]}", "running")
                return

            # /clear
            if action == "clear":
                if chat_w:
                    chat_w.transcript.messages.clear()
                    chat_w.transcript.collapsed.clear()   # F26: drop stale index-keyed fold state
                    chat_w.transcript.expanded.clear()
                    chat_w.transcript.scroll = 0
                    chat_w.transcript._follow = True
                    chat_w.add("system", "cleared.")
                if agent_w:
                    agent_w.reset()
                if tree_w:
                    tree_w.reset()        # don't leave stale run state after a clear
                return

            # Empty Enter is NOT a user message — handle it BEFORE adding any bubble, so it
            # never drops an empty "❯ you" box into the chat (user: "why are we sending empty
            # msgs"). Its only job: approve a pending plan ("press Enter to start").
            if action == "empty":
                if last_task_id and not active and not controller.running():
                    if hasattr(controller, "result") and controller.result:
                        verdict = getattr(controller.result, "verdict", "")
                        if verdict == "plan_pending":
                            _submit("/resume")
                return
            # all other slash commands
            if chat_w:
                chat_w.add("user", text)
            with capture_output() as out_lines:
                handled = _handle_session_action(action, arg, last_task_id)
            body = "\n".join(out_lines).rstrip()
            if not handled:
                if chat_w:
                    chat_w.add("system", f"unknown: {text}")
            elif body:
                if chat_w:
                    chat_w.add("system", body)

        def _handle_layout(arg: str):
            parts = arg.split() if arg else []
            if not parts or parts[0] == "list":
                names = list(_LAYOUTS.keys())
                if chat_w:
                    chat_w.add("system", f"layouts: {', '.join(names)}\n  /layout <name> to switch\n  /layout save <name> to save current")
                return
            if parts[0] == "save" and len(parts) > 1:
                _save_layout(parts[1], state["layout"])
                if chat_w:
                    chat_w.add("system", f"saved layout: {parts[1]}")
                return
            if parts[0] == "load" and len(parts) > 1:
                loaded = _load_saved_layout(parts[1])
                if loaded:
                    state["layout"] = loaded
                    if chat_w:
                        chat_w.add("system", f"loaded layout: {parts[1]}")
                else:
                    if chat_w:
                        chat_w.add("system", f"no saved layout: {parts[1]}")
                return
            # switch to a preset
            name = parts[0].lower()
            factory = _LAYOUTS.get(name)
            if factory:
                state["layout"] = factory()
                if chat_w:
                    chat_w.add("system", f"layout: {name}")
            else:
                if chat_w:
                    chat_w.add("system", f"unknown layout: {name}. try: {', '.join(_LAYOUTS.keys())}")

        # ── selection / click helpers ──
        def _copy_selection():
            """Extract the highlighted region and OSC52-copy it. Uses the SAME span logic as the
            highlight (P1/P2) so copy == what you see. A CONTENT-anchored chat selection (#89)
            reads the FULL rendered line list — so it copies text that scrolled OFF-SCREEN, not
            just the visible grid. Every other selection copies the visible screen rows."""
            cols_now = len(_screen_text[0]) if _screen_text else 0
            if _sel["mode"] == "content" and chat_w is not None:
                # the full pre-scroll content lines (memoized — no extra wrap work)
                from ..core.tui_model import content_selection_copy
                _bub = chat_w._render_bubbles_cached()
                bubble_texts = [t for (t, _r) in _bub]
                text = content_selection_copy(bubble_texts, _sel["cl0"], _sel["ccol0"],
                                              _sel["cl1"], _sel["ccol1"], cols_now or 9999)
            else:
                from ..core.tui_model import selection_spans
                _cx0, _cx1 = _sel.get("cx0", 0), _sel.get("cx1", cols_now)   # box bounds (F23)
                rows_by_y: dict[int, str] = {}
                for sy, a, b in selection_spans(_sel["y0"], _sel["x0"],
                                                _sel["y1"], _sel["x1"], cols_now):
                    if 0 <= sy < len(_screen_text):
                        a2, b2 = max(a, _cx0), min(b, _cx1)      # clamp copy to the same box
                        if b2 > a2:
                            rows_by_y[sy] = _screen_text[sy][a2:b2]
                out_lines = [rows_by_y[y] for y in sorted(rows_by_y)]
                text = "\n".join(s.rstrip() for s in out_lines).strip("\n")
            if text:
                from ..core.clipboard import copy as _clip_copy
                _clip_copy(text)
                toasts.show(f"copied {len(text)} chars", "success")
            return bool(text)

        def _copy_last_reply():
            """Copy the most recent assistant message text (not screen rows).
            Sets a brief highlight on the copied message so the user sees what was copied."""
            if not chat_w:
                return
            for i, m in reversed(list(enumerate(chat_w.transcript.messages))):
                if getattr(m, "role", "").startswith("assistant"):
                    from ..core.clipboard import copy as _clip_copy
                    txt = getattr(m, "text", "") or ""
                    _clip_copy(txt)
                    _copy_highlight["msg_idx"] = i
                    _copy_highlight["until"] = _time.time() + 1.0  # flash for 1 second
                    toasts.show(f"copied ({len(txt)} chars)", "success")
                    return
            toasts.show("no reply to copy", "error")

        def _copy_whole_chat():
            """Copy the ENTIRE conversation (every message, role-labelled) to the clipboard
            — no fiddly drag needed for the whole thing."""
            if not chat_w:
                return
            _labels = {"user": "you", "assistant": "syntra", "assistant_stream": "syntra"}
            out = []
            for m in chat_w.transcript.messages:
                role = getattr(m, "role", "")
                txt = (getattr(m, "text", "") or "").rstrip()
                if not txt:
                    continue
                if role in _labels or role == "system":
                    out.append(f"{_labels.get(role, role)}: {txt}")
                else:
                    out.append(txt)              # trace lines verbatim
            blob = "\n".join(out).strip()
            if blob:
                from ..core.clipboard import copy as _clip_copy
                _clip_copy(blob)
                toasts.show(f"copied the whole chat ({len(blob)} chars)", "success")
            else:
                toasts.show("nothing to copy yet", "error")

        def _msg_index_at(rects, mx, my):
            """Which chat transcript message is under the screen point, if any.

            row_map (last_row_to_msg) has an entry per rendered row, but a click can land on a
            row that maps to nothing exact — e.g. a message's box border, a blank line, or a
            1-px boundary. An EXACT `row_map.get(local_y)` then returns None and the click
            "does nothing" (the flaky #130/#131 "must click many spots"). So: try exact, then
            fall back to the NEAREST mapped row within a couple of rows (the same message's box),
            so a click anywhere on a folded card / trace line reliably resolves."""
            if not chat_w:
                return None
            chat_rect = rects.get("chat")
            if not chat_rect or not chat_rect.contains(mx, my):
                return None
            row_map = getattr(chat_w, "last_row_to_msg", None)
            if not row_map:
                return None
            local_y = my - chat_rect.y
            if local_y in row_map:
                return row_map[local_y]
            # nearest-row fallback: search outward ±2 rows for the closest mapped row.
            for d in (1, -1, 2, -2):
                if (local_y + d) in row_map:
                    return row_map[local_y + d]
            return None

        def _minimap_on_rail(rects, mx, my) -> bool:
            """True if (mx,my) is on the chat's minimap rail column (its right edge).
            The chat content is drawn at cr.x + 1 (a 1-col left pad), so the rail glyph
            sits at screen column cr.x + 1 + _rail_col — account for that pad here."""
            if not chat_w or chat_w._rail is None or chat_w._rail_col < 0:
                return False
            cr = rects.get("chat")
            if not cr or not cr.contains(mx, my):
                return False
            # tolerate ±1 so the thin rail is easy to hit (glyph at cr.x+1+_rail_col)
            return abs((mx - cr.x - 1) - chat_w._rail_col) <= 1

        def _on_scrollbar(rects, mx, my) -> bool:
            """True if (mx,my) is on the chat's draggable scrollbar column. Content is drawn at
            cr.x + 1 (a 1-col left pad), so the scrollbar glyph sits at cr.x + 1 + _sb_col."""
            if not chat_w or getattr(chat_w, "_sb_col", -1) < 0:
                return False
            cr = rects.get("chat")
            if not cr or not cr.contains(mx, my):
                return False
            # only within the content rows the track spans (below the tab bar)
            top = cr.y + getattr(chat_w, "_sb_top", 1)
            if not (top <= my < top + max(1, getattr(chat_w, "_sb_content_h", 0))):
                return False
            # exact column when the rail sits right beside it (don't steal rail clicks at _w-2);
            # ±1 tolerance only when the scrollbar is alone, so the thin bar stays easy to grab.
            col = mx - cr.x - 1
            if getattr(chat_w, "_rail_col", -1) >= 0:
                return col == chat_w._sb_col
            return abs(col - chat_w._sb_col) <= 1

        def _scrollbar_drag_to(rects, my) -> None:
            """Map a cursor screen-row over the scrollbar track → an absolute scroll position.
            The thumb follows the cursor: y at the track top = chat top, bottom = chat bottom."""
            if not chat_w:
                return
            cr = rects.get("chat")
            if not cr:
                return
            top = cr.y + getattr(chat_w, "_sb_top", 1)
            h = max(1, getattr(chat_w, "_sb_content_h", 1))
            frac = (my - top) / max(1, h - 1)
            chat_w.transcript.scroll_to_fraction(frac)

        def _chat_content_geom(rects):
            """(chat_rect, content_top, content_h, content_x0) for the chat pane, or None if the
            chat isn't laid out. content_top = first content row (below the tab bar); content_x0 =
            first content column (1-col left pad, matching _on_scrollbar)."""
            cr = rects.get("chat")
            if not cr or not chat_w:
                return None
            top = cr.y + getattr(chat_w, "_sb_top", 1)
            h = max(1, getattr(chat_w, "_sb_content_h", 1))
            return (cr, top, h, cr.x + 1)

        def _chat_point_to_content(mx, my, rects):
            """Map a screen (mx,my) inside the chat content area → (content_line, col), clamped to
            the real transcript. Returns None if (mx,my) isn't in the chat content rows (tab bar,
            scrollbar track, or outside) so the caller can fall back to screen-mode selection."""
            geom = _chat_content_geom(rects)
            if geom is None:
                return None
            cr, content_top, content_h, content_x0 = geom
            if not cr.contains(mx, my) or my < content_top or my >= content_top + content_h:
                return None
            from ..core.tui_model import chat_content_line
            total = max(1, getattr(chat_w.transcript, "_view_total", 1))
            cl = chat_content_line(my, content_top, chat_w.transcript.scroll)
            cl = max(0, min(total - 1, cl))
            col = max(0, mx - content_x0)
            return (cl, col)

        def _set_chat_endpoint(mx, my, rects) -> None:
            """Move the content-anchored selection's far endpoint to follow the cursor, AND
            auto-scroll when the cursor is dragged past the chat's top/bottom edge (so a selection
            can grow beyond one screen — #89). Keeps x1/y1 in sync for any screen-coord fallback."""
            geom = _chat_content_geom(rects)
            if geom is None:
                return
            cr, content_top, content_h, content_x0 = geom
            tr = chat_w.transcript
            # auto-scroll: distance past the edge sets the step (1..3 lines) so a fast drag past the
            # edge scrolls faster. scroll_up/down clamp + drop follow-mode themselves. Record the
            # edge direction + last point so the IDLE tick can KEEP scrolling while the button is
            # held at the edge (the cursor can't move further there, so no more motion events fire
            # — this is what made bottom-to-top drag stall; #122). Symmetric up and down.
            if my < content_top:
                tr.scroll_up(min(3, content_top - my)); _sel["edge"] = -1
            elif my >= content_top + content_h:
                tr.scroll_down(min(3, my - (content_top + content_h) + 1)); _sel["edge"] = 1
            else:
                _sel["edge"] = 0                      # inside the pane → no auto-scroll needed
            # clamp the cursor row into the content band before mapping to a content line, so a
            # cursor past the edge still extends to the (now scrolled) first/last visible line.
            cy = max(content_top, min(content_top + content_h - 1, my))
            from ..core.tui_model import chat_content_line
            total = max(1, getattr(tr, "_view_total", 1))
            _sel["cl1"] = max(0, min(total - 1, chat_content_line(cy, content_top, tr.scroll)))
            _sel["ccol1"] = max(0, mx - content_x0)
            _sel["x1"], _sel["y1"] = mx, my
            _sel["last_mx"], _sel["last_my"] = mx, my

        def _set_drag_motion(on: bool) -> None:
            """Enable xterm button-event motion (?1002h) JUST for the lifetime of a scrollbar
            drag, then revert to click-only on release. Why scoped: globally capturing motion
            (?1003h any-event) was the root cause of the old lag / auto-select / flaky-click and
            it kills the terminal's NATIVE text selection. ?1002 reports motion ONLY while a
            button is held — so during a thumb drag the cursor row streams in and the thumb
            follows smoothly, and the instant the button is released we drop back to click-only
            and the terminal owns selection again everywhere else. No-op if the session was
            launched in a global motion mode (SYNTRA_MOUSE_MODE=drag/all) — motion's already on."""
            if _any_motion:
                return
            try:
                if on:
                    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
                    _sys.stdout.write("\x1b[?1002h"); _sys.stdout.flush()
                else:
                    _sys.stdout.write("\x1b[?1002l"); _sys.stdout.flush()
                    curses.mousemask(curses.ALL_MOUSE_EVENTS & ~curses.REPORT_MOUSE_POSITION)
            except Exception:  # noqa: BLE001 - terminal may reject; click-only still works
                pass

        def _minimap_focus_for_y(rects, my) -> int:
            """The rail message index nearest a hovered screen row `my` (for hover-focus)."""
            rail = chat_w._rail if chat_w else None
            if rail is None:
                return minimap_modal["focus"]
            cr = rects.get("chat")
            if not cr:
                return minimap_modal["focus"]
            row = max(0, my - cr.y - chat_w._rail_top)   # content row under the cursor
            # map that pane row to a message via the same even-distribution as ticks()
            n = len(rail.index)
            h = max(1, chat_w._rail_content_h)
            if n <= h:
                return max(0, min(row, n - 1))
            return max(0, min((row * n) // h, n - 1))

        def _open_link(target: str):
            """M7: act on a clicked link. URLs open via the OS opener (detached, output silenced
            so it never disrupts the curses screen); a clicked IMAGE file renders inline (reusing
            the /view overlay); other file refs are copied to the clipboard ($EDITOR would have to
            suspend curses, so copy is the safe act)."""
            if "://" in target:
                try:
                    import subprocess
                    opener = {"darwin": "open", "win32": "start"}.get(_sys.platform, "xdg-open")
                    subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                    toasts.show("opened link", "success", ttl=1.2)
                    return
                except Exception:  # noqa: BLE001 - fall back to clipboard if no opener
                    pass
            # A clicked workspace IMAGE → render it inline (the same overlay /view uses), so a
            # clicked screenshot/diagram just shows. Strip a trailing :line:col if present.
            import re as _re_local
            _fp = _re_local.sub(r":\d+(?::\d+)?$", "", target)
            try:
                from pathlib import Path as _P
                _pp = _P(_fp).expanduser()
                if _pp.is_file() and _pp.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    _show_image(str(_pp))   # self-managing: sharp inline OR preview + auto-open viewer
                    toasts.show(f"showing {_pp.name}", "success", ttl=1.2)
                    _draw()
                    return
            except Exception:  # noqa: BLE001 - fall through to copy on any trouble
                pass
            try:
                from ..core.clipboard import copy as _clip_copy
                _clip_copy(target)
                toasts.show(f"copied {target}", "success", ttl=1.4)
            except Exception:  # noqa: BLE001
                pass

        def _click_at(rects, mx, my):
            nonlocal focused
            for name, rect in rects.items():
                if rect.contains(mx, my):
                    widget = registry.get(name)
                    if widget and widget.focusable and name in focusable_names:
                        focused = name
                    # Minimap rail: a CLICK on the right-edge rail toggles the rolling-wheel
                    # navigator (works in click-only mouse mode, where there's no hover). If
                    # the panel is already open, a click on one of its rows scrolls the chat
                    # to that message (read-only nav) and closes it.
                    if name == "chat" and chat_w and chat_w._rail is not None:
                        if minimap_modal["active"] and minimap_modal.get("geom"):
                            _gtop, _gleft, _gh = minimap_modal["geom"]
                            if _gtop <= my < _gtop + _gh and mx >= _gleft:
                                _ridx = chat_w._rail.row_at(my, minimap_modal["focus"],
                                                            pane_top=_gtop + 1)
                                if _ridx >= 0 and _ridx < len(chat_w._rail.index):
                                    chat_w.scroll_to_message(chat_w._rail.index[_ridx][0])
                                    toasts.show("jumped to message", "info", ttl=1.0)
                                minimap_modal["active"] = False
                                break
                        if _minimap_on_rail(rects, mx, my):
                            minimap_modal["active"] = not minimap_modal["active"]
                            if minimap_modal["active"]:
                                minimap_modal["focus"] = _minimap_focus_for_y(rects, my)
                                minimap_modal["anchor_y"] = my   # open centered on the click
                            break
                    # Action-feed fold line: a click on a "▸ group · N more" line toggles
                    # that group (expand/collapse). _feed_row0 is the widget-local row where
                    # the feed block starts (recorded at render); map the screen row to a
                    # feed-render-row and ask the model which fold group (if any) is there.
                    if (name == "chat" and chat_w is not None
                            and getattr(chat_w, "action_feed", None) is not None
                            and getattr(chat_w, "_feed_row0", -1) >= 0):
                        _frow = (my - rect.y) - chat_w._feed_row0
                        if _frow >= 0:
                            _grp = chat_w.action_feed.fold_group_at(_frow)
                            if _grp is not None:
                                chat_w.action_feed.toggle_group(_grp)
                                break
                    # #132: click a code-block header ("[n] lang   ⧉ copy") → copy that block's
                    # RAW content (no gutter/borders) to the clipboard. This is the TUI-native
                    # clean copy people will actually use (vs typing /copy n, which they won't).
                    if name == "chat":
                        _row = _screen_text[my] if 0 <= my < len(_screen_text) else ""
                        import re as _re_cb
                        _m = _re_cb.search(r"\[(\d+)\]\s+\S+\s+⧉ copy", _row)
                        if _m:
                            try:
                                from ..core.tui_model import extract_code_blocks
                                from ..core.clipboard import copy as _clip_copy
                                _blocks = extract_code_blocks(chat_w.transcript.messages) if chat_w else []
                                _bn = int(_m.group(1))
                                if 1 <= _bn <= len(_blocks):
                                    _clip_copy(_blocks[_bn - 1])
                                    toasts.show(f"copied block [{_bn}]", "success", ttl=1.4)
                                    break
                            except Exception:  # noqa: BLE001
                                pass
                    # M7: click a URL / file:line in the transcript → open it. The recorded
                    # screen row + absolute column map straight to a link span.
                    if name == "chat":
                        from ..core.tui_model import link_at
                        _row = _screen_text[my] if 0 <= my < len(_screen_text) else ""
                        _tgt = link_at(_row, mx)
                        if _tgt:
                            _open_link(_tgt)
                            break
                    # Left-click on a chat message: toggle the background fold, or expand
                    # a collapsed message. It does NOT open the action menu — that blocked
                    # drag-select/copy (user). The action menu is RIGHT-click only now.
                    if name == "chat":
                        idx = _msg_index_at(rects, mx, my)
                        if idx is not None:
                            from ..core.tui_model import classify_fold_row
                            msgs = chat_w.transcript.messages if chat_w else []
                            role = getattr(msgs[idx], "role", "user") if 0 <= idx < len(msgs) else "user"
                            # BUG2: decide the fold action from the ROW ACTUALLY CLICKED (its drawn
                            # affordance), not a recomputed line-count that can disagree with what was
                            # rendered — so a click on the answer card expands the ANSWER and a click
                            # on the trace summary toggles the TRACE, independently. Falls back to the
                            # legacy role/autofold logic only when the clicked row carries no affordance.
                            _crect = rects.get("chat")
                            _row_text = ""
                            if _crect:
                                _r2t = getattr(chat_w, "last_row_to_text", {}) or {}
                                _ly = my - _crect.y
                                _row_text = _r2t.get(_ly) or _r2t.get(_ly + 1) or _r2t.get(_ly - 1) or ""
                            _fold = classify_fold_row(_row_text)
                            if _fold == "trace":
                                tc = not chat_w.transcript.trace_collapsed
                                chat_w.transcript.trace_collapsed = tc
                                toasts.show("background " + ("hidden" if tc else "shown"), "info", ttl=0.8)
                                break
                            cset = chat_w.transcript.collapsed if chat_w else set()
                            if _fold == "expand":
                                # expand THIS element: clear a manual collapse or add to `expanded`
                                if idx in cset:
                                    cset.discard(idx)
                                else:
                                    chat_w.transcript.expanded.add(idx)
                                toasts.show("expanded", "info", ttl=0.8)
                                break
                            if _fold == "collapse":
                                chat_w.transcript.expanded.discard(idx)
                                toasts.show("folded", "info", ttl=0.8)
                                break
                            # ── no affordance on the clicked row: legacy fallbacks ──
                            # a background/trace content line → toggle the fold (show/hide)
                            if role in ("mode", "tool", "thinking", "ok", "error"):
                                tc = not chat_w.transcript.trace_collapsed
                                chat_w.transcript.trace_collapsed = tc
                                toasts.show("background " + ("hidden" if tc else "shown"), "info", ttl=0.8)
                                break
                            if idx in cset:
                                cset.discard(idx)        # click-to-expand a collapsed msg
                                toasts.show("expanded", "info", ttl=0.8)
                                break
                            if chat_w and chat_w.transcript.is_autofoldable(idx):
                                exp = chat_w.transcript.expanded
                                if idx in exp:
                                    exp.discard(idx)
                                    toasts.show("folded", "info", ttl=0.8)
                                else:
                                    exp.add(idx)
                                    toasts.show("expanded", "info", ttl=0.8)
                                break
                            # Left-click elsewhere does NOTHING (RIGHT-click = menu; drag = select).
                            break
                    if widget:
                        b1 = getattr(__import__("curses"), "BUTTON1_CLICKED", 0)
                        # Pass coords relative to where the widget's content is
                        # actually painted: chat = 1-col left pad; other panels sit
                        # inside a border (content at rect.x+2, rect.y+1). Using the
                        # outer rect here mis-mapped every bordered widget's clicks.
                        if name == "chat":
                            dx, dy = rect.x + 1, rect.y
                        elif rect.w >= 4 and rect.h >= 3:
                            dx, dy = rect.x + 2, rect.y + 1
                        else:
                            dx, dy = rect.x, rect.y
                        widget.handle_mouse(mx - dx, my - dy, b1)
                    break

        # ── per-message actions (Copy / Edit / Retry / Fork / Revert) ──
        def _do_msg_action(action_id: str, idx: int):
            nonlocal last_task_id
            if not chat_w:
                return
            msgs = chat_w.transcript.messages
            if not (0 <= idx < len(msgs)):
                return
            target = msgs[idx]
            text = getattr(target, "text", "") or ""

            if action_id == "copy":
                from ..core.clipboard import copy as _clip_copy
                _clip_copy(text)
                toasts.show(f"copied {len(text)} chars", "success")

            elif action_id == "collapse":
                cset = chat_w.transcript.collapsed
                if idx in cset:
                    cset.discard(idx)
                    toasts.show("expanded", "info", ttl=0.8)
                else:
                    cset.add(idx)
                    toasts.show("collapsed", "info", ttl=0.8)

            elif action_id == "edit":
                # edit only your own turns, not background/assistant lines (P35)
                if getattr(target, "role", "") != "user":
                    toasts.show("edit only on your messages", "info", ttl=1.2)
                else:
                    chat_w.editor.clear()
                    chat_w.editor.insert(text)
                    chat_w.transcript.to_bottom()
                    chat_w.add("system", "[editing — change it and press enter]")

            elif action_id == "retry":
                if active or controller.running():
                    toasts.show("wait for the current run to finish", "info", ttl=1.5)
                elif getattr(target, "role", "").startswith("assistant"):
                    # F31: re-generate THIS answer as an in-place ALTERNATIVE (‹n/N›,
                    # ◂ ▸ to switch) — NOT a new reply appended below. Re-run the user
                    # prompt that produced it on a bg thread; fold the answer into the
                    # message's variants. Uses the synchronous _run_goal (no streaming
                    # bubble), so the live poll loop is untouched.
                    prompt = ""
                    for _m in reversed(msgs[:idx]):
                        if getattr(_m, "role", "") == "user":
                            prompt = getattr(_m, "text", "") or ""
                            break
                    if not prompt:
                        toasts.show("no earlier prompt to regenerate from", "info", ttl=1.5)
                    else:
                        toasts.show("regenerating an alternative…", "info", ttl=1.5)

                        def _regen(p=prompt, tgt=target):
                            try:
                                res = controller._run_goal(p, lambda _t, _r="tool": None, None)
                                done = [s for s in getattr(res.state, "plan", []) or []
                                        if getattr(s, "status", "") == "done"
                                        and (getattr(s, "result", "") or "").strip()]
                                ans = "\n\n".join((s.result or "").strip() for s in done)
                            except Exception as _e:  # noqa: BLE001
                                _post(lambda e=_e: toasts.show(f"retry failed: {e}", "error", ttl=2.5))
                                return

                            def _apply(a=ans, t=tgt):
                                if not chat_w:
                                    return
                                try:
                                    ci = chat_w.transcript.messages.index(t)
                                except ValueError:
                                    return
                                if chat_w.transcript.add_variant(ci, a or "(empty answer)"):
                                    chat_w.transcript.to_bottom()
                                    toasts.show("added alternative — ◂ ▸ to switch", "success", ttl=2.5)
                            _post(_apply)
                        _run_bg("retry", _regen)
                else:
                    _submit(text)   # a user turn: re-send it (original behavior)

            elif action_id == "fork":
                # Branch the TASK this turn belongs to — NOT using the chat-message
                # index as a rollout-event index (those are different coordinate
                # spaces). Find the nearest tagged user turn at/before the clicked
                # message → branch that task at its FULL rollout (a complete copy you
                # continue/diverge). Falls back to the latest task.
                branch_tid = ""
                for _m in reversed(msgs[:idx + 1]):
                    _t = getattr(_m, "task_id", "")
                    if _t:
                        branch_tid = _t
                        break
                if not branch_tid:
                    branch_tid = last_task_id
                if branch_tid:
                    try:
                        from ..core import rollout
                        sr = str(_state_root())
                        at = len(rollout.read(sr, branch_tid))   # valid event coordinate
                        new_id = rollout.branch(sr, branch_tid, at=at)
                        last_task_id = new_id                    # AUTO-SWITCH onto the branch (user [150])
                        chat_w.add("system",
                            "✓ Forked this conversation — you are now on a COPY of it.\n"
                            f"  • Whatever you type next continues on this copy (branch {new_id[:8]}).\n"
                            f"  • The original is saved untouched — go back any time with:  /resume {branch_tid[:8]}\n"
                            "  This lets you try a different direction without losing the original.")
                        toasts.show("switched to a copy — type to continue here", "success", ttl=2.0)
                    except Exception as e:  # noqa: BLE001
                        chat_w.add("system", f"✗ Couldn't fork: {e}")
                else:
                    chat_w.add("system", "Nothing to fork yet — run a task first, then fork from one of its messages.")

            elif action_id == "revert":
                # drop this message + everything after it, and put the last USER
                # message from the dropped range back into the input box so you can
                # edit + resend it (P14).
                dropped = chat_w.transcript.messages[idx:]
                last_user = next((getattr(m, "text", "") for m in reversed(dropped)
                                  if getattr(m, "role", "") == "user"), "")
                del chat_w.transcript.messages[idx:]
                chat_w.transcript.to_bottom()
                if last_user:
                    chat_w.editor.clear()
                    chat_w.editor.insert(last_user)
                    chat_w.add("system", "↩ reverted — your message is back in the input (edit + Enter)")
                else:
                    chat_w.add("system", "↩ reverted to this point")

        # ── apply a panel toggle: dock/undock it on the right ──
        def _resize_focused(delta: float) -> bool:
            """Grow/shrink the FOCUSED panel by mutating its weight in the main h-split,
            then re-resolve on the next draw (#8 interactive resize via Ctrl+←/→ or +/-).
            Returns True if a panel's width actually changed."""
            if focused == "chat":
                return False
            hsplit = _find_main_hsplit(state["layout"])
            if not hsplit:
                return False
            for c in hsplit.children:
                if isinstance(c, LayoutLeaf) and c.widget_name == focused:
                    c.weight = max(0.3, min(5.0, c.weight + delta))
                    return True
                if isinstance(c, LayoutSplit) and _layout_has(c, focused):
                    c.weight = max(0.2, min(3.0, c.weight + delta * 0.5))
                    return True
            return False

        def _move_focused(direction: int) -> bool:
            """Swap the FOCUSED panel with its neighbor in the main h-split (#8 move/reorder).
            `direction` is -1 (left) or +1 (right). Returns True if a swap happened."""
            if focused == "chat":
                return False
            hsplit = _find_main_hsplit(state["layout"])
            if not hsplit or len(hsplit.children) < 2:
                return False
            idx = None
            for i, c in enumerate(hsplit.children):
                if ((isinstance(c, LayoutLeaf) and c.widget_name == focused)
                        or (isinstance(c, LayoutSplit) and _layout_has(c, focused))):
                    idx = i
                    break
            if idx is None:
                return False
            j = idx + direction
            if not (0 <= j < len(hsplit.children)):
                return False
            hsplit.children[idx], hsplit.children[j] = hsplit.children[j], hsplit.children[idx]
            return True

        def _divider_at(rects: dict, mx: int):
            """If column `mx` sits on a vertical divider between two horizontally-adjacent
            panels, return the name of the panel just LEFT of it (the one a drag resizes),
            else None. A divider is where one panel's right edge meets the next's left edge
            (within 1 col, to make the 1-px border easy to grab)."""
            # name -> rect; sort by x so we can find adjacent pairs
            items = sorted(rects.items(), key=lambda kv: kv[1].x)
            for i in range(len(items) - 1):
                lname, lr = items[i]
                rname, rr = items[i + 1]
                edge = lr.x + lr.w           # left panel's right boundary == the divider col
                # adjacency check (same row band) + the next panel starts right after
                if abs(rr.x - edge) <= 1 and abs(mx - edge) <= 1:
                    return lname
            return None

        def _set_leaf_weight(name: str, delta_cols: int, total_cols: int) -> bool:
            """Resize the panel `name` by a column delta (drag). Translates the pixel delta
            into a weight nudge on its node in the main h-split, then clamps. Returns True
            if it changed."""
            hsplit = _find_main_hsplit(state["layout"])
            if not hsplit or total_cols <= 0:
                return False
            # weight units per column: the split shares total weight across total_cols, so
            # one column ≈ (sum_weight / total_cols). Nudge proportional to the drag.
            sumw = sum(getattr(c, "weight", 1.0) for c in hsplit.children) or 1.0
            per_col = sumw / max(1, total_cols)
            for c in hsplit.children:
                if ((isinstance(c, LayoutLeaf) and c.widget_name == name)
                        or (isinstance(c, LayoutSplit) and _layout_has(c, name))):
                    c.weight = max(0.2, min(6.0, getattr(c, "weight", 1.0) + delta_cols * per_col))
                    return True
            return False

        def _apply_panel(kind: str, enabled: bool):
            if enabled:
                _summon_panel(kind)
                toasts.show(f"{kind} on", "info")
            else:
                _dismiss_panel(kind)
                toasts.show(f"{kind} off", "info")

        def _dismiss_panel(kind: str):
            from ..core.widget import LayoutSplit, LayoutLeaf
            cur = state["layout"]
            visible = set()
            def _collect(node):
                if isinstance(node, LayoutLeaf):
                    if node.widget_name != "chat":
                        visible.add(node.widget_name)
                elif isinstance(node, LayoutSplit):
                    for c in node.children:
                        _collect(c)
            _collect(cur)
            visible.discard(kind)
            if not visible:
                state["layout"] = _clean_layout()
                return
            # Rebuild using _summon_panel's logic by starting clean and adding each
            state["layout"] = _clean_layout()
            for p in sorted(visible):
                _summon_panel(p)

        def _summon_panel(kind: str):
            """Add a panel by rebuilding the layout from the set of active panels.

            Instead of manually assembling splits (which produces bad proportions),
            we rebuild a btop-style layout from the set of all currently-visible
            panels plus the new one. Chat is always the dominant center pane.
            """
            from ..core.widget import LayoutSplit, LayoutLeaf
            cur = state["layout"]
            if _layout_has(cur, kind):
                return

            # Collect currently visible panels (excluding chat which is always there)
            visible = set()
            def _collect(node):
                if isinstance(node, LayoutLeaf):
                    if node.widget_name != "chat":
                        visible.add(node.widget_name)
                elif isinstance(node, LayoutSplit):
                    for c in node.children:
                        _collect(c)
            _collect(cur)
            visible.add(kind)

            # Placement order is data-driven (panel_menu.LEFT_ORDER/RIGHT_ORDER) and
            # invariant-tested, so every toggleable panel is actually placeable — no
            # more "registered + in the menu but never placed" false-done (P25 bug).
            from ..core.panel_menu import LEFT_ORDER, RIGHT_ORDER
            left = [p for p in LEFT_ORDER if p in visible]
            right = [p for p in RIGHT_ORDER if p in visible]

            # Build the layout: [left_sidebar? | chat | right_stack?]
            cols = []
            if left:
                left_children = [LayoutLeaf(p, 1.0) for p in left]
                cols.append(LayoutSplit("v", left_children, weight=0.7))
            cols.append(LayoutLeaf("chat", weight=3.0))
            if right:
                right_children = [LayoutLeaf(p, 1.0) for p in right]
                cols.append(LayoutSplit("v", right_children, weight=1.2))

            state["layout"] = LayoutSplit("v", [
                LayoutSplit("h", cols, weight=1.0),
            ])
            nonlocal focused
            if kind in ("file_tree", "file_viewer") and kind in focusable_names:
                focused = kind

        # ── chained command menu (Ctrl+P) ──
        def _open_cmd_menu():
            from ..core.menu import MenuStack
            from ..core.menu_tree import build_root_menu
            from ..core.panel_menu import PANELS
            from ..core.themes import list_themes   # current_theme covered by the module import
            # build the model list (id, hint) from the catalog
            models = []
            try:
                from ..core.catalog import Catalog
                cat = Catalog.load(os.environ.get("SYNTRA_CATALOG_PATH")
                                   or _default_catalog_path())
                models.extend((m.id, f"{m.speed_tps:.0f}t/s")
                              for m in sorted(cat.models, key=lambda x: -x.intelligence_index)[:40])
            except Exception:  # noqa: BLE001
                pass
            root = build_root_menu(
                themes=list_themes(),
                layouts=list(_LAYOUTS.keys()),
                panels=PANELS,
                models=models,
                current_theme=current_theme(),
            )
            st = MenuStack()
            st.open("menu", root)
            cmd_menu["stack"] = st

        def _run_menu_action(action: str):
            """Resolve a leaf action id from the chained menu."""
            if not action:
                return
            kind, _, arg = action.partition(":")
            if kind == "open_models":
                # F34: Ctrl+P → Models opens the SAME searchable picker as /models.
                cmd_menu["stack"] = None
                from ..core.overlay import make_model_overlay
                overlay["current"] = make_model_overlay(arg)   # arg = role or "" (all roles)
                return
            if kind == "theme":
                from ..core.themes import set_theme as _st
                if _st(arg):
                    _load_colors()
                    toasts.show(f"theme: {arg}", "success")
            elif kind == "layout":
                fac = _LAYOUTS.get(arg)
                if fac:
                    state["layout"] = fac()
                    toasts.show(f"layout: {arg}", "success")
            elif kind == "panel":
                on = not _layout_has(state["layout"], arg)
                _apply_panel(arg, on)
                toasts.show(f"panel {arg}: {'on' if on else 'off'}", "info")
            elif kind == "model":
                # Per-role model assignment. arg format:
                #   "model_id" → pin all roles (legacy, from menu top-level)
                #   "planner:model_id" → pin only planner
                #   "executor:model_id" → pin only executor
                #   "reviewer:model_id" → pin only reviewer
                try:
                    from ..core.overrides import Overrides
                    ov = Overrides.load()
                    if ":" in arg and arg.split(":")[0] in ("planner", "executor", "reviewer"):
                        role_name, model_id = arg.split(":", 1)
                        ov.pin_role(role_name, model_id)
                        ov.save()
                        toasts.show(f"{role_name} → {model_id.split('/')[-1]}", "success")
                    else:
                        for role in ("planner", "executor", "reviewer"):
                            ov.pin_role(role, arg)
                        ov.save()
                        toasts.show(f"all roles → {arg.split('/')[-1]}", "success")
                except Exception as e:  # noqa: BLE001
                    toasts.show(f"pin failed: {e}", "error")
            elif kind == "session":
                if arg == "new" and chat_w:
                    chat_w.transcript.messages.clear()
                    chat_w.transcript._follow = True
                    toasts.show("new session", "info")
                elif arg == "resume":
                    _submit(f"/resume {last_task_id}" if last_task_id else "/resume")
                elif arg == "fork":
                    _do_msg_action("fork", len(chat_w.transcript.messages) - 1 if chat_w else 0)
            elif kind == "copy":
                if arg == "last":
                    _copy_last_reply()
                elif arg == "native":
                    state["native_select"] = True
                    try:
                        curses.mousemask(0)
                    except curses.error:
                        pass
                    toasts.show("native-select ON — drag to copy, Ctrl+Y to exit", "info")
            elif kind == "help":
                _submit("/keymap" if arg == "keys" else "/help")

        # wire chat submit
        if chat_w:
            def _chat_event(w, ev, data):
                if ev == "submit":
                    _submit(data)
                elif ev == "open_palette":
                    _open_cmd_menu()
                elif ev == "open_filepicker":
                    from ..core.overlay import make_file_overlay
                    overlay["current"] = make_file_overlay()
            chat_w._on_event = _chat_event

        # wire file tree: clicking/opening a file shows it in the file viewer panel
        ft_widget = registry.get("file_tree")
        fv_widget = registry.get("file_viewer")

        def _on_file_open(_w, ev, path):
            if ev != "open_file" or not fv_widget:
                return
            fv_widget.open(path)
            _summon_panel("file_viewer")
            # focus the viewer so scroll keys go to it
            nonlocal focused
            if "file_viewer" in focusable_names:
                focused = "file_viewer"
            if chat_w:
                chat_w.add("system", f"[opened {os.path.basename(path)}]")

        if ft_widget:
            ft_widget._on_event = _on_file_open

        # #152/#235: the AGENTS panel emits "kill" when you press k in an agent's detail view.
        # There's no per-sub-agent cancel primitive (SteeringInbox is per-run), so this stops
        # the whole run via the SAME hard_stop the rest of the TUI uses — honest + consistent.
        def _on_agent_event(_w, ev, _role):
            if ev != "kill":
                return
            if controller.hard_stop():
                if chat_w:
                    chat_w.add("system", "⏹ stopped the run")
                toasts.show("run stopped", "info", ttl=1.4)
            else:
                toasts.show("nothing running", "info", ttl=1.2)
        if agent_w:
            agent_w._on_event = _on_agent_event

        # ── main loop ──
        _set_bracketed(True)
        stdscr.timeout(80)
        _draw()

        try:
            while True:
                _drain()
                for w in registry.all().values():
                    if w.tick():
                        pass  # changed, will redraw
                if _bg_dirty["v"]:
                    # A background thread (e.g. clipboard image paste) mutated UI state via _post;
                    # repaint NOW so the result shows even when the app is otherwise idle.
                    _bg_dirty["v"] = False
                    _draw()

                try:
                    ch = stdscr.getch()
                except KeyboardInterrupt:
                    ch = 3  # treat as Ctrl+C key below

                if _key_debug:
                    try:
                        _last_key["code"] = int(ch)
                        _last_key["name"] = (curses.keyname(ch).decode(errors="replace") if ch != -1 else "")
                    except Exception:  # noqa: BLE001
                        _last_key["code"] = int(ch) if isinstance(ch, int) else -999
                        _last_key["name"] = ""

                # configurable keymap (A1): a user's rebound key -> the canonical default
                # code the handlers below check. No-op ({} ) on the default keymap.
                if _key_remap and ch in _key_remap:
                    ch = _key_remap[ch]

                # #173: a user-bound /command key — fires ONLY on an EMPTY chat prompt (so it
                # never eats a keystroke while composing) and only when no modal owns input.
                # _submit runs the command string end-to-end (same path as typing it).
                if (_cmd_binds and ch in _cmd_binds and focused == "chat"
                        and chat_w and chat_w.editor.is_empty()
                        and overlay["current"] is None and perm_modal["req"] is None):
                    _submit(_cmd_binds[ch])
                    _draw(); continue

                # Ctrl+C: if the input has text, clear it; if the input is already empty, quit.
                if ch == 3:
                    if chat_w and not chat_w.editor.is_empty():
                        chat_w.editor.text = ""
                        chat_w.editor.cursor = 0
                        _draw(); continue
                    break  # empty input → quit (resume hint prints on exit)

                if ch == -1:
                    # #122: a content drag HELD past the top/bottom edge keeps auto-scrolling on
                    # the idle tick — the cursor can't move further at the edge, so motion events
                    # stop and this is the only thing that keeps the scroll going (fixes the
                    # jittery/stalled bottom-to-top drag). Re-extends the selection each tick.
                    if _sel["active"] and _sel["mode"] == "content" and _sel["edge"]:
                        _rows, _cols = stdscr.getmaxyx()
                        _al = _focus_layout() if _cols < 90 else state["layout"]
                        _rc = resolve_layout(_al, Rect(0, 1, _cols, _rows - 2))
                        _set_chat_endpoint(_sel["last_mx"], _sel["last_my"], _rc)
                        stdscr.timeout(40)            # smooth scroll cadence while at the edge
                        _draw_coalesced()
                        continue
                    if controller.running() or controller.finished or active:
                        _drain()
                        # Faster polling during streaming for smooth token display
                        stdscr.timeout(30 if controller.running() else 80)
                    _draw()
                    continue

                if ch == curses.KEY_RESIZE:
                    _draw(); continue

                # typing or any non-mouse key dismisses a finished selection highlight
                if _sel["show"] and ch not in (curses.KEY_MOUSE,):
                    _sel["show"] = False

                # ── /compare cards intercept keys while open (A-fix 5) ──
                if compare_modal["view"] is not None:
                    cv = compare_modal["view"]

                    def _accept_focused():
                        """Accept the focused card/synthesis into the chat (manual override)."""
                        if not cv.done:
                            return
                        picked = cv.pick()
                        compare_modal["view"] = None
                        if picked and chat_w:
                            _which = ("synthesis" if cv.is_synthesis_focused()
                                      else cv.candidates[cv.focus].model)
                            chat_w.add("system", f"✓ accepted {_which}:")
                            chat_w.add("assistant", picked)
                            toasts.show("answer accepted", "success", ttl=1.5)

                    if ch == curses.KEY_MOUSE:
                        # click a tab to focus it; click the already-focused tab to accept.
                        # wheel scrolls the focused answer body.
                        try:
                            _mid, _mx, _my, _mz, _mb = curses.getmouse()
                        except curses.error:
                            _draw(); continue
                        _up = getattr(curses, "BUTTON4_PRESSED", 0)
                        _dn = getattr(curses, "BUTTON5_PRESSED", 0)
                        _b1 = (getattr(curses, "BUTTON1_CLICKED", 0)
                               | getattr(curses, "BUTTON1_PRESSED", 0)
                               | getattr(curses, "BUTTON1_RELEASED", 0))
                        if _mb & _up:
                            cv.scroll_by(-3)
                        elif _mb & _dn:
                            cv.scroll_by(3)
                        elif _mb & _b1:
                            _gx, _gy, _gw, _gh = compare_modal.get("geom", (0, 0, 0, 0))
                            _tab = cv.tab_at_row(_my - _gy)
                            if _tab is not None:
                                if _tab == cv.focus:
                                    _accept_focused()
                                else:
                                    cv.focus_index(_tab)
                        _draw(); continue

                    if ch == 27:                         # Esc closes (discard pick)
                        _esc_burst()
                        compare_modal["view"] = None
                    elif ch in (curses.KEY_LEFT, ord("h")):
                        cv.move(-1)
                    elif ch in (curses.KEY_RIGHT, ord("l"), ord("\t"), 9):
                        cv.move(1)
                    elif ch in (curses.KEY_UP, ord("k")):
                        cv.scroll_by(-1)
                    elif ch in (curses.KEY_DOWN, ord("j")):
                        cv.scroll_by(1)
                    elif ch in (curses.KEY_NPAGE,):
                        cv.scroll_by(8)
                    elif ch in (curses.KEY_PPAGE,):
                        cv.scroll_by(-8)
                    elif ch in (curses.KEY_ENTER, 10, 13):
                        _accept_focused()
                    _draw(); continue

                # ── tool-permission prompt intercepts keys AND mouse while pending ──
                if perm_modal["req"] is not None:
                    # #83 deny+tell: once the user picked [6], we capture a one-line reason. Enter
                    # submits deny_guide(reason) → the agent gets it back; Esc cancels to the choices.
                    if perm_modal.get("reason") is not None:
                        if ch in (curses.KEY_ENTER, 10, 13):
                            _r = perm_modal["reason"].strip()
                            perm_modal["reason"] = None
                            _perm_answer("deny_guide" if _r else "deny", reason=_r)
                        elif ch == 27:
                            _esc_burst(); perm_modal["reason"] = None   # back to the button choices
                        elif ch in (curses.KEY_BACKSPACE, 127, 8):
                            perm_modal["reason"] = perm_modal["reason"][:-1]
                        elif 32 <= ch < 127:
                            perm_modal["reason"] += chr(ch)
                        _draw(); continue
                    if ch == curses.KEY_MOUSE:
                        # map a click/hover on the action row to an option (same actions as
                        # the keys). Motion updates the hover highlight; a left-click resolves.
                        from ..core.tui_model import permission_click_action
                        try:
                            _pmid, _pmx, _pmy, _pmz, _pmb = curses.getmouse()
                        except curses.error:
                            _draw(); continue
                        _g = perm_modal.get("geom") or {}
                        _act = (permission_click_action(_g.get("line", ""), _pmx - _g.get("x0", 0))
                                if _pmy == _g.get("y") else None)
                        _b1 = (getattr(curses, "BUTTON1_CLICKED", 0)
                               | getattr(curses, "BUTTON1_PRESSED", 0)
                               | getattr(curses, "BUTTON1_RELEASED", 0))
                        if _act and (_pmb & _b1):
                            if _act == "deny_guide":       # click [6] → start typing the reason
                                perm_modal["reason"] = ""
                            else:
                                _perm_answer(_act)          # click an option -> resolve
                        else:
                            perm_modal["hover"] = _act      # hover -> live highlight (or clear)
                        _draw(); continue
                    if ch == 5:                          # #222: Ctrl+E toggles the risk explainer
                        perm_modal["explain"] = not perm_modal.get("explain")
                        _draw(); continue
                    if ch == ord("1"):
                        _perm_answer("once")
                    elif ch == ord("2"):
                        _perm_answer("session")
                    elif ch == ord("3"):
                        _perm_answer("always")
                    elif ch == ord("4"):                 # 4 = deny THIS call, keep asking
                        _perm_answer("deny")
                    elif ch == ord("5"):                 # 5 = deny + stop asking this session
                        _perm_answer("deny_all")
                    elif ch == ord("6"):                 # 6 = deny + type guidance for the agent
                        perm_modal["reason"] = ""
                    elif ch == 27:
                        # Esc at a permission prompt follows the ONE unified interrupt model
                        # (same as mid-run): a real bare Esc = deny this call AND stop the whole
                        # run (no immediate re-prompt for the next tool); double-Esc = complete
                        # stop of everything. Decode the burst to tell bare-Esc from double.
                        burst = _esc_burst()
                        double = (burst == "\x1b\x1b") or (burst == "\x1b" and last_esc)
                        _q = len(_pending)
                        act = _esc_action(double=double, run_active=True, queued=_q)
                        if act == "flush":
                            # deny the pending call, then fold the queued msg(s) into the run
                            _perm_answer("deny")
                            n = _flush_pending_to_run()
                            if n == 0 and _pending:
                                n = _interrupt_merge_resend()
                            last_esc = False
                            if n:
                                toasts.show(f"denied + added {n} message{'s' if n != 1 else ''}",
                                            "info", ttl=1.2)
                        elif act == "arm":
                            # single Esc, nothing queued → arm; a following Esc completes the stop
                            last_esc = True
                            _esc_time = _time.time()
                        else:                             # "stop" / "quit" → complete stop
                            _do_complete_stop("stopped")
                            active = False
                            last_esc = False
                    _draw(); continue

                # ── API-key entry popup intercepts keys while open ── masked form
                if key_modal["current"] is not None:
                    kf = key_modal["current"]
                    if ch in (9, curses.KEY_DOWN):           # Tab / ↓ → next field
                        kf.move(1)
                    elif ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):  # Shift-Tab / ↑
                        kf.move(-1)
                    elif ch == curses.KEY_LEFT:              # ← move caret left within the field
                        kf.left()
                    elif ch == curses.KEY_RIGHT:             # → move caret right
                        kf.right()
                    elif ch == curses.KEY_HOME:
                        kf.home()
                    elif ch == curses.KEY_END:
                        kf.end()
                    elif ch == 18:                           # Ctrl+R → reveal/hide the key
                        kf.toggle_reveal()
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        kf.backspace()
                    elif ch in (curses.KEY_ENTER, 10, 13):
                        sig = kf.submit()
                        if sig is not None:                  # validated → save via the runner seam
                            _prov, _key, _burl = sig[1]
                            key_modal["current"] = None
                            _adder = getattr(controller._run_goal, "add_provider_key", None)
                            if _adder is None:
                                if chat_w:
                                    chat_w.add("system", "key entry unavailable (no provider runner)")
                            else:
                                ok, msg = _adder(_prov, _key, _burl)
                                if chat_w:
                                    chat_w.add("system", ("✓ " if ok else "✗ ") + msg)
                                toasts.show(("✓ key added" if ok else "key not added"),
                                            "success" if ok else "error", ttl=2.0)
                                if ok:
                                    _detect_models_bg(_prov)   # auto-discover what this key unlocks
                        # else: missing field → stays open with kf.error shown
                    elif ch == 27:
                        # An Esc-prefixed burst here is one of THREE things — decode before acting:
                        #  1) a BRACKETED PASTE (\x1b[200~ … \x1b[201~) → paste the key into the
                        #     field (the whole reason paste "closed" the popup before);
                        #  2) an ARROW / Home / End that ncurses split into ESC + tail → move caret;
                        #  3) a real bare Esc (burst == "\x1b") → cancel the popup.
                        burst = _esc_burst()
                        if "[200~" in burst:
                            body = burst.split("[200~", 1)[1].split("[201~", 1)[0].rstrip("\x1b")
                            kf.paste(body)
                        else:
                            _kk = _split_csi(burst)
                            if _kk:
                                for _k, _ in _kk:
                                    if _k == curses.KEY_LEFT: kf.left()
                                    elif _k == curses.KEY_RIGHT: kf.right()
                                    elif _k == curses.KEY_HOME: kf.home()
                                    elif _k == curses.KEY_END: kf.end()
                                    elif _k == curses.KEY_DC: kf.backspace()  # best-effort del
                            elif burst == "\x1b":
                                key_modal["current"] = None
                                if chat_w:
                                    chat_w.add("system", "key entry cancelled")
                    elif 32 <= ch < 127:
                        kf.type_char(chr(ch))
                    _draw(); continue

                # ── access-modes popup intercepts keys + mouse while open ── ↑↓ move, ←→/Space
                # change, Enter apply, Esc cancel, m switches the mode preset, click a row to
                # focus+cycle it.
                if access_modal["current"] is not None:
                    af = access_modal["current"]
                    if ch == curses.KEY_MOUSE:
                        try:
                            _amid, _amx, _amy, _amz, _amb = curses.getmouse()
                        except curses.error:
                            _draw(); continue
                        _b1 = (getattr(curses, "BUTTON1_CLICKED", 0)
                               | getattr(curses, "BUTTON1_PRESSED", 0)
                               | getattr(curses, "BUTTON1_RELEASED", 0))
                        if _amb & _b1 and af._screen_y0 >= 0:
                            # content row clicked → focus that row, then cycle its value once.
                            _crow = _amy - af._screen_y0
                            _before = af.focus
                            af.click_row(_crow)
                            if af.focus != _before or _crow in af._click_rows:
                                af.cycle(1)
                        _draw(); continue
                    if ch in (9, curses.KEY_DOWN):                  # Tab / ↓ → next row
                        af.move(1)
                    elif ch in (getattr(curses, "KEY_BTAB", 353), curses.KEY_UP):  # Shift-Tab / ↑
                        af.move(-1)
                    elif ch in (curses.KEY_RIGHT, 32):             # → / Space → cycle value forward
                        af.cycle(1)
                    elif ch == curses.KEY_LEFT:                    # ← → cycle value back
                        af.cycle(-1)
                    elif ch in (ord("m"), ord("M")):               # m → switch mode preset
                        af.focus = 0; af.cycle(1)
                    elif ch in (curses.KEY_ENTER, 10, 13):
                        sig = af.submit()
                        if sig is not None:
                            _apply_access(sig[1])
                            access_modal["current"] = None
                            toasts.show(f"✓ access: {sig[1].summary()}", "success", ttl=2.0)
                    elif ch == 27:
                        _esc_burst()
                        access_modal["current"] = None
                        if chat_w:
                            chat_w.add("system", "access settings unchanged")
                    _draw(); continue

                # ── plan-approval modal intercepts keys while open ── interactive + scrollable
                if plan_modal["current"] is not None:
                    pa = plan_modal["current"]
                    if pa.mode == "text":
                        if ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                            _plan_signal(pa.submit_text())   # Enter/→ submits the modify text
                        elif ch in (curses.KEY_BACKSPACE, 127, 8):
                            if pa.text_buf:
                                pa.backspace()               # delete a char while there's text
                            else:
                                pa.cancel_text()             # empty + Backspace → back to choices
                        elif ch in (curses.KEY_LEFT, 27):
                            _esc_burst(); pa.cancel_text()   # ← / Esc → back to the choices
                        elif 32 <= ch < 127:
                            pa.type_char(chr(ch))
                        _draw(); continue
                    # choose mode: ↑/↓ pick action, PgUp/PgDn scroll the plan body
                    if ch in (curses.KEY_UP, ord("k")):
                        pa.move(-1)
                    elif ch in (curses.KEY_DOWN, ord("j")):
                        pa.move(1)
                    elif ch == curses.KEY_PPAGE:
                        pa.scroll_body(-max(1, pa.body_height - 1))
                    elif ch == curses.KEY_NPAGE:
                        pa.scroll_body(max(1, pa.body_height - 1))
                    elif ord("1") <= ch <= ord("9"):
                        _plan_signal(pa.number(ch - ord("0")))
                    elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                        _plan_signal(pa.activate())          # Enter/→ picks the highlighted action
                    elif ch == 27:
                        # Esc DISCARDS the pending plan — it must NOT clear the whole chat (bug the
                        # user hit). Same as the Discard action: drop the plan, keep the chat.
                        _esc_burst()
                        plan_modal["current"] = None
                        _drop_pending_plan()
                        if chat_w:
                            chat_w.add("system", "plan discarded")
                    _draw(); continue

                # ── minimap navigator intercepts keys while open ── ↑↓ roll the focus,
                # Enter jumps to the focused message (+ closes), Esc closes. (#25)
                if minimap_modal["active"] and chat_w and chat_w._rail is not None:
                    _n = len(chat_w._rail.index)
                    if ch in (curses.KEY_UP, ord("k")):
                        minimap_modal["focus"] = max(0, minimap_modal["focus"] - 1)
                        _draw(); continue
                    if ch in (curses.KEY_DOWN, ord("j")):
                        minimap_modal["focus"] = min(_n - 1, minimap_modal["focus"] + 1)
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13):
                        _f = max(0, min(minimap_modal["focus"], _n - 1))
                        if _f < _n:
                            chat_w.scroll_to_message(chat_w._rail.index[_f][0])
                            toasts.show("jumped to message", "info", ttl=1.0)
                        minimap_modal["active"] = False
                        _draw(); continue
                    if ch == 27:
                        _esc_burst()
                        minimap_modal["active"] = False
                        _draw(); continue
                    # any other key falls through (e.g. wheel handled in the mouse block)

                # ── effort slider intercepts keys while open (P19) ── model-aware (#13)
                if effort_modal["active"]:
                    from ..core.tui_model import EFFORT_SLIDER_LEVELS as _ESL
                    _lv = effort_modal.get("levels") or _ESL
                    if ch in (curses.KEY_LEFT, ord("h")):
                        effort_modal["idx"] = max(0, effort_modal["idx"] - 1)
                    elif ch in (curses.KEY_RIGHT, ord("l")):
                        effort_modal["idx"] = min(len(_lv) - 1, effort_modal["idx"] + 1)
                    elif ch in (curses.KEY_ENTER, 10, 13):
                        _apply_effort(_lv[max(0, min(effort_modal["idx"], len(_lv) - 1))])
                        effort_modal["active"] = False
                    elif ch == 27:
                        _esc_burst()
                        effort_modal["active"] = False
                    _draw(); continue

                # ── question wizard intercepts keys while open (P11) ──
                if wizard_modal["current"] is not None:
                    wz = wizard_modal["current"]
                    if ch == curses.KEY_MOUSE:        # P11: wire the advertised click support
                        try:
                            _mid, _mx, _my, _mz, _mb = curses.getmouse()
                        except curses.error:
                            _draw(); continue
                        _wizard_signal(wz.click_row(_my - getattr(wz, "_screen_y0", 0)), wz)
                        _draw(); continue
                    if wz.mode == "text":
                        if ch in (curses.KEY_ENTER, 10, 13):
                            wz.submit_text()
                        elif ch in (curses.KEY_BACKSPACE, 127, 8):
                            wz.backspace()
                        elif ch == 27:
                            _esc_burst(); wz.cancel_text()
                        elif 32 <= ch < 127:
                            wz.type_char(chr(ch))
                        _draw(); continue
                    if ch in (curses.KEY_UP, ord("k")):
                        wz.move(-1)
                    elif ch in (curses.KEY_DOWN, ord("j")):
                        wz.move(1)
                    elif ch == ord(" "):
                        wz.toggle_current()
                    elif ord("1") <= ch <= ord("9"):
                        sig = wz.number(ch - ord("0"))
                        _wizard_signal(sig, wz)
                    elif ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                        # Enter OR → selects/advances (consistent popup nav: → goes IN).
                        sig = wz.activate()
                        _wizard_signal(sig, wz)
                    elif ch in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
                        # ← OR Backspace goes BACK a step (and closes from the first step).
                        if not wz.back():
                            wizard_modal["current"] = None
                            _wizard_dismissed()      # unblock a clarify-question that's waiting
                    elif ch == 27:
                        _esc_burst()
                        wizard_modal["current"] = None
                        _wizard_dismissed()          # unblock a clarify-question that's waiting
                        if chat_w:
                            chat_w.add("system", "question cancelled")
                    _draw(); continue

                # ── message action popup intercepts keys while open ──
                if msg_menu["current"] is not None:
                    mm = msg_menu["current"]
                    if ch == 27:
                        msg_menu["current"] = None
                        _draw(); continue
                    if ch in (curses.KEY_UP, ord("k")):
                        mm.move(-1); _draw(); continue
                    if ch in (curses.KEY_DOWN, ord("j")):
                        mm.move(1); _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13):
                        action_id = mm.confirm()
                        idx = mm.msg_index
                        msg_menu["current"] = None
                        _do_msg_action(action_id, idx)
                        _draw(); continue
                    if 32 <= ch < 127:
                        aid = mm.hotkey(chr(ch))
                        if aid:
                            idx = mm.msg_index
                            msg_menu["current"] = None
                            _do_msg_action(aid, idx)
                        _draw(); continue
                    _draw(); continue

                # ── panel checklist intercepts keys while open ──
                if panel_menu["current"] is not None:
                    pm = panel_menu["current"]
                    if ch == 27:
                        # A bare ESC closes; but a split arrow sequence (ESC + "[A"/"[B")
                        # must move the selection, not close + leak the tail into the input.
                        burst = _esc_burst()
                        if burst == "\x1b":
                            panel_menu["current"] = None
                            _draw(); continue
                        for _k, _ in _split_csi(burst):
                            if _k == curses.KEY_UP:
                                pm.move(-1)
                            elif _k == curses.KEY_DOWN:
                                pm.move(1)
                        _draw(); continue
                    if ch in (curses.KEY_UP, ord("k")):
                        pm.move(-1); _draw(); continue
                    if ch in (curses.KEY_DOWN, ord("j")):
                        pm.move(1); _draw(); continue
                    if ch == ord(" "):
                        # space toggles the highlighted panel (menu stays open)
                        kind, on = pm.toggle_selected()
                        _apply_panel(kind, on)
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                        # enter / → = toggle + close (apply and dismiss)
                        kind, on = pm.toggle_selected()
                        _apply_panel(kind, on)
                        panel_menu["current"] = None
                        _draw(); continue
                    if ch in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
                        panel_menu["current"] = None   # ← / Backspace closes (back out)
                        _draw(); continue
                    _draw(); continue

                # ── chained command menu intercepts keys while open ──
                if cmd_menu["stack"] is not None and cmd_menu["stack"].is_open:
                    st = cmd_menu["stack"]
                    if ch == 27:
                        cmd_menu["stack"] = None
                        _draw(); continue
                    if ch in (curses.KEY_UP, ord("k")):
                        st.move(-1); _draw(); continue
                    if ch in (curses.KEY_DOWN, ord("j")):
                        st.move(1); _draw(); continue
                    if ch in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
                        if not st.back():
                            cmd_menu["stack"] = None
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                        leaf = st.enter()
                        if leaf is not None:  # leaf selected -> run + close
                            cmd_menu["stack"] = None
                            _run_menu_action(leaf.action)
                        _draw(); continue
                    _draw(); continue

                # ── overlay intercepts all keys while open ──
                if overlay["current"] is not None:
                    ov = overlay["current"]
                    if ch == 27:  # Esc OR an arrow that arrived as a split escape burst
                        burst = _esc_burst()
                        # Arrows reach us as escape bursts ('\x1b[D' etc.), so decode them here
                        # instead of treating the whole burst as a bare Esc. ← goes BACK to the
                        # parent overlay (model picker → roleboard); ↑↓ drive the list. A real
                        # bare Esc always CLOSES the whole overlay (familiar; never traps you in
                        # a drill-down) — going back one level is ←'s job, not Esc's.
                        _keys = _split_csi(burst)
                        if _keys:
                            for _k, _alt in _keys:
                                # #221: in the turn-diff browser, ←/→ step older/newer + re-render.
                                _tdh = getattr(ov, "turn_diff_hist", None)
                                if _tdh is not None and _k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                                    (_tdh.prev if _k == curses.KEY_LEFT else _tdh.next)()
                                    try:
                                        _sr, _sc = stdscr.getmaxyx()
                                    except Exception:  # noqa: BLE001
                                        _sc = 100
                                    from ..core.overlay import make_styled_overlay
                                    _nov = make_styled_overlay("turn diffs",
                                                               _tdh.render(min(88, max(40, _sc - 8))))
                                    _nov.turn_diff_hist = _tdh   # type: ignore[attr-defined]
                                    overlay["current"] = _nov
                                    ov = _nov
                                    continue
                                if _k == curses.KEY_LEFT and overlay["stack"]:
                                    overlay["current"] = overlay["stack"].pop()
                                    ov = overlay["current"]   # subsequent keys act on the parent
                                elif _k == curses.KEY_UP:
                                    ov.move(-1)
                                elif _k == curses.KEY_DOWN:
                                    ov.move(1)
                            _draw(); continue
                        if burst == "\x1b":
                            if ov.kind == "theme":
                                # cancel: restore the theme active when opened
                                from ..core.themes import set_theme as _st
                                if _st(ov.preview_orig):
                                    _load_colors()
                            overlay["current"] = None
                            overlay["stack"] = []   # closing drops the whole drill-down chain
                        _draw(); continue
                    # ← (delivered as a direct keycode by some terminals): back to the parent.
                    if ch == curses.KEY_LEFT and overlay["stack"]:
                        overlay["current"] = overlay["stack"].pop()
                        _draw(); continue
                    if ch == 5 and ov.kind == "skills":   # A4: Ctrl+E = enable/disable a skill
                        _sel = (ov.select.current() or "").split("  —")[0].strip()
                        if _sel and not _sel.startswith("("):
                            from ..core.plugin_loader import bundled_skills, discover_plugins
                            from ..core import installer as _inst
                            if _sel in {s.name for s in bundled_skills()}:
                                toasts.show(f"'{_sel}' is built-in — always on", "info", ttl=1.8)
                            else:
                                _owner = next((p for p in discover_plugins()
                                               if any(s.name == _sel for s in p.skills)), None)
                                if _owner is None:
                                    toasts.show("re-enable disabled plugins via /plugins", "info", ttl=2.0)
                                else:
                                    _ok, _msg = _inst.set_enabled(_owner.name, False)   # disable
                                    toasts.show(_msg, "success" if _ok else "error", ttl=2.0)
                                    from ..core.overlay import make_skills_overlay as _msko
                                    overlay["current"] = _msko()   # refresh the list
                        _draw(); continue
                    if ch in (curses.KEY_ENTER, 10, 13):
                        chosen = ov.confirm()
                        kind = ov.kind
                        _parent_ov = ov            # keep the just-confirmed overlay for drill-down
                        overlay["current"] = None
                        overlay["stack"] = []        # a confirmed pick closes the whole flow
                        if chosen and kind in ("model", "file", "command", "session"):
                            # M4: record the pick so it ranks higher next time (persisted)
                            try:
                                _frec.record(_frec_key(kind, chosen), _time.time())
                                _frec.save(_frec_path)
                            except Exception:  # noqa: BLE001
                                pass
                        if chosen:
                            if kind == "command":
                                from ..core.commands import name_from_label, command_for
                                name = name_from_label(chosen)
                                cmd = command_for(name)
                                if cmd and cmd.takes_arg and chat_w:
                                    # insert into the editor for arg entry
                                    chat_w.editor.clear()
                                    chat_w.editor.insert(name + " ")
                                else:
                                    _submit(name)
                            elif kind == "file" and chat_w:
                                pre = chat_w.editor.text[:chat_w.editor.cursor]
                                if pre and pre[-1].isalnum():
                                    chat_w.editor.insert(" ")
                                chat_w.editor.insert(chosen)
                            elif kind == "model":
                                # row is a column table: "model_id   provider   key" — the id is
                                # the first whitespace token. The FIRST row is the AUTO sentinel:
                                # selecting it UNPINS (back to automatic routing), not pins.
                                from ..core.overlay import AUTO_ROW
                                role = getattr(ov, "model_role", "") or ""
                                roles = [role] if role else ["planner", "executor", "reviewer"]
                                try:
                                    from ..core.overrides import Overrides
                                    o = Overrides.load()
                                    label = role if role else "all roles"
                                    if chosen.strip() == AUTO_ROW.strip():
                                        for r in roles:
                                            o.unpin_role(r)
                                        o.save()
                                        if chat_w:
                                            chat_w.add("system", f"{label} → AUTO (best-fit routing)")
                                        toasts.show(f"{label} → AUTO", "success")
                                    else:
                                        model_id = chosen.split()[0].strip() if chosen.split() else ""
                                        for r in roles:
                                            o.pin_role(r, model_id)
                                        o.save()
                                        short = model_id.split("/")[-1]
                                        if chat_w:
                                            chat_w.add("system", f"pinned {label} → {short}")
                                        toasts.show(f"{label} → {short}", "success")
                                except Exception as e:  # noqa: BLE001
                                    if chat_w:
                                        chat_w.add("system", f"pin failed: {e}")
                            elif kind == "theme":
                                # keep the highlighted theme (already applied via live
                                # preview); persist it for the session + confirm.
                                from ..core.themes import set_theme as _st
                                if _st(chosen):
                                    _load_colors()
                                    toasts.show(f"theme: {chosen}", "success")
                            elif kind == "roleboard":
                                # Enter on a role row -> drill into the picker to set MANUAL.
                                # Push the ROLEBOARD overlay (captured as _parent_ov BEFORE it
                                # was nulled above) so ← returns to the 4-role board instead of
                                # Esc closing everything (user).
                                role = (chosen.split() or [""])[0]
                                if role:
                                    from ..core.overlay import make_model_overlay
                                    overlay["stack"] = [_parent_ov]
                                    overlay["current"] = make_model_overlay(role)
                            elif kind == "session":
                                # row begins with the full session id; resume or fork it
                                # by delegating to the existing handlers (no duplication).
                                sid = chosen.split()[0].strip() if chosen.split() else ""
                                if sid:
                                    intent = getattr(ov, "action", "") or "resume"
                                    _submit(f"/fork {sid}" if intent == "fork" else f"/resume {sid}")
                            elif kind == "msgs":
                                # row begins with the transcript message index — scroll the chat to it
                                try:
                                    _mi = int(chosen.split()[0])
                                except (ValueError, IndexError):
                                    _mi = -1
                                if _mi >= 0 and chat_w and chat_w.scroll_to_message(_mi):
                                    toasts.show("jumped to message", "info", ttl=1.2)
                            elif kind == "search" and chat_w:
                                # #217: insert an @path#Lnn mention for the selected hit so the
                                # agent can read that exact location. Map the selected ROW back to
                                # its parsed (path, line, text) via the stashed results list.
                                from ..core.tui_model import search_mention
                                _res = getattr(ov, "search_results", []) or []
                                _si = ov.select.selected if 0 <= ov.select.selected < len(_res) else -1
                                if _si >= 0:
                                    _m = search_mention(_res[_si])
                                    _pre = chat_w.editor.text[:chat_w.editor.cursor]
                                    if _pre and _pre[-1].isalnum():
                                        chat_w.editor.insert(" ")
                                    chat_w.editor.insert(_m + " ")
                                    toasts.show(f"inserted {_m}", "info", ttl=1.4)
                            elif kind == "backtrack":
                                # row begins with the rollout index of the chosen turn;
                                # load its text for editing and arm the next submit to branch.
                                try:
                                    _idx = int(chosen.split()[0])
                                except (ValueError, IndexError):
                                    _idx = -1
                                _recs = _backtrack.get("records", [])
                                if 0 <= _idx < len(_recs) and chat_w:
                                    chat_w.editor.clear()
                                    chat_w.editor.insert(_recs[_idx].get("text", ""))
                                    _backtrack["turn_index"] = _idx
                                    _backtrack["pending"] = True
                                    toasts.show("edit it, then Enter to re-run as a branch",
                                                "info", ttl=1.8)
                            elif kind == "skills":
                                # A4: show the chosen skill's detail (name/desc/when-to-use)
                                _name = chosen.split("  —")[0].strip()
                                if _name and _name != "(no skills found)":
                                    from ..core.plugin_loader import bundled_skills, discover_plugins
                                    _alls = (list(bundled_skills())
                                             + [s for p in discover_plugins() for s in p.skills])
                                    sk = next((s for s in _alls if s.name == _name), None)
                                    if sk:
                                        det = [sk.name, ""]
                                        if getattr(sk, "description", ""):
                                            det += [sk.description, ""]
                                        if getattr(sk, "when_to_use", ""):
                                            det += ["When to use:", "  " + sk.when_to_use, ""]
                                        det.append("Auto-picked by the agentic executor; "
                                                   "force it with /agent <goal>.")
                                        _info_popup(f"skill: {sk.name}", "\n".join(det))
                            elif kind == "agents":
                                # #228: show the chosen agent's RICH inspector (when-to-use +
                                # tool surface + system prompt), not just name+description.
                                _name = chosen.split("  —")[0].strip()
                                if _name and not _name.startswith("(no agents"):
                                    from ..core.installer import installed_agents
                                    ag = next((a for a in installed_agents()
                                               if getattr(a, "name", "") == _name), None)
                                    if ag:
                                        from ..core.tui_model import render_agent_inspector
                                        try:
                                            _sr, _sc = stdscr.getmaxyx()
                                        except Exception:  # noqa: BLE001
                                            _sc = 100
                                        _rows = render_agent_inspector(
                                            name=getattr(ag, "name", _name),
                                            description=getattr(ag, "description", "") or "",
                                            tools=getattr(ag, "tools", []) or [],
                                            system_prompt=getattr(ag, "system_prompt", "") or "",
                                            width=min(84, max(40, _sc - 8)))
                                        from ..core.overlay import make_styled_overlay
                                        overlay["current"] = make_styled_overlay(
                                            f"agent · {getattr(ag, 'name', _name)}", _rows)
                            elif kind == "attachments":
                                # #128: Enter REMOVES the selected attachment. The row starts with
                                # its 1-based index; recover it, drop that one, and reopen the
                                # manager if any remain (so you can remove several in a row).
                                _tok = chosen.split()[0] if chosen and chosen.split() else ""
                                if _tok.isdigit():
                                    _attach_remove(int(_tok) - 1)
                                    if len(_attached["items"]) > 1:
                                        from ..core.overlay import make_attachments_overlay
                                        overlay["current"] = make_attachments_overlay(
                                            [i["label"] for i in _attached["items"]])
                        _draw(); continue
                    if ch == curses.KEY_UP:
                        ov.move(-1)
                        if ov.kind == "theme": _preview_theme(ov)
                        _draw(); continue
                    if ch == curses.KEY_DOWN:
                        ov.move(1)
                        if ov.kind == "theme": _preview_theme(ov)
                        _draw(); continue
                    if ch == curses.KEY_MOUSE:           # F33: scroll the list with the wheel
                        try:
                            _mb = curses.getmouse()[4]
                        except curses.error:
                            _draw(); continue
                        if _mb & getattr(curses, "BUTTON4_PRESSED", 0):
                            ov.scroll(-3)          # CLAMPED — no wrap-around flicker at the ends
                        elif _mb & getattr(curses, "BUTTON5_PRESSED", 0):
                            ov.scroll(3)
                        if ov.kind == "theme": _preview_theme(ov)
                        _draw(); continue
                    if ch == curses.KEY_PPAGE:
                        ov.scroll(-8)
                        if ov.kind == "theme": _preview_theme(ov)
                        _draw(); continue
                    if ch == curses.KEY_NPAGE:
                        ov.scroll(8)
                        if ov.kind == "theme": _preview_theme(ov)
                        _draw(); continue
                    if ov.kind == "roleboard":
                        # 'a' sets the selected role back to AUTO (unpin); board takes no typing
                        if ch in (ord("a"), ord("A")):
                            role = ((ov.select.current() or "").split() or [""])[0]
                            if role:
                                try:
                                    from ..core.overrides import Overrides
                                    o = Overrides.load(); o.unpin_role(role); o.save()
                                    toasts.show(f"{role} → AUTO", "success")
                                except Exception:  # noqa: BLE001
                                    pass
                                from ..core.overlay import make_roleboard_overlay
                                overlay["current"] = make_roleboard_overlay()
                        _draw(); continue
                    # info popups are read-only: no search/typing/backspace
                    if ov.kind != "info":
                        if ch in (curses.KEY_BACKSPACE, 127, 8):
                            ov.backspace()
                            if ov.kind == "theme": _preview_theme(ov)   # filter changes highlight
                            _draw(); continue
                        if 32 <= ch < 127:
                            ov.type_char(chr(ch))
                            if ov.kind == "theme": _preview_theme(ov)
                            _draw(); continue
                    else:
                        # any other key closes the info popup
                        if ch in (curses.KEY_ENTER, 10, 13, ord("q")):
                            overlay["current"] = None
                    _draw(); continue

                # ── ESC handling ──  single Esc (with queued msgs) = interrupt the
                # run, merge the queued messages and resend them together (P30);
                # double Esc (esc-esc) = interrupt the run, or quit when idle.
                if ch == 27:
                    burst = _esc_burst()
                    # ── bracketed paste (\x1b[200~ <content> \x1b[201~) ── insert as a
                    # paste chip / text, NOT a stray Esc. Before this, big pastes went
                    # char-by-char and the burst was discarded → the paste vanished (user [138]).
                    if "[200~" in burst:
                        if "[201~" not in burst:        # large paste may arrive in chunks
                            stdscr.timeout(60)
                            quiet = 0
                            try:
                                while "[201~" not in burst and len(burst) < 8_000_000:
                                    c = stdscr.getch()
                                    if c == -1:
                                        quiet += 1
                                        if quiet > 3:    # ~180ms silence → paste done/aborted
                                            break
                                        continue
                                    quiet = 0
                                    if 0 <= c <= 0x10FFFF:
                                        burst += chr(c)
                            finally:
                                stdscr.timeout(80)
                        body = burst.split("[200~", 1)[1]
                        if "[201~" in body:
                            body = body.split("[201~", 1)[0]
                        body = body.rstrip("\x1b")
                        if focused == "chat" and chat_w and body:
                            # Drag-and-drop a FILE (any type, #127): a terminal pastes the file PATH
                            # (not bytes) when you drag a file in. If the paste resolves to a single
                            # existing file, ATTACH it (_attach_src routes image→vision, text→context,
                            # binary→clear refusal) instead of inserting the path as text.
                            _dpath = None
                            try:
                                from ..core.multimodal import looks_like_file_path
                                _dpath = looks_like_file_path(body)
                            except Exception:  # noqa: BLE001
                                _dpath = None
                            if _dpath:
                                _attach_src(_dpath)
                            else:
                                chat_w.editor.paste(body)  # → "[paste #N]" chip (or inlined if small)
                                toasts.show("pasted", "info", ttl=0.8)
                        last_esc = False
                        _draw(); continue
                    # Shift+Enter variants are Esc-prefixed sequences, not a real Esc.
                    if (burst in ("\x1b\r", "\x1b\n")
                            or burst.endswith("[13;2u") or burst.endswith("[27;2;13~")
                            or burst.endswith("OM")):
                        if focused == "chat" and chat_w:
                            chat_w.editor.newline()
                        last_esc = False
                        _draw(); continue
                    # Ctrl+Shift+V (#128): terminals that DON'T bracket-paste on this chord deliver
                    # it as a CSI-u sequence (118='v'/86='V', modifier 6 = ctrl+shift). Treat it the
                    # same as Ctrl+V → attach an image from the clipboard. (When a terminal instead
                    # sends the clipboard TEXT as a bracketed paste, that's handled by the paste path
                    # above, so both chords "just work" everywhere.)
                    if burst.endswith("[118;6u") or burst.endswith("[86;6u"):
                        _attach_clipboard()
                        last_esc = False
                        _draw(); continue
                    # Alt+Z = REDO (#232) — the terminal-reachable counterpart to Ctrl+Z=undo,
                    # sent as a bare ESC-prefixed 'z'. Only while typing in the chat box.
                    if burst in ("\x1bz", "\x1bZ") and focused == "chat" and chat_w is not None:
                        chat_w.editor.redo()
                        last_esc = False
                        _draw(); continue
                    # A cursor/edit escape that ncurses handed us split (bare ESC + tail):
                    # decode it back to keycode(s) and run each through the SAME handling a
                    # directly-assembled key would get, so arrows / Home / End / Delete /
                    # Ctrl+arrow all work regardless of how the terminal delivered them — and
                    # a fast burst of several sequences (autorepeat) all land, not just one.
                    _csi_keys = _split_csi(burst)
                    if _csi_keys:
                        _w = registry.get(focused)
                        for _csi_key, _csi_alt in _csi_keys:
                            if (_csi_alt and focused == "chat" and chat_w
                                    and _csi_key in (curses.KEY_LEFT, curses.KEY_RIGHT)):
                                # Alt+←/→ = word-move (a readline alias for Ctrl+←/→).
                                (chat_w.editor.word_left if _csi_key == curses.KEY_LEFT
                                 else chat_w.editor.word_right)()
                                continue
                            if _csi_key == _CTRL_DEL:
                                # Ctrl+Delete = delete the word AHEAD (mirror of Ctrl+Backspace).
                                if focused == "chat" and chat_w:
                                    chat_w.editor.delete_word_right()
                                continue
                            if _w:
                                _w.handle_key(_csi_key)
                        last_esc = False
                        _draw(); continue
                    # Esc with text typed in the chat input = CANCEL the typed input (clear
                    # the buffer + close the slash palette), like readline. Without this, a
                    # user who types '/agent' then taps Esc to back out instead ARMS a quit —
                    # a second Esc then quit the whole app (user hit this: '/agent' + esc esc
                    # quit, then resume re-showed the old chat, looking like a surprise clear).
                    # Only when the input is already EMPTY does Esc fall through to arm/stop/quit.
                    if (not active and not controller.running()
                            and focused == "chat" and chat_w is not None
                            and not chat_w.editor.is_empty()):
                        chat_w.editor.clear()
                        last_esc = False
                        _draw(); continue
                    run_active = active or controller.running()
                    double = (burst == "\x1b\x1b") or (burst == "\x1b" and last_esc)
                    act = _esc_action(double=double, run_active=run_active, queued=len(_pending))
                    if act == "quit":
                        break
                    if act == "stop":
                        # INSTANT + COMPLETE stop (single-Esc no-queue, or double-Esc): detach the
                        # run NOW and cooperatively kill the orphaned thread, its tool loop, and any
                        # sub-agents at their next boundary — nothing survives in the background.
                        _do_complete_stop("stopped")
                        active = False
                        last_esc = False
                        _draw(); continue
                    if act == "flush":
                        # single Esc + queued msgs: ADD them into the ONGOING run (steer
                        # into its context), per the user's spec — keep the run going, do
                        # NOT start a separate run. If the run already ended, fall back to
                        # sending them as a fresh run so nothing is lost.
                        n = _flush_pending_to_run()
                        if n == 0 and _pending:
                            n = _interrupt_merge_resend()
                        last_esc = False
                        if n:
                            toasts.show(f"added {n} message{'s' if n != 1 else ''} to the run",
                                        "info", ttl=1.2)
                        _draw(); continue
                    # act == "arm": single Esc, nothing to flush -> arm for a following Esc
                    last_esc = True
                    _esc_time = _time.time()
                    _draw(); continue

                if last_esc and (_time.time() - _esc_time) > 0.6:
                    last_esc = False

                # ── global hotkeys ──
                # Shift+Tab = cycle the RUN MODE (Plan → Ask → Edit → Auto). Reached only when no
                # modal owns input (modals intercept + `continue` above), so it never fights a
                # popup's own Shift+Tab field-nav. The active mode shows as a chip in the top bar.
                if ch in (getattr(curses, "KEY_BTAB", 353), 353):
                    _cycle_mode(1)
                    _draw(); continue

                # Tab = autofill the selected /command when the palette is open; else cycle focus
                # (chat always first, then visible panels).
                if ch == 9:
                    if focused == "chat" and chat_w is not None and chat_w.fill_slash():
                        _draw(); continue          # filled the highlighted slash command
                    visible = ["chat"] + [n for n in focusable_names
                                          if n != "chat" and _layout_has(state["layout"], n)]
                    idx = visible.index(focused) if focused in visible else -1
                    focused = visible[(idx + 1) % len(visible)]
                    _draw(); continue

                # Ctrl+P = chained command menu (Models/Themes/Layouts/Panels/...)
                if ch == 16:
                    _open_cmd_menu()
                    _draw(); continue

                # Ctrl+T = file picker
                if ch == 20:
                    from ..core.overlay import make_file_overlay
                    overlay["current"] = make_file_overlay(os.getcwd())
                    _draw(); continue

                # Ctrl+E = panel checklist (summon/remove right-side panels)
                if ch == 5:
                    from ..core.panel_menu import PanelMenu
                    pm = PanelMenu()
                    # pre-check panels already in the layout
                    for kind, _label in __import__("syntra.core.panel_menu",
                                                   fromlist=["PANELS"]).PANELS:
                        if _layout_has(state["layout"], kind):
                            pm.enabled.add(kind)
                    panel_menu["current"] = pm
                    _draw(); continue

                # Ctrl+Y — SMART GATE (#231): while typing in the chat box it's the readline
                # kill-ring YANK (handled by the chat widget below); on an EMPTY box it keeps
                # its existing meaning, "copy last reply". So the copy-last habit is preserved
                # and yank lands where a shell user expects it.
                if ch == 25 and not (focused == "chat" and chat_w and not chat_w.editor.is_empty()):
                    _copy_last_reply()
                    _draw(); continue

                # Ctrl+V = attach an IMAGE from the system clipboard (a screenshot you copied).
                # Terminals deliver a TEXT paste as a bracketed-paste burst (handled above), so a
                # bare Ctrl+V here means "no text paste pending" → try the clipboard for an image.
                if ch == 22:
                    _attach_clipboard()
                    _draw(); continue

                # ":" on empty chat input = chained command menu
                if ch == ord(":") and focused == "chat" and chat_w and chat_w.editor.is_empty():
                    _open_cmd_menu()
                    _draw(); continue

                # F31: ◂ ▸ on EMPTY chat input flips the most recent assistant turn
                # that has alternative outputs (Retry stores them in-place). Guarded by
                # is_empty() so it never steals cursor movement while you're typing.
                if (ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and focused == "chat"
                        and chat_w and chat_w.editor.is_empty()):
                    _tr = chat_w.transcript
                    _flipped = False
                    for _k in range(len(_tr.messages) - 1, -1, -1):
                        _mm = _tr.messages[_k]
                        if (_mm.role in ("assistant", "assistant_stream")
                                and len(getattr(_mm, "variants", None) or []) > 1):
                            _tr.cycle_variant(_k, -1 if ch == curses.KEY_LEFT else +1)
                            _flipped = True
                            break
                    if _flipped:
                        _draw(); continue
                    # no alternatives to flip → let the key fall through to normal handling

                # "@" on chat = file picker
                if ch == ord("@") and focused == "chat" and chat_w:
                    from ..core.overlay import make_file_overlay
                    overlay["current"] = make_file_overlay(os.getcwd())
                    _draw(); continue

                # Ctrl+D = quit
                if ch == 4:
                    break

                # Ctrl+B = toggle file tree sidebar
                if ch == 2:
                    has_tree = _layout_has(state["layout"], "file_tree")
                    if has_tree:
                        _dismiss_panel("file_tree")
                        toasts.show("sidebar off", "info", ttl=0.8)
                    else:
                        _summon_panel("file_tree")
                        toasts.show("sidebar on", "info", ttl=0.8)
                    _draw(); continue

                # Ctrl+K = kill running task (INSTANT, like Esc-stop). But while you're
                # TYPING in the chat box with nothing running, Ctrl+K is readline
                # kill-to-end-of-line instead — so it never eats text you're editing.
                if ch == 11:
                    _run_live = active or controller.running()
                    if (not _run_live) and focused == "chat" and chat_w and not chat_w.editor.is_empty():
                        chat_w.editor.kill_to_end(); _draw(); continue
                    if controller.hard_stop():
                        _pending.clear()
                        active = False
                        if chat_w:
                            chat_w.working = False
                            chat_w.add("system", "⏹ Stopped.")
                            # BUG3: freeze any live "Editing…" action-feed line on stop
                            if getattr(chat_w, "action_feed", None) is not None:
                                chat_w.action_feed.freeze_running()
                        if activity_w:
                            activity_w.log("interrupted by user", "error")
                        toasts.show("stopped", "info", ttl=1.0)
                    _draw(); continue

                # Ctrl+L = clear chat
                if ch == 12:
                    if chat_w:
                        chat_w.transcript.messages.clear()
                        chat_w.transcript.scroll = 0
                        chat_w.transcript._follow = True
                        chat_w.add("system", "cleared.")
                    if agent_w:
                        agent_w.reset()
                    if tree_w:
                        tree_w.reset()
                    _draw(); continue

                # Ctrl+O = newline in chat
                if ch == 15 and focused == "chat" and chat_w:
                    chat_w.editor.newline()
                    _draw(); continue

                # Ctrl+R = backtrack: step back to a past user turn, edit + re-run as a branch
                if ch == 18:
                    _open_backtrack()
                    _draw(); continue

                # +/- = resize focused panel (when not in chat or chat is empty)
                if ch in (ord("+"), ord("=")) and focused != "chat":
                    if _resize_focused(+0.2):
                        toasts.show("panel wider", "info", ttl=0.8)
                    _draw(); continue
                if ch == ord("-") and focused != "chat":
                    if _resize_focused(-0.2):
                        toasts.show("panel narrower", "info", ttl=0.8)
                    _draw(); continue
                # Ctrl+←/→ resize the FOCUSED panel (when not in chat, where ctrl+arrow is
                # word-move). Ctrl+right = wider, Ctrl+left = narrower (#8 interactive resize).
                if focused != "chat" and ch in (561, getattr(curses, "KEY_SRIGHT", -99)):
                    if _resize_focused(+0.2):
                        toasts.show("panel wider", "info", ttl=0.8)
                    _draw(); continue
                if focused != "chat" and ch in (546, getattr(curses, "KEY_SLEFT", -98)):
                    if _resize_focused(-0.2):
                        toasts.show("panel narrower", "info", ttl=0.8)
                    _draw(); continue
                # < / > MOVE/reorder the focused panel among its siblings (#8 move/reorder).
                # Plain ASCII so it works on every terminal (Shift+arrow codes vary wildly);
                # only active when a side PANEL is focused, never while typing in chat.
                if focused != "chat" and ch in (ord("<"), ord(",")):
                    if _move_focused(-1):
                        toasts.show("panel moved left", "info", ttl=0.8)
                    _draw(); continue
                if focused != "chat" and ch in (ord(">"), ord(".")):
                    if _move_focused(+1):
                        toasts.show("panel moved right", "info", ttl=0.8)
                    _draw(); continue

                # Mouse
                if ch == curses.KEY_MOUSE:
                    try:
                        _id, mx, my, _mz, bstate = curses.getmouse()
                    except curses.error:
                        _draw(); continue
                    rows, cols = stdscr.getmaxyx()
                    # the question wizard accepts CLICKS on its options (P11 advertises
                    # "click"): map the click row to a wizard row + activate it.
                    if wizard_modal["current"] is not None:
                        wz = wizard_modal["current"]
                        B1 = (getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
                              | getattr(curses, "BUTTON1_RELEASED", 0))   # click-only mode reports RELEASED
                        if (bstate & B1) and wz._screen_y0 >= 0:
                            sig = wz.click_row(my - wz._screen_y0)
                            _wizard_signal(sig, wz)
                        _draw(); continue
                    # the plan-approval modal accepts CLICKS on its Approve/Modify/Discard rows.
                    # Include BUTTON1_RELEASED: in the default click-only mouse mode many terminals
                    # report a tap as RELEASED (not CLICKED/PRESSED), so without it the popup looked
                    # "unclickable" (#134a). Also try a nearest-row fallback so a click a row off an
                    # action still resolves.
                    if plan_modal["current"] is not None:
                        pa = plan_modal["current"]
                        B1 = (getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
                              | getattr(curses, "BUTTON1_RELEASED", 0))
                        if (bstate & B1) and pa._screen_y0 >= 0:
                            _crow = my - pa._screen_y0
                            _sig = pa.click_row(_crow)
                            if _sig is None:                       # nearest-row fallback (±1)
                                for _d in (1, -1):
                                    _sig = pa.click_row(_crow + _d)
                                    if _sig is not None:
                                        break
                            if _sig is not None:
                                _plan_signal(_sig)
                        _draw(); continue
                    # the API-key popup accepts CLICKS on its fields → focus that field
                    if key_modal["current"] is not None:
                        kf = key_modal["current"]
                        B1 = (getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
                              | getattr(curses, "BUTTON1_RELEASED", 0))
                        if (bstate & B1) and kf._screen_y0 >= 0:
                            kf.click_row(my - kf._screen_y0)
                        _draw(); continue
                    # don't let clicks leak to widgets beneath ANY other open modal
                    # (the keyboard handlers already intercept these; the mouse must too)
                    if (effort_modal["active"]
                            or perm_modal["req"] is not None
                            or overlay["current"] is not None
                            or plan_modal["current"] is not None or key_modal["current"] is not None
                            or panel_menu["current"] is not None
                            or (cmd_menu["stack"] is not None and cmd_menu["stack"].is_open)):
                        _draw(); continue
                    body_rect = Rect(0, 1, cols, rows - 2)
                    _al = _focus_layout() if cols < 90 else state["layout"]
                    rects = resolve_layout(_al, body_rect)

                    # Close message popup on any click
                    if msg_menu["current"] is not None:
                        msg_menu["current"] = None
                        _draw(); continue

                    B1_PRESS = getattr(curses, "BUTTON1_PRESSED", 0)
                    B1_RELEASE = getattr(curses, "BUTTON1_RELEASED", 0)
                    B1_CLICK = getattr(curses, "BUTTON1_CLICKED", 0)
                    B3_CLICK = getattr(curses, "BUTTON3_CLICKED", 0)
                    B3_PRESS = getattr(curses, "BUTTON3_PRESSED", 0)
                    WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0)
                    WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0)

                    # wheel rolls the minimap NAVIGATOR when it's open ("roll through the
                    # scroll button" — user), else scrolls the panel under the cursor.
                    if (bstate & (WHEEL_UP | WHEEL_DOWN)) and minimap_modal["active"] \
                            and chat_w and chat_w._rail is not None:
                        _n = len(chat_w._rail.index)
                        if bstate & WHEEL_UP:
                            minimap_modal["focus"] = max(0, minimap_modal["focus"] - 1)
                        else:
                            minimap_modal["focus"] = min(_n - 1, minimap_modal["focus"] + 1)
                        _draw(); continue
                    # wheel → scroll the panel under the cursor; if the cursor is
                    # over chrome (top/bottom bar) or nothing, scroll the chat so the
                    # conversation always responds to the wheel (P36).
                    if bstate & (WHEEL_UP | WHEEL_DOWN):
                        # A CONTENT-anchored chat selection survives the scroll (#89): it re-projects
                        # to the new scroll position each paint, so you can scroll to bring more into
                        # view and the highlight stays on the same text. A screen-anchored selection
                        # would now sit on the WRONG text, so it's dropped as before.
                        if _sel["mode"] != "content":
                            _sel["active"] = False
                            _sel["show"] = False
                        hit = None
                        for name, rect in rects.items():
                            if rect.contains(mx, my):
                                hit = (name, rect); break
                        if hit is None and chat_w and "chat" in rects:
                            hit = ("chat", rects["chat"])
                        if hit:
                            name, rect = hit
                            widget = registry.get(name)
                            if widget:
                                widget.handle_mouse(mx - rect.x, my - rect.y, bstate)
                        _draw(); continue

                    # Click the 📎 attachment chip on the TOP bar (#128): with ONE attachment,
                    # clear it; with MULTIPLE, open the attachment manager so you can remove one at
                    # a time (user: "remove individual attachments from multiple"). The chip stays
                    # clickable, not just informational.
                    if ((bstate & (B1_CLICK | B1_PRESS | B1_RELEASE)) and my == 0
                            and _attached["items"]):
                        _row0 = _screen_text[0] if _screen_text else ""
                        _pin = _row0.find("📎")
                        # the chip occupies from the 📎 glyph to the next double-space gap
                        if _pin >= 0 and mx >= _pin:
                            _gap = _row0.find("  ", _pin)
                            _end = _gap if _gap > _pin else len(_row0)
                            if mx < _end:
                                if len(_attached["items"]) == 1:
                                    _attach_clear()
                                    toasts.show("attachment removed", "info", ttl=1.2)
                                else:
                                    from ..core.overlay import make_attachments_overlay
                                    overlay["current"] = make_attachments_overlay(
                                        [i["label"] for i in _attached["items"]])
                                _draw(); continue

                    # F22: click the bottom status bar (right side — the "▸ N agents"
                    # area) → open the agents panel (or focus it if already open). ONLY when
                    # agents actually exist: opening an empty "no agents running" panel on a
                    # stray bottom-bar click was confusing (user). If the panel is already open,
                    # a click still focuses it; otherwise the click falls through to normal
                    # press/select handling so it isn't swallowed.
                    if ((bstate & (B1_CLICK | B1_PRESS | B1_RELEASE)) and my == rows - 1
                            and mx > cols // 3):
                        _have_agents = bool(agent_w and agent_w.agents)
                        if _layout_has(state["layout"], "agent_status"):
                            focused = "agent_status"
                            _draw(); continue
                        if _have_agents:
                            _summon_panel("agent_status")
                            toasts.show("agents panel — click an agent to see its work", "info", ttl=1.4)
                            _draw(); continue
                        # no agents + panel not open → not an agents-bar click; fall through

                    # right-click on a chat message → action popup
                    if bstate & (B3_CLICK | B3_PRESS):
                        idx = _msg_index_at(rects, mx, my)
                        if idx is not None:
                            from ..core.msg_menu import MessageMenu
                            msgs = chat_w.transcript.messages if chat_w else []
                            role = getattr(msgs[idx], "role", "user") if 0 <= idx < len(msgs) else "user"
                            msg_menu["current"] = MessageMenu(msg_index=idx, role=role)
                            msg_menu["x"] = mx
                            msg_menu["y"] = my
                        _draw(); continue

                    # left button press → start a drag-select. Some terminals fire
                    # PRESS repeatedly during a drag; only ANCHOR on the first one
                    # (else the anchor follows the cursor and the range collapses).
                    if bstate & B1_PRESS:
                        # If a scrollbar drag is already live, a repeated PRESS report (some
                        # terminals fire PRESS, not motion, while held) just continues it —
                        # even if the cursor has slid off the column.
                        if _sb_drag["active"]:
                            _scrollbar_drag_to(rects, my)
                            _draw_coalesced(); continue
                        # A press on the chat SCROLLBAR column starts a scroll drag (most
                        # specific — checked before divider/select). Jump to the pressed
                        # position immediately, then motion drags the thumb.
                        if (not _sel["active"] and not _resize_drag["active"]
                                and _on_scrollbar(rects, mx, my)):
                            _sb_drag["active"] = True
                            _set_drag_motion(True)   # stream held-motion for a SMOOTH drag
                            _scrollbar_drag_to(rects, my)
                            _draw_coalesced(); continue
                        # U1 (#8): a press ON a panel divider starts a RESIZE drag, not a
                        # text selection. Motion then re-weights the panel left of the divider.
                        if not _sel["active"] and not _resize_drag["active"]:
                            _dname = _divider_at(rects, mx)
                            if _dname:
                                _hsp = _find_main_hsplit(state["layout"])
                                _w0 = next((getattr(c, "weight", 1.0) for c in (_hsp.children if _hsp else [])
                                            if (isinstance(c, LayoutLeaf) and c.widget_name == _dname)
                                            or (isinstance(c, LayoutSplit) and _layout_has(c, _dname))), 1.0)
                                _resize_drag.update(active=True, left=_dname, anchor_x=mx,
                                                    w0=_w0, px=mx)
                                _draw(); continue
                        if _resize_drag["active"]:
                            # held-button drag report → apply the incremental column delta
                            _set_leaf_weight(_resize_drag["left"], mx - _resize_drag["px"], cols)
                            _resize_drag["px"] = mx
                            _draw_coalesced(); continue   # firehose: throttle the repaint
                        if not _sel["active"]:
                            _sel["active"] = True
                            _sel["x0"] = mx; _sel["y0"] = my
                            # remember the box the drag started in, so the selection
                            # stays inside it and can't bleed across the screen (F23).
                            _sel["cx0"], _sel["cx1"] = 0, cols
                            for _rr in rects.values():
                                if _rr.contains(mx, my):
                                    _sel["cx0"], _sel["cx1"] = _rr.x, _rr.x + _rr.w
                                    break
                            # #89: a press in the CHAT content area starts a CONTENT-anchored
                            # selection (survives scroll, copy reaches off-screen) and turns on
                            # button-held motion so a drag past the edge streams + auto-scrolls.
                            # Anywhere else stays screen-anchored (today's behavior).
                            _cpt = _chat_point_to_content(mx, my, rects)
                            if _cpt is not None:
                                _sel["mode"] = "content"
                                _sel["cl0"], _sel["ccol0"] = _cpt
                                _sel["cl1"], _sel["ccol1"] = _cpt
                                _set_drag_motion(True)
                            else:
                                _sel["mode"] = "screen"
                        if _sel["mode"] == "content":
                            _set_chat_endpoint(mx, my, rects)
                        else:
                            _sel["x1"] = mx; _sel["y1"] = my
                        _sel["show"] = False
                        _draw_coalesced(); continue       # firehose: throttle the repaint

                    # left button release → finish a resize drag, else finish select/click
                    if bstate & B1_RELEASE:
                        if _sb_drag["active"]:
                            _scrollbar_drag_to(rects, my)
                            _sb_drag["active"] = False
                            _set_drag_motion(False)   # revert to click-only (native selection back)
                            _draw(); continue
                        if _resize_drag["active"]:
                            _set_leaf_weight(_resize_drag["left"], mx - _resize_drag["px"], cols)
                            _resize_drag["active"] = False
                            toasts.show("panel resized", "info", ttl=0.8)
                            _draw(); continue
                        if _sel["active"]:
                            _content = _sel["mode"] == "content"
                            if _content:
                                _set_chat_endpoint(mx, my, rects)
                                _set_drag_motion(False)   # done dragging → hand selection back
                                moved = ((_sel["cl1"], _sel["ccol1"])
                                         != (_sel["cl0"], _sel["ccol0"]))
                            else:
                                _sel["x1"] = mx; _sel["y1"] = my
                                moved = (_sel["x1"], _sel["y1"]) != (_sel["x0"], _sel["y0"])
                            _sel["active"] = False
                            _sel["edge"] = 0             # stop idle-tick auto-scroll on release
                            if moved:
                                _sel["show"] = _copy_selection()  # keep it lit
                            else:
                                _sel["show"] = False
                                _click_at(rects, mx, my)
                        else:
                            _click_at(rects, mx, my)
                        _draw(); continue

                    if bstate & B1_CLICK:
                        # a "click" with a recorded press that moved = a fast drag. The press
                        # always recorded x0/y0 (both modes), so the screen-delta test holds.
                        _content = _sel["mode"] == "content"
                        if _sel["active"] and (mx != _sel["x0"] or my != _sel["y0"]):
                            if _content:
                                _set_chat_endpoint(mx, my, rects)
                                _set_drag_motion(False)
                            else:
                                _sel["x1"] = mx; _sel["y1"] = my
                            _sel["active"] = False
                            _sel["edge"] = 0
                            _sel["show"] = _copy_selection()
                        else:
                            if _content:
                                _set_drag_motion(False)
                            _sel["active"] = False
                            _sel["edge"] = 0
                            _sel["show"] = False
                            _click_at(rects, mx, my)
                        _draw(); continue

                    # motion: drive an ACTIVE drag (scrollbar/resize/select) — these report
                    # while a button is held. Otherwise it's a pure HOVER: update the last-
                    # hovered cell and only redraw when it MOVED to a new cell, so sweeping the
                    # mouse across the screen doesn't repaint every event (the lag fix).
                    if _sb_drag["active"]:
                        _scrollbar_drag_to(rects, my)
                        _draw_coalesced(); continue       # firehose: throttle the repaint
                    if _resize_drag["active"]:
                        _set_leaf_weight(_resize_drag["left"], mx - _resize_drag["px"], cols)
                        _resize_drag["px"] = mx
                        _draw_coalesced(); continue       # firehose: throttle the repaint
                    if _sel["active"]:
                        if _sel["mode"] == "content":
                            _set_chat_endpoint(mx, my, rects)   # follows cursor + auto-scrolls
                        else:
                            _sel["x1"] = mx; _sel["y1"] = my
                        _draw_coalesced(); continue        # firehose: throttle the repaint
                    # pure hover — no button held. Drive the minimap rail: hovering the
                    # right-edge rail expands the rolling-wheel panel + focuses the message
                    # under the cursor; moving off the rail AND its panel collapses it.
                    _on_rail = _minimap_on_rail(rects, mx, my)
                    _on_panel = bool(minimap_modal["active"] and minimap_modal.get("geom")
                                     and minimap_modal["geom"][0] <= my < minimap_modal["geom"][0]
                                     + minimap_modal["geom"][2]
                                     and mx >= minimap_modal["geom"][1])
                    if _on_rail:
                        _was = (minimap_modal["active"], minimap_modal["focus"])
                        minimap_modal["active"] = True
                        minimap_modal["focus"] = _minimap_focus_for_y(rects, my)
                        _hover["x"], _hover["y"] = mx, my
                        # a state change (just opened / focus moved) paints now; otherwise throttle
                        (_draw if _was != (True, minimap_modal["focus"]) else _draw_coalesced)()
                        continue
                    if minimap_modal["active"] and not _on_panel:
                        minimap_modal["active"] = False
                        _hover["x"], _hover["y"] = mx, my
                        _draw(); continue                  # discrete collapse → paint now
                    if (mx, my) != (_hover["x"], _hover["y"]):
                        _hover["x"], _hover["y"] = mx, my
                        _draw_coalesced()                  # firehose: hover highlight, throttled
                    continue

                # ── forward to focused widget ──
                widget = registry.get(focused)
                if widget and widget.handle_key(ch):
                    _draw(); continue

                _draw()

        except _Quit:
            pass
        finally:
            # Record the session for the resume hint (printed by run_tui2 AFTER curses
            # restores the terminal, so it shows on every exit path — Ctrl+C included).
            session["task_id"] = last_task_id
            session["cost"] = cost
            session["tokens"] = tokens_in + tokens_out
            # Persist the WHOLE conversation so `resume` can reload ALL of it (each
            # message is its own task, so this is the only place the full chat exists).
            try:
                if chat_w and last_task_id:
                    import json as _json
                    _tdir = _state_root() / "transcripts"
                    _tdir.mkdir(parents=True, exist_ok=True)
                    _keep = ("user", "assistant", "assistant_stream", "system")
                    _msgs = [{"role": getattr(m, "role", ""), "text": getattr(m, "text", "")}
                             for m in chat_w.transcript.messages
                             if getattr(m, "role", "") in _keep and (getattr(m, "text", "") or "").strip()]
                    (_tdir / f"{last_task_id}.json").write_text(_json.dumps(_msgs))
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass
            # also snapshot the append-only rollout (the resume/fork/backtrack pickers
            # read THIS, not the legacy transcript dump above) — covers pure-chat turns
            # that never triggered a run-completion persist (A2.4).
            try:
                _persist_rollout(last_task_id)
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass
            # #232: persist the input history so ↑/Ctrl-R recall survives the next launch.
            try:
                if chat_w:
                    chat_w.editor.save_history(_history_path)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            # Stop any recurring /loop thread so it doesn't outlive the session.
            _loop_ev = state.get("_loop_stop")
            if _loop_ev:
                _loop_ev.set()
            _set_bracketed(False)
            try:
                # Always disable any-motion + button-event tracking on exit — covers both the
                # global motion modes AND the transient ?1002h we toggle during a scrollbar
                # drag (so a crash mid-drag can't leave motion tracking stuck on the terminal).
                # Harmless to send even if neither was ever enabled.
                _sys.stdout.write("\x1b[?1003l\x1b[?1002l")
                _sys.stdout.flush()
            except Exception:
                pass
            if _kitty_protocol:
                try:
                    _sys.stdout.write("\x1b[<u")  # disable kitty protocol
                    _sys.stdout.flush()
                except Exception:
                    pass
            _set_title("")
            # #250(b): drain any buffered input (a half-read mouse/paste escape burst) before
            # curses restores the terminal — otherwise those bytes spill onto the SHELL prompt
            # after exit as literal garbage (e.g. a stray `<35;…M`). curses.wrapper resets modes
            # + endwin but does NOT flush the input queue.
            try:
                curses.flushinp()
            except Exception:  # noqa: BLE001 - best-effort; never block a clean exit
                pass

        return 0

    def _print_resume_hint():
        """Clear, unmissable 'how to continue' note. Printed AFTER curses exits, on
        EVERY exit path, so the user always knows their session is saved + the command."""
        try:
            out = _sys.stdout
            tid = session.get("task_id") or ""
            if tid:
                bar = "  " + "─" * 52
                out.write("\n" + bar + "\n")
                out.write("\033[1m  ✓ This session is saved — you can continue it later.\033[0m\n")
                if session.get("cost", 0) > 0:
                    out.write(f"    used ${session['cost']:.4f}  ·  {session.get('tokens', 0)} tokens\n")
                out.write("    To continue, just run:  \033[96msyntra resume\033[0m"
                          "   \033[2m(no id needed — picks up this session)\033[0m\n")
                out.write(f"    Or a specific one:      \033[96msyntra resume {tid}\033[0m\n")
                out.write(bar + "\n\n")
            else:
                out.write("\n  \033[2mNo task ran this session — nothing to resume.\033[0m\n\n")
            out.flush()
        except Exception:
            pass

    # #250(c): route stderr to a buffer DURING the curses session so a dependency/thread that
    # writes a raw warning (or an escape sequence) can't corrupt the alt-screen mid-frame.
    # Restored on exit; anything captured is written to a log the user can inspect. Our own
    # subprocesses already capture/DEVNULL their stderr — this catches third-party noise.
    import io as _io
    _real_stderr = _sys.stderr
    _err_buf = _io.StringIO()
    try:
        _sys.stderr = _err_buf
        try:
            return curses.wrapper(_main)
        except KeyboardInterrupt:
            return 0
    finally:
        _sys.stderr = _real_stderr
        _captured = _err_buf.getvalue()
        if _captured.strip():
            try:
                _logp = _state_root() / "tui_stderr.log"
                _logp.parent.mkdir(parents=True, exist_ok=True)
                with open(_logp, "a", encoding="utf-8") as _lf:
                    _lf.write(_captured)
            except Exception:  # noqa: BLE001 - logging stderr is best-effort
                pass
        _print_resume_hint()    # ALWAYS — clean quit, Ctrl+C, or exception
