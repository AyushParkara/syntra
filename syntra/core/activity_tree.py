"""The Cockpit Trace — Syntra's live run tree.

Where other agent UIs show a flat action/result list, Syntra groups a run into
**mode blocks**: each top node is the active MODE (analyze / plan / execute /
review / auto), carrying its model + live elapsed, and its work hangs beneath it as
indented children. You read a run top-to-bottom as "which mode am I in, and what did
it do" — the mode owns the node. This block-per-mode layout is Syntra's own.

```
⊙ ANALYZE                        · coding
▣ PLAN  · grok-4-3
  ├ s1  establish the core module
  └ s2  wire it into the loop
▶ EXECUTE  · grok-4-3
  ├ ✶ thinking  (dim; collapses to "✶ thought · N lines" once the answer starts)
  ├ ▸ s1  edit loop.py            ✓
  └ ▸ s2  bash: pytest            ✓
◎ REVIEW  · 3 lenses
  ├ 〈Correctness〉 logic sound
  ├ 〈Completeness〉 tests cover the path
  ├ 〈Goal-fit〉 meets the goal
  └ ✓ PASS · 0.86
```

This module is PURE: feed it the loop's `(kind, payload)` progress events, ask it
for render lines. No curses, no I/O — unit-tested. The TUI owns colors via the
per-line `role` tag; this owns structure + glyphs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mode chips — Syntra's own glyph language for the trace.
_MODE_CHIP = {
    "analyzing": ("⊙", "ANALYZE"),
    "planning": ("▣", "PLAN"),
    "planned": ("▣", "PLAN"),
    "executing": ("▶", "EXECUTE"),
    "reviewing": ("◎", "REVIEW"),
    "reviewed": ("◎", "REVIEW"),
    "auto": ("↻", "AUTO"),
    "done": ("✓", "DONE"),
    "failed": ("✗", "FAILED"),
}
# Map a raw phase name -> the mode key whose block it belongs to.
_PHASE_TO_MODE = {
    "analyzing": "analyzing", "planning": "planning", "planned": "planning",
    "executing": "executing", "reviewing": "reviewing", "reviewed": "reviewing",
    "done": "done", "failed": "failed",
}

# Child connectors.
_BRANCH = "├ "
_LAST = "└ "
_INDENT = "  "

_SPIN = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _spinner(tick: int) -> str:
    return _SPIN[int(tick) % len(_SPIN)]


@dataclass
class Child:
    text: str
    role: str = "tool"          # render role/color tag for the TUI
    status: str = ""            # "", "running", "ok", "fail"
    collapsed_note: str = ""    # if set + collapsed, show this instead of text


@dataclass
class ModeNode:
    mode: str                   # key into _MODE_CHIP
    model: str = ""
    note: str = ""              # e.g. category, "3 lenses"
    children: list = field(default_factory=list)
    # thinking is special: a single live, collapsible child the TUI can grow/shrink
    thinking_lines: list = field(default_factory=list)
    thinking_done: bool = False


class ActivityTree:
    """Accumulates progress events into mode blocks; renders an indented tree.

    `feed(kind, payload)` mutates the tree; `lines(width)` renders it. Robust to
    out-of-order / missing events: any child event with no open mode opens a
    sensible default block."""

    def __init__(self) -> None:
        self.nodes: list[ModeNode] = []

    # ---- ingest -------------------------------------------------------------
    def _open(self, mode: str, *, model: str = "", note: str = "") -> ModeNode:
        """Open (or reuse) the block for `mode`.

        A run progresses through modes monotonically (analyze→plan→execute→
        review), and the loop re-emits the same phase several times (e.g. planning
        with model, then again after analysis). So we COALESCE: reuse the latest
        existing block of this mode rather than appending a duplicate — the trace
        stays one clean block per mode instead of fragmenting."""
        for existing in reversed(self.nodes):
            if existing.mode == mode:
                if model:
                    existing.model = model
                if note:
                    existing.note = note
                return existing
        node = ModeNode(mode=mode, model=model, note=note)
        self.nodes.append(node)
        return node

    def _current(self) -> ModeNode:
        if not self.nodes:
            return self._open("executing")
        return self.nodes[-1]

    def feed(self, kind: str, payload: dict | None) -> None:
        p = payload or {}
        if kind == "phase":
            phase = str(p.get("phase", "")).lower()
            mode = _PHASE_TO_MODE.get(phase, phase or "executing")
            model = _short_model(p.get("model", ""))
            node = self._open(mode, model=model)
            if mode == "reviewing" and "verdict" in p:
                verdict = str(p.get("verdict", "")).lower()
                conf = p.get("confidence")
                mark = "✓ PASS" if verdict == "pass" else "✗ FAIL"
                tail = f" · {float(conf):.2f}" if isinstance(conf, (int, float)) else ""
                node.children.append(Child(f"{mark}{tail}", role="ok" if verdict == "pass" else "error",
                                           status="ok" if verdict == "pass" else "fail"))
            return
        if kind == "analysis":
            # Annotate the analyze block with the task category. Don't CREATE one
            # if analysis arrives after we've already moved on (avoids a stray block).
            cat = p.get("category") or p.get("mode") or ""
            existing = next((n for n in reversed(self.nodes) if n.mode == "analyzing"), None)
            node = existing or self._open("analyzing")
            if cat:
                node.note = str(cat)
            return
        if kind == "mode":   # explicit mode hint from the loop (model/permission)
            mode = str(p.get("mode", "")).lower()
            if mode:
                self._open(mode, model=_short_model(p.get("model", "")), note=str(p.get("note", "")))
            return
        if kind == "plan_step":
            self._open("planning").children.append(
                Child(f"{p.get('step_id', '?')}  {str(p.get('description', ''))[:200]}", role="tool"))
            return
        if kind == "plan_ready":
            # The approval prompt is shown ONCE as a clear system block in the chat (with
            # the numbered steps). Don't also repeat it in the trace — that duplicated the
            # whole plan when the trace was expanded (user F11).
            return
        if kind == "step_start":
            self._open("executing").children.append(
                Child(f"▸ {p.get('step_id', '?')}  {str(p.get('description', ''))[:200]}",
                      role="tool", status="running"))
            return
        if kind == "step_done":
            self._mark_last_step_done(p.get("step_id", ""))
            return
        if kind == "edit":
            status = str(p.get("status", ""))
            mark = {"applied": "✓", "proposed": "·", "failed": "✗"}.get(status, "·")
            self._current().children.append(
                Child(f"{mark} edit {p.get('path', '?')}", role="tool",
                      status="ok" if status == "applied" else ("fail" if status == "failed" else "")))
            return
        if kind == "verify_result":
            ok = bool(p.get("ok"))
            self._current().children.append(
                Child(f"{'✓' if ok else '✗'} verify (exit {p.get('exit_code', '?')})",
                      role="ok" if ok else "error", status="ok" if ok else "fail"))
            return
        if kind == "review_lens":
            label = p.get("label", p.get("lens", "?"))
            ok = bool(p.get("ok", True))
            note = str(p.get("note", "")).strip()
            mark = "" if ok else "✗ "
            self._open("reviewing").children.append(
                Child(f"〈{label}〉 {mark}{note}"[:200], role="tool" if ok else "error",
                      status="ok" if ok else "fail"))
            return
        if kind == "autopilot":
            it, total = p.get("iteration", "?"), p.get("max", p.get("of", "?"))
            self._open("auto", note=f"pass {it}/{total}").children.append(
                Child(f"↻ retry pass {it}: unmet {', '.join(p.get('unmet', []) or []) or '—'}",
                      role="system"))
            return
        if kind == "loop_halted":
            self._current().children.append(
                Child(f"⛔ halted: {p.get('reason', '?')}", role="error", status="fail"))
            return
        # reasoning text is fed separately via feed_thinking()

    def feed_thinking(self, chunk: str) -> None:
        """Append a live chain-of-thought chunk to the current mode's thinking block."""
        node = self._current()
        node.thinking_done = False
        if not node.thinking_lines:
            node.thinking_lines = [""]
        # split on newlines, keep building the last partial line
        text = node.thinking_lines.pop() + chunk
        parts = text.split("\n")
        node.thinking_lines.extend(parts)

    def end_thinking(self) -> None:
        """Mark the current thinking block complete (so it collapses)."""
        for n in self.nodes:
            if n.thinking_lines and not n.thinking_done:
                n.thinking_done = True

    def _mark_last_step_done(self, step_id: str) -> None:
        for node in reversed(self.nodes):
            for ch in reversed(node.children):
                if ch.status == "running" and (not step_id or step_id in ch.text):
                    ch.status = "ok"
                    if not ch.text.rstrip().endswith("✓"):
                        ch.text = ch.text + "  ✓"
                    return

    # ---- render -------------------------------------------------------------
    def lines(self, width: int, *, thinking_collapsed: bool = True,
              tick: int = 0, collapsed: set | None = None,
              active: bool = True) -> list[tuple[str, str]]:
        """Render the whole tree to (text, role) lines.

        tick      — when >0, the CURRENT (last) mode header + its live thinking line
                    get an animated spinner suffix (P24).
        collapsed — set of mode-node indices to fold to a one-line "… N steps" (P25).
        active    — whether the last node is the live one (drives the spinner).
        """
        out: list[tuple[str, str]] = []
        w = max(20, int(width))
        collapsed = collapsed or set()
        spin = _spinner(tick) if tick else ""
        n = len(self.nodes)
        for ni, node in enumerate(self.nodes):
            is_live = active and ni == n - 1
            header = _mode_header(node, w)
            if is_live and spin:
                header = (header + "  " + spin)[:w]
            out.append((header, "mode"))
            kids = _node_render_children(node, thinking_collapsed)
            if ni in collapsed:
                out.append((_INDENT + _LAST + f"… {len(kids)} step{'s' if len(kids) != 1 else ''} (click to expand)", "dim"))
                continue
            for i, (text, role) in enumerate(kids):
                connector = _LAST if i == len(kids) - 1 else _BRANCH
                if is_live and spin and text.strip() == "✶ thinking":
                    text = f"✶ thinking {spin}"
                out.append((_INDENT + connector + text[: max(0, w - 4)], role))
        return out

    def header_rows(self, rendered: list) -> dict:
        """Map a rendered-row index -> mode-node index (for click-to-collapse).
        Counts the 'mode'-role rows in order produced by lines()."""
        mapping: dict = {}
        ni = -1
        for ri, (_t, role) in enumerate(rendered):
            if role == "mode":
                ni += 1
            if ni >= 0:
                mapping[ri] = ni
        return mapping


def _mode_header(node: ModeNode, width: int) -> str:
    glyph, name = _MODE_CHIP.get(node.mode, ("·", node.mode.upper()))
    head = f"{glyph} {name}"
    bits = []
    if node.note:
        bits.append(node.note)
    if node.model:
        bits.append(node.model)
    if bits:
        head = f"{head}  · " + " · ".join(bits)
    return head[: max(0, width)]


def _node_render_children(node: ModeNode, thinking_collapsed: bool) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    # thinking first (it happens before the answer)
    if node.thinking_lines:
        real = [ln for ln in node.thinking_lines if ln.strip()]
        if node.thinking_done and thinking_collapsed:
            rows.append((f"✶ thought · {len(real)} line{'s' if len(real) != 1 else ''}", "thinking"))
        else:
            rows.append(("✶ thinking", "thinking"))
            rows.extend(("  " + ln.strip()[:70], "thinking") for ln in real[-8:])  # cap live view to last 8 lines
    rows.extend((ch.text, ch.role) for ch in node.children)
    return rows


def _short_model(model: str) -> str:
    if not model:
        return ""
    return model.split("/")[-1] if "/" in model else model
