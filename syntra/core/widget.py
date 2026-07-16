"""Widget system for the modular TUI (Track T3).

Every panel in the TUI is a Widget. Widgets are independent, composable,
resizable, toggleable, and can be arranged in a split-tree layout (like tmux
panes). The curses layer draws widgets into their assigned Rect; the layout
engine decides Rects.

This is the pure, testable core — no curses here. The curses layer calls
draw(win, rect, theme) and passes key events to the focused widget.

Widget lifecycle:
    1. Registry creates widgets by name
    2. LayoutEngine assigns each widget a Rect
    3. The draw loop calls widget.render(width, height) -> list of (text, attr)
    4. Key events go to the focused widget via widget.handle_key(ch)
    5. Widgets can emit events (e.g. "submit", "scroll") via a callback
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    """A rectangular area on screen. Immutable. Pure geometry."""
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def shrink(self, top: int = 0, bottom: int = 0, left: int = 0, right: int = 0) -> "Rect":
        return Rect(
            x=self.x + left, y=self.y + top,
            w=max(0, self.w - left - right),
            h=max(0, self.h - top - bottom),
        )

    def split_h(self, left_w: int) -> tuple["Rect", "Rect"]:
        """Split horizontally: left gets left_w cols, right gets the rest."""
        lw = max(0, min(left_w, self.w))
        return (
            Rect(self.x, self.y, lw, self.h),
            Rect(self.x + lw, self.y, max(0, self.w - lw), self.h),
        )

    def split_v(self, top_h: int) -> tuple["Rect", "Rect"]:
        """Split vertically: top gets top_h rows, bottom gets the rest."""
        th = max(0, min(top_h, self.h))
        return (
            Rect(self.x, self.y, self.w, th),
            Rect(self.x, self.y + th, self.w, max(0, self.h - th)),
        )

    @property
    def area(self) -> int:
        return self.w * self.h

    def __bool__(self) -> bool:
        return self.w > 0 and self.h > 0


@dataclass
class RenderLine:
    """One line of widget output: text + a style/role tag for the curses layer."""
    text: str
    style: str = "default"  # maps to a theme color role
    rstyle: str = ""        # optional style for the LAST cell (e.g. a scrollbar) — when
                            # set, the paint layer overpaints the final column in this
                            # style so it doesn't inherit the line's content color.
    spans: list = field(default_factory=list)  # optional intra-line (start, end, style)
                            # char-offset runs the paint layer OVERPAINTS on top of the
                            # base line — code syntax-highlight + word-level diff (M1).


class Widget:
    """Base class. Subclass and implement render() + handle_key().

    Pure: no curses, no I/O. The curses layer paints render() output and
    forwards key events. Widgets hold their own state (scroll, selection, etc).
    """

    kind: str = "base"         # unique widget type name
    title: str = ""            # shown in the widget border/tab
    focusable: bool = True     # can this widget receive keyboard focus?
    visible: bool = True       # hidden widgets get 0 area from the layout
    minimized: bool = False    # minimized = title bar only, no content

    def __init__(self, *, title: str = "", on_event=None):
        self.title = title or self.kind
        self._on_event = on_event  # callback(widget, event_name, data)

    def render(self, width: int, height: int) -> list[RenderLine]:
        """Return lines to draw. Must fit in width x height."""
        return []

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        """Handle a key event. Return True if consumed."""
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        """Handle a mouse event (coords relative to widget). Return True if consumed."""
        return False

    def emit(self, event: str, data: Any = None) -> None:
        if self._on_event:
            self._on_event(self, event, data)

    def tick(self) -> bool:
        """Called on every draw cycle (~80ms). Return True if content changed."""
        return False

    def resize(self, width: int, height: int) -> None:
        """Called when the widget's rect changes. Override to recompute layout."""
        pass


# ──────────────────────────── Layout tree ────────────────────────────

@dataclass
class LayoutLeaf:
    """A leaf in the layout tree — holds one widget by name."""
    widget_name: str
    weight: float = 1.0  # relative size weight within the parent split

    def to_dict(self) -> dict:
        return {"type": "leaf", "widget": self.widget_name, "weight": self.weight}

    @classmethod
    def from_dict(cls, d: dict) -> "LayoutLeaf":
        return cls(widget_name=d["widget"], weight=d.get("weight", 1.0))


@dataclass
class LayoutSplit:
    """A split node: horizontal or vertical, containing children."""
    direction: str  # "h" (side-by-side) or "v" (stacked)
    children: list  # list of LayoutLeaf | LayoutSplit
    weight: float = 1.0

    def to_dict(self) -> dict:
        return {
            "type": "split", "direction": self.direction,
            "weight": self.weight,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LayoutSplit":
        children = []
        for c in d.get("children", []):
            if c.get("type") == "split":
                children.append(LayoutSplit.from_dict(c))
            else:
                children.append(LayoutLeaf.from_dict(c))
        return cls(direction=d["direction"], children=children, weight=d.get("weight", 1.0))


LayoutNode = LayoutLeaf | LayoutSplit


def resolve_layout(node: LayoutNode, rect: Rect) -> dict[str, Rect]:
    """Walk the layout tree and assign a Rect to each widget name. Pure."""
    result: dict[str, Rect] = {}
    _resolve(node, rect, result)
    return result


def _resolve(node: LayoutNode, rect: Rect, out: dict[str, Rect]) -> None:
    if isinstance(node, LayoutLeaf):
        out[node.widget_name] = rect
        return

    if not node.children:
        return

    total_weight = sum(c.weight for c in node.children)
    if total_weight <= 0:
        total_weight = len(node.children)

    if node.direction == "h":
        # split horizontally (side by side)
        x = rect.x
        remaining_w = rect.w
        for i, child in enumerate(node.children):
            if i == len(node.children) - 1:
                cw = max(1, remaining_w)
            else:
                cw = max(1, int(rect.w * child.weight / total_weight))
                remaining_w -= cw
            _resolve(child, Rect(x, rect.y, cw, rect.h), out)
            x += cw
    else:
        # split vertically (stacked)
        y = rect.y
        remaining_h = rect.h
        for i, child in enumerate(node.children):
            if i == len(node.children) - 1:
                ch = max(1, remaining_h)
            else:
                ch = max(1, int(rect.h * child.weight / total_weight))
                remaining_h -= ch
            _resolve(child, Rect(rect.x, y, rect.w, ch), out)
            y += ch


# ──────────────────────────── Default layouts ────────────────────────────

def default_layout() -> LayoutSplit:
    """The default layout: chat (center) + info panel (right), status bar at bottom.

    ┌──────────────────────┬──────────┐
    │                      │          │
    │    chat (75%)        │ info     │
    │                      │ (25%)    │
    │                      │          │
    ├──────────────────────┴──────────┤
    │ status_bar                      │
    └─────────────────────────────────┘
    """
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutLeaf("chat", weight=3.0),
            LayoutLeaf("info", weight=1.0),
        ], weight=1.0),
        LayoutLeaf("status_bar", weight=0.0),  # 0 weight = fixed 1 row
    ])


def coding_layout() -> LayoutSplit:
    """Coding layout: file tree + chat + info.

    ┌────────┬───────────────┬──────────┐
    │        │               │          │
    │ files  │    chat       │ info     │
    │ (15%)  │    (60%)      │ (25%)    │
    │        │               │          │
    ├────────┴───────────────┴──────────┤
    │ status_bar                        │
    └───────────────────────────────────┘
    """
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutLeaf("file_tree", weight=0.8),
            LayoutLeaf("chat", weight=3.0),
            LayoutLeaf("info", weight=1.2),
        ], weight=1.0),
        LayoutLeaf("status_bar", weight=0.0),
    ])


def focus_layout() -> LayoutSplit:
    """Focus layout: just chat, nothing else.

    ┌─────────────────────────────────┐
    │                                 │
    │             chat                │
    │                                 │
    ├─────────────────────────────────┤
    │ status_bar                      │
    └─────────────────────────────────┘
    """
    return LayoutSplit("v", [
        LayoutLeaf("chat", weight=1.0),
        LayoutLeaf("status_bar", weight=0.0),
    ])


def review_layout() -> LayoutSplit:
    """Review layout: chat + diff + activity.

    ┌───────────────┬─────────────────┐
    │               │  diff_viewer    │
    │    chat       ├─────────────────┤
    │               │  activity_log   │
    ├───────────────┴─────────────────┤
    │ status_bar                      │
    └─────────────────────────────────┘
    """
    return LayoutSplit("v", [
        LayoutSplit("h", [
            LayoutLeaf("chat", weight=1.5),
            LayoutSplit("v", [
                LayoutLeaf("diff_viewer", weight=1.0),
                LayoutLeaf("activity_log", weight=1.0),
            ], weight=1.0),
        ], weight=1.0),
        LayoutLeaf("status_bar", weight=0.0),
    ])


PRESET_LAYOUTS: dict[str, LayoutSplit] = {
    "default": default_layout(),
    "coding": coding_layout(),
    "focus": focus_layout(),
    "review": review_layout(),
}


# ──────────────────────────── Layout persistence ────────────────────────────

def layout_to_dict(node: LayoutNode) -> dict:
    return node.to_dict()


def layout_from_dict(d: dict) -> LayoutNode:
    if d.get("type") == "split":
        return LayoutSplit.from_dict(d)
    return LayoutLeaf.from_dict(d)


# ──────────────────────────── Widget Registry ────────────────────────────

class WidgetRegistry:
    """Factory + lookup for widget instances. The TUI creates widgets through
    here so layouts can reference widgets by name."""

    def __init__(self):
        self._factories: dict[str, type] = {}  # kind -> Widget subclass
        self._instances: dict[str, Widget] = {}  # name -> live instance

    def register(self, kind: str, cls: type) -> None:
        """Register a widget class by its kind name."""
        self._factories[kind] = cls

    def get(self, name: str) -> Widget | None:
        """Get a live widget instance by name."""
        return self._instances.get(name)

    def create(self, name: str, kind: str, **kwargs) -> Widget:
        """Create a named widget instance. Replaces any existing one."""
        cls = self._factories.get(kind, Widget)
        w = cls(title=name, **kwargs)
        w.kind = kind
        self._instances[name] = w
        return w

    def remove(self, name: str) -> bool:
        """Remove a widget by name. Returns True if it existed."""
        return self._instances.pop(name, None) is not None

    def names(self) -> list[str]:
        return list(self._instances.keys())

    def kinds(self) -> list[str]:
        return sorted(self._factories.keys())

    def all(self) -> dict[str, Widget]:
        return dict(self._instances)
