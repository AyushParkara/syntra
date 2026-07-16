"""Live activity-tree widget (P24/P25) — the Cockpit "working tree" panel.

Groups a run's trace into collapsible mode blocks with an animated header
(spinner + elapsed + tokens), per-step rows, and click-to-expand/collapse.

It accepts input two ways:
  - feed(kind, payload):  the CLEAN progress-event API — the loop's structured
    events route here for the richest tree (the proper binding);
  - feed_line(role, text): a best-effort binding from the already-formatted trace
    strings the TUI receives today (via _drain), so the panel works right now.

Pure state + rendering; the curses layer paints render() and forwards clicks.
"""

from __future__ import annotations

from ..widget import Widget, RenderLine


class _Block:
    __slots__ = ("header", "children")

    def __init__(self, header: str):
        self.header = header
        self.children: list[tuple[str, str]] = []   # (text, role)


_MODE_NAMES = ("ANALYZE", "PLAN", "EXECUTE", "REVIEW", "AUTO", "DONE", "FAILED",
               "ANALYZING", "PLANNING", "EXECUTING", "REVIEWING")
_MODE_GLYPHS = ("⊙", "▣", "▶", "◎", "↻", "✓", "✗")


def _fmt_event(kind: str, p: dict | None) -> tuple[str, str]:
    """Format a clean progress event into a (role, line) for the block model."""
    p = p or {}
    if kind in ("phase", "mode"):
        name = str(p.get("phase", p.get("mode", ""))).upper()
        model = str(p.get("model", "") or "")
        head = name + (f"  · {model.split('/')[-1]}" if model else "")
        return ("mode", head)
    if kind == "plan_step":
        return ("tool", f"{p.get('step_id', '?')}  {str(p.get('description', ''))[:60]}")
    if kind == "step_start":
        return ("tool", f"▸ {p.get('step_id', '?')}  {str(p.get('description', ''))[:56]}")
    if kind == "step_done":
        return ("ok", f"✓ {p.get('step_id', '?')} done")
    if kind == "edit":
        return ("tool", f"edit {p.get('path', '?')}")
    if kind == "review_lens":
        return ("tool", f"〈{p.get('label', p.get('lens', '?'))}〉 {str(p.get('note', ''))[:48]}")
    if kind == "verify_result":
        ok = bool(p.get("ok"))
        return (("ok" if ok else "error"), f"{'✓' if ok else '✗'} verify")
    if kind == "loop_halted":
        return ("error", f"⛔ halted: {p.get('reason', '?')}")
    return ("tool", str(p)[:60])


class ActivityTreeWidget(Widget):
    kind = "activity_tree"
    focusable = True

    def __init__(self, *, title: str = "WORKING TREE", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self.blocks: list[_Block] = []
        self.collapsed: set = set()
        self.running: bool = False
        self.elapsed_s: float = 0.0
        self.tokens: int = 0
        self.label: str = "working"
        self._tick: int = 0
        self._header_rows: dict = {}   # render-row -> block index (for clicks)

    # ---- ingest ------------------------------------------------------------
    def feed(self, kind: str, payload: dict | None = None) -> None:
        role, text = _fmt_event(kind, payload)
        self.feed_line(role, text)

    def _is_mode_line(self, role: str, text: str) -> bool:
        if role == "mode":
            return True
        t = text.strip()
        return bool(t) and t[0] in _MODE_GLYPHS and any(n in t.upper() for n in _MODE_NAMES)

    def feed_line(self, role: str, text: str) -> None:
        text = (text or "").rstrip("\n")
        if not text.strip():
            return
        if self._is_mode_line(role, text):
            self.blocks.append(_Block(text.strip()))
            return
        if role in ("tool", "thinking", "ok", "error", "system"):
            if not self.blocks:
                self.blocks.append(_Block("WORKING"))
            self.blocks[-1].children.append((text.strip(), role))

    def reset(self) -> None:
        self.blocks = []
        self.collapsed = set()

    def tick(self) -> bool:
        self._tick += 1
        return self.running

    def toggle(self, block_index: int) -> None:
        if block_index in self.collapsed:
            self.collapsed.discard(block_index)
        else:
            self.collapsed.add(block_index)

    # ---- render ------------------------------------------------------------
    def render(self, width: int, height: int) -> list[RenderLine]:
        from ..tui_model import spinner_frame, abbrev_count
        w = max(8, int(width))
        out: list[RenderLine] = []
        self._header_rows = {}

        spin = spinner_frame(self._tick) if self.running else "●"
        if self.elapsed_s >= 60:
            el = f"{int(self.elapsed_s // 60)}m {int(self.elapsed_s % 60):02d}s"
        else:
            el = f"{self.elapsed_s:.0f}s"
        hdr = f"{spin} {self.label}…" + (f"  ({el} · ↓{abbrev_count(self.tokens)} tok)"
                                         if self.tokens else f"  {el}")
        out.append(RenderLine(hdr[:w], "accent"))

        # flatten blocks -> rows, then tail to fit (latest activity stays visible)
        rows: list[tuple[str, str, int | None]] = []
        last = len(self.blocks) - 1
        for bi, blk in enumerate(self.blocks):
            head = blk.header
            if self.running and bi == last:
                head = f"{head}  {spinner_frame(self._tick)}"
            rows.append((head, "mode", bi))
            if bi in self.collapsed:
                # #17: collapsed = the TOP-3 most-recent steps with a done/undone mark,
                # not a bare "… N steps". 'ok' children = done (✓), the rest = pending (·).
                kids = blk.children
                n = len(kids)
                done = sum(1 for _, r in kids if r == "ok")
                top3 = kids[-3:]
                for ci, (ctext, crole) in enumerate(top3):
                    mark = "✓" if crole == "ok" else ("✗" if crole == "error" else "·")
                    mstyle = "diff_add" if mark == "✓" else ("error" if mark == "✗" else "dim")
                    rows.append((f"  {mark} {ctext}", mstyle, None))
                if n > 3:
                    rows.append((f"  └ … +{n - 3} more · {done}/{n} done (click to expand)",
                                 "dim", None))
                else:
                    rows.append((f"  └ {done}/{n} done (click to expand)", "dim", None))
                continue
            for ci, (ctext, crole) in enumerate(blk.children):
                connector = "└" if ci == len(blk.children) - 1 else "├"
                rows.append((f"  {connector} {ctext}", crole, None))

        avail = max(1, height - 1)
        view = rows[-avail:] if len(rows) > avail else rows
        for i, (text, style, bidx) in enumerate(view):
            ry = 1 + i
            if bidx is not None:
                self._header_rows[ry] = bidx
            out.append(RenderLine(text[:w], style))
        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        b1 = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
        if button & b1 and y in self._header_rows:
            self.toggle(self._header_rows[y])
            return True
        return False
