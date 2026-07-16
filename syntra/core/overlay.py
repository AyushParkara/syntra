"""Modal overlay for the widget TUI (command palette / file picker).

A floating, centered box that captures all input while open. Pure state +
render math; the curses layer paints overlay_box() centered on screen.

Two kinds:
- "command": fuzzy-filtered slash commands
- "file":    fuzzy-filtered workspace files

Both reuse SelectList for the fuzzy filter + navigation; this wraps it with
a title, a query line, and a bordered box. The TUI forwards keys here while
an overlay is open and reads .result() when the user presses Enter.
"""

from __future__ import annotations

from dataclasses import dataclass
from .select_list import SelectList


@dataclass
class Overlay:
    kind: str                  # "command" | "file" | "model" | "roleboard" | "info" | "session"
    title: str
    select: SelectList
    chosen: str | None = None
    cancelled: bool = False
    model_role: str = ""       # for kind="model": which role to pin (or "" = all)
    action: str = ""           # for kind="session": intent — "resume" | "fork"
    preview_orig: str = ""     # for kind="theme": theme active when opened (restore on cancel)
    # #218: optional per-content-line theme roles for a RICH info popup (dashboards need
    # color hierarchy — accent headers, dim captions, a density-ramp heatmap — not one flat
    # tint). Parallel to the SelectList items; empty ⇒ classic flat "default" rendering, so
    # every existing make_info_overlay caller is unchanged.
    line_styles: list = None   # type: ignore[assignment]  (list[str] | None)

    def type_char(self, ch: str) -> None:
        self.select.type_char(ch)

    def backspace(self) -> None:
        self.select.backspace()

    def move(self, delta: int) -> None:
        self.select.move(delta)

    def scroll(self, delta: int) -> None:   # clamped (wheel/page) — no wrap-around flicker
        self.select.scroll(delta)

    def confirm(self) -> str | None:
        self.chosen = self.select.current()
        return self.chosen

    def cancel(self) -> None:
        self.cancelled = True


def make_command_overlay() -> Overlay:
    from .commands import command_labels
    return Overlay(
        kind="command",
        title="commands",
        select=SelectList(command_labels(), height=12),
    )


def make_file_overlay(root: str = ".") -> Overlay:
    from .files import list_workspace_files
    try:
        files = list_workspace_files(root)
    except Exception:  # noqa: BLE001
        files = []
    return Overlay(kind="file", title="files", select=SelectList(files, height=12))


def _credential_label(endpoint) -> str:
    """Return safe credential state for UI display without reading key text."""
    state = getattr(endpoint, "credential_state", "")
    if state == "keyed":
        return "configured"
    if state == "no-auth":
        return "no-auth"
    return "missing"


# The picker's first row returns to automatic routing (unpin) instead of pinning a model.
# Sentinel the TUI recognizes as "this row means AUTO". Its first token is NOT a model id.
AUTO_ROW = "⟳ Auto  ·  let Syntra pick the best model per task"


def make_model_overlay(role: str = "") -> Overlay:
    """Searchable model picker: each row is 'model_id  ·  provider  ·  credential state'.

    The FIRST row is always "⟳ Auto" — selecting it UNPINS the role (returns to automatic
    best-fit routing), so a manual choice is never a dead end (user: once a model is chosen
    there was no way back to auto). Every other row pins that model. When `role` is set the
    action applies to that role; otherwise to all roles. Rows are built from the catalog
    filtered to models that resolve to a provider, so you only see models you can use.
    """
    import os
    rows: list[str] = [AUTO_ROW]
    try:
        from .catalog import Catalog
        from .registry import ProviderRegistry
        cat_path = os.environ.get("SYNTRA_CATALOG_PATH")
        cat = Catalog.load(cat_path) if cat_path else _bundled_catalog()
        reg = ProviderRegistry.load()
        # For each model that resolves to a provider, collect safe display metadata.
        _raw: list[tuple[str, str, str]] = []
        for m in sorted(cat.models, key=lambda x: -x.intelligence_index):
            ep = reg.find_for_model(m.id)
            if ep is None:
                continue
            _raw.append((m.id, ep.name, _credential_label(ep)))
        # …then align into fixed-width COLUMNS so it reads as a proper table (the ids
        # vary in width, so a plain "id · provider · state" looked ragged).
        _mw = min(38, max((len(r[0]) for r in _raw), default=10))
        _pw = min(16, max((len(r[1]) for r in _raw), default=6))
        for mid, prov, key in _raw:
            rows.append(f"{mid[:_mw].ljust(_mw)}  {prov[:_pw].ljust(_pw)}  {key}")
    except Exception:  # noqa: BLE001 - picker must open even if catalog/registry fails
        rows = [AUTO_ROW]                       # keep AUTO reachable even with no catalog
    title = f"models → {role}" if role else "models (pin to all roles)"
    return Overlay(kind="model", title=title, select=SelectList(rows, height=14),
                   model_role=role)


