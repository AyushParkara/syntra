"""Plan-approval modal — a scrollable, INTERACTIVE plan review (not a plain text dump).

When a run pauses for plan approval (verdict == "plan_pending"), the TUI used to print a
plain "Plan ready — Enter to approve / type to modify / /clear to discard" block into the
chat. That left the user guessing WHERE to press Enter, and a long plan was truncated.

This replaces it with a real modal the user answers like a question:
  - the WHOLE plan is shown as a numbered list, SCROLLABLE when it's taller than the box
    (↑/↓ scroll, never trimmed),
  - five selectable actions, each an ICON + label with a ❯ cursor (NO ugly "1. 2. 3."
    numbers on screen) — chosen with ↑/↓ + Enter or a mouse CLICK on the row:
      ✓ Approve & run · ↻ Approve for this session · ⏻ Approve always ·
      ✎ Modify the plan · ✕ Discard
    The three "Approve" rows differ only in SCOPE — once (default, keeps asking),
    just-this-session (stop pausing until the app closes), or always (stop pausing for
    good, persisted). That's how the user turns plan-review off without a separate command.
  - Modify drops to a free-text box so the user types how to change the plan.

PURE (no curses): a small state machine + a render function. The curses layer paints
plan_box() and forwards keys/clicks. Unit-tested. Mirrors question_wizard.py's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# The five fixed actions, in display + cursor order: (key, icon, label).
# The three approve rows return the SAME ("approve", scope) signal kind, differing only in
# scope so the TUI can stop pausing at the chosen scope. NO visible numbers (user: "1 2 3
# looks bad") — the icon + ❯ cursor IS the affordance, and every row is clickable.
_ACTIONS = [
    ("approve_once", "✓", "Approve & run"),
    ("approve_session", "↻", "Approve for this session"),
    ("approve_always", "⏻", "Approve always"),
    ("modify", "✎", "Modify the plan"),
    ("discard", "✕", "Discard"),
]

# Map an action key → the (kind, payload) signal the caller acts on.
_SIGNAL = {
    "approve_once": ("approve", "once"),
    "approve_session": ("approve", "session"),
    "approve_always": ("approve", "always"),
    "discard": ("discard", None),
}


@dataclass
class PlanApproval:
    """State for the plan-approval modal.

    `steps` is the list of plan-step description strings (the WHOLE plan — never trimmed).
    `body_height` is how many plan rows the box can show at once; the body scrolls when the
    plan is taller. `scroll` is the first visible plan row. `cursor` selects the action row.
    `mode` ∈ {choose, text, done, cancelled}; `text_buf` holds the Modify free text."""

    steps: list = field(default_factory=list)        # list[str] step descriptions
    title: str = "Plan ready"
    cursor: int = 0                                   # index into _ACTIONS
    scroll: int = 0                                   # first visible plan row
    body_height: int = 8                              # plan rows visible at once (set by TUI)
    mode: str = "choose"                              # choose | text | done | cancelled
    text_buf: str = ""
    result_value: tuple | None = None                 # ("approve",None)|("modify",text)|("discard",None)
    _click_rows: dict = field(default_factory=dict)   # render-row -> action index
    _screen_y0: int = -1                              # screen y of first content row (set by TUI)

    # ---- navigation --------------------------------------------------------
    def move(self, delta: int) -> None:
        """↑/↓ over the ACTION rows (wraps)."""
        if self.mode != "choose":
            return
        n = len(_ACTIONS)
        self.cursor = (self.cursor + delta) % n

    def move_to(self, i: int) -> None:
        if self.mode == "choose" and 0 <= i < len(_ACTIONS):
            self.cursor = i

    def number(self, n: int):
        """1-based action hotkey: select row n and activate it."""
        if self.mode == "choose" and 1 <= n <= len(_ACTIONS):
            self.cursor = n - 1
            return self.activate()
        return None

    def scroll_body(self, delta: int) -> None:
        """Scroll the PLAN body (not the actions) so a long plan is fully reachable."""
        top = max(0, len(self.steps) - max(1, self.body_height))
        self.scroll = max(0, min(top, self.scroll + delta))

    def max_scroll(self) -> int:
        return max(0, len(self.steps) - max(1, self.body_height))

    def click_row(self, content_row: int):
        """A mouse click on render content-row `content_row` (0-based). Maps an ACTION row to
        its action and activates it; clicks on the plan body are ignored. Returns the activate()
        signal or None."""
        idx = self._click_rows.get(int(content_row))
        if idx is None:
            return None
        if not (0 <= idx < len(_ACTIONS)):
            return None
        self.cursor = idx
        return self.activate()

    # ---- activation --------------------------------------------------------
    def activate(self):
        """Act on the cursor action. Returns a signal for the caller:
        - ("approve", scope)  → resume + run; scope ∈ {"once","session","always"} controls
          whether (and how durably) plan-review stops pausing afterwards
        - ("modify", "")      → caller should let the user type (mode flips to text)
        - ("discard", None)   → drop the plan
        None while internally transitioning (text mode opened)."""
        if self.mode != "choose":
            return None
        action = _ACTIONS[min(self.cursor, len(_ACTIONS) - 1)][0]
        if action in _SIGNAL:
            self.mode = "done"
            self.result_value = _SIGNAL[action]
            return self.result_value
        # modify → free-text entry
        self.mode = "text"
        self.text_buf = ""
        return ("modify", "")

    def submit_text(self):
        """Commit the Modify free text. Returns ("modify", text) or None if empty (stays open)."""
        if self.mode != "text":
            return None
        text = self.text_buf.strip()
        if not text:
            return None
        self.mode = "done"
        self.result_value = ("modify", text)
        return ("modify", text)

    def cancel_text(self) -> None:
        """Esc out of the text box → back to the action chooser."""
        if self.mode == "text":
            self.mode = "choose"
            self.text_buf = ""

    def type_char(self, ch: str) -> None:
        if self.mode == "text" and ch and (ch == " " or ord(ch[0]) >= 32):
            self.text_buf += ch

    def backspace(self) -> None:
        if self.mode == "text":
            self.text_buf = self.text_buf[:-1]

    def result(self):
        return self.result_value if self.mode == "done" else None


# ---- render ----------------------------------------------------------------

def plan_box(pa: PlanApproval, width: int) -> list:
    """Render the modal -> [(text, style)]. Pure; the caller centers + borders it.

    Layout: a scroll-aware plan body (numbered steps, '↑ N more' / '↓ N more' markers when
    the plan overflows the body) then the three action rows. Records _click_rows so a mouse
    click on an action maps to it."""
    w = max(30, int(width))
    out: list = []
    pa._click_rows = {}

    n = len(pa.steps)
    bh = max(1, pa.body_height)
    pa.scroll = max(0, min(pa.scroll, pa.max_scroll()))

    head = f"{n} step{'s' if n != 1 else ''}"
    out.append((f"  ● {head}", "accent"))
    out.append(("", "default"))

    if pa.mode == "text":
        # Modify: show a couple of plan lines for context + the text entry. Wrap each so a
        # long step isn't cut (continuation indents under the number).
        from syntra.core.tui_model import wrap_lines
        for i, s in enumerate(pa.steps[:3], 1):
            _pfx = f"  {i}. "
            _wr = wrap_lines(str(s), max(1, w - len(_pfx))) or [""]
            out.append((_pfx + _wr[0], "default"))
            out.extend((" " * len(_pfx) + _c, "default") for _c in _wr[1:])
        if n > 3:
            out.append((f"  … {n - 3} more step(s)", "dim"))
        out.append(("", "default"))
        out.append(("  How should the plan change?", "accent"))
        # WRAP the typed text so a long modify message stays INSIDE the box (was clipped with
        # [:w] and spilled past the right border). Continuation lines indent under the ❯.
        _ipfx = "  ❯ "
        _itext = pa.text_buf or ""
        _iwr = wrap_lines(_itext, max(1, w - len(_ipfx))) if _itext else [""]
        out.append((_ipfx + _iwr[0], "user"))
        out.extend((" " * len(_ipfx) + _c, "user") for _c in _iwr[1:])
        out.append(("", "default"))
        out.append(("  Enter to submit · Esc to go back", "dim"))
        return out

    # scrollable plan body — the WHOLE plan is reachable, never trimmed. Each step WRAPS
    # to the box width (continuation lines indent under the number) so a long step shows
    # in full instead of being cut at the right edge.
    from syntra.core.tui_model import wrap_lines
    if pa.scroll > 0:
        out.append((f"  ↑ {pa.scroll} more above", "dim"))
    visible = pa.steps[pa.scroll:pa.scroll + bh]
    for off, s in enumerate(visible):
        idx = pa.scroll + off + 1
        prefix = f"  {idx}. "
        wrapped = wrap_lines(str(s), max(1, w - len(prefix))) or [""]
        out.append((prefix + wrapped[0], "default"))
        out.extend((" " * len(prefix) + cont, "default") for cont in wrapped[1:])  # indent continuation
    below = n - (pa.scroll + len(visible))
    if below > 0:
        out.append((f"  ↓ {below} more below — ↑/↓ to scroll", "dim"))
    out.append(("", "default"))

    # action rows (selectable like a question). ICON + label with a ❯ cursor — NO visible
    # "1. 2. 3." numbers (user: "the 1 2 3 type of options does not look good"). The selected
    # row is highlighted; the discard row stays dim; every row is recorded in _click_rows so a
    # CLICK activates it (user: "i should be able to click those options too").
    for i, (key, icon, label) in enumerate(_ACTIONS):
        cur = "❯ " if i == pa.cursor else "  "
        if i == pa.cursor:
            style = "user"
        elif key == "discard":
            style = "dim"
        else:
            style = "default"
        pa._click_rows[len(out)] = i
        out.append((f"  {cur}{icon}  {label}"[:w], style))

    out.append(("", "default"))
    out.append(("  ↑↓ move · click/Enter select · PgUp/PgDn scroll · Esc discard", "dim"))
    out.append(("  approve = run now · session = stop asking until exit · always = stop for good", "dim"))
    return out
