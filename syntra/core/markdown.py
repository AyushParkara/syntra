"""Minimal markdown -> terminal rendering (Track T1).

Assistant output is markdown; dumping it raw is hard to read. This is a focused,
PURE renderer for the constructs that matter in a coding-agent transcript:

- fenced code blocks (```lang ... ```) -> kept verbatim, marked, never wrapped;
- ATX headings (#, ##, ...) -> uppercased / prefixed;
- ordered + (nested) bullet lists -> normalized markers, indentation preserved;
- blockquotes;
- inline: **bold**, *italic*, `code`, ~~strike~~ markers stripped; [text](url)
  -> "text (url)"; ![alt](img) -> "alt"; left readable in a plain terminal.

Returns styled "lines" (text + a style tag) so the curses layer can colour them.
Pure -> unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RLine:
    text: str
    style: str   # "text" | "h1" | "h2" | "h3" | "code" | "bullet" | "quote"


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*```(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")

_INLINE = [
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),          # image -> alt text
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)"),    # link  -> text (url)
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),                   # bold
    (re.compile(r"__(.+?)__"), r"\1"),                       # bold
    (re.compile(r"~~(.+?)~~"), r"\1"),                       # strikethrough
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),  # italic
    (re.compile(r"`([^`]+)`"), r"\1"),                       # inline code
]


# A small, dependency-free, language-agnostic-ish syntax lexer for code-fence lines.
# Heuristic (a few false positives on identifiers named like keywords are acceptable);
# it emits non-overlapping char-offset spans tagged with theme roles that already exist
# (keyword/string/comment/number), so code reads far better than one flat tint.
_CODE_KEYWORDS = frozenset({
    "def", "return", "if", "elif", "else", "for", "while", "do", "in", "of", "import",
    "from", "as", "class", "try", "except", "catch", "finally", "with", "lambda", "pass",
    "break", "continue", "and", "or", "not", "is", "none", "null", "nil", "true", "false",
    "function", "func", "fn", "var", "let", "const", "new", "delete", "await", "async",
    "yield", "public", "private", "protected", "static", "final", "void", "struct", "enum",
    "interface", "impl", "trait", "use", "pub", "mod", "match", "case", "switch", "default",
    "throw", "throws", "extends", "implements", "package", "namespace", "using", "include",
    "require", "module", "then", "elsif", "begin", "unless", "self", "super", "raise",
    "assert", "global", "nonlocal", "go", "defer", "select", "where", "when",
})
_SLASH_LANGS = frozenset({
    "c", "cpp", "c++", "cc", "h", "hpp", "js", "javascript", "jsx", "ts", "typescript",
    "tsx", "java", "go", "golang", "rust", "rs", "kotlin", "kt", "swift", "php", "scala",
    "dart", "cs", "csharp", "json5", "groovy", "objc",
})
_DASH_LANGS = frozenset({"sql", "lua", "haskell", "hs", "elm", "ada", "vhdl"})
_HASH_LANGS = frozenset({
    "python", "py", "sh", "bash", "zsh", "ruby", "rb", "yaml", "yml", "toml", "r", "perl",
    "pl", "makefile", "make", "dockerfile", "conf", "ini", "elixir", "ex", "nim", "coffee",
})
_NUM_RE = re.compile(r"\b\d[\d_]*\.?\d*\b")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _comment_markers(lang: str) -> tuple:
    """Comment-start tokens valid for a language; unknown lang accepts the common ones."""
    l = (lang or "").strip().lower()
    if l in _SLASH_LANGS:
        return ("//",)
    if l in _DASH_LANGS:
        return ("--",)
    if l in _HASH_LANGS:
        return ("#",)
    return ("#", "//", "--")   # unknown -> accept the usual markers


def highlight_code_spans(code: str, lang: str = "") -> list:
    """Return non-overlapping, sorted ``(start, end, style)`` char-offset spans for one
    line of code, where style ∈ {comment, string, number, keyword}. Strings/comments take
    precedence so keywords/numbers inside them aren't mis-coloured. Pure -> unit-tested."""
    code = code or ""
    n = len(code)
    style: list = [None] * n
    markers = _comment_markers(lang)
    i = 0
    in_str = None
    while i < n:
        c = code[i]
        if in_str is not None:
            style[i] = "string"
            if c == "\\" and i + 1 < n:        # escaped char stays in the string
                style[i + 1] = "string"
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if any(code.startswith(m, i) for m in markers):   # comment -> to end of line
            for j in range(i, n):
                style[j] = "comment"
            break
        if c in ("'", '"', "`"):
            in_str = c
            style[i] = "string"
            i += 1
            continue
        i += 1
    # numbers + keywords only on still-unstyled (None) runs
    for m in _NUM_RE.finditer(code):
        if all(style[k] is None for k in range(m.start(), m.end())):
            for k in range(m.start(), m.end()):
                style[k] = "number"
    for m in _IDENT_RE.finditer(code):
        if m.group(0).lower() in _CODE_KEYWORDS and \
                all(style[k] is None for k in range(m.start(), m.end())):
            for k in range(m.start(), m.end()):
                style[k] = "keyword"
    # coalesce consecutive same-style chars into spans
    spans: list = []
    k = 0
    while k < n:
        if style[k] is None:
            k += 1
            continue
        st = style[k]
        s = k
        while k < n and style[k] == st:
            k += 1
        spans.append((s, k, st))
    return spans


