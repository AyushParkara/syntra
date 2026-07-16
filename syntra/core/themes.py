"""TUI color themes (real, switchable).

Each theme maps semantic roles (user, assistant, tool, system, accent, dim,
error, code) to ANSI/256-color codes the curses layer applies. Themes are
selectable at runtime (/themes <name>) or via the SYNTRA_THEME env var. Pure
data + lookups -> unit-tested; the curses layer reads `theme_colors(name)`.
"""

from __future__ import annotations

import os

# role -> xterm-256 color index. Chosen for contrast on dark terminals.
# Includes syntax-highlight roles (keyword/string/comment/number) per theme.
_THEMES: dict[str, dict[str, int]] = {
    # Syntra's signature blue palette: clean blue accents, bright text.
    "default": {"user": 75, "assistant": 252, "tool": 110, "system": 244,
                "accent": 69, "dim": 239, "error": 210, "code": 115,
                "keyword": 69, "string": 115, "comment": 243, "number": 216,
                "diff_add": 114, "diff_del": 210, "diff_hunk": 75,
                "focus_border": 69},
    "midnight": {"user": 75, "assistant": 252, "tool": 67, "system": 60,
                 "accent": 81, "dim": 238, "error": 204, "code": 114,
                 "keyword": 111, "string": 114, "comment": 60, "number": 222,
                 "diff_add": 72, "diff_del": 168, "diff_hunk": 81,
                 "focus_border": 81},
    "solarized": {"user": 33, "assistant": 187, "tool": 136, "system": 66,
                  "accent": 37, "dim": 240, "error": 160, "code": 64,
                  "keyword": 125, "string": 64, "comment": 66, "number": 136,
                  "diff_add": 64, "diff_del": 160, "diff_hunk": 37,
                  "focus_border": 37},
    "dracula": {"user": 212, "assistant": 253, "tool": 141, "system": 103,
                "accent": 212, "dim": 238, "error": 203, "code": 84,
                "keyword": 212, "string": 228, "comment": 103, "number": 215,
                "diff_add": 84, "diff_del": 203, "diff_hunk": 212,
                "focus_border": 212},
    "matrix": {"user": 47, "assistant": 84, "tool": 35, "system": 28,
               "accent": 46, "dim": 22, "error": 196, "code": 120,
               "keyword": 46, "string": 120, "comment": 28, "number": 84,
               "diff_add": 47, "diff_del": 196, "diff_hunk": 46,
               "focus_border": 46},
    "amber": {"user": 214, "assistant": 223, "tool": 172, "system": 130,
              "accent": 214, "dim": 94, "error": 203, "code": 180,
              "keyword": 214, "string": 180, "comment": 130, "number": 223,
              "diff_add": 178, "diff_del": 203, "diff_hunk": 214,
              "focus_border": 214},
    "nord": {"user": 110, "assistant": 254, "tool": 109, "system": 66,
             "accent": 111, "dim": 239, "error": 174, "code": 108,
             "keyword": 111, "string": 108, "comment": 66, "number": 222,
             "diff_add": 108, "diff_del": 174, "diff_hunk": 111,
             "focus_border": 111},
    # Catppuccin family (community pastel palettes, MIT). Hex -> nearest xterm-256.
    "catppuccin-mocha": {"user": 111, "assistant": 189, "tool": 117, "system": 243,
                         "accent": 183, "dim": 240, "error": 211, "code": 150,
                         "keyword": 183, "string": 150, "comment": 243, "number": 216,
                         "diff_add": 150, "diff_del": 211, "diff_hunk": 117,
                         "focus_border": 183},
    "catppuccin-macchiato": {"user": 111, "assistant": 189, "tool": 116, "system": 102,
                             "accent": 183, "dim": 239, "error": 210, "code": 150,
                             "keyword": 183, "string": 150, "comment": 102, "number": 216,
                             "diff_add": 150, "diff_del": 210, "diff_hunk": 116,
                             "focus_border": 183},
    "catppuccin-frappe": {"user": 110, "assistant": 189, "tool": 116, "system": 102,
                          "accent": 183, "dim": 60, "error": 174, "code": 150,
                          "keyword": 183, "string": 150, "comment": 102, "number": 216,
                          "diff_add": 150, "diff_del": 174, "diff_hunk": 116,
                          "focus_border": 183},
    "catppuccin-latte": {"user": 33, "assistant": 60, "tool": 31, "system": 102,
                         "accent": 98, "dim": 250, "error": 160, "code": 28,
                         "keyword": 98, "string": 28, "comment": 102, "number": 130,
                         "diff_add": 28, "diff_del": 160, "diff_hunk": 31,
                         "focus_border": 98},
    # Tokyo Night (very popular dark editor theme).
    "tokyonight": {"user": 111, "assistant": 188, "tool": 117, "system": 60,
                   "accent": 141, "dim": 238, "error": 203, "code": 115,
                   "keyword": 141, "string": 115, "comment": 60, "number": 215,
                   "diff_add": 115, "diff_del": 203, "diff_hunk": 117,
                   "focus_border": 141},
    # Gruvbox (warm retro, hugely popular).
    "gruvbox": {"user": 109, "assistant": 223, "tool": 108, "system": 245,
                "accent": 214, "dim": 240, "error": 167, "code": 142,
                "keyword": 167, "string": 142, "comment": 245, "number": 175,
                "diff_add": 142, "diff_del": 167, "diff_hunk": 109,
                "focus_border": 214},
    # --- Signature "cosmic" family: deep-space backgrounds, neon accents.
    # Inspired by Ghostty/Warp/cosmic-terminal aesthetics; values are ours.
    "cosmic": {"user": 51, "assistant": 189, "tool": 99, "system": 60,
               "accent": 207, "dim": 237, "error": 197, "code": 86,
               "keyword": 207, "string": 86, "comment": 60, "number": 219,
               "diff_add": 85, "diff_del": 197, "diff_hunk": 51,
               "focus_border": 207},
    "nebula": {"user": 141, "assistant": 225, "tool": 135, "system": 97,
               "accent": 213, "dim": 53, "error": 204, "code": 159,
               "keyword": 213, "string": 159, "comment": 97, "number": 223,
               "diff_add": 121, "diff_del": 204, "diff_hunk": 141,
               "focus_border": 213},
    "aurora": {"user": 80, "assistant": 254, "tool": 79, "system": 66,
               "accent": 121, "dim": 238, "error": 211, "code": 158,
               "keyword": 121, "string": 158, "comment": 66, "number": 222,
               "diff_add": 158, "diff_del": 211, "diff_hunk": 80,
               "focus_border": 121},
    # Rosé Pine (very popular muted theme).
    "rose-pine": {"user": 73, "assistant": 223, "tool": 175, "system": 60,
                  "accent": 168, "dim": 238, "error": 203, "code": 116,
                  "keyword": 168, "string": 116, "comment": 60, "number": 180,
                  "diff_add": 116, "diff_del": 203, "diff_hunk": 73,
                  "focus_border": 168},
    # Everforest (calm green, popular).
    "everforest": {"user": 108, "assistant": 187, "tool": 144, "system": 245,
                   "accent": 142, "dim": 240, "error": 174, "code": 151,
                   "keyword": 174, "string": 151, "comment": 245, "number": 179,
                   "diff_add": 151, "diff_del": 174, "diff_hunk": 108,
                   "focus_border": 142},
    # Monokai (classic vivid).
    "monokai": {"user": 81, "assistant": 253, "tool": 148, "system": 242,
                "accent": 197, "dim": 238, "error": 197, "code": 186,
                "keyword": 197, "string": 186, "comment": 242, "number": 141,
                "diff_add": 148, "diff_del": 197, "diff_hunk": 81,
                "focus_border": 197},
    # #237 Colorblind-safe (deuteranopia/protanopia). Red-green is the common CVD, and it's
    # exactly the diff add/del pairing — so here ADD is BLUE and DEL is ORANGE (a blue/orange
    # axis both deuteranopes and protanopes separate reliably), errors are orange not red,
    # and the accent is a high-chroma blue. Distinct from every other theme's green/red diff.
    "colorblind": {"user": 39, "assistant": 253, "tool": 74, "system": 244,
                   "accent": 33, "dim": 240, "error": 208, "code": 45,
                   "keyword": 33, "string": 45, "comment": 244, "number": 214,
                   "diff_add": 39, "diff_del": 208, "diff_hunk": 33,
                   "focus_border": 33},
    # #237 ANSI-16-only: uses ONLY the 16 standard terminal colours (0–15) so it renders
    # correctly on a terminal without 256-colour support (or with a heavily-customised
    # 16-colour palette the user tuned themselves). No 6×6×6-cube indices anywhere.
    "ansi16": {"user": 12, "assistant": 15, "tool": 14, "system": 8,
               "accent": 12, "dim": 8, "error": 9, "code": 10,
               "keyword": 12, "string": 10, "comment": 8, "number": 11,
               "diff_add": 10, "diff_del": 9, "diff_hunk": 14,
               "focus_border": 12},
}

