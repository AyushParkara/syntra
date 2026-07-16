"""Canonical resolvers for Syntra's packaged data files.

The model catalog and example config ship INSIDE the package (``syntra/data/``)
so a clean ``pip install`` works — an installed wheel has no repo-root ``data/``.
These helpers find a data file across three layers, in order:

  1. an explicit env override (e.g. ``SYNTRA_CATALOG_PATH``),
  2. the packaged copy at ``syntra/data/<name>`` (the normal installed case),
  3. a dev fallback at the repo-root ``data/<name>`` (editable checkouts that
     haven't been re-synced) — best-effort only.

Centralizing this kills the five copies of
``Path(__file__).parent.parent.parent / "data" / ...`` scattered across the code,
each of which silently broke once the package was installed rather than run from
the source tree.
"""

from __future__ import annotations

import os
from pathlib import Path

# syntra/core/paths.py -> parent is syntra/core, parent.parent is the `syntra` package.
_PACKAGE_DIR = Path(__file__).resolve().parent.parent      # .../syntra
_REPO_ROOT = _PACKAGE_DIR.parent                            # .../<repo>


def package_data_dir() -> Path:
    """The packaged data directory (``syntra/data/``)."""
    return _PACKAGE_DIR / "data"


def _resolve(filename: str, env_var: str | None = None) -> Path:
    """Resolve a data file: env override → packaged copy → repo-root dev fallback."""
    if env_var:
        explicit = os.environ.get(env_var)
        if explicit:
            return Path(explicit).expanduser().resolve()
    packaged = package_data_dir() / filename
    if packaged.exists():
        return packaged
    dev = _REPO_ROOT / "data" / filename
    if dev.exists():
        return dev
    # Nothing found — return the packaged path so the error message points at the
    # canonical, expected location rather than a stale dev path.
    return packaged


def default_catalog_path() -> Path:
    """Path to the model catalog (``aa_catalog.json``)."""
    return _resolve("aa_catalog.json", env_var="SYNTRA_CATALOG_PATH")


def providers_example_path() -> Path:
    """Path to the example providers config (``providers.example.json``)."""
    return _resolve("providers.example.json")


def default_catalog_overlay_path() -> Path:
    """User catalog OVERLAY (auto-detected models append here, never the bundled catalog).
    Env override ``SYNTRA_CATALOG_OVERLAY`` → else ``$XDG_CONFIG_HOME/syntra/catalog_overlay.json``
    (defaults to ``~/.config/syntra/catalog_overlay.json``), beside providers.json/state."""
    explicit = os.environ.get("SYNTRA_CATALOG_OVERLAY")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "syntra" / "catalog_overlay.json"
