"""Configurable, collision-checked keymap.

Keybindings must be customizable, simple, and NON-COLLIDING (avoiding the
"a letter shortcut ate the keystroke" bug). This module is the pure,
testable core of that:

- a default action->keys map (send / newline / interrupt / scroll / steer / ...);
- merge a user `.syntra/keymap.json` over the defaults;
- reverse-lookup key->action for the TUI loop;
- COLLISION DETECTION so two actions can't claim the same key silently.

Keys are simple string tokens ("enter", "esc esc", "ctrl+s", "pageup"). The
curses layer maps raw key codes to these tokens (thin glue); all the logic that
matters is here and unit-tested.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# action -> default key tokens. Enter sends; newline uses fallbacks that work on
# every terminal (Alt+Enter and trailing backslash), since Shift+Enter is
# terminal-dependent. esc esc interrupts.
DEFAULT_KEYMAP: dict[str, list[str]] = {
    "send": ["enter"],
    "newline": ["alt+enter", "shift+enter", "\\+enter"],
    "interrupt": ["esc esc"],
    "quit": ["ctrl+d"],
    "scroll_up": ["pageup"],
    "scroll_down": ["pagedown"],
    "steer_now": ["ctrl+s"],     # inject instruction into the running task NOW (F5)
    "steer_queue": ["ctrl+q"],   # queue instruction for after the current step (F5)
    "find": ["ctrl+f"],
    "file_picker": ["ctrl+t"],   # open the fuzzy file-reference picker (F7)
    "command_palette": ["ctrl+p"],  # open the slash-command palette
    "message_select": ["ctrl+y"],   # enter message-select mode (copy/retry) (F8)
}


_KEY_SYM = {"enter": "↵", "pageup": "pgup", "pagedown": "pgdn", "tab": "tab",
            "space": "␣", "esc": "esc", "del": "del", "backspace": "⌫", "up": "↑",
            "down": "↓", "left": "←", "right": "→"}
_KEY_MOD = {"ctrl": "^", "alt": "⌥", "shift": "⇧", "cmd": "⌘", "super": "❖"}


def key_label(token: str) -> str:
    """Compact display label for a key token — for the footer hint bar, so it reflects
    the LIVE keymap (a rebound action shows its new key, not a hardcoded glyph).
    'ctrl+p'->'^P', 'esc esc'->'esc esc', 'pageup'->'pgup', 'alt+enter'->'⌥↵'. Pure."""
    t = (token or "").strip().lower()
    if not t:
        return ""
    if " " in t:                       # chord like "esc esc"
        return " ".join(key_label(p) for p in t.split())
    if "+" in t:
        *mods, base = t.split("+")
        prefix = "".join(_KEY_MOD.get(m, m + "+") for m in mods)
        b = _KEY_SYM.get(base, base.upper() if len(base) == 1 else base)
        return prefix + b
    return _KEY_SYM.get(t, t.upper() if len(t) == 1 else t)


@dataclass
class Keymap:
    bindings: dict[str, list[str]] = field(default_factory=lambda: {
        k: list(v) for k, v in DEFAULT_KEYMAP.items()
    })

    # ------------------------------------------------------------- queries

    def keys_for(self, action: str) -> list[str]:
        return list(self.bindings.get(action, []))

    def action_for(self, key_token: str) -> str | None:
        """Which action this key triggers (first match in stable order)."""
        kt = (key_token or "").strip().lower()
        for action, keys in self.bindings.items():
            if kt in [k.lower() for k in keys]:
                return action
        return None

    def collisions(self) -> dict[str, list[str]]:
        """Map each key bound to >1 action -> the list of colliding actions.

        F4 guarantee: a keymap with collisions is rejected/flagged, never used
        silently (that was the source of the keystroke-eating bug).
        """
        rev: dict[str, list[str]] = {}
        for action, keys in self.bindings.items():
            for k in keys:
                rev.setdefault(k.lower(), []).append(action)
        return {k: acts for k, acts in rev.items() if len(acts) > 1}

    def is_valid(self) -> bool:
        return not self.collisions()

    # ------------------------------------------------------------- (de)serialize

    def to_dict(self) -> dict:
        return {"_schema_version": 1, "bindings": {k: list(v) for k, v in self.bindings.items()}}

    @classmethod
    def load(cls, path: Path | str | None) -> "Keymap":
        """Defaults merged with a user keymap.json (user keys REPLACE per action).

        A malformed file or one that introduces collisions falls back to the
        valid defaults for the offending actions, so the TUI never boots into an
        unusable (key-eating) state.
        """
        km = cls()
        if not path:
            return km
        p = Path(path)
        if not p.exists():
            return km
        try:
            raw = json.loads(p.read_text())
        except Exception:
            return km
        user = raw.get("bindings", raw) if isinstance(raw, dict) else {}
        for action, keys in (user or {}).items():
            if action in DEFAULT_KEYMAP and isinstance(keys, list) and keys:
                km.bindings[action] = [str(k) for k in keys]
        # Reject collisions: revert colliding actions to their defaults.
        if km.collisions():
            colliding_actions = {a for acts in km.collisions().values() for a in acts}
            for a in colliding_actions:
                km.bindings[a] = list(DEFAULT_KEYMAP[a])
        return km


def keycode_for_token(token: str) -> int | None:
    """Resolve a SINGLE-key token to its terminal key code: ``ctrl+x`` -> 1..26, a
    bare char -> its ordinal, and a few named keys (enter/tab/esc/space/backspace).

    Multi-key chords (``"esc esc"``), modifier combos the terminal can't deliver as one
    code (``alt+enter``/``shift+enter``), and curses-only named keys (``pageup``) return
    ``None`` — the caller leaves those on their robust default handlers. Pure -> tested.
    """
    t = (token or "").strip().lower()
    if not t or " " in t:                       # multi-key chord
        return None
    specials = {"enter": 10, "return": 10, "tab": 9, "space": 32,
                "esc": 27, "escape": 27, "backspace": 127}
    if t in specials:
        return specials[t]
    if t.startswith("ctrl+") and len(t) == 6 and "a" <= t[5] <= "z":
        return ord(t[5]) - ord("a") + 1         # ctrl+a=1 … ctrl+z=26
    if len(t) == 1:
        return ord(t)
    return None


def remap_table(km: "Keymap") -> dict[int, int]:
    """Build ``user_keycode -> canonical_default_keycode`` for the actions the user has
    REBOUND to a single key. The TUI applies this right after reading a key, so a custom
    key is translated to the default code the existing handlers already check — no handler
    needs rewriting, and fundamental keys left on multi-key/unresolvable defaults (send/
    newline/interrupt) are untouched. Pure -> tested."""
    out: dict[int, int] = {}
    for action, default_keys in DEFAULT_KEYMAP.items():
        user_keys = km.keys_for(action)
        if user_keys == list(default_keys):
            continue                            # not customized
        canon = next((c for c in (keycode_for_token(k) for k in default_keys)
                      if c is not None), None)
        if canon is None:
            continue                            # default isn't a single key -> skip
        for uk in user_keys:
            uc = keycode_for_token(uk)
            if uc is not None and uc != canon:
                out[uc] = canon
    return out


# #173: bind an arbitrary /command to a single key. This is a SEPARATE layer from the fixed
# action keymap above — those actions are hardwired handlers; a command bind runs a slash
# command string via the TUI's _submit(). Keys the cockpit already owns must never be stolen.
#
# Keycodes the cockpit hardwires (from cli/tui2.py): Ctrl+P=16 (menu), Ctrl+E=5 (panels),
# Ctrl+L=12 (clear), Ctrl+K=11 (stop), Ctrl+Y=25 (copy/yank), Ctrl+T=20 (files), Ctrl+R=18
# (backtrack), Ctrl+C=3 (quit/clear), Ctrl+O=15 (newline), Ctrl+W=23, Ctrl+U=21, Ctrl+F=6
# (search — also a DEFAULT_KEYMAP action). Plus every DEFAULT_KEYMAP-bound key.
_RESERVED_KEYCODES = frozenset({3, 5, 6, 9, 10, 11, 12, 13, 15, 16, 18, 20, 21, 23, 25, 27})


def _reserved_keycodes() -> set:
    """Every keycode the app already uses (hardwired cockpit keys + all DEFAULT_KEYMAP keys)
    — a command bind may not claim any of these (would eat a builtin)."""
    reserved = set(_RESERVED_KEYCODES)
    for keys in DEFAULT_KEYMAP.values():
        for k in keys:
            c = keycode_for_token(k)
            if c is not None:
                reserved.add(c)
    return reserved


def command_bind_conflicts(binds: dict) -> dict:
    """#173: of the `{key_token: /command}` binds, which tokens collide with a reserved
    builtin key (so they'd eat it)? Returns the colliding subset. Pure -> unit-tested."""
    reserved = _reserved_keycodes()
    out = {}
    for token, cmd in (binds or {}).items():
        c = keycode_for_token(token)
        if c is not None and c in reserved:
            out[token] = cmd
    return out


def command_bind_keycode_map(binds: dict) -> dict:
    """#173: `{key_token: /command}` -> `{keycode: /command}`, keeping ONLY binds that are
    (a) a single resolvable key, (b) a value that is a /command, and (c) NOT a reserved builtin
    key. The TUI checks this map right after reading a key (on an empty prompt). Pure."""
    reserved = _reserved_keycodes()
    out: dict[int, str] = {}
    for token, cmd in (binds or {}).items():
        cmd = str(cmd or "").strip()
        if not cmd.startswith("/"):
            continue                            # only slash-commands are bindable
        c = keycode_for_token(token)
        if c is None or c in reserved:
            continue                            # unresolvable/multi-key or reserved -> drop
        out[c] = cmd
    return out


def load_command_binds(path) -> dict:
    """#173: load `{key_token: /command}` from a keybinds.json (fail-open to {} on
    missing/corrupt). Only /command values survive; the caller resolves + conflict-checks."""
    from pathlib import Path
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt -> no binds, never blocks boot
        return {}
    binds = raw.get("binds", raw) if isinstance(raw, dict) else {}
    out = {}
    for token, cmd in (binds or {}).items():
        if isinstance(token, str) and isinstance(cmd, str) and cmd.strip().startswith("/"):
            out[token.strip().lower()] = cmd.strip()
    return out


def save_command_binds(path, binds: dict) -> None:
    """#173: persist `{key_token: /command}` to keybinds.json (best-effort)."""
    from pathlib import Path
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"_schema_version": 1, "binds": dict(binds or {})},
                                indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass
