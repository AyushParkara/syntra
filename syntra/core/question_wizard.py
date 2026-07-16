"""Interactive question wizard (P11) — a multi-step clarifying-question modal.

When the agent needs structured input mid-run, the engine raises a *question spec*
(a list of steps); this drives a focused wizard the user steps through:

- a stepper header (Question 1 · 2 · 3 · ✔ Submit) with done/▸current/□pending marks,
- single-select (radio ❯ + numbers) and multi-select ([x] checkboxes) steps,
- EVERY step also offers "Type something" (free text) and "Chat about this" (pause
  the wizard and drop to chat), per the operator's spec,
- a final "Review your answers" screen, then Submit / Cancel.

Inputs the TUI maps onto this: ↑↓ + Enter, number hotkeys, mouse click, free text.

This module is PURE (no curses): the wizard is a state machine + a render function.
The curses layer paints wizard_box() and forwards keys/clicks. Unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WizardOption:
    label: str
    value: str = ""

    def val(self) -> str:
        return self.value or self.label


@dataclass
class WizardStep:
    id: str
    prompt: str
    kind: str = "single"            # "single" | "multi" | "text"
    options: list = field(default_factory=list)   # list[WizardOption]
    allow_text: bool = True
    allow_chat: bool = True


# A row in the current step's choice list. type ∈ {option, submit, text, chat}.
@dataclass
class _Row:
    type: str
    idx: int = -1                   # option index when type == "option"
    label: str = ""


@dataclass
class QuestionWizard:
    steps: list                     # list[WizardStep]
    title: str = "Question"
    step_idx: int = 0
    cursor: int = 0
    answers: dict = field(default_factory=dict)      # step_id -> str | list[str]
    _multi: dict = field(default_factory=dict)       # step_id -> set[int]
    mode: str = "step"              # step | text | review | chat | done | cancelled
    text_buf: str = ""
    chat_step_id: str = ""          # set when mode == "chat"; resume target
    result_value: dict | None = None
    _click_rows: dict = field(default_factory=dict)   # wizard_box row -> rows() index
    _screen_y0: int = -1            # screen y of the first content row (set by TUI)

    # ---- helpers -----------------------------------------------------------
    def _step(self) -> "WizardStep":
        return self.steps[self.step_idx]

    def _sel_set(self, step_id: str) -> set:
        return self._multi.setdefault(step_id, set())

    def rows(self) -> list:
        """Selectable rows for the current step (options + the fixed extras)."""
        if self.mode == "review":
            return [_Row("submit", label="Submit answers"), _Row("chat", label="Cancel")]
        s = self._step()
        rows: list = []
        if s.kind != "text":
            for i, o in enumerate(s.options):
                rows.append(_Row("option", i, o.label))
        if s.kind == "multi":
            rows.append(_Row("submit", label="Submit"))
        if s.allow_text or s.kind == "text":
            rows.append(_Row("text", label="Type something"))
        if s.allow_chat:
            rows.append(_Row("chat", label="Chat about this"))
        return rows

    # ---- navigation --------------------------------------------------------
    def move(self, delta: int) -> None:
        n = max(1, len(self.rows()))
        self.cursor = (self.cursor + delta) % n

    def move_to(self, i: int) -> None:
        n = len(self.rows())
        if 0 <= i < n:
            self.cursor = i

    def click_row(self, content_row: int):
        """A mouse click on wizard_box content-row `content_row` (0-based). Maps it to
        the underlying option/button and activates it. Returns the activate() signal
        (or None if the row isn't actionable). Wires P11's advertised mouse support."""
        idx = self._click_rows.get(int(content_row))
        if idx is None:
            return None
        n = len(self.rows())
        if not (0 <= idx < n):
            return None
        self.cursor = idx
        return self.activate()

    def number(self, n: int):
        """1-based hotkey: jump to row n and activate it (toggle for multi)."""
        if 1 <= n <= len(self.rows()):
            self.cursor = n - 1
            return self.activate()
        return None

    def toggle_current(self) -> None:
        """Space on a multi-select option toggles it without advancing."""
        if self.mode != "step":
            return
        row = self.rows()[self.cursor]
        if row.type == "option" and self._step().kind == "multi":
            sel = self._sel_set(self._step().id)
            sel.discard(row.idx) if row.idx in sel else sel.add(row.idx)

    def activate(self):
        """Act on the cursor row. Returns a signal for the caller:
        - ("chat", step_id) when the user picks "Chat about this"
        - ("done", answers) when Submit on the review screen
        - ("cancel", None) when Cancel
        - None otherwise (state advanced internally)."""
        rows = self.rows()
        if not rows:
            return None
        row = rows[min(self.cursor, len(rows) - 1)]

        if self.mode == "review":
            if row.type == "submit":
                self.mode = "done"
                self.result_value = dict(self.answers)
                return ("done", self.result_value)
            self.mode = "cancelled"
            return ("cancel", None)

        s = self._step()
        if row.type == "chat":
            self.mode = "chat"
            self.chat_step_id = s.id
            return ("chat", s.id)
        if row.type == "text":
            self.mode = "text"
            self.text_buf = ""
            return None
        if row.type == "option":
            if s.kind == "multi":
                self.toggle_current()
                return None
            self.answers[s.id] = s.options[row.idx].val()
            self._advance()
            return None
        if row.type == "submit":          # multi-select submit
            sel = sorted(self._sel_set(s.id))
            self.answers[s.id] = [s.options[i].val() for i in sel]
            self._advance()
            return None
        return None

    def submit_text(self) -> None:
        """Commit the free-text answer and advance."""
        if self.mode != "text":
            return
        self.answers[self._step().id] = self.text_buf.strip()
        self.text_buf = ""
        self.mode = "step"
        self._advance()

    def cancel_text(self) -> None:
        self.mode = "step"
        self.text_buf = ""

    def type_char(self, ch: str) -> None:
        if self.mode == "text" and ch and (ch == " " or ord(ch[0]) >= 32):
            self.text_buf += ch

    def backspace(self) -> None:
        if self.mode == "text":
            self.text_buf = self.text_buf[:-1]

    def resume_from_chat(self) -> None:
        """Re-open the wizard at the step the user left to chat about."""
        if self.mode == "chat":
            self.mode = "step"
            self.cursor = 0

    def back(self) -> bool:
        """Go to the previous step. Returns False when already at the first step
        (the caller closes the wizard)."""
        if self.mode == "review":
            self.mode = "step"
            self.step_idx = len(self.steps) - 1
            self.cursor = 0
            return True
        if self.step_idx > 0:
            self.step_idx -= 1
            self.cursor = 0
            return True
        return False

    def _advance(self) -> None:
        self.cursor = 0
        if self.step_idx < len(self.steps) - 1:
            self.step_idx += 1
        else:
            self.mode = "review"

    def result(self) -> dict | None:
        return self.result_value if self.mode == "done" else None


# ---- spec parsing ----------------------------------------------------------

def wizard_from_spec(spec) -> QuestionWizard:
    """Build a wizard from a JSON-ish spec emitted by the engine:
        {"title": "...", "steps": [
            {"id","prompt","kind":"single|multi|text","options":[{"label","value"}|"label"],
             "allow_text":true,"allow_chat":true}, ...]}
    Tolerant: strings become label-only options; missing flags default on.
    """
    steps: list = []
    for i, s in enumerate(spec.get("steps", []) or []):
        opts = []
        for o in (s.get("options") or []):
            if isinstance(o, str):
                opts.append(WizardOption(o))
            else:
                opts.append(WizardOption(o.get("label", ""), o.get("value", "")))
        steps.append(WizardStep(
            id=str(s.get("id", f"q{i+1}")),
            prompt=str(s.get("prompt", "")),
            kind=s.get("kind", "single"),
            options=opts,
            allow_text=bool(s.get("allow_text", True)),
            allow_chat=bool(s.get("allow_chat", True)),
        ))
    return QuestionWizard(steps=steps, title=str(spec.get("title", "Question")))


# ---- engine <-> UI contract ------------------------------------------------

def format_question_request(spec: dict) -> str:
    """ENGINE side: format a question spec into the line the engine emits on its
    output stream. The TUI's _drain detects this, opens the wizard, and the loop
    pauses until the answers arrive. B's tools.py just does:
        on_line(format_question_request({"title":..., "steps":[...]}), role="tool")
    """
    import json
    return "[question] " + json.dumps(spec, separators=(",", ":"))


def parse_answers(line: str):
    """ENGINE side: parse the "[answers] {json}" steer the UI sends back when the
    user submits. Returns the answers dict, or None if the line isn't an answer."""
    import json
    s = (line or "").strip()
    if not s.startswith("[answers]"):
        return None
    try:
        return json.loads(s[len("[answers]"):].strip())
    except Exception:  # noqa: BLE001
        return None


# ---- render ----------------------------------------------------------------

def wizard_box(wiz: QuestionWizard, width: int) -> list:
    """Render the wizard -> [(text, style)]. Pure; the caller centers + borders it.

    Every line WRAPS to the box width (continuation lines indent under the text) instead of being
    truncated with [:w] — so a long question prompt or option label stays FULLY visible and never
    spills past the border (user: the text must fit inside the box, handled properly). Click rows
    map to the FIRST wrapped row of each option, so a mouse click still selects the right option."""
    from syntra.core.tui_model import wrap_lines

    w = max(30, int(width))
    out: list = []
    wiz._click_rows = {}   # content-row index -> rows() index (for mouse clicks, P11)

    def emit(text, style):
        """Append a line, wrapping it to the box width; continuation lines indent under the first
        non-space run so wrapped option text lines up under its label (not under the number). The
        wrap width is reduced by the indent so the indented continuation still fits inside the box
        (no spill past the border)."""
        text = text if text is not None else ""
        if not text.strip():
            out.append((text, style))
            return
        _ind = len(text) - len(text.lstrip(" "))
        _body_w = max(1, w - _ind)
        _stripped = text[_ind:]
        _wrapped = wrap_lines(_stripped, _body_w) or [_stripped]
        out.extend((" " * _ind + _c, style) for _c in _wrapped)

    def emit_click(rows_idx, text, style):
        """Like emit() but records the click-row mapping at the FIRST wrapped row, so a click on
        an option (even a multi-row wrapped one) resolves to that option."""
        wiz._click_rows[len(out)] = rows_idx
        emit(text, style)

    # stepper header: Q1 · Q2 · … · ✔ Submit  (☒ done · ▸ current · □ pending)
    chips = []
    for i, _s in enumerate(wiz.steps):
        if wiz.mode == "review" or i < wiz.step_idx:
            mark = "☒"
        elif i == wiz.step_idx and wiz.mode in ("step", "text", "chat"):
            mark = "▸"
        else:
            mark = "□"
        chips.append(f"{mark} {i + 1}")
    sub_mark = "✔" if wiz.mode in ("review", "done") else "□"
    chips.append(f"{sub_mark} Submit")
    emit("  " + "   ".join(chips), "accent")
    out.append(("", "default"))

    if wiz.mode == "review":
        out.append(("  Review your answers", "accent"))
        out.append(("", "default"))
        for s in wiz.steps:
            ans = wiz.answers.get(s.id, "—")
            ans = ", ".join(ans) if isinstance(ans, list) else str(ans)
            emit(f"  ● {s.prompt}", "default")
            emit(f"    → {ans or '(skipped)'}", "string")
        out.append(("", "default"))
        for i, row in enumerate(wiz.rows()):
            cur = "❯ " if i == wiz.cursor else "  "
            emit_click(i, f"  {cur}{i + 1}. {row.label}",
                       "user" if i == wiz.cursor else "default")
        out.append(("", "default"))
        emit("  ↑↓/numbers/click · Enter/→ select · Backspace/← back · Esc cancel", "dim")
        return out

    s = wiz._step()
    emit(f"  {s.prompt}", "accent")
    out.append(("", "default"))

    if wiz.mode == "text":
        out.append(("  Type your answer:", "dim"))
        emit(f"  ❯ {wiz.text_buf}", "user")
        out.append(("", "default"))
        emit("  Enter/→ to confirm · Backspace/← to go back · Esc cancel", "dim")
        return out

    sel = wiz._sel_set(s.id)
    for i, row in enumerate(wiz.rows()):
        cur = "❯ " if i == wiz.cursor else "  "
        if row.type == "option" and s.kind == "multi":
            box = "[x]" if row.idx in sel else "[ ]"
            line = f"  {cur}{i + 1}. {box} {row.label}"
        elif row.type == "option":
            line = f"  {cur}{i + 1}. {row.label}"
        else:
            line = f"  {cur}{i + 1}. {row.label}"
        style = "user" if i == wiz.cursor else (
            "dim" if row.type in ("text", "chat") else "default")
        emit_click(i, line, style)

    out.append(("", "default"))
    hint = ("Space toggles · Enter/→ on Submit" if s.kind == "multi"
            else "Enter/→ selects")
    emit(f"  ↑↓/numbers/click · {hint} · Backspace/← back · Esc cancel", "dim")
    return out
