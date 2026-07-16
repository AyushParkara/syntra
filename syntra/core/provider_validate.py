"""Provider validation + add-flow messaging (Track T2, E2/E4, ties to C7).

Pure, offline checks that catch broken provider configs BEFORE they cause confusing
runtime failures: missing fields, malformed base_url, raw-IP HTTPS (which can't
match a TLS cert -> would surface as a `tls` failure, C7), and missing credentials.
Network reachability is a separate, optional probe (needs live I/O); this module is
deterministic and unit-tested.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def _host(base_url: str) -> str:
    try:
        return urlparse(base_url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def is_raw_ip_host(base_url: str) -> bool:
    """True if base_url's host is a bare IP address (TLS cert can't match it)."""
    host = _host(base_url)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_provider_config(row: dict) -> list[str]:
    """Return a list of problems with a provider config row (empty == valid).

    Checks (offline): name, base_url scheme/host, raw-IP-over-HTTPS (C7), and that
    SOME credential is present unless the endpoint is explicitly no-auth/local.
    """
    problems: list[str] = []
    if not isinstance(row, dict):
        return ["provider entry must be an object"]

    name = (row.get("name") or "").strip()
    if not name:
        problems.append("missing 'name'")

    base_url = (row.get("base_url") or "").strip()
    if not base_url:
        problems.append("missing 'base_url'")
    else:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            problems.append("base_url must start with http:// or https://")
        elif not parsed.hostname:
            problems.append("base_url has no host")
        elif parsed.scheme == "https" and is_raw_ip_host(base_url):
            problems.append(
                "base_url uses HTTPS to a raw IP address; the TLS certificate "
                "cannot match an IP — use the provider's hostname (see C7)")

    api_key = (row.get("api_key") or "").strip()
    api_key_env = (row.get("api_key_env") or "").strip()
    host = _host(base_url)
    local = host in ("localhost", "127.0.0.1", "::1")
    no_auth = api_key.lower() == "no-auth"
    if not api_key and not api_key_env and not local and not no_auth:
        problems.append(
            "no credential: set 'api_key', 'api_key_env', or 'no-auth' "
            "(local endpoints may omit it)")

    allowed = row.get("allowed_models")
    if allowed is not None and not isinstance(allowed, (list, tuple)):
        problems.append("'allowed_models' must be a list when present")

    return problems


def no_models_message() -> str:
    """E2: the clear 'you have no usable model — add one' guidance."""
    return ("No usable model is configured. Add a provider with at least one "
            "model: run `syntra init` (guided) or edit your providers.json, then "
            "`syntra doctor` to verify. Free presets are available — see "
            "`syntra providers --free`.")
