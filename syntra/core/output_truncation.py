"""Bound large tool / command output before it goes back to the model.

Big tool output (a 50k-line log, a huge `git diff`, a `cat` of a vendored file)
is the classic way a context window explodes: one tool call balloons the
transcript and every subsequent turn re-pays for it. Syntra bounds it with a
middle-truncation policy — keep the HEAD and the TAIL (where the signal usually
is), drop the middle, and tell the model exactly how much was elided so it can
ask for more if it needs it.

The policy:

- `truncate_output(text, ...)` -> a bounded string with head+tail bookends and a
  single `… N lines / M chars omitted …` marker in the middle.
- Dual budget: a hard **char** ceiling AND a **line** ceiling; whichever bites
  first wins, so a few enormous lines and a million tiny ones are both handled.
- **UTF-8 safe**: we slice on `str` (code points), never on raw bytes, so a
  multibyte character is never cut in half.
- Idempotent-ish and cheap: text already under budget is returned unchanged
  (same object), so the overwhelmingly common small-output path costs nothing.

Pure string logic — unit-tested, no I/O.
"""

from __future__ import annotations

# Defaults tuned for a coding agent: ~12k chars ≈ a few thousand tokens, plenty
# to see a stack trace + the tail of a build, without swamping the window.
DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_LINES = 400
# When we DO truncate, how to split the surviving budget between the head and
# the tail. The tail (final error line, last test result, exit status) is the
# most load-bearing part of command output, so it gets the larger share.
_HEAD_FRACTION = 0.45


def _elision(lines_omitted: int, chars_omitted: int) -> str:
    """The single marker line we drop in place of the elided middle.

    #206: the marker is ACTIONABLE — it doesn't just say "bytes dropped", it tells
    the model how to retrieve the elided middle (re-read with an `offset`/`limit`
    window, or run a narrower command/query). Without this the model sees "…
    omitted …" and stops, unaware it can ask for the rest — the honest-truncation
    contract requires a next step, not just a count."""
    bits = []
    if lines_omitted > 0:
        bits.append(f"{lines_omitted} line{'s' if lines_omitted != 1 else ''}")
    if chars_omitted > 0:
        bits.append(f"{chars_omitted} char{'s' if chars_omitted != 1 else ''}")
    inner = " / ".join(bits) if bits else "output"
    return (f"… [{inner} omitted — middle truncated; re-read with an offset/limit "
            f"window or run a narrower command to see the rest] …")


def truncate_output(text: str, *, max_chars: int = DEFAULT_MAX_CHARS,
                    max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Bound `text` to (max_chars, max_lines) by middle-truncation.

    Returns `text` unchanged when it already fits both budgets. Otherwise keeps a
    head slice + a tail slice (joined by an elision marker) so the model still
    sees the start (what ran) and the end (how it ended), and is told how much
    was dropped. Operates on code points, never bytes, so it can't split a
    multibyte character.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    max_chars = max(0, int(max_chars))
    max_lines = max(0, int(max_lines))

    lines = text.split("\n")
    over_chars = max_chars and len(text) > max_chars
    over_lines = max_lines and len(lines) > max_lines
    if not over_chars and not over_lines:
        return text

    # --- line budget first: keep head/tail lines, elide the middle block ------
    if over_lines:
        head_n = max(1, int(max_lines * _HEAD_FRACTION))
        tail_n = max(1, max_lines - head_n)
        # guarantee head_n + tail_n < total so there's always something to omit
        if head_n + tail_n >= len(lines):
            tail_n = max(1, len(lines) - head_n - 1)
        head_lines = lines[:head_n]
        tail_lines = lines[len(lines) - tail_n:]
        omitted_lines = len(lines) - head_n - tail_n
        body = "\n".join(head_lines) + "\n" + _elision(omitted_lines, 0) + "\n" + "\n".join(tail_lines)
        # the line-trimmed body may still be over the char budget -> fall through
        text = body
        lines = text.split("\n")
        if not (max_chars and len(text) > max_chars):
            return text

    # --- char budget: middle-truncate the (possibly line-trimmed) text --------
    if max_chars and len(text) > max_chars:
        # Reserve exactly enough room for the marker + its two bracketing newlines,
        # derived from the marker's own worst-case width (the real omitted-char
        # count is <= len(text), so this upper-bounds the marker). Self-adjusting:
        # editing the marker text can't silently push the result past max_chars.
        marker_reserve = len(_elision(0, len(text))) + 2
        budget = max(0, max_chars - marker_reserve)
        head_len = max(1, int(budget * _HEAD_FRACTION))
        tail_len = max(1, budget - head_len)
        if head_len + tail_len >= len(text):
            tail_len = max(1, len(text) - head_len - 1)
        head = text[:head_len]
        tail = text[len(text) - tail_len:]
        chars_omitted = len(text) - head_len - tail_len
        return head + "\n" + _elision(0, chars_omitted) + "\n" + tail

    return text


def truncate_items(items: list[str], *, max_chars: int = DEFAULT_MAX_CHARS,
                   max_lines: int = DEFAULT_MAX_LINES) -> tuple[list[str], int]:
    """Bound a list of text segments to a shared budget (multi-item path).

    Walks items in order, spending the char budget; once it's exhausted, further
    text items are dropped. The first item that overflows is itself truncated to
    the remaining budget. Returns `(kept_items, omitted_count)` where
    `omitted_count` is how many whole text items were dropped. Useful when a tool
    returns several chunks (e.g. multiple file reads) and we want one coherent cap.
    """
    kept: list[str] = []
    omitted = 0
    remaining = max(0, int(max_chars))
    for it in items:
        s = it if isinstance(it, str) else str(it)
        if remaining <= 0:
            omitted += 1
            continue
        if len(s) <= remaining:
            kept.append(s)
            remaining -= len(s)
        else:
            snippet = truncate_output(s, max_chars=remaining, max_lines=max_lines)
            if snippet.strip():
                kept.append(snippet)
            else:
                omitted += 1
            remaining = 0
    return kept, omitted
