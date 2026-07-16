"""Input chips: paste-as-chip + file-ref chip (Track T1, req F6/F7).

When a user pastes a big blob or attaches a file, the input box should show a
compact CHIP (e.g. `[paste: 42 lines, 1.2k chars]` or `[main.py]`) instead of a
raw dump — but the FULL content is still what gets sent. This module is the pure,
testable model of that; the curses layer renders chips and substitutes content
on send.

- paste_chip(text)  -> chip whose label summarizes size, content = the full paste (F6)
- file_chip(path)   -> chip labelled with the filename, content = the full path (F7)
- a composed input is a list of `str | Chip`; display() shows labels, expand()
  substitutes full content.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Bracketed-paste terminal markers (xterm). The curses layer enables bracketed
# paste; everything between these is one paste event.
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

# Paste becomes a chip only when LARGE (>10 lines OR
# >1000 chars); smaller pastes are inserted inline.
PASTE_CHIP_MIN_LINES = 10
PASTE_CHIP_MIN_CHARS = 1000


@dataclass(frozen=True)
class Chip:
    kind: str        # "paste" | "file"
    label: str       # compact display token, e.g. "[paste: +42 lines]"
    content: str     # the full text substituted on send

    def __str__(self) -> str:
        return self.label


def _human(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def filter_printable(text: str) -> str:
    """Drop non-printable control bytes except newline."""
    return "".join(c for c in (text or "") if c == "\n" or ord(c) >= 32)


def paste_chip(text: str) -> "str | Chip":
    """A LARGE paste becomes a chip (>10 lines or >1000 chars); else inline (F6).

    Applies the size thresholds + control-char filtering so the editor never gets a
    raw blob dumped in. Content is filtered to printable + newlines.
    """
    text = filter_printable(text or "")
    lines = text.count("\n") + 1
    chars = len(text)
    if lines <= PASTE_CHIP_MIN_LINES and chars <= PASTE_CHIP_MIN_CHARS:
        return text  # small/medium paste: inline it
    label = (f"[paste: +{lines} lines]" if lines > PASTE_CHIP_MIN_LINES
             else f"[paste: {_human(chars)} chars]")
    return Chip(kind="paste", label=label, content=text)


def file_chip(path: str | Path) -> Chip:
    """A file reference: display the filename, but send the full path (F7)."""
    p = Path(path)
    return Chip(kind="file", label=f"[{p.name}]", content=str(p))


def strip_bracketed(seq: str) -> str:
    """Extract the paste payload from a bracketed-paste sequence (pure)."""
    s = seq
    if BRACKETED_PASTE_START in s:
        s = s.split(BRACKETED_PASTE_START, 1)[1]
    if BRACKETED_PASTE_END in s:
        s = s.split(BRACKETED_PASTE_END, 1)[0]
    return s


def display_parts(parts) -> str:
    """What the input box shows: chip labels, text verbatim."""
    return "".join(p.label if isinstance(p, Chip) else str(p) for p in parts)


def expand_parts(parts) -> str:
    """What actually gets sent: chip full content, text verbatim."""
    return "".join(p.content if isinstance(p, Chip) else str(p) for p in parts)
