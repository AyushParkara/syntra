"""Workspace manager — dockable/floating/maximized widget state machine.

This is the PURE core of the "dock model": every widget has a display_state
(normal | docked | maximized | floating). Normal widgets tile via the layout
tree; docked widgets become chips in a strip; maximized takes the whole body;
floating draws on top at a free (x,y,w,h).

No curses here — just geometry + state, so it's fully unit-testable. The curses
driver (cli/tui2.py) reads resolve() to know exactly what to paint where.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from .widget import Rect, LayoutNode, LayoutLeaf, LayoutSplit, resolve_layout


NORMAL = "normal"
DOCKED = "docked"
MAXIMIZED = "maximized"
FLOATING = "floating"


@dataclass
class FloatBox:
    """Position+size for a floating widget."""
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict) -> "FloatBox":
        return cls(d["x"], d["y"], d["w"], d["h"])


@dataclass
class WorkspaceManager:
    """Owns widget display-states and resolves them into draw rects.

    layout      : the split-tree of NORMAL widgets
    states      : name -> display_state
    floats      : name -> FloatBox (for floating widgets)
    dock_side   : "left" | "right" | "top" | "bottom" — where the chip strip lives
    dock_width  : columns (left/right) or rows (top/bottom) the strip occupies
    """
    layout: LayoutNode
    states: dict[str, str] = field(default_factory=dict)
    floats: dict[str, FloatBox] = field(default_factory=dict)
    dock_side: str = "left"
    dock_width: int = 3
    _maximized: str | None = None

    # ---- state queries ----

    def state_of(self, name: str) -> str:
        return self.states.get(name, NORMAL)

    def docked_names(self) -> list[str]:
        return [n for n, s in self.states.items() if s == DOCKED]

    def floating_names(self) -> list[str]:
        return [n for n, s in self.states.items() if s == FLOATING]

    # ---- mutations ----

    def dock(self, name: str) -> None:
        """Minimize a widget to a chip; remove it from the tile tree."""
        self.states[name] = DOCKED
        if self._maximized == name:
            self._maximized = None
        self.layout = _remove_leaf(self.layout, name)

    def undock(self, name: str, *, weight: float = 1.0) -> None:
        """Restore a docked widget back into the tile tree (appended to root)."""
        self.states[name] = NORMAL
        if not _has_leaf(self.layout, name):
            self.layout = _append_leaf(self.layout, name, weight)

    def maximize(self, name: str) -> None:
        self.states[name] = NORMAL
        if not _has_leaf(self.layout, name):
            self.layout = _append_leaf(self.layout, name, 1.0)
        self._maximized = name

    def restore(self, name: str) -> None:
        if self._maximized == name:
            self._maximized = None

    def toggle_maximize(self, name: str) -> None:
        if self._maximized == name:
            self.restore(name)
        else:
            self.maximize(name)

    def make_floating(self, name: str, box: FloatBox) -> None:
        self.states[name] = FLOATING
        self.floats[name] = box
        self.layout = _remove_leaf(self.layout, name)

    def close(self, name: str) -> None:
        """Remove the widget from the workspace entirely (it can be re-added)."""
        self.states[name] = DOCKED  # parks it in the dock's add list conceptually
        self.layout = _remove_leaf(self.layout, name)
        self.floats.pop(name, None)
        if self._maximized == name:
            self._maximized = None
        # mark as fully removed by deleting the state
        del self.states[name]

    def move_float(self, name: str, dx: int, dy: int) -> None:
        b = self.floats.get(name)
        if b:
            self.floats[name] = FloatBox(b.x + dx, b.y + dy, b.w, b.h)

    def resize_float(self, name: str, dw: int, dh: int) -> None:
        b = self.floats.get(name)
        if b:
            self.floats[name] = FloatBox(b.x, b.y, max(8, b.w + dw), max(3, b.h + dh))

    # ---- resolution: what to paint where ----

    def resolve(self, body: Rect) -> "ResolvedWorkspace":
        """Compute draw rects for tiled + chip strip + floating + maximized. Pure."""
        # 1. carve out the dock strip
        dock_rect, tile_area = self._carve_dock(body)

        # 2. maximized overrides the tile area entirely
        if self._maximized:
            tiled = {self._maximized: tile_area}
        else:
            tiled = resolve_layout(self.layout, tile_area)

        # 3. floating boxes clamped into body
        floating: dict[str, Rect] = {}
        for name in self.floating_names():
            b = self.floats.get(name)
            if not b:
                continue
            x = max(body.x, min(b.x, body.x + body.w - 8))
            y = max(body.y, min(b.y, body.y + body.h - 3))
            w = min(b.w, body.x + body.w - x)
            h = min(b.h, body.y + body.h - y)
            floating[name] = Rect(x, y, w, h)

        return ResolvedWorkspace(
            tiled=tiled, floating=floating,
            dock_rect=dock_rect, dock_chips=self.docked_names(),
            maximized=self._maximized,
        )

    def _carve_dock(self, body: Rect) -> tuple[Rect | None, Rect]:
        """Reserve the dock strip from `body`; return (dock_rect, remaining)."""
        if not self.docked_names():
            return None, body
        dw = self.dock_width
        if self.dock_side == "left":
            dock, rest = body.split_h(dw)
            return dock, rest
        if self.dock_side == "right":
            rest, dock = body.split_h(body.w - dw)
            return dock, rest
        if self.dock_side == "top":
            dock, rest = body.split_v(dw)
            return dock, rest
        # bottom
        rest, dock = body.split_v(body.h - dw)
        return dock, rest

    # ---- chip hit-testing ----

    def chip_at(self, dock_rect: Rect | None, px: int, py: int) -> str | None:
        """Which docked chip (if any) is at screen point (px,py)."""
        if not dock_rect or not dock_rect.contains(px, py):
            return None
        chips = self.docked_names()
        if not chips:
            return None
        if self.dock_side in ("left", "right"):
            idx = py - dock_rect.y
        else:
            idx = px - dock_rect.x
        if 0 <= idx < len(chips):
            return chips[idx]
        return None

    # ---- persistence ----

    def to_dict(self) -> dict:
        from .widget import layout_to_dict
        return {
            "layout": layout_to_dict(self.layout),
            "states": dict(self.states),
            "floats": {n: b.to_dict() for n, b in self.floats.items()},
            "dock_side": self.dock_side,
            "dock_width": self.dock_width,
            "maximized": self._maximized,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkspaceManager":
        from .widget import layout_from_dict
        wm = cls(
            layout=layout_from_dict(d["layout"]),
            states=dict(d.get("states", {})),
            floats={n: FloatBox.from_dict(b) for n, b in d.get("floats", {}).items()},
            dock_side=d.get("dock_side", "left"),
            dock_width=d.get("dock_width", 3),
        )
        wm._maximized = d.get("maximized")
        return wm


@dataclass
class ResolvedWorkspace:
    """The result of WorkspaceManager.resolve() — what the curses driver paints."""
    tiled: dict[str, Rect]
    floating: dict[str, Rect]
    dock_rect: Rect | None
    dock_chips: list[str]
    maximized: str | None

    def all_rects(self) -> dict[str, Rect]:
        """Tiled + floating combined (floating wins on name clash)."""
        out = dict(self.tiled)
        out.update(self.floating)
        return out


# ──────────────────────── layout-tree surgery (pure) ────────────────────────

def _has_leaf(node: LayoutNode, name: str) -> bool:
    if isinstance(node, LayoutLeaf):
        return node.widget_name == name
    return any(_has_leaf(c, name) for c in node.children)


def _remove_leaf(node: LayoutNode, name: str) -> LayoutNode:
    """Return a new tree with the named leaf removed; collapse empty splits."""
    if isinstance(node, LayoutLeaf):
        return node  # caller handles top-level leaf removal separately
    new_children = []
    for c in node.children:
        if isinstance(c, LayoutLeaf):
            if c.widget_name == name:
                continue
            new_children.append(c)
        else:
            pruned = _remove_leaf(c, name)
            # drop empty splits
            if isinstance(pruned, LayoutSplit) and not pruned.children:
                continue
            new_children.append(pruned)
    return LayoutSplit(node.direction, new_children, node.weight)


def _append_leaf(node: LayoutNode, name: str, weight: float) -> LayoutNode:
    """Append a leaf to the root split (creating one if the root is a leaf)."""
    leaf = LayoutLeaf(name, weight)
    if isinstance(node, LayoutLeaf):
        return LayoutSplit("h", [node, leaf])
    return LayoutSplit(node.direction, list(node.children) + [leaf], node.weight)
