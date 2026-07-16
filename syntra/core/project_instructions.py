"""AGENTS.md / CLAUDE.md hierarchical project-instruction loader.

The open standard (agents.md): a Markdown file giving agents build/test commands,
conventions, and constraints. Discovery walks from the project root DOWN to the
working dir, collecting the nearest instruction file at each level so deep-package
rules override repo-root rules (most-specific last). Output is injected into the
role prompts so planner/executor/reviewer all honor project conventions.

Security (fits the operator/scope rails): every file is scanned for
prompt-injection / role-hijack markers BEFORE injection, and a dirty file is skipped
(noted, not silently used). Pure + deterministic; fully unit-testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# Preferred order at each directory level — first match wins for that level.
_INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")
# A directory is the project root if it holds one of these markers.
_ROOT_MARKERS = (".git", ".hg", ".svn", "pyproject.toml", "package.json", "go.mod", ".syntra")
_MAX_BYTES = 32 * 1024  # the agents.md standard's size guidance

# Prompt-injection / role-hijack markers. A weak-but-useful guard for UNTRUSTED
# project files (a repo you didn't write could carry a hostile AGENTS.md).
_INJECTION_MARKERS = (
    "ignore previous instructions", "ignore all previous", "ignore the above",
    "disregard the above", "disregard previous", "you are now", "new system prompt",
    "system prompt:", "exfiltrate", "send your api", "send the api key", "reveal your",
    "print your system prompt", "override your", "forget your instructions",
)


def find_project_root(start: str | Path) -> Path:
    """Nearest ancestor (incl. start) holding a root marker; else the start dir."""
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        if any((d / m).exists() for m in _ROOT_MARKERS):
            return d
    return p


def generate_agents_md(root: str | Path) -> str:
    """Scaffold an AGENTS.md for a project root — detects the language mix + a likely
    test command + top-level layout, and lays out the standard sections for the user to
    refine. Pure: returns the markdown text (the open agents.md standard)."""
    root = Path(root)
    name = root.name or "project"
    _ext_lang = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".go": "Go",
                 ".rs": "Rust", ".java": "Java", ".rb": "Ruby", ".php": "PHP",
                 ".c": "C", ".cpp": "C++", ".sh": "Shell", ".kt": "Kotlin"}
    _skip = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build",
             "__pycache__", ".mypy_cache", ".ruff_cache"}
    counts: dict[str, int] = {}
    try:
        for p in root.rglob("*"):
            if any(part in _skip for part in p.parts):
                continue
            if p.is_file() and p.suffix in _ext_lang:
                counts[_ext_lang[p.suffix]] = counts.get(_ext_lang[p.suffix], 0) + 1
    except Exception:  # noqa: BLE001
        pass
    langs = ", ".join(k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]) or "—"
    if (root / "pyproject.toml").exists() or (root / "tests").is_dir() or (root / "setup.py").exists():
        test_cmd = "python3 -m pytest -q   # or: python3 -m unittest"
    elif (root / "package.json").exists():
        test_cmd = "npm test"
    elif (root / "go.mod").exists():
        test_cmd = "go test ./..."
    elif (root / "Cargo.toml").exists():
        test_cmd = "cargo test"
    else:
        test_cmd = "<your test command>"
    try:
        dirs = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))[:8]
    except Exception:  # noqa: BLE001
        dirs = []
    struct = "\n".join(f"- `{d}/`" for d in dirs) or "- (add the key directories here)"
    return (
        f"# {name} — agent guide\n\n"
        "> Project instructions for AI coding agents (the open AGENTS.md standard). This\n"
        "> was auto-scaffolded — refine the TODOs to match your project.\n\n"
        f"## Overview\n{name} — primary language(s): {langs}.\n\n"
        f"## Build & test\n```bash\n{test_cmd}\n```\nTODO: add build / lint / run commands.\n\n"
        f"## Layout\n{struct}\n\n"
        "## Conventions\n- TODO: code style, naming, and patterns to follow.\n"
        "- TODO: things NOT to do (e.g. don't add dependencies without asking).\n\n"
        "## Constraints\n- TODO: anything the agent must always honor (security, scope,\n"
        "  files to never touch).\n"
    )


def write_agents_md(root: str | Path, *, force: bool = False) -> tuple[bool, str]:
    """Write a scaffolded AGENTS.md at ``root`` (refuses to overwrite unless ``force``).
    Returns (wrote, message)."""
    root = Path(root)
    dest = root / "AGENTS.md"
    if dest.exists() and not force:
        return False, f"AGENTS.md already exists at {dest} — use --force to overwrite"
    try:
        dest.write_text(generate_agents_md(root), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return False, f"could not write {dest}: {e}"
    return True, f"wrote {dest}"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def looks_injected(text: str) -> bool:
    # Normalize whitespace + a few leetspeak digit swaps so "ignore   previous" and
    # "ign0re previous" still match the markers. A weak prior, not a full defense.
    import re
    low = re.sub(r"\s+", " ", (text or "").lower())
    low = low.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a"}))
    return any(m in low for m in _INJECTION_MARKERS)


@dataclass
class Rule:
    text: str
    origin_path: str
    origin_line: int
    sha256: str
    overridden_by: str = ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def compile_rules(cwd: str | Path, *, scan: bool = True) -> list[Rule]:
    """Walk root→cwd, compile instruction files into typed sha256-provenanced rules."""
    cwd = Path(cwd).resolve()
    root = find_project_root(cwd)
    chain: list[Path] = []
    d = cwd
    while True:
        chain.append(d)
        if d == root or d.parent == d:
            break
        d = d.parent
    chain.reverse()

    rules: list[Rule] = []
    for d in chain:
        for name in _INSTRUCTION_FILENAMES:
            f = d / name
            if not (f.exists() and f.is_file()):
                continue
            try:
                raw = f.read_text(errors="replace")[:_MAX_BYTES]
            except Exception:  # noqa: BLE001
                break
            txt = _strip_frontmatter(raw).strip()
            if not txt:
                break
            if scan and looks_injected(txt):
                break
            rules.append(Rule(text=txt, origin_path=str(f), origin_line=1,
                              sha256=_sha256_text(txt)))
            break
    return rules


def compiled_rules(cwd: str | Path, *, scan: bool = True) -> list[Rule]:
    """Typed compiled rules for the memory tier."""
    return compile_rules(cwd, scan=scan)


def _render_rules(rules: list[Rule]) -> str:
    if not rules:
        return ""
    by_file: dict[str, list[Rule]] = {}
    for r in rules:
        by_file.setdefault(r.origin_path, []).append(r)
    parts = []
    for path, rs in by_file.items():
        name = Path(path).name
        dir_part = str(Path(path).parent)
        body = "\n".join(r.text for r in rs)
        parts.append(f"## project instructions — {name} ({dir_part})\n{body}")
    text = "\n\n".join(parts)
    if len(text) > _MAX_BYTES:
        text = text[:_MAX_BYTES].rstrip() + "\n…[truncated]"
    return text


def load_project_instructions(cwd: str | Path, *, scan: bool = True) -> tuple[str, list[str]]:
    """Walk root→cwd, collect the nearest instruction file per level, concat
    most-specific-LAST, strip frontmatter, size-cap. Returns (text, notes) where
    notes records each file used or skipped. Best-effort: unreadable files are
    skipped, never raised."""
    cwd = Path(cwd).resolve()
    root = find_project_root(cwd)
    # build the chain root → … → cwd
    chain: list[Path] = []
    d = cwd
    while True:
        chain.append(d)
        if d == root or d.parent == d:
            break
        d = d.parent
    chain.reverse()  # root first, cwd last (most specific)

    parts: list[str] = []
    notes: list[str] = []
    for d in chain:
        for name in _INSTRUCTION_FILENAMES:
            f = d / name
            if not (f.exists() and f.is_file()):
                continue
            try:
                raw = f.read_text(errors="replace")[:_MAX_BYTES]
            except Exception:  # noqa: BLE001
                break
            txt = _strip_frontmatter(raw).strip()
            if not txt:
                break
            if scan and looks_injected(txt):
                notes.append(f"{f} SKIPPED (possible prompt-injection)")
                break
            parts.append(f"## project instructions — {name} ({d})\n{txt}")
            notes.append(str(f))
            break  # one file per directory level
    text = "\n\n".join(parts)
    if len(text) > _MAX_BYTES:
        text = text[:_MAX_BYTES].rstrip() + "\n…[truncated]"
    return text, notes


def project_clause(cwd: str | Path) -> str:
    """A ready-to-append system-prompt clause, or '' when there are no project
    instructions. Wrapped so the model knows these are repo conventions to honor."""
    text = _render_rules(compile_rules(cwd))
    if not text:
        return ""
    return ("\n\nPROJECT INSTRUCTIONS (honor these repo conventions — build/test "
            "commands, style, constraints):\n" + text)
