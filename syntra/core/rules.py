"""User-set GLOBAL rules that Syntra keeps and enforces.

Global rules live in one file (``<state>/rules.md``), apply to EVERY session, and
are injected as an INVIOLABLE preamble into every role prompt (via Memory.rules) so
no model or agent silently runs without them. They layer UNDER the user's explicit
intent only in the sense that a request which conflicts with a rule must be refused,
not obeyed -- the rules win.

This module owns just the durable GLOBAL store + parsing. Project-scoped rules
(``.syntra/rules.md``, ``AGENTS.md``) are loaded separately by
project_instructions.py and merged on top for the current workspace.

A rule is one line. The file is plain markdown so the user can hand-edit it; we
parse out bullet markers, blank lines, and ``#`` headings. Each rule is run through
the same prompt-injection guard as durable memory, since rules are injected into
every prompt -- a rule that itself says "ignore all rules" is rejected.
"""

from __future__ import annotations

from pathlib import Path

from .memory import looks_like_injection

_HEADER = "# Syntra global rules — inviolable, injected into every run.\n"


def global_rules_path(state_root) -> Path:
    return Path(state_root) / "rules.md"


def _parse(text: str) -> list[str]:
    """Pull individual rule lines out of a markdown rules file."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # strip a leading bullet / ordinal marker
        for pfx in ("- ", "* ", "+ "):
            if line.startswith(pfx):
                line = line[len(pfx):].strip()
                break
        else:
            # numbered list "1. ", "2) "
            i = 0
            while i < len(line) and line[i].isdigit():
                i += 1
            if i and i < len(line) and line[i] in ").":
                line = line[i + 1:].strip()
        if not line or looks_like_injection(line):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def load_global_rules(state_root) -> list[str]:
    """Every global rule the user has set (empty list if none / unreadable)."""
    try:
        return _parse(global_rules_path(state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _write(state_root, rules: list[str]) -> None:
    p = global_rules_path(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _HEADER + "".join(f"- {r}\n" for r in rules)
    p.write_text(body, encoding="utf-8")


def add_global_rule(state_root, rule) -> bool:
    """Add one rule. Returns False if blank, duplicate, or rejected by the guard."""
    rule = str(rule).strip()
    if not rule or looks_like_injection(rule):
        return False
    rules = load_global_rules(state_root)
    if rule in rules:
        return False
    rules.append(rule)
    _write(state_root, rules)
    return True


def remove_global_rule(state_root, needle) -> int:
    """Remove rules matching `needle` (exact or substring). Returns count removed."""
    needle = str(needle).strip()
    if not needle:
        return 0
    rules = load_global_rules(state_root)
    kept = [r for r in rules if r != needle and needle not in r]
    removed = len(rules) - len(kept)
    if removed:
        _write(state_root, kept)
    return removed
