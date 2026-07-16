"""Workspace repo map — fast symbol/structure index for code understanding.

Scans the workspace to build a lightweight map of files, their symbols (classes,
functions, imports), and structure. Used by the planner/executor to understand
what code exists and where, without reading every file.

Inspired by legacy/syntra-v1/orchestrator/repo_map.py but simplified and
adapted for Syntra's architecture.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


SKIP_DIRS = {".git", "__pycache__", "node_modules", ".syntra", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", "dist", "build", ".egg-info",
             ".tox", ".nox", "reference"}

SKIP_EXTS = {".pyc", ".pyo", ".so", ".o", ".a", ".lib", ".dll",
             ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
             ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4"}

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".rs": "rust", ".go": "go",
    ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".md": "markdown",
    ".html": "html", ".css": "css", ".scss": "css",
}

# Regex patterns for symbol extraction per language.
# NOTE: allow LEADING INDENTATION so CLASS METHODS are captured too — a line-start-anchored
# pattern only ever saw top-level defs/classes, making every method (e.g. `find_symbol` inside a
# class) invisible to symbol lookup. `^\s*` fixes that so named-symbol search is actually complete.
_PY_SYMBOLS = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)", re.MULTILINE)
_PY_IMPORTS = re.compile(r"^(?:from\s+\S+\s+)?import\s+(\S+)", re.MULTILINE)
_JS_SYMBOLS = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:function|class|const|let)\s+(\w+)", re.MULTILINE)
_RS_SYMBOLS = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+(\w+)", re.MULTILINE)


@dataclass(frozen=True)
class FileEntry:
    path: str
    language: str
    size: int
    symbols: tuple[str, ...]
    imports: tuple[str, ...]


@dataclass(frozen=True)
class SymbolHit:
    """One (file, symbol) match from a named-symbol lookup — where a symbol is DEFINED."""
    path: str
    language: str
    symbol: str


@dataclass
class RepoMap:
    root: str
    entries: list[FileEntry] = field(default_factory=list)
    file_count: int = 0
    dir_count: int = 0
    total_bytes: int = 0

    def summary(self, max_entries: int = 50) -> str:
        lines = [f"repo: {os.path.basename(self.root)} ({self.file_count} files, {self.dir_count} dirs)"]
        for e in self.entries[:max_entries]:
            syms = ", ".join(e.symbols[:5])
            if len(e.symbols) > 5:
                syms += f" +{len(e.symbols) - 5}"
            lines.append(f"  {e.path} ({e.language}) [{syms}]")
        if len(self.entries) > max_entries:
            lines.append(f"  ... +{len(self.entries) - max_entries} more files")
        return "\n".join(lines)

    def find_symbol(self, name: str) -> list[FileEntry]:
        return [e for e in self.entries if name in e.symbols]

    def find_symbols(self, query: str, *, partial: bool = True) -> list["SymbolHit"]:
        """Named-symbol lookup (the thing plain text-grep can't do semantically): find where a
        class/function/etc. is DEFINED. Matches a symbol EXACTLY, and — when `partial` —
        case-insensitively by substring, so `log` finds `login`/`logout`. Returns one SymbolHit
        per (file, symbol) match, so the same symbol defined in two files shows both. Pure."""
        q = (query or "").strip()
        if not q:
            return []
        ql = q.lower()
        hits: list[SymbolHit] = [
            SymbolHit(path=e.path, language=e.language, symbol=sym)
            for e in self.entries for sym in e.symbols
            if sym == q or (partial and ql in sym.lower())
        ]
        # Stable, useful order: exact matches first, then by path, then symbol.
        hits.sort(key=lambda h: (h.symbol.lower() != ql, h.path, h.symbol))
        return hits

    def files_for_language(self, lang: str) -> list[FileEntry]:
        return [e for e in self.entries if e.language == lang]


def build_repo_map(root: str, max_files: int = 500, max_file_bytes: int = 512_000) -> RepoMap:
    root = os.path.abspath(root)
    entries: list[FileEntry] = []
    file_count = 0
    dir_count = 0
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        dir_count += 1

        for fname in sorted(filenames):
            if file_count >= max_files:
                break
            ext = os.path.splitext(fname)[1].lower()
            if ext in SKIP_EXTS:
                continue

            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root)
            lang = LANG_BY_EXT.get(ext, "")

            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue

            total_bytes += size
            file_count += 1

            symbols: list[str] = []
            imports: list[str] = []

            if lang and size <= max_file_bytes:
                try:
                    with open(fpath, errors="replace") as _fh:
                        content = _fh.read(max_file_bytes)
                    if lang == "python":
                        symbols = _PY_SYMBOLS.findall(content)
                        imports = _PY_IMPORTS.findall(content)
                    elif lang in ("javascript", "typescript"):
                        symbols = _JS_SYMBOLS.findall(content)
                    elif lang == "rust":
                        symbols = _RS_SYMBOLS.findall(content)
                except (OSError, UnicodeDecodeError):
                    pass

            entries.append(FileEntry(
                path=rel,
                language=lang or ext.lstrip(".") or "unknown",
                size=size,
                # Keep enough symbols that lookup is COMPLETE for real files (methods included);
                # summary() independently trims to 5/file for display so this doesn't bloat prompts.
                symbols=tuple(symbols[:200]),
                imports=tuple(imports[:10]),
            ))

    entries.sort(key=lambda e: (-len(e.symbols), e.path))

    return RepoMap(
        root=root,
        entries=entries,
        file_count=file_count,
        dir_count=dir_count,
        total_bytes=total_bytes,
    )
