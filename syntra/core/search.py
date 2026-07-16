"""Web-search backend builder for deep-research (P22).

`web.run_web_search(query, backend)` calls an INJECTED backend
``backend(query) -> list[{title,url,snippet}]``. This module BUILDS that backend from
the user's configured search provider, or returns ``None`` when none is configured -- so
everything stays OFFLINE-SAFE by default (no provider => websearch is simply disabled,
never a surprise network call). The HTTP call is bounded and the opener is injectable so
the parsing is unit-tested with a fake. Independent implementation.

Config (env):
- ``SYNTRA_SEARCH_API_KEY``     -- REQUIRED to enable (no key => returns None).
- ``SYNTRA_SEARCH_URL``         -- endpoint (default: Brave Web Search).
- ``SYNTRA_SEARCH_AUTH_HEADER`` -- auth header name (default: ``X-Subscription-Token``).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

_DEFAULT_URL = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_AUTH_HEADER = "X-Subscription-Token"
_MAX_RESULTS = 10
_TIMEOUT = 20.0


def _coerce_results(body) -> list:
    """Pull [{title,url,snippet}] out of the common search-API response shapes
    (Brave ``web.results``, Tavily/Exa/generic ``results``/``items``/``data``)."""
    data = body
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(data, dict):
        return []
    raw = (((data.get("web") or {}).get("results") if isinstance(data.get("web"), dict) else None)
           or data.get("results") or data.get("items") or data.get("data") or [])
    out = []
    for r in raw[:_MAX_RESULTS]:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or r.get("name") or "").strip(),
            "url": str(r.get("url") or r.get("link") or r.get("href") or "").strip(),
            "snippet": str(r.get("snippet") or r.get("description")
                           or r.get("text") or r.get("content") or "").strip(),
        })
    return [r for r in out if r["url"]]


def build_search_backend(*, opener=None):
    """Return a backend ``callable(query) -> list[{title,url,snippet}]`` for the configured
    search provider, or ``None`` when unconfigured (the offline-safe default). The opener
    is injectable for tests; the default does a single bounded HTTPS GET."""
    key = os.environ.get("SYNTRA_SEARCH_API_KEY", "").strip()
    if not key:
        return None
    url = os.environ.get("SYNTRA_SEARCH_URL", _DEFAULT_URL)
    auth_header = os.environ.get("SYNTRA_SEARCH_AUTH_HEADER", _DEFAULT_AUTH_HEADER)

    def _default_opener(full_url: str, headers: dict) -> str:
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 - https endpoint
            return r.read(1024 * 1024).decode("utf-8", errors="replace")

    op = opener or _default_opener

    def backend(query: str) -> list:
        q = urllib.parse.quote((query or "").strip())
        full = f"{url}?q={q}&count={_MAX_RESULTS}"
        headers = {auth_header: key, "Accept": "application/json",
                   "User-Agent": "syntra-search/1.0"}
        try:
            body = op(full, headers)
        except Exception:  # noqa: BLE001 - surface as no results, never crash a run
            return []
        return _coerce_results(body)

    return backend