def make_theme_overlay(themes: list[str], current: str = "") -> Overlay:
    """Theme picker with LIVE PREVIEW: the TUI re-applies the highlighted theme on
    every move/filter so you browse by seeing the real UI; Esc restores the theme
    that was active when it opened (carried in ``preview_orig``), Enter keeps the
    highlighted one. (Direct ``/themes <name>`` switching still works too.)"""
    themes = [t for t in (themes or []) if t] or ["default"]
    sl = SelectList(themes, height=14)
    if current in themes:
        sl.move_to(themes.index(current))
    return Overlay(kind="theme",
                   title="themes · ↑↓ preview · Enter keep · esc cancel",
                   select=sl, preview_orig=(current or ""))


def make_message_overlay(index: list, height: int = 14) -> Overlay:
    """Message navigator (the /msgs picker): one row per USER message — its transcript index
    (first token, recovered via ``chosen.split()[0]``) + a truncated label. ``index`` is the
    list of ``(transcript_index, label)`` from ``ChatWidget.user_msg_rail_index()``. Reuses
    SelectList so type-to-filter + arrow nav come for free; Enter scrolls the chat to it."""
    rows: list[str] = []
    for ti, label in (index or []):
        rows.append(f"{ti}  {label}")
    if not rows:
        rows = ["(no messages yet)"]
    return Overlay(kind="msgs",
                   title="your messages · type to filter · Enter to jump · esc cancel",
                   select=SelectList(rows, height=height))


def make_attachments_overlay(labels: list, height: int = 12) -> Overlay:
    """The 📎 attachment manager (#128): one row per staged attachment, in order. Row begins with
    the 1-based index so the TUI's Enter handler can recover which one to REMOVE
    (``chosen.split()[0]``). Reuses SelectList for arrow-nav + type-to-filter for free."""
    rows: list[str] = []
    for i, label in enumerate(labels or []):
        rows.append(f"{i + 1}  {label}")
    if not rows:
        rows = ["(nothing attached)"]
    return Overlay(kind="attachments",
                   title="attachments · ↑↓ select · Enter to REMOVE · esc close",
                   select=SelectList(rows, height=height))


def make_roleboard_overlay() -> Overlay:
    """The model-assignment board (the toggle panel): one row per role showing its
    AUTO/MANUAL state. The TUI handles Enter (pick a model → MANUAL) and 'a' (set
    AUTO) per selected role. Sub-agents are included so every agent is configurable.

    Opens even if overrides fail to load (falls back to all-AUTO)."""
    roles = ("planner", "executor", "reviewer", "subagent")
    rows: list[str] = []
    try:
        from .overrides import Overrides
        ov = Overrides.load()
        for r in roles:
            pinned = ov.pinned_model_for(r)
            state = f"MANUAL → {pinned.split('/')[-1]}" if pinned else "AUTO  ·  best-fit routing"
            rows.append(f"{r:9s}  {state}")
    except Exception:  # noqa: BLE001 - the board must always open
        rows = [f"{r:9s}  AUTO" for r in roles]
    return Overlay(kind="roleboard",
                   title="models · Enter=pick · a=Auto · esc=close",
                   select=SelectList(rows, height=8))


