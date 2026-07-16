"""Persistent registry of MCP servers the user has configured (mcp.json).

A server spec uses the same string format accepted by ``--mcp``:
  stdio : ``npx -y @modelcontextprotocol/server-github``
  http  : ``https://host/mcp [bearer-token]``

Configured servers are attached automatically to every agent run (on top of any
per-run ``--mcp`` flags) and listed by the ``/mcp`` slash command. This is a tiny,
single-concern JSON file -- no blob state, easy to hand-edit.
"""

from __future__ import annotations

import json
from pathlib import Path


def config_path(state_root) -> Path:
    return Path(state_root) / "mcp.json"


def load_servers(state_root) -> list[str]:
    """Return the configured server specs (empty list if none / unreadable)."""
    try:
        data = json.loads(config_path(state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("servers", []) if isinstance(data, dict) else []
    return [str(s).strip() for s in servers if str(s).strip()]


def save_servers(state_root, servers) -> None:
    """Persist server specs, de-duplicated, order-preserving."""
    p = config_path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    out: list[str] = []
    for s in servers:
        s = str(s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    p.write_text(json.dumps({"servers": out}, indent=2), encoding="utf-8")


def add_server(state_root, spec) -> bool:
    """Add a server spec. Returns False if blank or already present."""
    spec = str(spec).strip()
    if not spec:
        return False
    servers = load_servers(state_root)
    if spec in servers:
        return False
    servers.append(spec)
    save_servers(state_root, servers)
    return True


def remove_server(state_root, spec) -> bool:
    """Remove a server by exact spec, leading-token, or substring. Returns False if no match."""
    spec = str(spec).strip()
    if not spec:
        return False
    servers = load_servers(state_root)
    kept = [s for s in servers if s != spec and not s.startswith(spec + " ")]
    if len(kept) == len(servers):  # fall back to substring convenience match
        kept = [s for s in servers if spec not in s]
    if len(kept) == len(servers):
        return False
    save_servers(state_root, kept)
    return True
