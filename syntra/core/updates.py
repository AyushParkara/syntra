"""Update checking + applying for Syntra.

Mechanism-agnostic core. The update SOURCE is PyPI's public JSON API (no auth, no
git) once the package is published; before publishing, the check degrades
gracefully (returns None -> "no update info", never raises). Applying detects HOW
Syntra was installed (uv tool / pipx / pip / editable dev checkout) and uses the
matching upgrade command -- so it works for every install type and never fights
pip on a dev checkout.

Everything here is pure or fail-silent and dependency-light (stdlib only, with an
optional `packaging` fast-path for version compare), so it's fully unit-testable
and can never crash a run on a flaky network.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

_DIST = "syntra"
_PYPI_JSON = "https://pypi.org/pypi/{name}/json"
_DEFAULT_INTERVAL = 24 * 3600        # check at most once per day
_TIMEOUT = 2.0                        # network checks must be quick + non-blocking


# --------------------------------------------------------------------- versions

def installed_version() -> str:
    import importlib.metadata as md
    try:
        return md.version(_DIST)
    except Exception:  # noqa: BLE001
        return "0.0.0"


def latest_version(*, timeout: float = _TIMEOUT, opener=None) -> str | None:
    """Latest published version from PyPI. Fail-silent: any network error, 404
    (not published yet), or parse problem -> None. `opener` is injectable for tests."""
    url = _PYPI_JSON.format(name=_DIST)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{_DIST}-update-check"})
        _open = opener or urllib.request.urlopen
        with _open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        v = data.get("info", {}).get("version")
        return str(v) if v else None
    except Exception:  # noqa: BLE001 -- never let an update check break anything
        return None


def _parse_version(v: str) -> tuple:
    """Numeric-tuple parse as a fallback when `packaging` isn't available."""
    out = []
    for seg in str(v).split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest: str | None, installed: str) -> bool:
    """True iff `latest` is a strictly newer version than `installed`."""
    if not latest:
        return False
    try:
        from packaging.version import Version  # accurate (handles rc/post/etc.)
        return Version(latest) > Version(installed)
    except Exception:  # noqa: BLE001
        return _parse_version(latest) > _parse_version(installed)


# ---------------------------------------------------------------- install method

def detect_install_method(*, module_file: str | None = None) -> str:
    """Return how Syntra was installed: 'editable' | 'uv-tool' | 'pipx' | 'pip' |
    'unknown'. `module_file` is injectable for tests (defaults to THIS file's path)."""
    import importlib.metadata as md
    # 1) editable install advertises itself via direct_url.json dir_info.editable
    try:
        durl = md.distribution(_DIST).read_text("direct_url.json")
        if durl:
            if json.loads(durl).get("dir_info", {}).get("editable"):
                return "editable"
    except Exception:  # noqa: BLE001
        pass
    loc = (module_file or __file__ or "").replace("\\", "/").lower()
    # 2) path-based detection of managed-tool installs
    if "/uv/tools/" in loc or "/uv/tool/" in loc:
        return "uv-tool"
    if "/pipx/venvs/" in loc or "/pipx/" in loc:
        return "pipx"
    # 3) a source tree that isn't under site-packages -> dev/editable checkout
    if loc and "site-packages" not in loc and "dist-packages" not in loc:
        return "editable"
    if "site-packages" in loc or "dist-packages" in loc:
        return "pip"
    return "unknown"


def upgrade_command(method: str) -> list[str] | None:
    """The shell command that upgrades Syntra for the given install method.
    None means 'no automated upgrade' (editable dev checkout -> update the source)."""
    return {
        "uv-tool": ["uv", "tool", "upgrade", _DIST],
        "pipx": ["pipx", "upgrade", _DIST],
        "pip": [sys.executable, "-m", "pip", "install", "--upgrade", _DIST],
        "editable": None,
        "unknown": [sys.executable, "-m", "pip", "install", "--upgrade", _DIST],
    }.get(method)


# ------------------------------------------------------------------------ state

@dataclass
class UpdateState:
    last_check_ts: float = 0.0       # when we last queried PyPI
    last_seen_latest: str = ""       # the newest version we've seen
    snooze_until_ts: float = 0.0     # don't notify again before this (user "remind in N days")


def _state_path(state_root: str | Path) -> Path:
    return Path(state_root) / "update-check.json"


def load_state(state_root: str | Path) -> UpdateState:
    try:
        d = json.loads(_state_path(state_root).read_text())
        return UpdateState(
            last_check_ts=float(d.get("last_check_ts", 0.0)),
            last_seen_latest=str(d.get("last_seen_latest", "")),
            snooze_until_ts=float(d.get("snooze_until_ts", 0.0)),
        )
    except Exception:  # noqa: BLE001
        return UpdateState()


def save_state(state_root: str | Path, st: UpdateState) -> None:
    try:
        p = _state_path(state_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(st), indent=2))
    except Exception:  # noqa: BLE001
        pass


def should_check(st: UpdateState, now: float, interval: float = _DEFAULT_INTERVAL) -> bool:
    """Throttle: only hit the network once per `interval` seconds."""
    return (now - st.last_check_ts) >= interval


def should_notify(st: UpdateState, now: float, latest: str | None, installed: str) -> bool:
    """Notify only if there's a genuinely newer version AND the user's snooze
    ('remind me in N days') has elapsed."""
    if not is_newer(latest, installed):
        return False
    return now >= st.snooze_until_ts


def snooze_for_days(st: UpdateState, days: float, now: float) -> UpdateState:
    """Record 'remind me in N days' (N can be fractional). Returns the same state."""
    st.snooze_until_ts = now + max(0.0, float(days)) * 86400.0
    return st


def check_for_notice(state_root: str | Path, *, now: float | None = None,
                     interval: float = _DEFAULT_INTERVAL, opener=None) -> str | None:
    """Throttled startup check used to NOTIFY (not prompt). Returns a one-line
    notice if a newer version is available and the snooze has elapsed, else None.
    Hits the network at most once per `interval`; otherwise uses the cached
    last-seen version. Fail-silent and quick so it never slows or breaks startup."""
    now = now if now is not None else time.time()
    st = load_state(state_root)
    if should_check(st, now, interval):
        latest = latest_version(opener=opener)
        st.last_check_ts = now
        if latest:
            st.last_seen_latest = latest
        save_state(state_root, st)
    else:
        latest = st.last_seen_latest or None
    installed = installed_version()
    if should_notify(st, now, latest, installed):
        return f"⬆ Syntra {latest} available (you have {installed}) — run `syntra update`"
    return None
