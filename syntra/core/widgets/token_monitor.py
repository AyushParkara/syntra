"""Token/cost monitor widget.

Compact display of total tokens, input/output breakdown, cost.
Updates live during runs.
"""

from __future__ import annotations

from ..widget import Widget, RenderLine


class TokenMonitorWidget(Widget):
    kind = "token_monitor"
    focusable = False

    def __init__(self, *, title: str = "TOKENS", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self.total = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.context_limit = 128_000

    def update(self, *, input_tokens: int = 0, output_tokens: int = 0, cost: float = 0.0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total = input_tokens + output_tokens
        self.cost = cost

    def _fmt(self, n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}k"
        return str(n)

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        out: list[RenderLine] = []

        # Context usage bar
        if self.total > 0 and self.context_limit > 0:
            pct = min(100, int(self.total / self.context_limit * 100))
            bar_w = max(4, w - 8)
            filled = int(bar_w * pct / 100)
            bar = "█" * filled + "░" * (bar_w - filled)
            bar_style = "diff_add" if pct < 60 else ("string" if pct < 85 else "error")
            out.append(RenderLine(f"  {bar} {pct}%"[:w], bar_style))
        out.append(RenderLine("", "default"))
        out.append(RenderLine(f"  TOTAL   {self._fmt(self.total):>8}"[:w], "accent"))
        out.append(RenderLine(f"  INPUT   {self._fmt(self.input_tokens):>8}"[:w], "default"))
        out.append(RenderLine(f"  OUTPUT  {self._fmt(self.output_tokens):>8}"[:w], "default"))
        out.append(RenderLine(f"  COST    ${self.cost:>7.4f}"[:w], "string"))
        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]
