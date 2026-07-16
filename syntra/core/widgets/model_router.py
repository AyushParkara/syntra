"""Model router widget — shows candidate models + routing metrics.

Optional, hidden by default. Displays the top models with speed/cost/quality
and which one is currently selected per role. Like the MODEL ROUTING panel
described in the design spec.
"""

from __future__ import annotations

from ..widget import Widget, RenderLine


class ModelRouterWidget(Widget):
    kind = "model_router"
    focusable = False

    def __init__(self, *, title: str = "ROUTER", on_event=None):
        super().__init__(title=title, on_event=on_event)
        self._rows: list[tuple[str, str, float, float]] = []  # (model, provider, tps, score)
        self._selected: dict[str, str] = {}  # role -> model
        self._tick = 0
        self._loaded = False

    def tick(self) -> bool:
        self._tick += 1
        if not self._loaded or self._tick % 80 == 1:
            self._refresh()
            return True
        return False

    def _refresh(self) -> None:
        try:
            from ..catalog import Catalog
            from ..router import Router, TaskProfile
            from ..registry import ProviderRegistry
            from ..overrides import Overrides
            import os

            cat_path = os.environ.get("SYNTRA_CATALOG_PATH")
            cat = Catalog.load(cat_path) if cat_path else _default_catalog()
            if cat is None:
                return
            try:
                registry = ProviderRegistry.load()
            except Exception:  # noqa: BLE001
                registry = None
            ov = Overrides.load()

            resolve = (lambda mid: (registry.find_for_model(mid).name
                                    if registry and registry.find_for_model(mid) else None))
            router = Router(
                cat,
                route_resolver=resolve,
                is_blacklisted=ov.is_blacklisted,
                extra_penalty=ov.penalty_for,
                extra_specialties=ov.extra_specialties,
                role_edits=ov.role_edits_for,
                pinned_model=ov.pinned_model_for,
            )

            self._selected = {}
            for role in ("planner", "executor", "reviewer"):
                try:
                    dec = router.pick(TaskProfile(role=role))
                    self._selected[role] = dec.model.id.split("/")[-1]
                except Exception:  # noqa: BLE001
                    self._selected[role] = "?"

            # top models by intelligence
            self._rows = []
            for m in sorted(cat.models, key=lambda x: -x.intelligence_index)[:12]:
                prov = resolve(m.id) or "-"
                short = m.id.split("/")[-1]
                self._rows.append((short, prov, m.speed_tps, m.intelligence_index))
            self._loaded = True
        except Exception:  # noqa: BLE001
            pass

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, width)
        out: list[RenderLine] = []
        out.append(RenderLine(" MODEL ROUTER"[:w], "dim"))

        # selected per role with clear formatting
        for role, model in self._selected.items():
            out.append(RenderLine(f"  {role[:3]} → {model}"[:w], "accent"))
        out.append(RenderLine("", "default"))

        # top candidates with availability indicator
        for short, prov, tps, score in self._rows[:height - len(out)]:
            avail = "●" if prov != "-" else "○"
            style = "default" if prov != "-" else "dim"
            line = f"  {avail} {short[:14]:<14} {tps:>3.0f}t/s {score:>4.1f}"
            out.append(RenderLine(line[:w], style))

        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]


def _default_catalog():
    try:
        from ..catalog import Catalog
        from ..paths import default_catalog_path
        p = default_catalog_path()           # packaged catalog (env → syntra/data → dev)
        if p.exists():
            return Catalog.load(p)
    except Exception:  # noqa: BLE001
        pass
    return None
