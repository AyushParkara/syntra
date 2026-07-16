"""Live side-by-side view for ``/compare`` (Loop.compare).

Pure state + render math — no curses. The TUI runs ``Loop.compare`` on a
background thread and feeds its events here via :meth:`CompareView.on_event`;
the draw loop paints :meth:`CompareView.render` and forwards keys/clicks to
:meth:`move` / :meth:`pick` / :meth:`scroll`.

The view shows every model's answer so you can compare MANUALLY, highlights the
judge's pick, and shows the synthesized best at the bottom. You navigate the
candidates (and the synthesis) as a row of tabs; the focused one's full text is
shown in a scrollable body so each answer is readable in full. Enter accepts the
focused answer (a candidate OR the synthesis) — the manual override.

Event contract (from Loop.compare, see core/loop.py):
- ``compare_start``     {index, model, provider}      — one per candidate, up front
- ``compare_candidate`` {index, model, provider, text} — each model's real answer
- ``compare_result``    {best_index, rationale, synthesis}

The "synthesis" tab is the LAST tab (index == number of candidates).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tui_model import clip_to_width, display_width, wrap_lines


@dataclass
class _Candidate:
    model: str = ""
    provider: str = ""
    text: str = ""
    status: str = "running"   # running | done | error


@dataclass
class CompareView:
    """State for one /compare run. ``focus`` indexes the tabs: 0..n-1 are the
    candidates, ``n`` is the synthesis tab (only selectable once it arrives)."""

    question: str = ""
    candidates: list[_Candidate] = field(default_factory=list)
    best_index: int = -1
    synthesis: str = ""
    rationale: str = ""
    done: bool = False          # set when compare_result arrives
    focus: int = 0              # which tab is focused
    scroll: int = 0             # vertical scroll within the focused body
    chosen: str | None = None   # text the user accepted (None until they pick)

    # ── ingest events ────────────────────────────────────────────────────────
    def on_event(self, kind: str, payload: dict) -> None:
        if kind == "compare_start":
            i = int(payload.get("index", len(self.candidates)))
            while len(self.candidates) <= i:
                self.candidates.append(_Candidate())
            self.candidates[i] = _Candidate(
                model=str(payload.get("model", "")),
                provider=str(payload.get("provider", "")),
                status="running",
            )
        elif kind == "compare_candidate":
            i = int(payload.get("index", 0))
            while len(self.candidates) <= i:
                self.candidates.append(_Candidate())
            text = str(payload.get("text", "") or "")
            self.candidates[i] = _Candidate(
                model=str(payload.get("model", "")) or self.candidates[i].model,
                provider=str(payload.get("provider", "")) or self.candidates[i].provider,
                text=text,
                status="error" if text.startswith("error:") else "done",
            )
        elif kind == "compare_result":
            self.best_index = int(payload.get("best_index", -1))
            self.synthesis = str(payload.get("synthesis", "") or "")
            self.rationale = str(payload.get("rationale", "") or "")
            self.done = True
            # default the focus to the synthesis once it exists (the recommended pick)
            if self.synthesis:
                self.focus = self.n_tabs() - 1
                self.scroll = 0

    # ── geometry / selection ─────────────────────────────────────────────────
    def n_tabs(self) -> int:
        """Candidate tabs + a synthesis tab when a synthesis exists."""
        return len(self.candidates) + (1 if self.synthesis else 0)

    def _synthesis_tab(self) -> int:
        return len(self.candidates)   # only valid when self.synthesis is set

    def is_synthesis_focused(self) -> bool:
        return bool(self.synthesis) and self.focus == self._synthesis_tab()

    def focused_text(self) -> str:
        if self.is_synthesis_focused():
            return self.synthesis
        if 0 <= self.focus < len(self.candidates):
            c = self.candidates[self.focus]
            return c.text or ("(running…)" if c.status == "running" else "(no output)")
        return ""

    def move(self, delta: int) -> None:
        n = max(1, self.n_tabs())
        self.focus = (self.focus + delta) % n
        self.scroll = 0

    def scroll_by(self, delta: int) -> None:
        self.scroll = max(0, self.scroll + delta)

    def focus_index(self, i: int) -> None:
        if 0 <= i < self.n_tabs():
            self.focus = i
            self.scroll = 0

    def header_rows(self) -> int:
        """Number of rendered lines BEFORE the tab strip (the title, plus the question
        line when present). The TUI uses this to map a mouse-click row to a tab."""
        return 1 + (1 if self.question else 0)

    def tab_at_row(self, row_in_box: int) -> int | None:
        """Map a 0-based row offset within the rendered box to a tab index, or None
        if the click landed outside the tab strip (title/body/footer)."""
        idx = row_in_box - self.header_rows()
        return idx if 0 <= idx < self.n_tabs() else None

    def pick(self) -> str | None:
        """Accept the focused answer (candidate or synthesis). Returns the text."""
        self.chosen = self.focused_text()
        return self.chosen

    # ── render ────────────────────────────────────────────────────────────────
    def render(self, width: int, height: int) -> list[tuple[str, str]]:
        """Render a centered bordered box -> [(text, style)]. Pure.

        Layout: title · a tab strip (every model + status + ★ for the judge's pick) ·
        the focused answer wrapped + scrolled · footer hints. The caller centers it.
        """
        w = max(28, int(width))
        inner = w - 2
        lines: list[tuple[str, str]] = []

        def frame(body: str, style: str) -> None:
            body = clip_to_width(body, inner)
            body += " " * max(0, inner - display_width(body))
            lines.append(("│" + body + "│", style))

        # title
        title = " compare "
        lines.append((("╭─" + title + "─" * max(0, w - len(title) - 3) + "╮")[:w], "accent"))

        # question (one clipped line)
        if self.question:
            frame(" ? " + self.question.replace("\n", " "), "dim")

        # tab strip — one row per tab so long model names never collide, with a marker
        # for the focused tab, the judge's ★ pick, and a running/done/error glyph.
        sep = self._synthesis_tab() if self.synthesis else -1
        for i in range(self.n_tabs()):
            focused = i == self.focus
            mark = "▌ " if focused else "  "
            if i == sep:
                star = "★ " if self.best_index >= 0 else "  "
                label = f"{star}synthesis (judge's best)"
                frame(mark + label, "user" if focused else "accent")
            else:
                c = self.candidates[i]
                star = "★ " if i == self.best_index else "  "
                glyph = {"running": "…", "done": "✓", "error": "✗"}.get(c.status, " ")
                label = f"{star}{glyph} {c.model} via {c.provider}"
                frame(mark + label, "user" if focused else "default")

        lines.append(("├" + "─" * inner + "┤", "dim"))

        # focused answer body — wrapped + vertically scrolled to fit the remaining rows
        head_rows = len(lines)
        foot_rows = 2  # footer separator + hint line
        body_rows = max(1, height - head_rows - foot_rows - 1)  # -1 for the bottom border
        wrapped: list[str] = []
        for para in self.focused_text().split("\n"):
            wrapped.extend(wrap_lines(para, inner - 1) or [""])
        total = len(wrapped)
        top = max(0, min(self.scroll, max(0, total - body_rows)))
        window = wrapped[top:top + body_rows]
        for ln in window:
            frame(" " + ln, "default")
        for _ in range(body_rows - len(window)):   # pad so the box keeps a stable height
            frame("", "default")

        # footer
        lines.append(("├" + "─" * inner + "┤", "dim"))
        more = ""
        if total > body_rows:
            more = f"  ({top + 1}-{min(top + body_rows, total)}/{total})"
        if not self.done:
            hint = "  ←/→ switch · ↑/↓ scroll · comparing…" + more
        else:
            what = "synthesis" if self.is_synthesis_focused() else "this answer"
            hint = f"  ←/→ switch · ↑/↓ scroll · ⏎ accept {what} · Esc close" + more
        frame(hint, "dim")
        lines.append((("╰" + "─" * inner + "╯")[:w], "accent"))
        return lines