def strip_inline(text: str) -> str:
    """Remove inline emphasis markers, leaving readable plain text."""
    out = text
    for pat, repl in _INLINE:
        out = pat.sub(repl, out)
    return out


def _split_row(line: str) -> list:
    """Split a markdown table row into trimmed cells."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _cell_width(s: str) -> int:
    """Terminal cell width of a table cell (CJK/emoji count as 2). Falls back to
    len() if the width helper isn't importable (keeps markdown standalone-usable)."""
    try:
        from .tui_model import display_width
        return display_width(s)
    except Exception:  # noqa: BLE001
        return len(s)


def _wrap_cell(text: str, width: int) -> list[str]:
    """Word-wrap a cell to `width` display columns, breaking over-long words. Never
    returns an empty list (an empty cell yields one empty line so rows stay aligned)."""
    width = max(1, int(width))
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for w in words:
        # a single word wider than the column: hard-split it across lines.
        while _cell_width(w) > width:
            head, w = _fit_prefix(w, width)
            if cur:
                lines.append(cur); cur = ""
            lines.append(head)
        cand = w if not cur else cur + " " + w
        if _cell_width(cand) <= width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit_prefix(s: str, width: int) -> tuple[str, str]:
    """Split `s` into (head, tail) where head is the longest prefix ≤ width cells."""
    width = max(1, int(width))
    used = 0
    for idx, ch in enumerate(s):
        cw = _cell_width(ch)
        if used + cw > width:
            return s[:idx], s[idx:]
        used += cw
    return s, ""


