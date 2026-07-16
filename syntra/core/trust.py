"""Folder-local provider-config trust (VS Code "workspace trust" style).

The first time Syntra would load a folder-local `./.syntra/providers.json`, the CLI
asks once whether to trust it; the answer is remembered per (config path + content
hash). If the file later changes, trust is re-asked. This stops a cloned repo from
silently feeding Syntra an attacker's provider config — without nagging you about
the folder-local configs you set up yourself.

Pure-ish: the only I/O is the trust-store JSON file. The prompt is supplied by the
caller (`registry.preflight_repo_local_trust`).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _store_path() -> Path:
    override = os.environ.get("SYNTRA_TRUST_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "syntra" / "trusted.json"


def _content_hash(config_path) -> str:
    try:
        return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_store() -> dict:
    try:
        return json.loads(_store_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def trust_status(config_path) -> str:
    """'trusted' | 'declined' | 'unknown' for this exact file (absolute path +
    current content). 'unknown' if never decided OR the file changed since (re-ask)."""
    entry = _load_store().get(str(Path(config_path).resolve()))
    if not isinstance(entry, dict) or entry.get("hash") != _content_hash(config_path):
        return "unknown"
    return "trusted" if entry.get("trusted") else "declined"


def is_trusted(config_path) -> bool:
    return trust_status(config_path) == "trusted"


def record_trust(config_path) -> None:
    """Remember this folder's config as TRUSTED (re-asked if the file changes)."""
    _record(config_path, True)


def record_decline(config_path) -> None:
    """Remember that this folder's config was DECLINED, so we don't re-ask."""
    _record(config_path, False)


def _record(config_path, trusted: bool) -> None:
    h = _content_hash(config_path)
    if not h:
        return
    store = _load_store()
    store[str(Path(config_path).resolve())] = {"hash": h, "trusted": bool(trusted)}
    dest = _store_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(dest)
