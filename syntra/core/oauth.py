"""Browser login via OAuth 2.0 device-code flow (Track T2, D1-D2; RFC 8628).

Generalized browser login for providers that support it, alongside API keys. The
device-code grant suits a CLI: no localhost redirect server — we show a code +
URL, the user authorizes in their browser, and we poll for the token. Tokens land
in the chmod-600 SecretStore (core/secrets.py) and refresh when expired.

The FLOW LOGIC is pure and the HTTP transport is INJECTED (`post(url, data)->dict`),
so the whole device/poll/refresh flow is unit-tested with a fake transport — no
live OAuth endpoint needed. The default real transport is a thin urllib POST.

NOTE: this module's flow is exercised by unit tests against a fake transport; it
has NOT been run against a live provider here (that needs real client_ids +
network). It never disables TLS verification.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

from .secrets import TokenRecord

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(RuntimeError):
    """A browser-login flow failed (denied, expired, or transport error)."""


@dataclass(frozen=True)
class DeviceLoginConfig:
    device_auth_url: str
    token_url: str
    client_id: str
    scope: str = ""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int = 5
    expires_in: int = 900


def parse_device_code(resp: dict) -> DeviceCode:
    """Parse a device-authorization response (RFC 8628 §3.2). Pure."""
    uri = resp.get("verification_uri_complete") or resp.get("verification_uri") or resp.get("verification_url", "")
    return DeviceCode(
        device_code=resp.get("device_code", ""),
        user_code=resp.get("user_code", ""),
        verification_uri=uri,
        interval=int(resp.get("interval", 5) or 5),
        expires_in=int(resp.get("expires_in", 900) or 900),
    )


def parse_token_response(resp: dict, *, now: float | None = None) -> TokenRecord:
    """Map a token response to a TokenRecord, deriving expires_at from expires_in."""
    now = now if now is not None else time.time()
    expires_in = resp.get("expires_in")
    expires_at = (now + float(expires_in)) if expires_in else 0.0
    return TokenRecord(
        access_token=resp.get("access_token", ""),
        refresh_token=resp.get("refresh_token", ""),
        expires_at=expires_at,
    )


def config_from_oauth_block(block) -> "DeviceLoginConfig | None":
    """Build a DeviceLoginConfig from a provider's `oauth` block (providers.json), or
    None if it lacks the fields needed to refresh. Refresh needs token_url + client_id;
    device_auth_url is only used for the initial device login, so it defaults to ''."""
    if not isinstance(block, dict):
        return None
    token_url = block.get("token_url")
    client_id = block.get("client_id")
    if not token_url or not client_id:
        return None
    return DeviceLoginConfig(
        device_auth_url=block.get("device_auth_url", "") or "",
        token_url=token_url,
        client_id=client_id,
        scope=block.get("scope", "") or "",
    )


def ensure_fresh_token(store, provider: str, config, *, post=None, now=None) -> "str | None":
    """Return a VALID access token for `provider`, refreshing it first when it has expired
    and a refresh_token + oauth config are available. Never raises — on a refresh failure
    (or no config / no refresh_token) it returns the existing (possibly stale) token, so a
    transient refresh outage can't lock the user out. Auto-refresh (B6)."""
    rec = store.get(provider)
    if rec is None:
        return None
    _now = (now if now is not None else time.time())
    if not rec.is_expired(now=_now) or not rec.refresh_token or config is None:
        return rec.access_token
    try:
        login = DeviceLogin(config, post=post) if post is not None else DeviceLogin(config)
        fresh = login.refresh(rec.refresh_token, now=_now)
        store.set(provider, fresh)            # persist the refreshed token
        return fresh.access_token
    except Exception:  # noqa: BLE001 - never lock the user out on a refresh blip
        return rec.access_token


def _real_post(url: str, data: dict) -> dict:
    body = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in data.items()).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    # Default context verifies TLS; we never pass an unverified context (C7).
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - https provider URL
        return json.loads(r.read().decode())


class DeviceLogin:
    """Device-code login. `post(url, data)->dict` is injected for testability."""

    def __init__(self, config: DeviceLoginConfig, post=_real_post):
        self.config = config
        self._post = post

    def start(self) -> DeviceCode:
        resp = self._post(self.config.device_auth_url, {
            "client_id": self.config.client_id,
            "scope": self.config.scope,
        })
        return parse_device_code(resp)

    def poll_once(self, device_code: str, *, now: float | None = None):
        """One poll of the token endpoint. Returns ('pending'|'slow_down', None),
        ('error', reason), or ('ok', TokenRecord)."""
        resp = self._post(self.config.token_url, {
            "client_id": self.config.client_id,
            "device_code": device_code,
            "grant_type": DEVICE_GRANT,
        })
        err = resp.get("error")
        if err == "authorization_pending":
            return ("pending", None)
        if err == "slow_down":
            return ("slow_down", None)
        if err:
            return ("error", err)
        if resp.get("access_token"):
            return ("ok", parse_token_response(resp, now=now))
        return ("error", "no access_token in response")

    def refresh(self, refresh_token: str, *, now: float | None = None) -> TokenRecord:
        resp = self._post(self.config.token_url, {
            "client_id": self.config.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        tok = parse_token_response(resp, now=now)
        # Some providers omit a new refresh_token on refresh -> keep the old one.
        if not tok.refresh_token:
            tok = TokenRecord(tok.access_token, refresh_token, tok.expires_at)
        return tok


def run_device_login(config: DeviceLoginConfig, store, provider: str, *,
                     post=_real_post, sleep=time.sleep, emit=print,
                     now=time.time, max_wait: float = 900.0) -> TokenRecord:
    """Drive a full device-code login and persist the token. Testable.

    Shows the user the verification URL + code, polls the token endpoint honoring
    the server interval (and `slow_down`), stores the resulting TokenRecord in
    `store` under `provider`, and returns it. All side effects (HTTP, sleeping,
    printing, clock) are injected so the whole loop is unit-tested with fakes.
    Raises OAuthError on denial/expiry/timeout.
    """
    dl = DeviceLogin(config, post=post)
    try:
        dc = dl.start()
    except Exception as e:  # noqa: BLE001
        raise OAuthError(f"could not start device login: {e}") from e
    emit(f"To authorize {provider}, open: {dc.verification_uri}")
    emit(f"and enter the code: {dc.user_code}")
    emit("waiting for authorization… (Ctrl-C to cancel)")

    interval = max(1, dc.interval)
    waited = 0.0
    while waited < max_wait:
        sleep(interval)
        waited += interval
        try:
            status, value = dl.poll_once(dc.device_code, now=now())
        except Exception as e:  # noqa: BLE001
            raise OAuthError(f"token poll failed: {e}") from e
        if status == "ok":
            store.set(provider, value)
            emit(f"authorized; token stored for {provider}.")
            return value
        if status == "slow_down":
            interval += 5
            continue
        if status == "pending":
            continue
        raise OAuthError(f"authorization failed: {value}")
    raise OAuthError("device login timed out")
