"""Config migrations runner + runtime model-alias remap (#148/#212).

Syntra stamps a `_schema_version` into ~20 on-disk state files but never ACTED on
it: when a model id is retired (a vendor renames or drops an id), a stored id in
the GLOBAL `providers.json` / `overrides.json` keeps pointing at the dead name and
silently mis-routes after an upgrade. This module closes that in two layers:

1. **A migrations runner.** `run_migrations(config_dir)` reads a single
   `<config_dir>/_migration_state.json` version int, runs only the migrations whose
   version is newer than the stored one, in order, and bumps the stored version
   ONCE at the end. Each migration is **idempotent** (safe to re-run) so a partial
   run or a re-invocation never double-applies.

2. **A runtime alias table.** `remap_model_id(id)` maps a retired id to its current
   id, passing every UNKNOWN id through unchanged — so it can be called on any id in
   the hot path without ever changing behavior for a live model. The migrations
   rewrite stored ids using this same table, so disk and runtime agree.

KEY CORRECTNESS RULE (from the spec): the runner rewrites the GLOBAL config it is
handed and must NEVER reach into a project-local `./.syntra` config. Rewriting from
a merged/project view would (a) risk an infinite loop (a project value re-appearing
after each rewrite) and (b) wrongly promote a project-only pin into the global
config. The caller passes the resolved GLOBAL config dir; we touch only files
directly inside it.

New module — zero conflict with the other build lanes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def global_config_dir() -> Path:
    """The GLOBAL Syntra config directory (never a project-local ./.syntra).

    Mirrors the XDG resolution the registry/overrides use: `$XDG_CONFIG_HOME/syntra`
    else `~/.config/syntra`. This is the ONLY dir the migrations runner is allowed to
    rewrite (the key correctness rule — see the module docstring)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "syntra"


# Bump when you ADD a migration below. The runner compares this to the stored
# version and runs the gap.
CURRENT_MIGRATION_VERSION = 1


# Retired model id -> current model id. The single source of truth for both the
# runtime remap and the disk rewrite. Empty by default (no ids retired yet); add a
# row when a vendor renames/drops an id. An id absent here is NEVER touched.
_MODEL_ALIASES: dict[str, str] = {}


def remap_model_id(model_id):
    """Map a retired model id to its current id; pass unknown ids through unchanged.

    Pure + total: safe to call on any id in the routing hot path — a live id (not in
    the alias table) is returned exactly as given, so behavior never changes for a
    model that still exists. `None`/"" pass through."""
    if not model_id:
        return model_id
    return _MODEL_ALIASES.get(model_id, model_id)


def _remap_ids_in_list(ids) -> tuple[list, bool]:
    """Remap a list of model ids, dropping duplicates the remap creates. Returns
    (new_list, changed)."""
    out: list = []
    changed = False
    seen: set = set()
    for mid in ids:
        new = remap_model_id(mid)
        if new != mid:
            changed = True
        if new not in seen:
            seen.add(new)
            out.append(new)
    return out, changed


# ---------------------------------------------------------------- migrations
# Each migration: (version, name, fn(config_dir: Path) -> bool). fn returns True if
# it changed anything (for reporting). fn MUST be idempotent and touch only files
# directly inside `config_dir` (the GLOBAL config), never a project-local tree.

def _mig_remap_retired_model_ids(config_dir: Path) -> bool:
    """Rewrite retired model ids to their current ids in the GLOBAL providers.json
    (`allowed_models`) and overrides.json (blacklists/penalties/pins/etc.).
    Idempotent: re-running finds nothing to change once ids are current."""
    changed = False
    changed |= _remap_providers_file(config_dir / "providers.json")
    changed |= _remap_overrides_file(config_dir / "overrides.json")
    return changed


def _remap_providers_file(path: Path) -> bool:
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return False
    changed = False
    for prov in doc.get("providers", []) or []:
        if not isinstance(prov, dict):
            continue
        allowed = prov.get("allowed_models")
        if isinstance(allowed, list):
            new, ch = _remap_ids_in_list(allowed)
            if ch:
                prov["allowed_models"] = new
                changed = True
    if changed:
        _write_json(path, doc)
    return changed


def _remap_overrides_file(path: Path) -> bool:
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return False
    changed = False
    # every section keys rows by a "model_id" field
    for section in ("blacklists", "penalties", "extra_specialties",
                    "role_overrides", "role_pins"):
        for row in doc.get(section, []) or []:
            if isinstance(row, dict) and row.get("model_id"):
                new = remap_model_id(row["model_id"])
                if new != row["model_id"]:
                    row["model_id"] = new
                    changed = True
    if changed:
        _write_json(path, doc)
    return changed


_MIGRATIONS = [
    (1, "remap-retired-model-ids", _mig_remap_retired_model_ids),
]


# ---------------------------------------------------------------- runner
def _state_path(config_dir: Path) -> Path:
    return Path(config_dir) / "_migration_state.json"


def stored_version(config_dir: Path) -> int:
    doc = _read_json(_state_path(config_dir))
    if isinstance(doc, dict):
        try:
            return int(doc.get("version", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def run_migrations(config_dir) -> list[str]:
    """Run every migration newer than the stored version against the GLOBAL config
    dir, in order, then stamp the version to current ONCE. Returns the names of the
    migrations that ran (empty when already current). Never touches a project config.
    """
    config_dir = Path(config_dir)
    have = stored_version(config_dir)
    if have >= CURRENT_MIGRATION_VERSION:
        return []
    ran: list[str] = []
    for version, name, fn in _MIGRATIONS:
        if version <= have:
            continue
        try:
            fn(config_dir)
        except Exception:  # noqa: BLE001 - a migration must never brick startup
            # Stop at the first failure WITHOUT bumping past it, so a fix + re-run
            # resumes here rather than skipping the failed migration.
            _stamp_version(config_dir, version - 1)
            return ran
        ran.append(name)
    _stamp_version(config_dir, CURRENT_MIGRATION_VERSION)
    return ran


def run_migrations_if_needed() -> list[str]:
    """Startup hook: run pending migrations against the resolved GLOBAL config dir.

    Only touches disk when the global config dir already exists (a first-ever run
    with no config has nothing to migrate). Fail-open: any error is swallowed so a
    migration issue can never block the app from starting."""
    try:
        cfg = global_config_dir()
        if not cfg.exists():
            return []
        return run_migrations(cfg)
    except Exception:  # noqa: BLE001 - startup must never die on a migration
        return []


def _stamp_version(config_dir: Path, version: int) -> None:
    _write_json(_state_path(config_dir), {"version": int(version),
                                          "_schema": "syntra.migration_state",
                                          "_schema_version": 1})


# ---------------------------------------------------------------- io helpers
def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, doc) -> None:
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique per-process temp + os.replace = atomic, no cross-process temp collision
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