def _parse_table(lines: list, start: int, *, max_width: int = 0):
    """If a markdown table begins at `start`, return (rendered_lines, next_index),
    else None. A table = a row with '|', then a '---' separator row, then body rows.

    #219 width-adaptive rendering:
      - fits within `max_width` (columns count as 2 pad + separators);
      - a table too wide has its columns shrunk proportionally and cell text WRAPPED
        across extra lines (no silent truncation);
      - when even that can't give every column a usable width, falls back to a
        vertical key/value layout (each cell on its own line, labelled by header).
    `max_width=0` (the default / non-TUI callers) keeps the classic full-width align."""
    if start + 1 >= len(lines):
        return None
    header, sep = lines[start], lines[start + 1]
    if "|" not in header or not _TABLE_SEP.match(sep):
        return None
    rows = [_split_row(header)]
    i = start + 2
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        rows.append(_split_row(lines[i]))
        i += 1
    # inline-format every cell, then compute natural (unconstrained) column widths.
    rows = [[strip_inline(c) for c in r] for r in rows]
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    nat = [max(_cell_width(r[c]) for r in rows) for c in range(ncols)]

    # Full natural width of the grid: "| " + cells joined by " | " + " |".
    # framing = leading "| " (2) + trailing " |" (2) + " | " (3) between columns.
    framing = 4 + 3 * (ncols - 1)
    natural_total = sum(nat) + framing

    out: list[RLine] = []
    if max_width <= 0 or natural_total <= max_width:
        # classic path: align to the widest natural cell (unchanged behaviour). The
        # ljust uses len(): for pure ASCII (the overwhelmingly common table content)
        # display-width == len, so output is byte-identical to the original.
        widths = nat
        def _fmt(cells):
            return "| " + " | ".join(cells[c].ljust(widths[c]) for c in range(ncols)) + " |"
        out.append(RLine(text=_fmt(rows[0]), style="table"))
        out.append(RLine(text="|" + "|".join("-" * (widths[c] + 2) for c in range(ncols)) + "|",
                         style="table"))
        out.extend(RLine(text=_fmt(r), style="table") for r in rows[1:])
        return out, i

    # ── constrained: solve per-column widths that fit max_width ──
    avail = max_width - framing            # columns' share of the row
    min_col = 3                            # a column narrower than this is unreadable
    if avail < ncols * min_col:
        # even min widths don't fit → vertical key/value fallback.
        return _render_vertical(rows, max_width), i

    widths = _solve_widths(nat, avail, min_col)

    def _row_lines(cells: list[str]) -> list[str]:
        # wrap each cell, then stack cells cell-by-cell into aligned physical lines.
        wrapped = [_wrap_cell(cells[c], widths[c]) for c in range(ncols)]
        height = max(len(w) for w in wrapped)
        physical: list[str] = []
        for h in range(height):
            parts = []
            for c in range(ncols):
                seg = wrapped[c][h] if h < len(wrapped[c]) else ""
                parts.append(seg + " " * (widths[c] - _cell_width(seg)))
            physical.append("| " + " | ".join(parts) + " |")
        return physical

    out.extend(RLine(text=ln, style="table") for ln in _row_lines(rows[0]))
    out.append(RLine(text="|" + "|".join("-" * (widths[c] + 2) for c in range(ncols)) + "|",
                     style="table"))
    for r in rows[1:]:
        out.extend(RLine(text=ln, style="table") for ln in _row_lines(r))
    return out, i


def _solve_widths(nat: list[int], avail: int, min_col: int) -> list[int]:
    """Distribute `avail` columns among cols, minimizing wrapping — WATER-FILLING.

    Process columns from narrowest natural width to widest. Each gets its fair share
    (`budget / columns-left`); a column whose natural width fits under its fair share
    takes only its natural width (no wasted space) and donates the surplus to the
    columns still to be sized. So short columns (keys, numbers, statuses) keep their
    full width and the genuinely-long column(s) absorb all the shrink + wrapping.
    Every column stays ≥ min_col."""
    ncols = len(nat)
    if sum(nat) <= avail:
        return list(nat)
    widths = [0] * ncols
    budget = avail
    for rank, c in enumerate(sorted(range(ncols), key=lambda c: nat[c])):
        left = ncols - rank
        fair = budget // left
        w = nat[c] if nat[c] <= fair else max(min_col, fair)
        w = max(min_col, min(w, budget - min_col * (left - 1)))   # leave min for the rest
        widths[c] = w
        budget -= w
    return widths


def _render_vertical(rows: list[list[str]], max_width: int) -> list[RLine]:
    """Vertical key/value fallback for very narrow terminals: each body row becomes a
    small block of `Header: value` lines (value wrapped), records separated by a rule."""
    header = rows[0]
    ncols = len(header)
    label_w = max((_cell_width(h) for h in header), default=0)
    out: list[RLine] = []
    body = rows[1:] or [[""] * ncols]
    for ri, r in enumerate(body):
        if ri:
            out.append(RLine(text="─" * min(max_width, 12), style="table"))
        for c in range(ncols):
            key = header[c] if c < len(header) else ""
            val = r[c] if c < len(r) else ""
            prefix = (key + ":").ljust(label_w + 1) + " "
            avail = max(1, max_width - _cell_width(prefix))
            wrapped = _wrap_cell(val, avail)
            out.append(RLine(text=(prefix + wrapped[0])[:_char_budget(prefix + wrapped[0], max_width)],
                             style="table"))
            cont_pad = " " * _cell_width(prefix)
            out.extend(RLine(text=cont_pad + extra, style="table") for extra in wrapped[1:])
    return out


