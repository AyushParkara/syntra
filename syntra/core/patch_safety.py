"""Patch safety assessment — classify a file write as auto-safe, ask-user, or reject.

Safety-aware patch validation. The decision is made BEFORE the edit is
applied (and re-checkable at apply time — defense in depth). Path normalization
defeats traversal tricks (../, ./, symlinks) regardless of OS.

Three outcomes:
  AUTO_APPROVE — write lands inside a declared writable root; obviously safe.
  ASK_USER     — write is inside the workspace but outside writable roots, or
                 touches a sensitive path; surface to the user.
  REJECT       — write escapes the workspace entirely, or hits a hard-blocked path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


AUTO_APPROVE = "auto_approve"
ASK_USER = "ask_user"
REJECT = "reject"

# Sensitive-path detection is owned by ONE place — access_modes.is_sensitive_path — so the
# pipeline-edit path (here) and the agent-tool path (permissions.permit) can never diverge.


@dataclass(frozen=True)
class SafetyVerdict:
    decision: str          # AUTO_APPROVE | ASK_USER | REJECT
    reason: str
    resolved_path: str = ""

    @property
    def auto(self) -> bool:
        return self.decision == AUTO_APPROVE

    @property
    def rejected(self) -> bool:
        return self.decision == REJECT


def _normalize(path: str, root: Path) -> Path:
    """Resolve a (possibly relative) path against root, collapsing ./ and ../
    and following symlinks, so traversal tricks can't escape detection.

    #257: NFKC-fold first so a Unicode-obfuscated traversal (fullwidth `．．／` → `../`) is
    resolved to its real form before the containment check. A null byte is rejected upstream
    (assess_write) — realpath would raise on it, which must be a clean REJECT, not a crash."""
    import unicodedata
    p = Path(unicodedata.normalize("NFKC", path))
    if not p.is_absolute():
        p = root / p
    # os.path.realpath resolves symlinks + normalizes; works even if the file
    # doesn't exist yet (resolves the existing prefix).
    return Path(os.path.realpath(str(p)))


def assess_write(
    path: str,
    *,
    workspace_root: str | Path,
    writable_roots: list[str] | None = None,
) -> SafetyVerdict:
    """Decide whether a write to `path` is auto-safe, needs approval, or rejected.

    writable_roots: subdirs (relative to workspace or absolute) where writes are
    auto-approved. Defaults to [workspace_root] when None — i.e. anywhere in the
    project is auto-safe, anything outside needs approval/rejection.
    """
    # #257: a null byte (or realpath choking on the path) is a clean REJECT — never a crash that
    # could bubble past the gate. Reject before any resolution touches the raw string.
    if "\x00" in (path or ""):
        return SafetyVerdict(REJECT, f"path contains a null byte: {path!r}", "")
    root = Path(os.path.realpath(str(workspace_root)))
    try:
        target = _normalize(path, root)
    except (ValueError, OSError) as e:
        return SafetyVerdict(REJECT, f"unresolvable path {path!r}: {e}", "")

    # Hard reject: escapes the workspace entirely.
    try:
        target.relative_to(root)
        inside_workspace = True
    except ValueError:
        inside_workspace = False
    if not inside_workspace:
        return SafetyVerdict(REJECT, f"write escapes workspace: {path!r}", str(target))

    # Sensitive path check — never auto-approve secrets/.git/etc. One canonical definition.
    from .access_modes import is_sensitive_path
    if is_sensitive_path(path) or is_sensitive_path(str(target)):
        return SafetyVerdict(ASK_USER, f"sensitive path: {path!r}", str(target))

    # Writable-roots check.
    roots = []
    for wr in (writable_roots or []):
        wrp = Path(wr)
        if not wrp.is_absolute():
            wrp = root / wrp
        roots.append(Path(os.path.realpath(str(wrp))))
    if not roots:
        roots = [root]  # default: whole workspace is writable

    for wr in roots:
        try:
            target.relative_to(wr)
            return SafetyVerdict(AUTO_APPROVE, f"within writable root {wr.name}/", str(target))
        except ValueError:
            continue

    return SafetyVerdict(ASK_USER, f"outside writable roots: {path!r}", str(target))
