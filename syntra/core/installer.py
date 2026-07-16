"""Install user/third-party agents + skills into Syntra's plugin directory.

A user brings their own agent/skill definitions (or a whole plugin repo) and
installs them so Syntra auto-discovers them (see plugin_loader). Sources:

  - a local DIRECTORY  : a plugin (has plugin.json / agents/ / skills/) is copied
                         as-is; a bare folder of *.md is wrapped as agents.
  - a local .md FILE   : wrapped into a one-agent plugin.
  - a git / http URL   : git-cloned into the plugins dir.

Everything lands under ~/.config/syntra/plugins/<name>/ and is picked up on the
next launch (or immediately, since discover_plugins() reads the dir live).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .plugin_loader import load_plugin


def plugins_dir() -> Path:
    d = Path.home() / ".config" / "syntra" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    """A filesystem-safe plugin directory name."""
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return n or "plugin"


def _summary(dest: Path) -> str:
    p = load_plugin(dest)
    if not p:
        return "installed (no agents/skills detected — check the structure)"
    bits = []
    if p.agents:
        bits.append(f"{len(p.agents)} agent(s): " + ", ".join(a.name for a in p.agents))
    if p.skills:
        bits.append(f"{len(p.skills)} skill(s): " + ", ".join(s.name for s in p.skills))
    if p.commands:
        bits.append(f"{len(p.commands)} command(s)")
    return " · ".join(bits) or "installed (empty)"


def _wrap_md_as_agent(md: Path, dest_root: Path) -> tuple[bool, str]:
    """A single agent .md → a one-agent plugin dir."""
    name = _safe_name(md.stem)
    dest = dest_root / name
    if dest.exists():
        return False, f"already installed: {name} (remove {dest} to reinstall)"
    (dest / "agents").mkdir(parents=True, exist_ok=True)
    shutil.copy2(md, dest / "agents" / f"{name}.md")
    (dest / "plugin.json").write_text(json.dumps({"name": name, "description": f"agent {name}"}))
    return True, f"installed agent '{name}' — {_summary(dest)}"


def _install_dir(src: Path, dest_root: Path) -> tuple[bool, str]:
    """A directory: a real plugin (copy as-is) or a folder of agent .md (wrap)."""
    is_plugin = ((src / "plugin.json").exists() or (src / "agents").is_dir()
                 or (src / "skills").is_dir() or (src / "commands").is_dir())
    if is_plugin:
        name = _safe_name(src.name)
        dest = dest_root / name
        if dest.exists():
            return False, f"already installed: {name} (remove {dest} to reinstall)"
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return True, f"installed plugin '{name}' — {_summary(dest)}"
    mds = list(src.glob("*.md"))
    if not mds:
        return False, f"no plugin.json / agents / skills / *.md found in {src}"
    name = _safe_name(src.name)
    dest = dest_root / name
    if dest.exists():
        return False, f"already installed: {name} (remove {dest} to reinstall)"
    (dest / "agents").mkdir(parents=True, exist_ok=True)
    for md in mds:
        shutil.copy2(md, dest / "agents" / md.name)
    (dest / "plugin.json").write_text(json.dumps({"name": name, "description": f"{len(mds)} agents"}))
    return True, f"installed '{name}' — {_summary(dest)}"


def _git_url_ok(url: str) -> tuple[bool, str]:
    """#261: only allow git URLs over a vetted transport, and never let a `--…`-leading URL be
    interpreted as a git OPTION (arg-injection, e.g. `--upload-pack=<cmd>`). Cleartext http,
    `file://`, and the `ext::`/`ftp://` transports are refused (MITM / local-file / RCE surfaces)."""
    u = (url or "").strip()
    if u.startswith("-"):
        return False, "refusing git URL that starts with '-' (arg-injection risk)"
    # Only vetted transports: https, ssh, and the scp-like git@host:path form. Cleartext http,
    # file://, ext::, ftp:// etc. are refused (MITM / local-file / RCE surfaces).
    if u.startswith(("https://", "ssh://", "git@")):
        return True, ""
    return False, f"refusing git URL scheme (only https/ssh/git@ allowed): {url!r}"


def _install_git(url: str, dest_root: Path) -> tuple[bool, str]:
    ok, why = _git_url_ok(url)
    if not ok:
        return False, why
    name = _safe_name(re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]))
    dest = dest_root / name
    if dest.exists():
        return False, f"already installed: {name} (remove {dest} to reinstall)"
    try:
        # #198/#261: harden the clone — kill the ext:: transport + any config-driven program, and
        # `--` ends option parsing so the URL can't inject a flag. Scheme is vetted above.
        subprocess.run(["git", "-c", "protocol.ext.allow=never", "-c", "core.fsmonitor=",
                        "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1", "--", url, str(dest)],
                       check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.CalledProcessError as e:
        return False, f"git clone failed: {(e.stderr or '')[:200]}"
    except subprocess.TimeoutExpired:
        return False, "git clone timed out"
    if not ((dest / "plugin.json").exists() or (dest / "agents").is_dir()
            or (dest / "skills").is_dir() or list(dest.glob("*.md"))):
        shutil.rmtree(dest, ignore_errors=True)
        return False, "cloned repo has no agents/skills/plugin.json — not a Syntra plugin"
    return True, f"installed '{name}' from git — {_summary(dest)}"


def install(source: str) -> tuple[bool, str]:
    """Install agents/skills from a local path or a git/http URL. Returns (ok, message)."""
    s = (source or "").strip()
    if not s:
        return False, "usage: install <local-folder | file.md | git-url>"
    dest_root = plugins_dir()
    # #261: explicitly REFUSE dangerous URL-ish transports up front (rather than silently
    # treating them as a missing local path) — cleartext http, file://, ext::, ftp://, and any
    # `-`-leading arg-injection string.
    if s.startswith(("http://", "file://", "ext::", "ftp://", "ftps://", "-")):
        return False, f"refusing unsafe install source (only https/ssh/git@ URLs): {source!r}"
    if s.startswith(("https://", "git@", "ssh://")) or s.endswith(".git"):
        return _install_git(s, dest_root)
    p = Path(s).expanduser()
    if p.is_dir():
        return _install_dir(p, dest_root)
    if p.is_file() and p.suffix == ".md":
        return _wrap_md_as_agent(p, dest_root)
    return False, f"not found or unsupported source: {source}"


def uninstall(name: str) -> tuple[bool, str]:
    """Remove an installed plugin by name."""
    dest = plugins_dir() / _safe_name(name)
    if not dest.is_dir():
        return False, f"not installed: {name}"
    shutil.rmtree(dest, ignore_errors=True)
    return True, f"uninstalled '{name}'"


def installed_agents() -> list:
    """All installed (plugin) agents across the plugins dir."""
    from .plugin_loader import discover_plugins
    out = []
    for p in discover_plugins():
        out.extend(p.agents)
    return out


def set_enabled(name: str, enabled: bool) -> tuple[bool, str]:
    """Enable/disable an installed plugin WITHOUT uninstalling it. Disabled plugins are
    dot-prefixed on disk (``.<name>``) so ``discover_plugins`` skips them; re-enabling
    restores the name. Returns (ok, message)."""
    root = plugins_dir()
    safe = _safe_name(name)
    active = root / safe
    hidden = root / f".{safe}"
    if enabled:
        if active.is_dir():
            return True, f"already enabled: {safe}"
        if hidden.is_dir():
            hidden.rename(active)
            return True, f"enabled '{safe}'"
        return False, f"not installed: {safe}"
    if hidden.is_dir():
        return True, f"already disabled: {safe}"
    if active.is_dir():
        active.rename(hidden)
        return True, f"disabled '{safe}'"
    return False, f"not installed: {safe}"


def list_installed() -> list[dict]:
    """Every installed plugin with its enabled state + one-line summary — the data behind
    the `/plugins` picker and `syntra plugins` listing. Disabled plugins are dot-prefixed."""
    root = plugins_dir()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        out.append({"name": entry.name.lstrip("."),
                    "enabled": not entry.name.startswith("."),
                    "summary": _summary(entry)})
    return out
