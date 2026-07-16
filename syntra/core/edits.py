"""File-edit proposals: diff -> approve -> apply -> checkpoint -> rollback (Phase 3).

Mutations are the riskiest thing Syntra does, so this module makes them:
- explicit    : a structured EditProposal, never an implicit write.
- previewable : a unified diff is rendered before anything touches disk.
- confined    : every path must resolve inside the workspace root.
- reversible  : applying snapshots the prior state to a checkpoint; restore puts
                it back exactly.
- safe to undo: restore refuses to clobber a file that changed AFTER apply
                (dirty-file protection) unless allow_dirty is set.

The approval gate itself lives with role_policy/CLI; this module performs the
mechanics only after the caller has approved. Deterministic + unit-tested
(hashes make "restores exactly" checkable).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---- #207: write fidelity (line-ending + encoding preservation) + atomicity ----
# Models emit LF/UTF-8 text. Writing that verbatim silently rewrites a CRLF file's
# every line ending and re-encodes a UTF-16 file as UTF-8. We keep the model's
# clean LF text internally (for the diff + the sha), but restore the file's own
# convention on write. And every write is atomic: a crash mid-write must leave the
# original intact, never a half-written file.
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF8 = b"\xef\xbb\xbf"


def _decode_fidelity(raw: bytes) -> tuple[str, str, str]:
    """Decode file bytes -> (text_lf, encoding, newline).

    - encoding: from a BOM if present (utf-16 / utf-8-sig), else utf-8, with a
      latin-1 fallback so a lone non-utf8 byte never crashes an edit.
    - newline: the file's dominant convention ("\\r\\n", "\\r", or "\\n").
    - text_lf: content normalized to LF so the model/diff/sha share one convention
      no matter what's on disk. `_encode_fidelity` is its exact inverse.
    """
    if raw.startswith(_BOM_UTF16_LE) or raw.startswith(_BOM_UTF16_BE):
        encoding = "utf-16"
    elif raw.startswith(_BOM_UTF8):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "latin-1"          # last resort: round-trips any byte
        text = raw.decode("latin-1")
    if "\r\n" in text:
        newline = "\r\n"
    elif "\r" in text:
        newline = "\r"
    else:
        newline = "\n"
    text_lf = text.replace("\r\n", "\n").replace("\r", "\n")
    return text_lf, encoding, newline


def _encode_fidelity(text_lf: str, encoding: str, newline: str) -> bytes:
    """Inverse of `_decode_fidelity`: re-apply `newline` + `encoding` to LF text."""
    if newline == "\r\n":
        body = text_lf.replace("\n", "\r\n")
    elif newline == "\r":
        body = text_lf.replace("\n", "\r")
    else:
        body = text_lf
    return body.encode(encoding)


def _read_text_fidelity(path: Path) -> tuple[str, str, str]:
    """Read a file as (text_lf, encoding, newline). Never raises on encoding."""
    return _decode_fidelity(Path(path).read_bytes())


def _atomic_write_bytes(path, data: bytes) -> None:
    """Write `data` to `path` atomically, preserving an edited file's existing mode.

    Thin wrapper over the shared hardened primitive (#258): temp+fsync+`os.replace` (a crash
    leaves the ORIGINAL untouched, never a truncated target) + `O_NOFOLLOW` symlink refusal at
    the target. `mode=None` PRESERVES the file's current permissions — editing a 0o755 script
    keeps +x (the old local writer silently reset it to the temp's mode), and edited source is
    never force-locked to 0o600. Checkpoint snapshots are brand-new files → left at the umask
    default, same as before.
    """
    fsutil.write_atomic_bytes(path, data, mode=None)


class EditError(RuntimeError):
    """An edit could not be proposed, applied, or rolled back safely."""


@dataclass(frozen=True)
class EditProposal:
    """A single proposed change to one file (relative to the workspace root)."""

    path: str              # relative path
    new_content: str       # full new file content ("" allowed); ignored if delete
    delete: bool = False

    def kind(self) -> str:
        return "delete" if self.delete else "write"


# Executor edit-block format (instructed in execute mode). Unambiguous + easy to
# parse deterministically:
#   <<<EDIT path=relative/file.py>>>
#   <full new file content>
#   <<<END>>>
#   <<<DELETE path=relative/old.py>>>
EDIT_FORMAT_INSTRUCTIONS = (
    "When you need to change files, emit edit blocks in EXACTLY this format "
    "(full file content per edit, paths relative to the workspace):\n"
    "<<<EDIT path=relative/path.py>>>\n<the COMPLETE new file content>\n<<<END>>>\n"
    "To delete a file: <<<DELETE path=relative/path.py>>>\n"
    "Emit nothing between blocks except normal explanation. Do not abbreviate file content."
)

_EDIT_RE = re.compile(
    r"<<<EDIT\s+path=(?P<path>[^>\n]+?)>>>\n(?P<body>.*?)\n?<<<END>>>",
    re.DOTALL,
)
_DELETE_RE = re.compile(r"<<<DELETE\s+path=(?P<path>[^>\n]+?)>>>")


def parse_edit_proposals(text: str) -> list[EditProposal]:
    """Extract structured EditProposals from executor output. Deterministic.

    EDIT blocks are parsed first and their spans removed, so a DELETE marker that
    happens to appear inside an edit body is not double-counted.
    """
    if not text:
        return []
    out: list[EditProposal] = []
    remainder_parts: list[str] = []
    last = 0
    for m in _EDIT_RE.finditer(text):
        remainder_parts.append(text[last:m.start()])
        last = m.end()
        out.append(EditProposal(path=m.group("path").strip(), new_content=m.group("body")))
    remainder_parts.append(text[last:])
    remainder = "".join(remainder_parts)
    out.extend(EditProposal(path=m.group("path").strip(), new_content="", delete=True)
               for m in _DELETE_RE.finditer(remainder))
    return out


@dataclass
class AppliedEdit:
    path: str
    kind: str              # "write" | "delete"
    existed_before: bool
    before_sha: str        # sha of prior content ("" if file didn't exist)
    after_sha: str         # sha of new content ("" for delete)
    checkpoint_id: str

    def to_dict(self) -> dict:
        return {
            "path": self.path, "kind": self.kind,
            "existed_before": self.existed_before,
            "before_sha": self.before_sha, "after_sha": self.after_sha,
            "checkpoint_id": self.checkpoint_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppliedEdit":
        return cls(
            path=d["path"], kind=d["kind"],
            existed_before=bool(d.get("existed_before", False)),
            before_sha=d.get("before_sha", ""), after_sha=d.get("after_sha", ""),
            checkpoint_id=d["checkpoint_id"],
        )


def _resolve_in_workspace(rel_path: str, root: Path) -> Path:
    """Resolve rel_path under root, refusing anything that escapes it."""
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise EditError(f"path escapes workspace: {rel_path!r}")
    return target


def render_diff(proposal: EditProposal, *, workspace_root: str | Path) -> str:
    """Unified diff of the proposal vs the current file. No disk writes."""
    root = Path(workspace_root).resolve()
    target = _resolve_in_workspace(proposal.path, root)
    # LF-normalized read so the diff is clean regardless of the file's on-disk
    # newline/encoding (and a UTF-16/binary-ish file can't crash the diff).
    before = _read_text_fidelity(target)[0] if target.is_file() else ""

    if proposal.delete:
        after = ""
        to_label = "(deleted)"
    else:
        after = proposal.new_content
        to_label = f"b/{proposal.path}"

    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{proposal.path}" if before else "(new file)",
        tofile=to_label,
    )
    return "".join(diff)


@dataclass
class EditApplier:
    """Applies approved edits with checkpointing + exact rollback."""

    workspace_root: Path
    checkpoints_root: Path  # where pre-apply snapshots live (e.g. task_dir/checkpoints)
    applied: list[AppliedEdit] = field(default_factory=list)
    turn_diff: object = None  # optional TurnDiffCapture; records before/after per apply
    _seq: int = 0  # monotonic counter -> unique checkpoint ids even within 1ms

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).resolve()
        self.checkpoints_root = Path(self.checkpoints_root)

    def apply(self, proposal: EditProposal) -> AppliedEdit:
        """Snapshot the current state, then apply the proposal. Confined.

        Preserves the file's on-disk line-ending + encoding (#207): reads with
        fidelity, works internally in LF, and re-applies the original convention on
        write. The write is atomic (temp + `os.replace`), so a crash mid-write
        leaves the original intact. The checkpoint snapshots the exact prior BYTES
        (not decoded text) so rollback is byte-perfect.
        """
        target = _resolve_in_workspace(proposal.path, self.workspace_root)
        existed = target.is_file()
        if existed:
            raw_before = target.read_bytes()
            before, encoding, newline = _decode_fidelity(raw_before)
        else:
            raw_before, before, encoding, newline = b"", "", "utf-8", "\n"

        # Turn-level diff: capture the baseline at first touch this turn.
        if self.turn_diff is not None:
            try:
                self.turn_diff.on_before_edit(proposal.path, before)
            except Exception:  # noqa: BLE001 - tracking must never break an apply
                pass

        # Monotonic seq guarantees uniqueness even for same-path same-ms applies
        # (the source of an earlier flaky rollback bug).
        self._seq += 1
        checkpoint_id = f"{int(time.time() * 1000)}-{self._seq:04d}-{_sha256(proposal.path)[:8]}"
        cp_dir = self.checkpoints_root / checkpoint_id
        cp_dir.mkdir(parents=True, exist_ok=True)
        # Record provenance + the exact prior bytes (or a "did not exist" marker).
        (cp_dir / "meta.txt").write_text(
            f"path={proposal.path}\nexisted={existed}\nbefore_sha={_sha256(before) if existed else ''}\n"
        )
        if existed:
            # snapshot the RAW bytes -> byte-exact rollback (keeps CRLF/BOM/encoding).
            _atomic_write_bytes(cp_dir / "before.snapshot", raw_before)

        if proposal.delete:
            if existed:
                target.unlink()
            after_sha = ""
            kind = "delete"
        else:
            # Re-apply the file's own newline + encoding to the model's LF content.
            _atomic_write_bytes(target, _encode_fidelity(proposal.new_content, encoding, newline))
            after_sha = _sha256(proposal.new_content)
            kind = "write"

        # Turn-level diff: record the latest content after this apply.
        if self.turn_diff is not None:
            try:
                self.turn_diff.on_after_edit(proposal.path, "" if proposal.delete else proposal.new_content)
            except Exception:  # noqa: BLE001
                pass

        edit = AppliedEdit(
            path=proposal.path, kind=kind, existed_before=existed,
            before_sha=_sha256(before) if existed else "",
            after_sha=after_sha, checkpoint_id=checkpoint_id,
        )
        self.applied.append(edit)
        self._persist_ledger()
        return edit

    # ---- durable ledger -------------------------------------------------
    # The in-memory ``applied`` list dies with the run, but ``/undo`` and
    # ``/rollback`` need to act on a finished task. We mirror ``applied`` to a
    # ledger.json next to the checkpoints so a fresh process can reconstruct an
    # applier and restore exactly (after_sha is needed for the dirty check, and
    # meta.txt alone does not carry it).
    @property
    def _ledger_path(self) -> Path:
        return self.checkpoints_root / "ledger.json"

    def _persist_ledger(self) -> None:
        try:
            self.checkpoints_root.mkdir(parents=True, exist_ok=True)
            self._ledger_path.write_text(
                json.dumps([e.to_dict() for e in self.applied], indent=2),
                encoding="utf-8",
            )
        except OSError:  # ledger is a convenience; never fail an apply over it
            pass

    @classmethod
    def from_checkpoints(cls, workspace_root, checkpoints_root) -> "EditApplier":
        """Rebuild an applier from a finished task's ledger (for /undo, /rollback)."""
        applier = cls(workspace_root=Path(workspace_root),
                      checkpoints_root=Path(checkpoints_root))
        try:
            raw = applier._ledger_path.read_text(encoding="utf-8")
            applier.applied = [AppliedEdit.from_dict(d) for d in json.loads(raw)]
        except (OSError, ValueError, KeyError):
            applier.applied = []
        return applier

    def undo_last(self, *, allow_dirty: bool = False) -> AppliedEdit | None:
        """Restore the most-recent applied edit and drop it from the ledger.

        Returns the edit that was undone, or None if there is nothing to undo.
        Raises EditError on a dirty file (unless allow_dirty) -- the caller
        surfaces that so we never silently destroy newer work.
        """
        if not self.applied:
            return None
        edit = self.applied[-1]
        self.restore_checkpoint(edit, allow_dirty=allow_dirty)
        self.applied.pop()
        self._persist_ledger()
        return edit

    def rollback_to(self, checkpoint_id: str, *, allow_dirty: bool = False) -> list[AppliedEdit]:
        """Undo edits from newest down to AND INCLUDING checkpoint_id.

        Returns the state to just before checkpoint_id was applied. Raises
        EditError if the id is not in the ledger.
        """
        ids = [e.checkpoint_id for e in self.applied]
        if checkpoint_id not in ids:
            raise EditError(f"no such checkpoint in this task: {checkpoint_id}")
        undone: list[AppliedEdit] = []
        # reverse order, stop once we have restored the target checkpoint
        while self.applied and self.applied[-1].checkpoint_id != checkpoint_id:
            undone.append(self.undo_last(allow_dirty=allow_dirty))  # type: ignore[arg-type]
        if self.applied:  # the target itself
            undone.append(self.undo_last(allow_dirty=allow_dirty))  # type: ignore[arg-type]
        return undone

    def restore_checkpoint(self, edit: AppliedEdit, *, allow_dirty: bool = False) -> None:
        """Restore a file to its pre-apply state, EXACTLY.

        Dirty-file protection: if the file's current content differs from what we
        wrote at apply time (someone edited it afterwards), refuse unless
        allow_dirty=True -- we will not silently destroy newer work.
        """
        target = _resolve_in_workspace(edit.path, self.workspace_root)
        cp_dir = self.checkpoints_root / edit.checkpoint_id
        if not cp_dir.is_dir():
            raise EditError(f"checkpoint missing: {edit.checkpoint_id}")

        # Dirty check: current state must match what apply() produced. Compare on
        # LF-normalized text (matching after_sha, which is a sha of the model's LF
        # content) so a preserved-CRLF file isn't falsely flagged dirty, and a
        # UTF-16/odd-encoding file can't crash the check.
        current = _read_text_fidelity(target)[0] if target.is_file() else None
        current_sha = _sha256(current) if current is not None else ""
        expected_sha = edit.after_sha if edit.kind == "write" else ""
        if not allow_dirty and current_sha != expected_sha:
            raise EditError(
                f"refusing to roll back {edit.path}: it changed after apply "
                f"(expected sha {expected_sha[:8] or '(absent)'}, found {current_sha[:8] or '(absent)'}). "
                f"Use allow_dirty=True to override."
            )

        # Restore exactly: the snapshot holds the prior RAW bytes -> byte-perfect,
        # keeping the original line-endings/encoding. Atomic so an interrupted
        # rollback never truncates the file.
        if edit.existed_before:
            raw_before = (cp_dir / "before.snapshot").read_bytes()
            _atomic_write_bytes(target, raw_before)
        else:
            # File did not exist before -> remove it to restore the prior state.
            if target.is_file():
                target.unlink()

    def rollback_all(self, *, allow_dirty: bool = False) -> int:
        """Roll back applied edits in reverse order. Returns count restored.

        On success the ledger is emptied so a re-run does not re-undo.
        """
        n = 0
        for edit in reversed(self.applied):
            self.restore_checkpoint(edit, allow_dirty=allow_dirty)
            n += 1
        self.applied = []
        self._persist_ledger()
        return n
