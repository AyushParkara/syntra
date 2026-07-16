"""Raw key-code -> key-token mapping for the curses loop (Track T1 / F4).

core/keymap.py works in terms of string tokens ("ctrl+t", "pageup", "enter").
The curses loop reads integer key codes. This module is the PURE bridge: map a
key code to its token so the loop can consult the configurable Keymap instead of
hardcoding integers. Kept here (no curses import) so it is unit-testable; the
special-key codes are passed in by the caller (curses.KEY_* constants).

ctrl_token: control bytes 1..26 -> "ctrl+a".."ctrl+z" (excluding tab/enter which
are their own keys). Returns None for anything else.
"""

from __future__ import annotations

# Control bytes that are really named keys, not Ctrl+letter chords.
_CTRL_EXCLUDE = {9, 10, 13}  # tab, newline, carriage-return


def ctrl_token(ch: int) -> str | None:
    """Map a control byte (1..26) to a 'ctrl+<letter>' token, else None."""
    if 1 <= ch <= 26 and ch not in _CTRL_EXCLUDE:
        return f"ctrl+{chr(ch + 96)}"
    return None


def special_token(ch: int, codes: dict[int, str]) -> str | None:
    """Map a curses special key code to a token using the provided code->token map."""
    return codes.get(ch)