_DEFAULT = "default"

# Load user-defined custom themes from ~/.config/syntra/themes.json
# Format: {"theme_name": {"user": 75, "accent": 69, ...}}
def _load_custom_themes():
    import json
    paths = [
        os.path.expanduser("~/.config/syntra/themes.json"),
        os.path.join(os.environ.get("SYNTRA_STATE_DIR", ".syntra"), "themes.json"),
    ]
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, dict):
                for name, colors in custom.items():
                    if isinstance(colors, dict) and name not in _THEMES:
                        _THEMES[name.lower()] = colors
        except Exception:  # noqa: BLE001 - a bad/binary themes.json (incl. UnicodeDecodeError)
            pass            # must NEVER abort the module import (would leave it half-defined)


# Import-time best-effort: even an unexpected failure here must not leave the module
# partially initialized (without list_themes/theme_colors), which silently breaks every
# later `from .themes import ...` in the process.
try:
    _load_custom_themes()
except Exception:  # noqa: BLE001
    pass


def list_themes() -> list[str]:
    return sorted(_THEMES.keys())


def color_enabled() -> bool:
    """Honor the NO_COLOR standard (no-color.org): any value of ``NO_COLOR`` disables
    color output, UNLESS ``FORCE_COLOR`` is set (which wins). When False, the TUI loads
    no color pairs and renders monochrome (structure is still carried by glyphs/bold)."""
    if os.environ.get("FORCE_COLOR"):
        return True
    return os.environ.get("NO_COLOR") is None


