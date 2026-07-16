"""Minimal, dependency-free syntax highlighting for terminal code fences.

Not a full lexer — a pragmatic highlighter that classifies the spans of a line
into roles (keyword / string / comment / number / text) for the common languages
an assistant emits (python, js/ts, go, rust, bash, json). The curses layer maps
each role to a theme color. Pure + deterministic -> unit-tested.

We return a list of (text, role) spans per line so the caller can paint them;
no ANSI is embedded here (the TUI owns color attributes).
"""

from __future__ import annotations

import re

# A broad keyword set across the common languages (over-inclusive is fine for
# highlighting — a word that isn't a keyword in one language just won't appear).
_KEYWORDS = {
    "def", "class", "return", "if", "elif", "else", "for", "while", "in", "not",
    "and", "or", "import", "from", "as", "with", "try", "except", "finally",
    "raise", "lambda", "yield", "pass", "break", "continue", "global", "nonlocal",
    "True", "False", "None", "async", "await", "is", "del", "assert",
    "function", "const", "let", "var", "new", "typeof", "instanceof", "export",
    "default", "extends", "implements", "interface", "type", "enum", "public",
    "private", "protected", "static", "void", "this", "super", "throw", "catch",
    "func", "package", "go", "defer", "chan", "map", "struct", "range", "fallthrough",
    "fn", "let", "mut", "impl", "trait", "pub", "use", "mod", "match", "loop", "where",
    "echo", "then", "fi", "do", "done", "case", "esac", "local", "export",
}

_TOKEN = re.compile(r"""
    (?P<comment>(\#[^\n]*|//[^\n]*))         |
    (?P<string>("([^"\\]|\\.)*"|'([^'\\]|\\.)*'|`([^`\\]|\\.)*`)) |
    (?P<number>\b\d+(\.\d+)?\b)              |
    (?P<word>[A-Za-z_]\w*)                   |
    (?P<other>.)
""", re.VERBOSE)


def highlight_line(line: str) -> list[tuple[str, str]]:
    """Classify a code line into (text, role) spans. role in
    {keyword, string, comment, number, code}. Coalesces adjacent 'code' spans."""
    spans: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(line):
        if m.lastgroup == "comment":
            role = "comment"
        elif m.lastgroup == "string":
            role = "string"
        elif m.lastgroup == "number":
            role = "number"
        elif m.lastgroup == "word":
            role = "keyword" if m.group() in _KEYWORDS else "code"
        else:
            role = "code"
        text = m.group()
        if spans and spans[-1][1] == role == "code":
            spans[-1] = (spans[-1][0] + text, "code")
        else:
            spans.append((text, role))
    return spans
