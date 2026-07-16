"""Terminal image rendering — show real pixels in the terminal (view / preview).

Most TUI coding agents can't display an image; ours can. This module is the PURE core:
detect which graphics protocol the terminal supports, and encode image bytes into the
escape sequence that paints them. The curses layer (tui2) reserves blank rows and writes
the returned sequence straight to the tty — curses owns the text cells, we paint pixels
into the reserved region out-of-band.

Protocols, best → worst, with graceful fallback:
  - Kitty graphics protocol  (Kitty, Ghostty, WezTerm, Konsole) — inline base64, chunked.
  - iTerm2 inline images      (iTerm2 ≥ 3, WezTerm) — OSC 1337 File=.
  - Sixel                     (foot, mlterm, xterm -ti, WezTerm) — DEC sixel bitmap.
  - Unicode half-block        (any truecolor terminal) — ▀ with fg/bg per cell. Last resort;
                              needs pixel access (Pillow) — omitted if Pillow is absent.
  - none                      — caller shows a one-line text placeholder.

DEPENDENCY-FREE core: image DIMENSIONS are parsed from the file header here (PNG/JPEG/GIF/
WEBP), so sizing never needs Pillow. Pillow is used ONLY for the half-block fallback (pixel
resize) and is optional — everything else works without it. Pure + unit-tested; detection
reads env (and optionally a caller-supplied DA1 reply), encoders are byte→str transforms.
"""

from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass


# ── protocol identifiers ──────────────────────────────────────────────────────
KITTY = "kitty"
ITERM2 = "iterm2"
SIXEL = "sixel"
HALFBLOCK = "halfblock"
NONE = "none"


