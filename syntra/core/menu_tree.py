"""The Syntra command-menu tree.

Builds the chained menu the user navigates (no command memorization). Categories
drill into full lists generated on demand: all models, all themes, all layouts,
all panels. Leaf items carry an action id the TUI resolves.

Action ids (resolved in cli/tui2.py):
    layout:<name>        switch layout
    theme:<name>         switch theme
    panel:<kind>         toggle a panel
    model:<id>           pin the executor (or show route) to a model
    session:new          clear/new session
    session:resume       resume last task
    copy:last            copy the last assistant message
    help:keys            show keybindings
"""

from __future__ import annotations

from .menu import MenuItem


def build_root_menu(*, themes, layouts, panels, models, current_theme="") -> list:
    """Construct the root menu items. Lists are passed in so this stays pure."""

    def _theme_items():
        return [MenuItem(label=t, action=f"theme:{t}",
                         hint="●" if t == current_theme else "")
                for t in themes]

    def _layout_items():
        return [MenuItem(label=name, action=f"layout:{name}") for name in layouts]

    def _panel_items():
        return [MenuItem(label=label, action=f"panel:{kind}")
                for kind, label in panels]

    def _model_items():
        # Open the SAME searchable model picker as /models (consistent everywhere) —
        # the old inline submenus were a different, un-searchable list (user F34).
        return [
            MenuItem(label="Planner model", action="open_models:planner", hint="search"),
            MenuItem(label="Executor model", action="open_models:executor", hint="search"),
            MenuItem(label="Reviewer model", action="open_models:reviewer", hint="search"),
            MenuItem(label="All roles (same model)", action="open_models:", hint="search"),
        ]

    def _session_items():
        return [
            MenuItem(label="New session", action="session:new"),
            MenuItem(label="Resume last", action="session:resume"),
            MenuItem(label="Fork current", action="session:fork"),
        ]

    def _copy_items():
        return [
            MenuItem(label="Copy last reply", action="copy:last"),
            MenuItem(label="Native select mode", action="copy:native"),
        ]

    def _help_items():
        return [
            MenuItem(label="Keybindings", action="help:keys"),
            MenuItem(label="Commands", action="help:commands"),
        ]

    return [
        MenuItem(label="Models",   submenu=_model_items,  hint=f"{len(models)}"),
        MenuItem(label="Themes",   submenu=_theme_items,  hint=current_theme),
        MenuItem(label="Layouts",  submenu=_layout_items),
        MenuItem(label="Panels",   submenu=_panel_items),
        MenuItem(label="Session",  submenu=_session_items),
        MenuItem(label="Copy",     submenu=_copy_items),
        MenuItem(label="Help",     submenu=_help_items),
    ]
