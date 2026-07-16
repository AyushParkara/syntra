"""Plan-mode phase ribbon — Syntra's own take on 'plan mode' (NOT a static banner clone).

It auto-activates while the pipeline is in its analyze/plan phase: an animated ribbon
(twinkling glyph + rotating 'shaping the plan' verb + live elapsed) with a live preview
of the plan steps as they form, before the interactive approval modal opens. Pure model;
the curses layer paints render() at the top of the chat column. Mirrors action_feed.py.
"""
from __future__ import annotations

from .tui_model import _frame, _THINK_FRAMES, motion_enabled

_PLAN_VERBS = ("shaping the plan", "mapping the work", "sketching steps",
               "laying out the plan", "sequencing the work")
_PHASE_STYLE = {"analyze": "string", "analyzing": "string",
                "plan": "accent", "planning": "accent"}


class PlanRibbon:
    def __init__(self) -> None:
        self.active = False
        self.phase = ""
        self.steps: list = []
        self.elapsed = ""

    def set_phase(self, name: str) -> None:
        self.phase = str(name or "").lower()

    def set_steps(self, steps) -> None:
        self.steps = [str(s) for s in (steps or [])]

    def _verb(self, tick: int) -> str:
        if not motion_enabled():
            return _PLAN_VERBS[0]
        return _PLAN_VERBS[(tick // 12) % len(_PLAN_VERBS)]

    def render(self, width: int, tick: int = 0) -> list:
        """Render the ribbon as [(text, style)]. Empty when not active.

        No box-drawing frame (a half-open ╭│╰ box read as a broken glitch at the chat
        width — user). Instead: a clean animated header line (glyph + phase + rotating
        verb + elapsed) and, when steps are forming, a couple of subtly-indented preview
        lines — matching the working-line / feed style, which use glyphs + plain text,
        not frames."""
        if not self.active:
            return []
        w = max(12, int(width))
        glyph = _frame(_THINK_FRAMES, tick)
        style = _PHASE_STYLE.get(self.phase, "accent")
        head = f" {glyph} {self.phase or 'plan'} · {self._verb(tick)}"
        if self.elapsed:
            head += f" · {self.elapsed}"
        out = [(head.ljust(w)[:w], style)]
        if self.steps:
            for i, s in enumerate(self.steps[:2], 1):
                out.append((f"     {i}. {s}".ljust(w)[:w], "dim"))
            if len(self.steps) > 2:
                out.append((f"     … {len(self.steps) - 2} more forming".ljust(w)[:w], "dim"))
        return out

    def status_line(self, *, agent: str = "", model: str = "", elapsed: str = "",
                    tick: int = 0) -> tuple[str, str] | None:
        """The compact status row just above the input: the active phase + a live fan-out
        worker. e.g. '▣ plan · laying out the plan · ● sub·1 · grok'. Returns (text, style)
        or None when there's nothing to show (idle). Pure."""
        if not self.active and not agent:
            return None
        bits = []
        if self.active:
            # ▣ (not a diamond — the user dislikes ◈/◇/◆) marks plan mode, matching the
            # PLAN-tab / in-progress glyph vocabulary.
            bits.append(f"▣ {self.phase or 'plan'} · {self._verb(tick)}")
        if agent:
            seg = f"● {agent}"
            if model:
                seg += f" · {model.split('/')[-1]}"
            if elapsed:
                seg += f" · {elapsed}"
            bits.append(seg)
        if not bits:
            return None
        return ("  " + "  ·  ".join(bits), "accent")

    def clear(self) -> None:
        self.active = False
        self.phase = ""
        self.steps = []
        self.elapsed = ""