# ── image dimensions from header bytes (dependency-free) ──────────────────────
def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels parsed from the file header, or None if unknown.

    Covers PNG / GIF / JPEG / WEBP (the same set multimodal.sniff_mime supports). No Pillow —
    we read the few header bytes each format puts its dimensions in. Pure."""
    if len(data) < 24:
        return None
    # PNG: 8-byte sig, then IHDR with width/height as big-endian uint32 at offset 16.
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)
    # GIF: 'GIF87a'/'GIF89a' then logical-screen width/height as little-endian uint16.
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h)
    # WEBP (VP8/VP8L/VP8X) — RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fmt = data[12:16]
        try:
            if fmt == b"VP8 ":
                # lossy: 16-bit width/height (14 bits) at offset 26, after the start code.
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return (w, h)
            if fmt == b"VP8L":
                b = data[21:25]
                bits = int.from_bytes(b, "little")
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return (w, h)
            if fmt == b"VP8X":
                w = (int.from_bytes(data[24:27], "little") & 0xFFFFFF) + 1
                h = (int.from_bytes(data[27:30], "little") & 0xFFFFFF) + 1
                return (w, h)
        except struct.error:
            return None
    # JPEG: scan the SOFn marker for height/width (big-endian uint16).
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15 carry dimensions.
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h)
            # otherwise skip this segment by its length field.
            if i + 4 > n:
                break
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
    return None


# ── terminal capability detection ─────────────────────────────────────────────
@dataclass(frozen=True)
class TermCaps:
    """What the current terminal can do, and whether graphics are safe to emit here."""
    protocol: str                 # KITTY | ITERM2 | SIXEL | HALFBLOCK | NONE
    multiplexed: bool = False     # inside tmux/screen/Zellij — graphics disabled by default
    reason: str = ""              # how we decided (for diagnostics)


def _env(name: str, env: dict | None) -> str:
    return (env if env is not None else os.environ).get(name, "") or ""


def detect_caps(env: dict | None = None, *, da1: str | None = None,
                allow_multiplexed: bool = False) -> TermCaps:
    """Decide the best graphics protocol for this terminal.

    Cheap path: environment variables (TERM, TERM_PROGRAM, KITTY_WINDOW_ID, …) identify the
    common terminals with no I/O. `da1` (an optional Device-Attributes reply the caller may
    have queried) confirms Sixel when env is ambiguous: support iff its parameter list contains
    `4`. Inside a multiplexer (tmux/screen/Zellij) graphics corrupt the scrollback and the
    Kitty probe leaks into the pane title, so we DISABLE by default there (override with
    allow_multiplexed). Pure — no terminal I/O of its own."""
    term = _env("TERM", env).lower()
    term_program = _env("TERM_PROGRAM", env).lower()

    multiplexed = bool(_env("TMUX", env) or _env("STY", env)
                       or _env("ZELLIJ", env) or "screen" in term or "tmux" in term)

    def _cap(proto, reason):
        return TermCaps(protocol=(NONE if (multiplexed and not allow_multiplexed and proto != HALFBLOCK)
                                  else proto),
                        multiplexed=multiplexed, reason=reason)

    # Kitty graphics: Kitty itself, Ghostty, WezTerm, Konsole (env-detectable).
    if _env("KITTY_WINDOW_ID", env) or "kitty" in term:
        return _cap(KITTY, "env: kitty")
    if _env("GHOSTTY_RESOURCES_DIR", env) or "ghostty" in term_program:
        return _cap(KITTY, "env: ghostty")
    if _env("WEZTERM_PANE", env) or "wezterm" in term_program:
        return _cap(KITTY, "env: wezterm (kitty proto)")
    if "konsole" in term_program or _env("KONSOLE_VERSION", env):
        return _cap(KITTY, "env: konsole")

    # iTerm2 inline images.
    if "iterm" in term_program or _env("ITERM_SESSION_ID", env):
        return _cap(ITERM2, "env: iterm2")

    # RUNTIME probe reply wins over env guesses (the terminal told us the truth). A kitty ack in
    # the reply → kitty graphics; a DA1 param list with `4` → sixel. This is what makes detection
    # self-managing: e.g. a VTE terminal whose env "looks" 256color but whose DA1 lacks `4` is
    # correctly NOT given sixel (proven: DA1 \x1b[?65;1;9c → no graphics), while a real kitty term
    # is correctly given kitty even with a bare TERM.
    if da1 and _da1_has_kitty(da1):
        return _cap(KITTY, "probe: kitty graphics ack")
    # Sixel: confirmed by a DA1 reply containing `4` (the sixel attribute), or known sixel
    # terminals by env.
    if da1 and _da1_has_sixel(da1):
        return _cap(SIXEL, "probe/da1: sixel attribute present")
    if any(t in term for t in ("foot", "mlterm", "yaft", "sixel")) or "foot" in term_program:
        return _cap(SIXEL, "env: known sixel terminal")

    # Truecolor terminal → half-block fallback (works even multiplexed; needs Pillow at encode).
    if _env("COLORTERM", env) in ("truecolor", "24bit") or term.endswith("-256color"):
        return TermCaps(protocol=HALFBLOCK, multiplexed=multiplexed,
                        reason="env: truecolor → half-block fallback")
    return TermCaps(protocol=NONE, multiplexed=multiplexed, reason="no known graphics support")


def _da1_has_sixel(da1: str) -> bool:
    """The DA1 portion of a probe reply is `ESC [ ? <params> c`. Sixel support = attribute `4` in
    the param list. Robust to a kitty ack being prepended (we scan for the `[?...c` span)."""
    import re
    m = re.search(r"\x1b\[\?([0-9;]+)c", da1)
    body = m.group(1) if m else da1.replace("\x1b", "").lstrip("[?").rstrip("c")
    return "4" in [p.strip() for p in body.split(";")]


def _da1_has_kitty(reply: str) -> bool:
    """A terminal that speaks the kitty graphics protocol answers KITTY_QUERY with a kitty
    APC response (`ESC _ G ... ESC \\`) BEFORE the DA1 `c`. Its presence = kitty support."""
    return ("\x1b_G" in reply) and (("OK" in reply) or ("i=31337" in reply) or ("_Gi=" in reply))


def probe_terminal(*, timeout: float = 0.4, out_fd: int | None = None,
                   in_fd: int | None = None) -> str:
    """RUNTIME capability probe (the ONE place this module does tty I/O). Writes KITTY_QUERY
    (kitty ack + DA1) to the terminal and reads the raw reply, so detect_caps() can decide from
    what the terminal ACTUALLY reports instead of env guesses. Returns the raw reply (feed it to
    detect_caps(da1=...)) or "" when not a tty / on any error. Safe: restores termios, never
    raises. Must run BEFORE curses puts the tty in its own mode (call once at startup)."""
    import sys as _sys
    o = out_fd if out_fd is not None else _sys.stdout.fileno()
    i = in_fd if in_fd is not None else _sys.stdin.fileno()
    try:
        if not (os.isatty(i) and os.isatty(o)):
            return ""
    except OSError:
        return ""
    import termios, tty, select, time as _t
    try:
        old = termios.tcgetattr(i)
    except termios.error:
        return ""
    reply = ""
    try:
        tty.setcbreak(i)
        os.write(o, KITTY_QUERY.encode())
        end = _t.time() + timeout
        while _t.time() < end:
            r, _, _ = select.select([i], [], [], 0.05)
            if r:
                try:
                    reply += os.read(i, 4096).decode("latin-1", "replace")
                except OSError:
                    break
                if reply.rstrip().endswith("c"):     # DA1 terminator → reply complete
                    break
    except Exception:  # noqa: BLE001 - probing must never break startup
        pass
    finally:
        try:
            termios.tcsetattr(i, termios.TCSADRAIN, old)
        except Exception:  # noqa: BLE001
            pass
    return reply


def caps_for_terminal(*, probe: bool = True, **kw) -> TermCaps:
    """Convenience: run the runtime probe (once) and return the resolved TermCaps. The caller
    should cache the result and reuse it (don't re-probe per image). `probe=False` skips tty I/O
    (env-only) — used in tests / non-tty contexts."""
    da1 = probe_terminal() if probe else None
    return detect_caps(da1=da1, **kw)


# Detection probe sequences the caller can WRITE to the tty (we don't do tty I/O here):
DA1_QUERY = "\x1b[c"                                   # → ESC [ ? ... c  (sixel = contains '4')
KITTY_QUERY = "\x1b_Gi=31337,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c"  # kitty ack before DA1 = supported
CELL_PIXELS_QUERY = "\x1b[14t"                         # → ESC [ 4 ; <h> ; <w> t  (window px size)


# ── encoders: image bytes → escape sequence ───────────────────────────────────
def encode_kitty(data: bytes, *, chunk: int = 4096, cols: int = 0, rows: int = 0,
                 image_id: int | None = None) -> str:
    """Kitty graphics protocol, INLINE base64 transmit (works over SSH — no server-side file).

    Chunked into `chunk`-byte payloads with the m=1/m=0 continuation flag, f=100 (PNG-or-any:
    Kitty sniffs), a=T (transmit+display). `cols`/`rows` place it in a cell box if given; an
    explicit `image_id` (i=) lets the caller delete it later (a=d) on scroll. Pure str builder."""
    b64 = base64.b64encode(data).decode("ascii")
    ctrl = ["a=T", "f=100"]
    if image_id is not None:
        ctrl.append(f"i={int(image_id)}")
    if cols:
        ctrl.append(f"c={int(cols)}")
    if rows:
        ctrl.append(f"r={int(rows)}")
    out = []
    # split base64 into chunks; first chunk carries the control keys, all but the last set m=1.
    chunks = [b64[i:i + chunk] for i in range(0, len(b64), chunk)] or [""]
    for idx, part in enumerate(chunks):
        first = idx == 0
        last = idx == len(chunks) - 1
        keys = list(ctrl) if first else []
        keys.append(f"m={0 if last else 1}")
        out.append("\x1b_G" + ",".join(keys) + ";" + part + "\x1b\\")
    return "".join(out)


def kitty_delete(image_id: int) -> str:
    """Delete a previously-transmitted Kitty image by id (call before repaint/scroll)."""
    return f"\x1b_Ga=d,d=i,i={int(image_id)}\x1b\\"


def encode_iterm2(data: bytes, *, cols: int = 0, rows: int = 0,
                  preserve_aspect: bool = True, name: str = "image") -> str:
    """iTerm2 inline image: OSC 1337 ; File = [args] : <base64> BEL. Sizes in CELLS when given."""
    b64 = base64.b64encode(data).decode("ascii")
    args = [f"name={base64.b64encode(name.encode()).decode('ascii')}",
            f"size={len(data)}", "inline=1",
            f"preserveAspectRatio={1 if preserve_aspect else 0}"]
    if cols:
        args.append(f"width={int(cols)}")
    if rows:
        args.append(f"height={int(rows)}")
    return "\x1b]1337;File=" + ";".join(args) + ":" + b64 + "\x07"


def wrap_for_tmux(seq: str) -> str:
    """Wrap a graphics escape for tmux passthrough (requires `set -g allow-passthrough on`).
    tmux needs the payload inside its own DCS and every ESC doubled. Use only when the caller
    has explicitly opted into multiplexed graphics."""
    return "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"


# ── half-block fallback (optional Pillow) ─────────────────────────────────────
def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def encode_halfblock(data: bytes, *, cols: int = 80, rows: int = 24) -> str | None:
    """Render the image as Unicode upper-half-blocks (▀): each character cell shows TWO vertical
    pixels — foreground color = top pixel, background = bottom — via truecolor SGR. Works on any
    24-bit terminal (incl. multiplexed), so it's the universal fallback. Needs Pillow to resize +
    read pixels; returns None when Pillow is absent (caller then shows a text placeholder)."""
    try:
        import io
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    # Each cell = 1 col wide, 2 px tall. Fit within (cols, rows*2) px preserving aspect.
    max_w = max(1, int(cols))
    max_h = max(1, int(rows) * 2)
    w, h = img.size
    scale = min(max_w / w, max_h / h, 1.0) if w and h else 1.0
    tw, th = max(1, int(w * scale)), max(1, int(h * scale))
    if th % 2:
        th += 1                                  # even height so rows pair cleanly
    img = img.resize((tw, th))
    px = img.load()
    lines = []
    for y in range(0, th, 2):
        cells = []
        for x in range(tw):
            tr, tg, tb = px[x, y][:3]
            br, bg, bb = px[x, y + 1][:3] if y + 1 < th else (tr, tg, tb)
            cells.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m▀")
        cells.append("\x1b[0m")
        lines.append("".join(cells))
    return "\n".join(lines)


def text_placeholder(mime: str | None, size: tuple[int, int] | None, label: str = "") -> str:
    """The graceful no-graphics fallback line: `[image: 800x600 png · cat.png]`."""
    parts = []
    if size:
        parts.append(f"{size[0]}x{size[1]}")
    if mime:
        parts.append(mime.split("/")[-1])
    if label:
        parts.append(label)
    return "[image" + (": " + " · ".join(parts) if parts else "") + "]"


# ── chafa: optional quality renderer (used only when installed; never a required dep) ──
# chafa is a mature terminal-image tool that renders better than our built-in encoders. It CANNOT
# probe the terminal through a pipe, so we tell it the format from OUR detection (caps): a proven
# pixel protocol → that format (kitty/iterm/sixels); otherwise its `symbols` mode = the universal
# truecolor floor. When chafa is absent, render_image() falls back to the built-in encoders below,
# so nothing new needs installing. (Proven live: kitty→crystal-clear; VTE→symbols preview.)
_CHAFA_FORMAT = {KITTY: "kitty", ITERM2: "iterm", SIXEL: "sixels"}


def chafa_path() -> str | None:
    """Absolute path to a chafa binary, or None if not installed."""
    import shutil
    return shutil.which("chafa")


def chafa_render(image_path: str, caps: TermCaps, *, cols: int = 40, rows: int = 20) -> str | None:
    """Render `image_path` with chafa → escape/ANSI string, or None on any failure (missing binary,
    bad path, timeout, error) so the caller falls back to the built-in encoders. Format comes from
    `caps` (our detection), never guessed; symbols mode gets high-detail flags."""
    exe = chafa_path()
    if not exe or not image_path:
        return None
    proto = caps.protocol if caps else NONE
    fmt = _CHAFA_FORMAT.get(proto, "symbols")
    args = [exe, "-f", fmt, "--size", f"{max(1, int(cols))}x{max(1, int(rows))}"]
    if fmt == "symbols":
        args += ["--symbols", "all", "--work", "9"]
    args.append(image_path)
    try:
        import subprocess
        out = subprocess.run(args, capture_output=True, timeout=8)
        if out.returncode != 0 or not out.stdout:
            return None
        return out.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - any chafa trouble → built-in fallback
        return None


def render_image(data: bytes, caps: TermCaps, *, cols: int = 40, rows: int = 20,
                 image_id: int | None = None, label: str = "",
                 image_path: str | None = None) -> tuple[str, int]:
    """Top-level: encode `data` for the detected terminal, returning (sequence, rows_used).

    rows_used is how many terminal rows the caller should reserve. For Kitty/iTerm2 we ask for a
    `rows`-tall cell box; for half-block it's the number of text lines produced; for NONE it's a
    single placeholder line. The caller writes `sequence` into a reserved region and never lets
    the text layer overwrite those rows mid-paint.

    When `image_path` is given AND chafa is installed, render via chafa (higher quality; its
    symbols mode is the universal floor). chafa failing for any reason falls through to the
    built-in encoders below — so chafa is never required."""
    proto = caps.protocol if caps else NONE
    if image_path:
        seq = chafa_render(image_path, caps, cols=cols, rows=rows)
        if seq:
            used = rows if proto in (KITTY, ITERM2, SIXEL) else (seq.count("\n") + 1)
            return seq, used
    if proto == KITTY:
        return encode_kitty(data, cols=cols, rows=rows, image_id=image_id), rows
    if proto == ITERM2:
        return encode_iterm2(data, cols=cols, rows=rows), rows
    if proto == SIXEL:
        # Sixel encoding is heavy + Pillow-dependent; if we can produce half-blocks instead they
        # render everywhere a sixel terminal also supports truecolor. Prefer the dependency-free
        # placeholder when Pillow is absent rather than ship a half sixel encoder.
        hb = encode_halfblock(data, cols=cols, rows=rows)
        if hb is not None:
            return hb, hb.count("\n") + 1
    if proto == HALFBLOCK:
        hb = encode_halfblock(data, cols=cols, rows=rows)
        if hb is not None:
            return hb, hb.count("\n") + 1
    from .multimodal import sniff_mime
    return text_placeholder(sniff_mime(data), image_size(data), label), 1
