"""Folder-local EXEC-surface trust (#201c / #200).

`trust.py` already gates a repo's own `./.syntra/providers.json` (which model endpoint
serves the session). But a cloned repo can ALSO ship exec surfaces that run on the host
with no consent today:

  - `./.syntra/hooks.json`  — a `pre_tool_use` command hook runs `shell=True` on the first
    tool call (arbitrary host RCE) when folder-local mode is on (`SYNTRA_STATE_DIR=.syntra`).
  - `./.syntra/plugins/*`   — agent/command/skill PROMPT TEXT is auto-discovered from the CWD
    with no trust gate and no injection scan (prompt-injection into every pipeline role).
  - repo-local MCP server specs — auto-spawned subprocesses.

This module extends the SAME hash-based trust primitive to those surfaces. Enforcement is
OFF by default so the library/tests behave exactly as before; the CLI turns it on at startup
(mirroring `registry.enable_trust_enforcement`). When on, a repo-local exec surface is only
loaded if the repo's `.syntra` dir has been explicitly trusted; global (`~/.config/syntra`)
surfaces are always allowed. Prompt bodies are additionally screened for injection markers.
"""

from __future__ import annotations

import os
from pathlib import Path

# Enforcement is a process-level switch (like registry._trust_enforced). Default OFF: a plain
# `import` / test run must not start refusing repo-local surfaces. The CLI flips it on.
_enforced = False


def enable_enforcement() -> None:
    """Turn on repo-local exec-surface trust gating (the CLI calls this at startup)."""
    global _enforced
    _enforced = True


def reset_enforcement() -> None:
    """Turn gating back OFF (tests; also the default state)."""
    global _enforced
    _enforced = False


def enforcement_enabled() -> bool:
    return _enforced


def _global_roots() -> list[Path]:
    """Directories whose contents are user-installed and always trusted."""
    roots = [Path.home() / ".config" / "syntra"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        roots.append(Path(xdg).expanduser() / "syntra")
    return [r.resolve() for r in roots]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _is_repo_local(surface_path, repo_root=None) -> bool:
    """True if this surface is a REPO-LOCAL auto-discovered exec surface — i.e. it lives inside
    the current working directory tree (a cloned repo's `./.syntra/...`), which is what a
    malicious clone can plant. A global install (`~/.config/syntra`) is trusted; an explicitly
    configured path OUTSIDE the CWD (e.g. `$SYNTRA_STATE_DIR=/opt/team-config`) is a deliberate
    user choice, not a cloned-repo surface, so it is not gated. A `repo_root` hint (used by the
    unit tests / callers that already know the owning root) overrides the CWD check."""
    p = Path(surface_path).resolve()
    for g in _global_roots():
        if _is_under(p, g):
            return False                        # global install → always trusted
    if repo_root is not None:
        return _is_under(p, Path(repo_root).resolve())
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return True                             # can't determine cwd → fail closed (gate it)
    return _is_under(p, cwd)                     # under CWD → cloned-repo surface → gate it


def repo_local_exec_allowed(surface_path, repo_root=None) -> bool:
    """May this exec surface (hooks.json / an mcp spec file / a plugin's own file) be loaded?

    - Enforcement OFF  → always True (unchanged behavior; tests/library).
    - Global surface   → always True (user-installed under ~/.config/syntra).
    - Repo-local file  → True only if THIS FILE is explicitly trusted (content-hashed, so an
      edit re-asks) — reusing the same per-file trust primitive as providers.json.
    """
    if not _enforced:
        return True
    if not _is_repo_local(surface_path, repo_root):
        return True                             # global → trusted
    from .trust import is_trusted
    return is_trusted(surface_path)             # per-file content-hash trust


def plugin_body_is_injected(text: str) -> bool:
    """True if a plugin's agent/command/skill body carries a prompt-injection marker — screened
    (like AGENTS.md) so untrusted repo-local prompt text can't hijack a pipeline role even in a
    folder that was trusted for exec. Reuses the one canonical marker scan."""
    try:
        from .project_instructions import looks_injected
        return looks_injected(text or "")
    except Exception:  # noqa: BLE001 - never block on the scanner failing
        return False