def _age_str(seconds: float) -> str:
    """Compact human age for the session picker: '12s' '5m' '3h' '2d'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def make_session_overlay(sessions: list, now: float | None = None,
                         title: str = "resume a session",
                         intent: str = "resume") -> Overlay:
    """Resume/fork picker: one row per recent session — id, first goal, turn count,
    age, and a ``forked`` flag for branches. Each row STARTS with the full session
    id so the TUI recovers it via ``chosen.split()[0]``. ``sessions`` come from
    :meth:`TaskStore.recent_sessions`; pass ``now`` to make ages deterministic.
    ``intent`` ("resume"|"fork") is stored on the overlay so the TUI knows what to
    do with the chosen session.
    """
    import time as _t
    now = _t.time() if now is None else now
    rows: list[str] = []
    for s in sessions:
        sid = str(s.get("id", ""))
        # Prefer a user-set title; fall back to the first goal. Title wins so a named
        # session reads as its name, not the raw first prompt.
        label = str(s.get("title", "")).strip() or str(s.get("goal", "")).strip() or "(no goal)"
        goal = label.replace("\n", " ")
        if len(goal) > 48:
            goal = goal[:47] + "…"
        turns = int(s.get("turns", 0) or 0)
        age = _age_str(now - float(s.get("updated", 0.0) or 0.0))
        forked = "  · forked" if str(s.get("branched_from", "")) else ""
        rows.append(f"{sid}  {goal}  · {turns} turns · {age}{forked}")
    return Overlay(kind="session", title=title, select=SelectList(rows, height=12),
                   action=intent)


def make_skills_overlay() -> Overlay:
    """Searchable picker of every available skill (built-in + installed plugins),
    one row per skill: 'name  — description'. Type to filter, Enter shows detail.
    Replaces the old read-only info popup (A4). The agentic executor auto-picks the
    right skill by description; this is for browsing/discovery."""
    rows: list[str] = []
    try:
        from .plugin_loader import bundled_skills, discover_plugins
        skills = list(bundled_skills()) + [s for p in discover_plugins() for s in p.skills]
        seen: set[str] = set()
        for s in skills:
            if s.name in seen:
                continue
            seen.add(s.name)
            desc = (getattr(s, "description", "") or "").replace("\n", " ").strip()
            if len(desc) > 60:
                desc = desc[:59] + "…"
            rows.append(f"{s.name}  — {desc}" if desc else s.name)
    except Exception:  # noqa: BLE001 - picker must open even if discovery fails
        rows = []
    if not rows:
        rows = ["(no skills found)"]
    return Overlay(kind="skills", title="skills · filter · Enter = details · ^E enable/disable",
                   select=SelectList(rows, height=14))


def make_agents_overlay() -> Overlay:
    """Searchable picker of installed agents (plugins), one row per agent:
    'name  — description'. Type to filter, Enter shows detail. Parallel to the skills
    picker (A4). Empty until you install agents via `syntra install <…>`."""
    rows: list[str] = []
    try:
        from .installer import installed_agents
        seen: set[str] = set()
        for a in installed_agents():
            nm = getattr(a, "name", "")
            if not nm or nm in seen:
                continue
            seen.add(nm)
            desc = (getattr(a, "description", "") or "").replace("\n", " ").strip()
            if len(desc) > 60:
                desc = desc[:59] + "…"
            rows.append(f"{nm}  — {desc}" if desc else nm)
    except Exception:  # noqa: BLE001 - picker must open even if discovery fails
        rows = []
    if not rows:
        rows = ["(no agents installed — add one with `syntra install <agent>`)"]
    return Overlay(kind="agents", title="agents · type to filter · Enter = details",
                   select=SelectList(rows, height=14))


def _bundled_catalog():
    from .catalog import Catalog
    from .paths import default_catalog_path
    return Catalog.load(default_catalog_path())


def make_info_overlay(title: str, content: str, height: int = 16,
                      *, wrap_width: int = 90) -> Overlay:
    """Read-only scrollable info popup for command output (status, usage, etc.).

    Replaces dumping long output into the chat transcript. The content is shown
    as lines; arrows scroll, Enter/Esc dismiss. No search box, no selection.

    ``wrap_width`` should be the box's INNER width so the FULL content is visible at
    the actual render size — the caller passes ``modal inner = box_w - 4``. (A fixed
    90 used to over-run the box on terminals narrower than ~96 cols, re-clipping.)
    """
    import textwrap
    from .tui_model import display_width
    w = max(8, int(wrap_width))
    lines: list[str] = []
    for ln in (content.split("\n") if content else ["(empty)"]):
        if display_width(ln) <= w:
            lines.append(ln)
        else:
            lines.extend(textwrap.wrap(ln, width=w, replace_whitespace=False,
                                       drop_whitespace=False) or [""])
    return Overlay(kind="info", title=title, select=SelectList(lines, height=height))


def make_styled_overlay(title: str, rows, *, height: int = 18) -> Overlay:
    """#218: a RICH read-only info popup where each row carries its own theme role — for
    dashboards (accent headers, dim captions, a density-ramp heatmap). ``rows`` is a list of
    ``(text, style)``; the text is shown as-is (caller pre-fits it to the box width — no
    wrapping, so style↔line alignment can't drift). Scroll/Esc like the plain info popup."""
    texts = [str(t) for t, _s in rows]
    styles = [str(s or "default") for _t, s in rows]
    ov = Overlay(kind="info", title=title, select=SelectList(texts, height=height))
    ov.line_styles = styles
    return ov


def overlay_box(overlay: Overlay, width: int, height: int) -> list[tuple[str, str]]:
    """Render the overlay as a centered bordered box -> [(text, style)]. Pure.

    Returns lines for a box of (inner) width `width`; the caller centers it.
    """
    from .tui_model import clip_to_width, display_width
    w = max(20, width)
    lines: list[tuple[str, str]] = []

    def frame(body: str, style: str) -> None:
        # Frame `body` between the │ borders, CELL-accurately clipped + padded to
        # exactly (w-2) cells. Char-based ljust/[:w] used to mis-size rows that held
        # wide/emoji glyphs (chars != cells), breaking the right border.
        body = clip_to_width(body, w - 2)
        body += " " * max(0, (w - 2) - display_width(body))
        lines.append(("│" + body + "│", style))

    # top border with title (titles are ASCII -> len math is fine here)
    title = f" {overlay.title} "
    top = "╭─" + title + "─" * max(0, w - len(title) - 3) + "╮"
    lines.append((top[:w], "accent"))

    is_info = overlay.kind == "info"
    # The roleboard (toggle panel) is a selectable list but has NO search box.
    show_query = overlay.kind not in ("info", "roleboard")

    if show_query:
        # query line (search box) — only for interactive fuzzy pickers
        frame(" ❯ " + overlay.select.query, "user")
        # separator
        lines.append(("├" + "─" * (w - 2) + "┤", "dim"))
        rows = max(1, height - 4)
        # #12: label the model picker's columns so the trailing "…abc123" reads as the
        # API KEY in use (it was a cryptic unlabeled hash). One dim caption row, then a rule.
        if overlay.kind == "model":
            frame("   model" + " " * 33 + "provider" + " " * 9 + "· key in use", "dim")
            lines.append(("├" + "─" * (w - 2) + "┤", "dim"))
            rows = max(1, rows - 2)
    else:
        rows = max(1, height - 3)

    # results / content
    visible = overlay.select.visible()
    sel_row = overlay.select.visible_selected()
    # #218: for a styled dashboard popup, map each visible row back to its per-line theme
    # role (viewport_start + i). No styles ⇒ flat "default" (unchanged classic behavior).
    _lstyles = getattr(overlay, "line_styles", None)
    _vstart = overlay.select._viewport_start() if _lstyles else 0
    if not visible:
        frame("  (no match)", "dim")
    for i, item in enumerate(visible[:rows]):
        if is_info:
            # read-only: no selection marker. A styled popup colors each row by its role;
            # a plain popup renders content flat.
            _st = "default"
            if _lstyles is not None:
                _idx = _vstart + i
                if 0 <= _idx < len(_lstyles):
                    _st = _lstyles[_idx]
            frame(" " + item, _st)
        else:
            marker = "▌ " if i == sel_row else "  "
            frame(marker + item, "user" if i == sel_row else "default")

    if is_info:
        # footer hint for info popups
        lines.append(("├" + "─" * (w - 2) + "┤", "dim"))
        frame(" ↑↓ scroll · esc close", "dim")

    # bottom border
    lines.append(("╰" + "─" * (w - 2) + "╯", "accent"))

    return lines
