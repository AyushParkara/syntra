"""Render a web page IN the terminal.

When there's something visual a terminal can't natively show — a live URL, a local HTML file, or a
raw HTML string — this renders it with the user's ALREADY-INSTALLED desktop browser in headless
screenshot mode (`<browser> --headless=new --screenshot=<png> <target>`), producing a PNG. That PNG
then flows through Syntra's existing inline-image path (core.terminal_image), which paints it as
truecolor half-blocks on terminals without a graphics protocol (GNOME/VTE) — so the page shows
INSIDE the terminal, not in a separate window.

No new required dependency: it uses whatever Chromium-family browser is on PATH (detected at
runtime, none hardcoded as "the" one). If none is present it degrades cleanly (returns a reason,
never raises). The model-call/screenshot subprocess is injected (`runner`) so it's unit-tested
network-free.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

# Chromium-family browsers that support `--headless --screenshot`, in rough preference order.
# Detected from PATH at call time — the list is a search order, NOT a hardcoded choice.
_BROWSER_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                       "brave-browser", "microsoft-edge", "chrome")


def find_headless_browser() -> "str | None":
    """The first Chromium-family browser on PATH that can do `--headless --screenshot`, or None."""
    for name in _BROWSER_CANDIDATES:
        if shutil.which(name):
            return name
    return None


def classify(target: str) -> str:
    """'url' | 'html' | 'file' — what KIND of thing to render. Raw HTML is detected by shape."""
    t = (target or "").strip()
    low = t.lower()
    if urlparse(t).scheme in ("http", "https", "file", "data"):
        return "url"
    if low.startswith("<") or "<html" in low or "<body" in low or "<!doctype html" in low:
        return "html"
    return "file"


def render_to_png(target: str, out_png, *, width: int = 1100, height: int = 800,
                  timeout_s: int = 45, runner=subprocess.run,
                  scratch_dir=None, allow_no_sandbox: bool = False) -> "tuple[bool, str]":
    """Render `target` (URL / local file path / raw HTML string) to a PNG at `out_png` using a
    headless desktop browser. Returns (ok, detail). Never raises.

    `runner(cmd, **kw)` is injected (defaults to subprocess.run) so tests stay network-free.
    Raw HTML is written to a file next to out_png first (browsers open file:// but not a string).
    """
    out_png = Path(out_png)
    browser = find_headless_browser()
    if not browser:
        return (False, "no headless browser found on PATH (chrome/chromium/brave/edge) to render with")
    kind = classify(target)
    nav = target
    scratch = Path(scratch_dir) if scratch_dir else out_png.parent
    try:
        if kind == "html":
            scratch.mkdir(parents=True, exist_ok=True)
            f = scratch / (out_png.stem + ".html")
            f.write_text(target, encoding="utf-8")
            nav = f.resolve().as_uri()
        elif kind == "file":
            p = Path(target).expanduser()
            if not p.is_file():
                return (False, f"no such file: {target}")
            nav = p.resolve().as_uri()
        elif urlparse(target).scheme == "":
            nav = "https://" + target
    except Exception as e:  # noqa: BLE001
        return (False, f"could not prepare target: {e}")

    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        if out_png.exists():
            out_png.unlink()
    except OSError:
        pass
    # --headless=new is the modern headless mode; --screenshot writes the PNG then exits.
    # F17: KEEP the Chromium sandbox on by default — this renders untrusted URLs/HTML, so
    # --no-sandbox would remove the boundary a browser RCE would otherwise be trapped by.
    # Only pass it when the caller explicitly opts in (e.g. running as root/in a container).
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars"]
    if allow_no_sandbox:
        cmd.append("--no-sandbox")
    cmd += [f"--window-size={int(width)},{int(height)}", f"--screenshot={out_png}", nav]
    try:
        runner(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (False, f"{browser} timed out rendering the page")
    except Exception as e:  # noqa: BLE001
        return (False, f"{browser} failed: {e}")
    if out_png.is_file() and out_png.stat().st_size > 0:
        return (True, f"rendered {kind} via {browser}")
    return (False, f"{browser} produced no screenshot")
