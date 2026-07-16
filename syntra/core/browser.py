"""Playwright browser automation — navigate, screenshot, extract, interact.

Optional: requires `pip install playwright && playwright install chromium`.
All functions degrade gracefully with a clear message when Playwright is absent,
so importing this module never fails and the rest of Syntra works without it.

A single headless browser/page is reused across calls (lazy-started) and closed
on shutdown. The page is the unit of state; tools act on it.
"""

from __future__ import annotations

import atexit


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class Browser:
    """Lazy headless Chromium wrapper. One page, reused across calls."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None

    def _ensure(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page()
        atexit.register(self.close)
        return self._page

    def navigate(self, url: str, *, timeout_ms: int = 15000) -> str:
        page = self._ensure()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return f"navigated to {page.url} — title: {page.title()!r}"

    def text(self, max_chars: int = 4000) -> str:
        page = self._ensure()
        body = page.inner_text("body")
        return body[:max_chars]

    def screenshot(self, path: str = "screenshot.png", *, full_page: bool = False) -> str:
        page = self._ensure()
        page.screenshot(path=path, full_page=full_page)
        return f"screenshot saved to {path}"

    def click(self, selector: str, *, timeout_ms: int = 8000) -> str:
        page = self._ensure()
        page.click(selector, timeout=timeout_ms)
        return f"clicked {selector!r} — now at {page.url}"

    def fill(self, selector: str, value: str, *, timeout_ms: int = 8000) -> str:
        page = self._ensure()
        page.fill(selector, value, timeout=timeout_ms)
        return f"filled {selector!r}"

    def eval_js(self, expression: str) -> str:
        page = self._ensure()
        return str(page.evaluate(expression))[:2000]

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._page = self._browser = self._pw = None


# Module-level singleton so all tools share one page.
_BROWSER: Browser | None = None


def get_browser() -> Browser:
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = Browser()
    return _BROWSER


def browser_tools() -> dict:
    """Playwright browser tools for the agent tool registry.
    Returns {} when Playwright is not installed (so the registry stays clean)."""
    if not playwright_available():
        return {}
    from .tools import Tool
    _str = {"type": "string"}
    _bool = {"type": "boolean"}

    def _nav(args, ctx):
        return get_browser().navigate(args.get("url", ""))

    def _text(args, ctx):
        return get_browser().text(int(args.get("max_chars", 4000)))

    def _shot(args, ctx):
        import os
        from .tools import _safe_path
        path = args.get("path") or os.path.join(ctx.workspace_root, "screenshot.png")
        full = _safe_path(ctx.workspace_root, path)
        return get_browser().screenshot(str(full), full_page=bool(args.get("full_page")))

    def _click(args, ctx):
        return get_browser().click(args.get("selector", ""))

    def _fill(args, ctx):
        return get_browser().fill(args.get("selector", ""), args.get("value", ""))

    return {t.name: t for t in [
        Tool("browser_navigate", "Open a URL in a headless browser (renders JS).",
             {"type": "object", "properties": {"url": _str}, "required": ["url"]},
             "exec", _nav, example='{"url":"example.com"}'),
        Tool("browser_text", "Get the visible text of the current page.",
             {"type": "object", "properties": {"max_chars": {"type": "integer"}}},
             "safe", _text),
        Tool("browser_screenshot", "Screenshot the current page to a PNG file.",
             {"type": "object", "properties": {"path": _str, "full_page": _bool}},
             "write", _shot),
        Tool("browser_click", "Click an element by CSS selector.",
             {"type": "object", "properties": {"selector": _str}, "required": ["selector"]},
             "exec", _click),
        Tool("browser_fill", "Fill a form field by CSS selector.",
             {"type": "object", "properties": {"selector": _str, "value": _str},
              "required": ["selector", "value"]},
             "exec", _fill),
    ]}
