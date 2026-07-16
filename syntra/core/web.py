"""Web tools backend: fetch a URL and (optionally) web search.

webfetch downloads an http(s) URL and returns it as text / markdown / html,
bounded in size and time. The HTTP opener is INJECTABLE so the logic is unit-
tested with a fake (no network). websearch has no built-in provider — it calls an
INJECTED backend (e.g. an Exa/Parallel-style API the user configures); without
one it returns a clear "not configured" message. Independent implementation.
"""

from __future__ import annotations

import html as _html
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

MAX_FETCH_BYTES = 2 * 1024 * 1024      # 2 MB download cap
MAX_RESULT_CHARS = 50_000              # returned text cap
MAX_TIMEOUT = 120.0


# ── #189 SSRF guard ────────────────────────────────────────────────────────────────────
# A prompt-injected agent could point webfetch at cloud metadata (169.254.169.254), an
# internal admin panel (127.0.0.1:8080), or a private-range host — exfiltrating credentials
# or reaching services the operator never intended. webfetch does NOT go through bwrap, so
# this is the only network boundary on that path. We refuse any URL whose host resolves to a
# non-public address, and re-check on every redirect hop (an approved fetch of a public URL
# must not 302 into the metadata endpoint). `allow_private=True` opts back in for localhost
# dev. A resolved-IP TOCTOU (DNS rebind between check and connect) is a known residual — the
# redirect re-check plus per-hop resolution shrink it; full pinning would need a custom
# connection class.

def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP is NOT a normal public address (loopback / private / link-local /
    metadata / multicast / reserved / unspecified). Pure — unit-tested."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True                       # unparseable → refuse (fail closed)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped               # ::ffff:169.254.169.254 → check the v4 target
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or ip == ipaddress.ip_address("169.254.169.254")   # explicit: cloud IMDS (also link-local)
    )


def _host_is_blocked(host: str) -> bool:
    """Resolve `host` and return True if it is a literal blocked IP or resolves to ANY
    blocked address. An empty host is blocked. Resolution failure → blocked (fail closed)."""
    if not host:
        return True
    host = host.strip("[]")               # IPv6 literal brackets
    # Literal IP?
    try:
        ipaddress.ip_address(host)
        return _ip_is_blocked(host)
    except ValueError:
        pass
    if host.lower() == "localhost":
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, socket.herror, OSError, UnicodeError):
        return True                       # can't resolve → refuse
    return any(_ip_is_blocked(ai[4][0]) for ai in infos)


def _ssrf_check(url: str, allow_private: bool) -> str:
    """Return an error string if `url` targets a non-public host, else ''."""
    if allow_private:
        return ""
    host = urlparse(url).hostname or ""
    if _host_is_blocked(host):
        return (f"error: refusing to fetch {url!r} — it targets a private, loopback, or "
                f"metadata address (SSRF guard). Set the network to allow-private for local dev.")
    return ""


# #247 — an explicit redirect hop cap (defense against long/looping redirect chains; urllib's
# internal default is 10 but we own it here so it's documented + tunable).
MAX_REDIRECTS = 10


