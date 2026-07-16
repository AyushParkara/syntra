"""Terminal clipboard via OSC 52 (Track T1 / F8 message actions).

OSC 52 lets a terminal app set the system clipboard with a single escape
sequence — no `xclip`/`pbcopy` dependency, works over SSH on terminals that
support it (most modern ones; the caller degrades gracefully if not). This is the
PURE encoder; the curses layer writes the returned string to stdout.

Sequence: ESC ] 52 ; c ; <base64(text)> BEL
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys

_OSC = "\x1b]52;c;"
_BEL = "\x07"

# Guard against absurd payloads (some terminals cap OSC 52 length).
MAX_OSC52_BYTES = 100_000


def osc52(text: str) -> str:
    """Return the OSC 52 escape sequence that copies `text` to the clipboard.

    Wraps in a tmux passthrough sequence when running inside tmux/screen, so the
    clipboard set reaches the outer terminal.
    """
    import os
    raw = (text or "").encode("utf-8")[:MAX_OSC52_BYTES]
    b64 = base64.b64encode(raw).decode("ascii")
    seq = f"{_OSC}{b64}{_BEL}"
    if os.environ.get("TMUX") or os.environ.get("STY"):
        # tmux/screen passthrough: \x1bPtmux;\x1b<seq>\x1b\\
        return f"\x1bPtmux;\x1b{seq}\x1b\\"
    return seq


def copy(text: str) -> bool:
    """Copy text to the system clipboard. Returns True if something was attempted.

    Strategy: always emit OSC52 (works over SSH), AND try a
    native tool if present (wl-copy / xclip / xsel / pbcopy / clip.exe) so it
    also works in terminals that don't honor OSC52.
    """
    import sys
    import shutil
    import subprocess

    if not text:
        return False

    # 1. OSC52 to the terminal
    try:
        if sys.stdout.isatty():
            sys.stdout.write(osc52(text))
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass

    # 2. native tool (best-effort, non-blocking failures ignored)
    candidates = []
    if shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if shutil.which("xclip"):
        candidates.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        candidates.append(["xsel", "--clipboard", "--input"])
    if shutil.which("pbcopy"):
        candidates.append(["pbcopy"])
    if shutil.which("clip.exe"):
        candidates.append(["clip.exe"])
    for cmd in candidates:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"), timeout=2)
            break
        except Exception:  # noqa: BLE001
            continue
    return True


# ---- reading an IMAGE off the system clipboard ------------------------------
# Terminals cannot transmit image bytes through a normal text paste (the paste
# protocol carries the TEXT clipboard only). So pulling an image out of the
# clipboard means shelling out to a platform tool — exactly what the user does
# when they take a screenshot and want to send it without saving a file first.
# Preference order favours lossless PNG, then JPEG/WebP/GIF (matches what the
# vision models accept; see core.multimodal.SUPPORTED_MIME).
_CLIP_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _run_capture(cmd, timeout=4):
    """Run `cmd`, return its raw stdout bytes (or None on any failure). Best-effort."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_image_wlpaste(finish):
    """Wayland: `wl-paste` lists the offered types, then dumps the one image type we accept.
    None if wl-paste is absent or no supported image type is offered."""
    if not shutil.which("wl-paste"):
        return None
    types = _run_capture(["wl-paste", "--list-types"]) or b""
    offered = types.decode("utf-8", "replace").split()
    for mime in _CLIP_IMAGE_MIMES:
        if mime in offered:
            got = finish(_run_capture(["wl-paste", "--type", mime, "--no-newline"]))
            if got:
                return got
    return None


def _read_image_xclip(finish):
    """X11: `xclip -selection clipboard -t <mime> -o` dumps the bytes for that type. None if
    xclip is absent or the clipboard holds no supported image type."""
    if not shutil.which("xclip"):
        return None
    for mime in _CLIP_IMAGE_MIMES:
        got = finish(_run_capture(["xclip", "-selection", "clipboard", "-t", mime, "-o"]))
        if got:
            return got
    return None