def current_theme() -> str:
    name = os.environ.get("SYNTRA_THEME", _DEFAULT).strip().lower()
    return name if name in _THEMES else _DEFAULT


def _xterm_rgb(idx: int) -> tuple[int, int, int]:
    """Approximate RGB for an xterm-256 colour index (system / 6×6×6 cube / grayscale)."""
    idx = int(idx)
    if 0 <= idx <= 15:
        base = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
                (128, 0, 128), (0, 128, 128), (192, 192, 192), (128, 128, 128),
                (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255),
                (0, 255, 255), (255, 255, 255)]
        return base[idx]
    if 16 <= idx <= 231:
        i = idx - 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[i // 36], steps[(i % 36) // 6], steps[i % 6])
    if 232 <= idx <= 255:
        v = 8 + (idx - 232) * 10
        return (v, v, v)
    return (0, 0, 0)


def luminance(idx: int) -> float:
    """Perceptual luminance (0–255) of an xterm-256 colour index (Rec. 601 weights)."""
    r, g, b = _xterm_rgb(idx)
    return 0.299 * r + 0.587 * g + 0.114 * b


def is_light_color(idx: int) -> bool:
    """True if the colour reads as 'light' (so a terminal with this background wants the
    light theme variant)."""
    return luminance(idx) > 127.5


def theme_variant() -> str:
    """Which palette variant to use: ``SYNTRA_THEME_VARIANT`` (light|dark) wins; else infer
    from the terminal's ``COLORFGBG`` (the de-facto bg-colour env — last field is the
    background index); else default 'dark'. (M6: per-role {dark,light} colours adapt to a
    light or dark terminal instead of assuming dark.)"""
    v = (os.environ.get("SYNTRA_THEME_VARIANT") or "").strip().lower()
    if v in ("light", "dark"):
        return v
    fgbg = os.environ.get("COLORFGBG", "")
    parts = [p for p in fgbg.split(";") if p.strip().lstrip("-").isdigit()]
    if parts:
        try:
            return "light" if is_light_color(int(parts[-1])) else "dark"
        except ValueError:
            pass
    return "dark"


def parse_osc11_reply(reply: str) -> str | None:
    """#237: parse a terminal's OSC-11 background-colour reply into 'light' or 'dark'.

    A terminal answers an OSC-11 query with ``ESC ] 11 ; rgb:RRRR/GGGG/BBBB BEL`` (or ST).
    We read the three 16-bit channels, compute luminance, and return the variant the theme's
    ``auto`` mode should use. Returns None if the reply isn't a recognizable OSC-11 colour
    (so the caller keeps its current variant). Pure → unit-tested; the actual tty query is a
    thin caller so this stays deterministic."""
    import re
    if not reply:
        return None
    m = re.search(r"\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not m:
        return None
    def _chan(h: str) -> int:
        # channels may be 1–4 hex digits; scale to 0–255 by the top byte.
        h = h[:2].ljust(2, h[-1] if h else "0")
        try:
            return int(h, 16)
        except ValueError:
            return 0
    r, g, b = _chan(m.group(1)), _chan(m.group(2)), _chan(m.group(3))
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "light" if lum > 127.5 else "dark"


def query_terminal_bg(*, timeout: float = 0.12) -> str | None:
    """#237: actively ask the terminal for its background colour (OSC-11) and return
    'light'/'dark', or None if unsupported/no-tty/timeout. Lets `auto` RE-RESOLVE the palette
    on a live background change (light↔dark) instead of freezing COLORFGBG at launch. Safe:
    only runs against a real tty, restores termios, never raises."""
    import sys, select
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        import termios, tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b]11;?\x07")
            sys.stdout.flush()
            buf = ""
            end = __import__("time").monotonic() + timeout
            while __import__("time").monotonic() < end:
                r, _, _ = select.select([fd], [], [], max(0.0, end - __import__("time").monotonic()))
                if not r:
                    break
                ch = sys.stdin.read(1)
                buf += ch
                if ch in ("\x07", "\\"):     # BEL or ST terminator
                    break
            return parse_osc11_reply(buf)
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    except Exception:  # noqa: BLE001 - any failure → caller keeps its current variant
        return None


def theme_colors(name: str | None = None, *, variant: str | None = None) -> dict[str, int]:
    """Return the role->color map for a theme (falls back to default).

    A role's value may be a plain xterm-256 int, OR a ``{"dark": N, "light": M}`` dict that
    resolves to the active ``variant`` (M6) — so a theme can look right on both light and
    dark terminals. Plain ints are variant-agnostic (unchanged behaviour). Also derives a
    few semantic roles used by the activity tree from existing palette entries: `thinking`
    (muted, like comments), `mode` (the mode-chip accent), `ok` (success green)."""
    n = (name or current_theme()).strip().lower()
    var = (variant or theme_variant())
    raw = _THEMES.get(n, _THEMES[_DEFAULT])
    colors: dict[str, int] = {}
    for role, val in raw.items():
        if isinstance(val, dict):
            colors[role] = int(val.get(var, val.get("dark", next(iter(val.values()), 0))))
        else:
            colors[role] = val
    colors.setdefault("thinking", colors.get("comment", colors.get("dim", 243)))
    colors.setdefault("mode", colors.get("accent", 69))
    colors.setdefault("ok", colors.get("diff_add", 114))
    return colors


def set_theme(name: str) -> bool:
    """Select a theme for this process (sets SYNTRA_THEME). True if valid."""
    n = (name or "").strip().lower()
    if n not in _THEMES:
        return False
    os.environ["SYNTRA_THEME"] = n
    return True