class _NoPrivateRedirect(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF check on every redirect target so a public URL can't bounce us into a
    private/metadata host (#189 — the classic redirect-SSRF and DNS-rebind-via-redirect bypass).
    #247 adds an explicit hop cap and refuses an https->http protocol downgrade (a MITM-able
    drop). Public->public cross-host redirects are deliberately ALLOWED — they are normal
    (github->codeload, URL shorteners, CDNs) and the per-hop private-IP recheck already stops SSRF."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_scheme = urlparse(req.get_full_url()).scheme.lower()
        new = urlparse(newurl)
        if _host_is_blocked(new.hostname or ""):
            raise urllib.error.HTTPError(newurl, code,
                                         "redirect to a blocked (private/metadata) host — refused",
                                         headers, fp)
        if old_scheme == "https" and new.scheme.lower() == "http":
            raise urllib.error.HTTPError(newurl, code,
                                         "refusing an https->http downgrade redirect (MITM risk)",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": "syntra-webfetch/1.0"})
    opener = urllib.request.build_opener(_NoPrivateRedirect())
    with opener.open(req, timeout=timeout) as r:   # noqa: S310 - http(s) + SSRF validated by caller
        ctype = r.headers.get("Content-Type", "") if r.headers else ""
        return r.read(MAX_FETCH_BYTES + 1), ctype


_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def _strip_to_text(html_doc: str) -> str:
    s = _SCRIPT_STYLE.sub(" ", html_doc)
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = _TAG.sub("", s)
    s = _html.unescape(s)
    s = _WS.sub(" ", s)
    return _BLANKS.sub("\n\n", s).strip()


def _to_markdownish(html_doc: str) -> str:
    s = _SCRIPT_STYLE.sub(" ", html_doc)
    s = re.sub(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
               lambda m: f"[{_TAG.sub('', m.group(2))}]({m.group(1)})", s, flags=re.IGNORECASE | re.DOTALL)
    for i in range(1, 7):
        s = re.sub(rf"<h{i}\b[^>]*>(.*?)</h{i}>",
                   lambda m: f"\n{'#' * i} {_TAG.sub('', m.group(1)).strip()}\n", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<(b|strong)\b[^>]*>(.*?)</\1>", lambda m: f"**{_TAG.sub('', m.group(2))}**", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<li\b[^>]*>(.*?)</li>", lambda m: f"- {_TAG.sub('', m.group(1)).strip()}\n", s, flags=re.IGNORECASE | re.DOTALL)
    return _strip_to_text(s)


def fetch_url(url: str, fmt: str = "markdown", timeout: float = 30.0, *,
              opener=None, allow_private: bool = False) -> str:
    """Fetch url, return content as 'text' | 'markdown' | 'html', bounded.

    `allow_private` lifts the SSRF guard for local development (localhost / private-range
    hosts). Default False — a prompt-injected agent must not reach internal/metadata targets."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return "error: url must start with http:// or https://"
    # SSRF guard applies to the REAL network path (the default opener). An injected opener is a
    # caller-controlled transport / test double and does its own thing — don't DNS-resolve for it.
    if opener is None:
        ssrf = _ssrf_check(url, allow_private)
        if ssrf:
            return ssrf
    timeout = max(1.0, min(float(timeout or 30.0), MAX_TIMEOUT))
    opener = opener or _default_opener
    try:
        raw, ctype = opener(url, timeout)
    except Exception as e:  # noqa: BLE001 - surface as text, never crash the loop
        return f"error: fetch failed: {e}"
    if len(raw) > MAX_FETCH_BYTES:
        raw = raw[:MAX_FETCH_BYTES]
    try:
        doc = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:  # noqa: BLE001
        return "error: could not decode response"
    is_html = "html" in (ctype or "").lower() or "<html" in doc[:2000].lower()
    if fmt == "html" or (fmt != "text" and not is_html):
        out = doc   # raw for html, or non-html bodies as-is
    elif fmt == "text":
        out = _strip_to_text(doc) if is_html else doc
    else:  # markdown
        out = _to_markdownish(doc)
    return out[:MAX_RESULT_CHARS]


def run_web_search(query: str, backend) -> str:
    """Run a web search via an injected backend(query)->list[{title,url,snippet}] | str."""
    if backend is None:
        return ("error: no web search backend configured. Configure a search provider "
                "(e.g. an Exa/Parallel-style API) to enable websearch.")
    try:
        results = backend(query)
    except Exception as e:  # noqa: BLE001
        return f"error: web search failed: {e}"
    if isinstance(results, str):
        return results[:MAX_RESULT_CHARS]
    lines = []
    for r in (results or [])[:10]:
        if not isinstance(r, dict):   # F24: a backend may yield a non-dict element — skip it
            continue                   # instead of raising AttributeError out of the format loop
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snip = (r.get("snippet") or "").strip()
        lines.append(f"- {title}\n  {url}\n  {snip}"[:600])
    return "\n".join(lines) if lines else "(no results)"
