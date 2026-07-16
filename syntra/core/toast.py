"""Transient notifications (toasts).

Small messages that appear briefly (e.g. "copied 42 chars", "theme: tokyonight")
and fade after a few seconds — NOT permanent chat messages. The curses layer
draws the active toast in a corner; the toast expires on its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Toast:
    text: str
    kind: str = "info"          # "info" | "success" | "error"
    created: float = 0.0
    ttl: float = 2.5            # seconds visible

    def expired(self) -> bool:
        return (time.time() - self.created) > self.ttl


class ToastManager:
    """Holds the currently-visible toast (one at a time, newest wins)."""

    def __init__(self):
        self._toast: Toast | None = None

    def show(self, text: str, kind: str = "info", ttl: float = 2.5) -> None:
        self._toast = Toast(text=text, kind=kind, created=time.time(), ttl=ttl)

    def active(self) -> Toast | None:
        if self._toast and self._toast.expired():
            self._toast = None
        return self._toast

    def render(self, max_width: int) -> tuple[str, str] | None:
        """Return (text, style) for the active toast, or None. Pure-ish (time)."""
        t = self.active()
        if not t:
            return None
        icon = {"info": "·", "success": "✓", "error": "✗"}.get(t.kind, "·")
        style = {"info": "accent", "success": "diff_add", "error": "diff_del"}.get(
            t.kind, "accent")
        from .tui_model import display_width, clip_to_width
        body = f" {icon} {t.text} "
        # clip cell-accurately, reserving 2 cells for the ▕ ▏ frame so the WHOLE
        # rendered string fits max_width — len() used to let wide/emoji toasts overflow.
        inner = max(0, max_width - 2)
        if display_width(body) > inner:
            body = clip_to_width(body, inner)
        return (f"▕{body}▏", style)