def _read_image_copyq(finish):
    """CopyQ clipboard manager: `copyq clipboard <mime>` dumps the raw bytes for that type, and
    CopyQ TRANSCODES on the fly (stores BMP, returns PNG on request). Talks to X11/Wayland
    itself — no xclip/wl-paste needed. None if copyq is absent or holds no image."""
    if not shutil.which("copyq"):
        return None
    for mime in _CLIP_IMAGE_MIMES:
        got = finish(_run_capture(["copyq", "clipboard", mime]))
        if got:
            return got
    return None


# A tiny self-contained program that reads the clipboard image via GTK and writes PNG to stdout.
# Run in a SUBPROCESS (same interpreter) so the brief GTK main-loop can never interfere with the
# host curses terminal, and so _run_capture's timeout bounds it. GTK ships on essentially every
# GTK/GNOME Linux desktop, so this is the no-extra-tool fallback.
_GTK_READ_SNIPPET = (
    "import sys\n"
    "try:\n"
    "    import gi\n"
    "    gi.require_version('Gtk', '3.0')\n"
    "    from gi.repository import Gtk, Gdk\n"
    "    pb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_image()\n"
    "    if pb is not None:\n"
    "        ok, buf = pb.save_to_bufferv('png', [], [])\n"
    "        if ok:\n"
    "            sys.stdout.buffer.write(bytes(buf))\n"
    "except Exception:\n"
    "    pass\n"
)


def _read_image_gtk(finish):
    """Pure-library clipboard image read via GTK (python3-gi) — NO external clipboard binary.
    Runs the read in an isolated subprocess (timeout-bounded, terminal-safe). None if gi is
    unavailable or the clipboard holds no image."""
    return finish(_run_capture([sys.executable, "-c", _GTK_READ_SNIPPET], timeout=6))


def _gtk_clipboard_available():
    """Is the GTK clipboard reader usable here? Checks python3-gi importability in a subprocess
    so gi is never loaded into the host process. Best-effort; never raises."""
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk, Gdk"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _has_any_image_reader():
    """Generic, OS-specific capability probe: is ANY clipboard-image reader usable right now?
    No single tool is hardcoded as 'the' one — we report True if even one method this OS supports
    is present. Drives the diagnostic (so it only complains when NOTHING can read an image)."""
    try:
        is_wsl = "microsoft" in (os.uname().release.lower() if hasattr(os, "uname") else "")
        if sys.platform.startswith("win") or is_wsl:
            return bool(shutil.which("powershell.exe") or shutil.which("powershell"))
        if sys.platform == "darwin":
            # osascript ships with macOS, so a capable reader is essentially always present.
            return bool(shutil.which("pngpaste") or shutil.which("osascript"))
        if sys.platform.startswith("linux"):
            if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
                return True
            if shutil.which("xclip") or shutil.which("copyq"):
                return True
            return _gtk_clipboard_available()
        return False
    except Exception:  # noqa: BLE001
        return False


def _image_readers():
    """The ordered list of clipboard-image readers to try FOR THIS OS. Generic: we don't favour
    any single tool — we try every method the platform offers and use whichever returns an image.
    Cheapest/most-exact first, pure-library and manager fallbacks last."""
    is_wsl = "microsoft" in (os.uname().release.lower() if hasattr(os, "uname") else "")
    if sys.platform.startswith("win") or is_wsl:
        # WSL: the Linux clipboard never sees a Windows screenshot, so go straight to PowerShell.
        readers = [_read_image_powershell]
        if not (sys.platform.startswith("win")):
            # native WSL Linux desktops (rare) may also have a Linux clipboard tool
            readers += [_read_image_wlpaste, _read_image_xclip, _read_image_copyq, _read_image_gtk]
        return readers
    if sys.platform == "darwin":
        return [_read_image_pngpaste, _read_image_osascript]
    if sys.platform.startswith("linux"):
        # On Wayland prefer wl-paste; on X11 prefer xclip — but try BOTH plus CopyQ + GTK so a
        # missing tool never blocks paste. Order is by directness, not preference for a brand.
        on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        native = [_read_image_wlpaste, _read_image_xclip] if on_wayland \
            else [_read_image_xclip, _read_image_wlpaste]
        return native + [_read_image_copyq, _read_image_gtk]
    return []


