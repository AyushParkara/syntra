"""Bounded repo context packing (Phase 2).

Legacy died partly from dumping whole files into context (token bleed, P2). This
module packs ONLY what a step needs, within an explicit char budget, with:

- workspace confinement   : never read outside the workspace root.
- excluded dirs           : .git/node_modules/.venv/.syntra/reference/repos/... skipped.
- line-range bounding     : no full-file dumps; cap lines + chars per file.
- char budget             : the whole pack never exceeds the budget (hard cap).
- provenance + hash       : every chunk carries its source path + sha256 of the
                            full file, so the model's context is auditable.
- redaction               : obvious secrets are masked before packing (no keys
                            leak into prompts/logs, PLAN Section 6 #10).

Pure + deterministic given a filesystem snapshot -> unit-tested. Persistence
(context_packs/<step>.json) is done by the caller via TaskStore.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing


# Directories never read into context. Lowercased names, matched per path part.
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".syntra", ".idea", ".vscode", "site-packages",
    "reference",  # our in-repo reference clones (req A8)
}

# Extension -> language label (for fenced code hints; not exhaustive).
LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".sql": "sql",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
}

# Secret redaction is centralized in core/redact.py (reused here so file content
# packed into prompts never leaks keys). Kept as a thin re-export for callers.
from .redact import redact


def language_for(path: str | Path) -> str:
    return LANGUAGE_MAP.get(Path(path).suffix.lower(), "")


def is_excluded(rel_path: str | Path) -> bool:
    """True if any path component is an excluded dir."""
    parts = Path(rel_path).parts
    return any(p.lower() in EXCLUDED_DIRS for p in parts)


@dataclass
class FileChunk:
    path: str            # relative to workspace_root (provenance)
    language: str
    start_line: int      # 1-indexed inclusive
    end_line: int
    content: str         # redacted, possibly line/char bounded
    sha256: str          # hash of the FULL original file (audit)
    char_count: int
    truncated: bool      # the file was longer than the per-file cap

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "sha256": self.sha256,
            "char_count": self.char_count,
            "truncated": self.truncated,
            # content intentionally stored too so the pack is replayable.
            "content": self.content,
        }


@dataclass
class ContextPack:
    char_budget: int
    chunks: list[FileChunk] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {path, reason}
    budget_hit: bool = False

    @property
    def total_chars(self) -> int:
        return sum(c.char_count for c in self.chunks)

    @property
    def est_tokens(self) -> int:
        return pricing.estimate_tokens("".join(c.content for c in self.chunks))

    def to_dict(self) -> dict:
        return {
            "_schema_version": 1,
            "char_budget": self.char_budget,
            "total_chars": self.total_chars,
            "est_tokens": self.est_tokens,
            "budget_hit": self.budget_hit,
            "chunks": [c.to_dict() for c in self.chunks],
            "skipped": list(self.skipped),
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def pack_paths(
    paths,
    *,
    workspace_root: str | Path,
    char_budget: int = 12_000,
    max_lines_per_file: int = 400,
    max_chars_per_file: int = 6_000,
) -> ContextPack:
    """Pack the given files into a bounded, audited ContextPack.

    Confined to ``workspace_root``; excluded dirs, missing files, binaries, and
    paths escaping the root are recorded in ``skipped`` (never silently dropped).
    The total never exceeds ``char_budget`` (hard cap). No full-file dumps:
    each file is bounded to max_lines_per_file / max_chars_per_file.
    """
    root = Path(workspace_root).resolve()
    pack = ContextPack(char_budget=char_budget)

    for raw in paths:
        p = Path(raw)
        abs_p = (p if p.is_absolute() else root / p).resolve()

        # Confinement: never escape the workspace root.
        if not abs_p.is_relative_to(root):
            pack.skipped.append({"path": str(raw), "reason": "outside workspace"})
            continue
        rel = str(abs_p.relative_to(root))
        if is_excluded(rel):
            pack.skipped.append({"path": rel, "reason": "excluded dir"})
            continue
        if not abs_p.is_file():
            pack.skipped.append({"path": rel, "reason": "not a file"})
            continue

        try:
            raw_text = abs_p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            pack.skipped.append({"path": rel, "reason": "unreadable/binary"})
            continue

        full_hash = _sha256(raw_text)
        text = redact(raw_text)

        # Line bound (no full-file dump).
        lines = text.splitlines()
        file_truncated = False
        if len(lines) > max_lines_per_file:
            lines = lines[:max_lines_per_file]
            file_truncated = True
        content = "\n".join(lines)

        # Char bound per file.
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file]
            file_truncated = True

        # Global budget: stop before exceeding it (hard cap).
        remaining = char_budget - pack.total_chars
        if remaining <= 0:
            pack.budget_hit = True
            pack.skipped.append({"path": rel, "reason": "budget exhausted"})
            continue
        if len(content) > remaining:
            content = content[:remaining]
            file_truncated = True
            pack.budget_hit = True

        pack.chunks.append(FileChunk(
            path=rel,
            language=language_for(abs_p),
            start_line=1,
            end_line=min(len(content.splitlines()), max_lines_per_file),
            content=content,
            sha256=full_hash,
            char_count=len(content),
            truncated=file_truncated,
        ))

    return pack