def _char_budget(s: str, max_width: int) -> int:
    """Index that keeps `s` within max_width display cells (for the rare wide-glyph
    label case; ASCII passes through unchanged)."""
    if _cell_width(s) <= max_width:
        return len(s)
    used = 0
    for idx, ch in enumerate(s):
        used += _cell_width(ch)
        if used > max_width:
            return idx
    return len(s)


def render_markdown(md: str, *, streaming: bool = False, width: int = 0) -> list[RLine]:
    """Render markdown into styled lines. Code fences preserved verbatim.

    ``streaming=True`` HOLDS BACK a table that runs to the end of the input (it may
    still be growing token-by-token): it's rendered raw so its column widths don't
    reflow on every chunk. Once the table is terminated (followed by another line) or
    the stream finalizes, it aligns. (M2 — the only streamed-markdown reflow Syntra has,
    since fences already render incrementally and tables are the one width-sensitive
    structure.)

    ``width`` (#219) is the terminal columns available for a table; when >0 a table
    wider than it is shrunk+wrapped, or falls back to a vertical key/value layout on
    very narrow terminals. ``width=0`` keeps the classic full-width alignment (used by
    non-TUI callers and existing tests)."""
    lines: list[RLine] = []
    src = (md or "").split("\n")
    in_code = False
    i = 0
    while i < len(src):
        raw = src[i]
        fence = _FENCE.match(raw)
        if fence:
            in_code = not in_code
            lang = fence.group(1).strip()
            lines.append(RLine(text=("```" + lang) if not in_code or lang else "```",
                               style="code"))
            i += 1
            continue
        if in_code:
            lines.append(RLine(text=raw, style="code"))   # verbatim, no inline-strip
            i += 1
            continue
        tbl = _parse_table(src, i, max_width=width)
        # holdback: a trailing table in a still-streaming message keeps reflowing as
        # rows arrive, so leave it raw until it's terminated/finalized.
        if tbl is not None and not (streaming and tbl[1] >= len(src)):
            rendered, nxt = tbl
            lines.extend(rendered)
            i = nxt
            continue
        h = _HEADING.match(raw)
        if h:
            level = len(h.group(1))
            body = strip_inline(h.group(2).strip())
            style = "h1" if level == 1 else ("h2" if level == 2 else "h3")
            text = body.upper() if level == 1 else body
            lines.append(RLine(text=text, style=style))
            i += 1
            continue
        b = _BULLET.match(raw)
        if b:
            lines.append(RLine(text=f"{b.group(1)}• {strip_inline(b.group(2))}", style="bullet"))
            i += 1
            continue
        n = _NUMBERED.match(raw)
        if n:
            lines.append(RLine(text=f"{n.group(1)}{n.group(2)}. {strip_inline(n.group(3))}", style="bullet"))
            i += 1
            continue
        q = _QUOTE.match(raw)
        if q:
            lines.append(RLine(text=f"│ {strip_inline(q.group(1))}", style="quote"))
            i += 1
            continue
        lines.append(RLine(text=strip_inline(raw), style="text"))
        i += 1
    return lines


def render_plain(md: str, *, streaming: bool = False, width: int = 0) -> str:
    """Convenience: the rendered text as a single string (no style tags). ``streaming``
    holds back a trailing (still-growing) table so it doesn't reflow per token.
    ``width`` (#219) fits a wide table to the terminal (0 = classic full-width)."""
    return "\n".join(rl.text for rl in render_markdown(md, streaming=streaming, width=width))