def image_paste_unavailable_reason():
    """Why can't we paste a clipboard IMAGE here? Returns a short note, or "" when image paste
    SHOULD work (some reader this OS supports is present). The UI shows this instead of a vague
    "no image". It is UNBIASED: it never tells you to install a specific tool — when nothing can
    read images it just points at the universal /attach <path> escape hatch. Never raises."""
    try:
        if _has_any_image_reader():
            return ""
        return "can't read images from this clipboard here — use /attach <path> to send an image"
    except Exception:  # noqa: BLE001 - a diagnostic must never break the caller
        return ""


def read_image():
    """Pull an image off the system clipboard. Returns (bytes, mime) or None.

    OS-aware + generic: tries every reader this platform supports (see _image_readers) and uses
    whichever returns a supported image first — so paste works without any one specific tool
    being installed. The mime is sniffed from the returned bytes (never trusted from the tool),
    so a wrong-labelled payload can't slip an unsupported type through. None = no image on the
    clipboard / no reader could get one; the caller shows image_paste_unavailable_reason().
    """
    from . import multimodal

    def _finish(data):
        if not data:
            return None
        mime = multimodal.sniff_mime(data)
        if mime in multimodal.SUPPORTED_MIME:
            return (data, mime)
        return None

    for reader in _image_readers():
        try:
            got = reader(_finish)
        except Exception:  # noqa: BLE001 - one reader failing must not block the others
            got = None
        if got:
            return got
    return None


def _read_image_pngpaste(finish):
    """macOS: `pngpaste -` emits the clipboard image as PNG. None if pngpaste isn't installed."""
    if not shutil.which("pngpaste"):
        return None
    return finish(_run_capture(["pngpaste", "-"]))


def _read_image_osascript(finish):
    """macOS without pngpaste: ask osascript to write the clipboard PNG to a temp file."""
    import os
    import tempfile
    import subprocess
    fd, path = tempfile.mkstemp(suffix=".png", prefix="syntra-clip-")
    os.close(fd)
    script = (
        'set thePNG to (the clipboard as «class PNGf»)\n'
        f'set theFile to open for access POSIX file "{path}" with write permission\n'
        'set eof theFile to 0\n'
        'write thePNG to theFile\n'
        'close access theFile\n'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        if r.returncode == 0 and os.path.getsize(path) > 0:
            with open(path, "rb") as fh:
                return finish(fh.read())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return None


def _read_image_powershell(finish):
    """Windows / WSL: use PowerShell to grab the clipboard image and emit PNG bytes.

    WSL's Linux clipboard never sees a Windows screenshot (Win+Shift+S), so we reach
    the Windows clipboard via powershell.exe and read the PNG it writes to a temp file
    (translated back to a Linux path with wslpath when in WSL)."""
    import os
    import shutil
    import tempfile
    import subprocess
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return None
    fd, win_tmp = tempfile.mkstemp(suffix=".png", prefix="syntra-clip-")
    os.close(fd)
    # In WSL the temp path is a Linux path; PowerShell needs the Windows form.
    target = win_tmp
    if shutil.which("wslpath"):
        conv = _run_capture(["wslpath", "-w", win_tmp])
        if conv:
            target = conv.decode("utf-8", "replace").strip()
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$img=[System.Windows.Forms.Clipboard]::GetImage();"
        f"if($img -ne $null){{$img.Save('{target}',"
        "[System.Drawing.Imaging.ImageFormat]::Png)}}"
    )
    try:
        subprocess.run([ps, "-NoProfile", "-Command", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if os.path.getsize(win_tmp) > 0:
            with open(win_tmp, "rb") as fh:
                return finish(fh.read())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            os.unlink(win_tmp)
        except OSError:
            pass
    return None
