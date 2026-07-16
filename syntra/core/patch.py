"""Multi-file edit-bundle parser + applier for the apply_patch tool.

Syntra's own edit-bundle envelope — a header grammar designed for this project so the
agent can add/update/delete/rename across many files in ONE atomic call:

    === SYNTRA EDIT BUNDLE ===
    file + path           add a new file (the following +lines are its contents)
    file - path           delete a file
    file ~ path           update a file (one or more @@ hunks follow)
    > rename newpath       rename (only right after a `file ~`)
    @@                     start a hunk (optional locator after @@ is ignored)
     context line          a space-prefixed unchanged line
    -removed line
    +added line
    === END BUNDLE ===

The hunk BODY (``@@``, space/``-``/``+`` line prefixes) is the long-standing Unix
unified-diff convention (``diff -u``, 1978) — universal prior art. The envelope header
grammar above is Syntra's own; the parser, the atomic apply, and the tolerant context
matching below are all original to this project.

Parsing is pure; application is atomic — every operation is computed against in-memory
contents first, and nothing is written unless ALL ops succeed (so a bad hunk can't leave
a half-applied tree). The tool layer adds workspace confinement + permission gating.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PatchError(Exception):
    pass


@dataclass
class FileOp:
    action: str               # "add" | "delete" | "update"
    path: str
    move_to: str = ""         # update-with-rename target
    add_lines: list = field(default_factory=list)         # for add
    hunks: list = field(default_factory=list)             # for update: list[list[str]] raw hunk lines


# Syntra's own edit-bundle markers (share no string with any other tool's format).
_BEGIN = "=== SYNTRA EDIT BUNDLE ==="
_END = "=== END BUNDLE ==="
_ADD = "file + "
_DEL = "file - "
_UPD = "file ~ "
_MOVE = "> rename "


def parse_patch(text: str) -> list[FileOp]:
    """Parse a patch envelope into FileOps. Raises PatchError on malformed input."""
    lines = (text or "").split("\n")
    # tolerate leading/trailing junk around the envelope
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == _BEGIN)
    except StopIteration:
        raise PatchError(f"missing '{_BEGIN}' header") from None
    try:
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == _END)
    except StopIteration:
        raise PatchError(f"missing '{_END}' footer") from None

    ops: list[FileOp] = []
    cur: FileOp | None = None
    cur_hunk: list | None = None

    def _close_hunk():
        nonlocal cur_hunk
        if cur is not None and cur_hunk:
            cur.hunks.append(cur_hunk)
        cur_hunk = None

    for raw in lines[start + 1:end]:
        if raw.startswith(_ADD):
            _close_hunk(); cur = FileOp("add", raw[len(_ADD):].strip()); ops.append(cur)
        elif raw.startswith(_DEL):
            _close_hunk(); cur = FileOp("delete", raw[len(_DEL):].strip()); ops.append(cur)
        elif raw.startswith(_UPD):
            _close_hunk(); cur = FileOp("update", raw[len(_UPD):].strip()); ops.append(cur)
        elif raw.startswith(_MOVE):
            if cur is None or cur.action != "update":
                raise PatchError("'> rename' outside a 'file ~' (update) section")
            cur.move_to = raw[len(_MOVE):].strip()
        elif raw.startswith("@@"):
            if cur is None or cur.action != "update":
                raise PatchError("hunk '@@' outside a 'file ~' (update) section")
            _close_hunk(); cur_hunk = []
        else:
            if cur is None:
                if raw.strip() == "":
                    continue
                raise PatchError(f"content line before any file header: {raw!r}")
            if cur.action == "add":
                if raw.startswith("+"):
                    cur.add_lines.append(raw[1:])
                elif raw.strip() == "":
                    cur.add_lines.append("")
                else:
                    raise PatchError(f"'file +' (add) lines must start with '+': {raw!r}")
            elif cur.action == "update":
                if cur_hunk is None:
                    cur_hunk = []          # allow a hunk with no explicit @@
                cur_hunk.append(raw)
            elif cur.action == "delete":
                if raw.strip():
                    raise PatchError("'file -' (delete) takes no content lines")
    _close_hunk()
    if not ops:
        raise PatchError("patch contains no file operations")
    return ops


def _norm_unicode(s: str) -> str:
    """Strip + fold common typographic Unicode to ASCII so a patch authored with
    plain ASCII still matches a file containing fancy dashes/quotes/spaces."""
    return s.strip().translate(_UNICODE_FOLD)


# Typographic code points -> ASCII (dashes, single/double quotes, odd spaces).
_UNICODE_FOLD = {
    0x2010: 0x2D, 0x2011: 0x2D, 0x2012: 0x2D, 0x2013: 0x2D, 0x2014: 0x2D,
    0x2015: 0x2D, 0x2212: 0x2D,                                   # dashes
    0x2018: 0x27, 0x2019: 0x27, 0x201A: 0x27, 0x201B: 0x27,       # single quotes
    0x201C: 0x22, 0x201D: 0x22, 0x201E: 0x22, 0x201F: 0x22,       # double quotes
    0x00A0: 0x20, 0x2007: 0x20, 0x202F: 0x20, 0x205F: 0x20,       # odd spaces
}
for _c in range(0x2000, 0x200B):                                 # en/em/thin spaces
    _UNICODE_FOLD[_c] = 0x20


def _find_block(src: list, old: list, *, eof: bool = False) -> int:
    """Locate the `old` block in `src`. Escalates tolerance: exact -> trailing-
    whitespace-insensitive -> fully-stripped -> Unicode-punctuation-folded.
    Returns the start index or -1.

    Each level requires a UNIQUE match (to avoid patching the wrong place); a
    looser level is only used when the stricter ones found nothing. When `eof`
    is set (the hunk is meant to match the file's end), the search starts at the
    last possible position so end-anchored context lands at the end.
    """
    n = len(old)
    if n == 0 or n > len(src):
        return -1
    start = (len(src) - n) if eof else 0

    def _scan(norm):
        want = [norm(x) for x in old]
        hits = [i for i in range(start, len(src) - n + 1)
                if [norm(s) for s in src[i:i + n]] == want]
        return hits[0] if len(hits) == 1 else (-2 if len(hits) > 1 else -1)

    for norm in (lambda s: s,                      # exact
                 lambda s: s.rstrip(),             # ignore trailing whitespace
                 lambda s: s.strip(),              # ignore leading+trailing
                 _norm_unicode):                   # fold typographic punctuation
        idx = _scan(norm)
        if idx >= 0:
            return idx
        if idx == -2:                              # ambiguous at this level -> stop
            return -1
    # eof anchor found nothing at the tail -> fall back to a full scan.
    if eof and start > 0:
        return _find_block(src, old, eof=False)
    return -1


def _apply_hunk(content: str, hunk: list) -> str:
    """Apply one hunk (context/-/+ lines) to content. Raises if context not found."""
    old_block, new_block = [], []
    for ln in hunk:
        if ln.startswith("-"):
            old_block.append(ln[1:])
        elif ln.startswith("+"):
            new_block.append(ln[1:])
        else:                                   # context (space prefix or bare)
            ctx = ln[1:] if ln.startswith(" ") else ln
            old_block.append(ctx)
            new_block.append(ctx)
    if not old_block:                           # pure insertion -> append
        return content + ("\n" if content and not content.endswith("\n") else "") + "\n".join(new_block)
    src = content.split("\n")
    i = _find_block(src, old_block)
    if i < 0:
        # End-anchored retry: a hunk meant to match the file's end may differ
        # mid-file but align at the tail (EOF-anchored sequence match).
        i = _find_block(src, old_block, eof=True)
    if i < 0:
        raise PatchError("hunk context not found in file (it may have changed)")
    src[i:i + len(old_block)] = new_block
    return "\n".join(src)


def apply_ops(ops: list[FileOp], read_fn, exists_fn) -> dict:
    """Compute the resulting files purely. Returns {path: new_content | None(delete)}.

    read_fn(path)->str, exists_fn(path)->bool. Raises PatchError on any failure
    (caller writes nothing unless this returns cleanly -> atomic).
    """
    result: dict = {}

    def _current(path):
        if path in result:
            return result[path]
        return read_fn(path) if exists_fn(path) else None

    for op in ops:
        if op.action == "add":
            if exists_fn(op.path):
                raise PatchError(f"add '{op.path}': already exists")
            result[op.path] = "\n".join(op.add_lines)
        elif op.action == "delete":
            if not exists_fn(op.path):
                raise PatchError(f"delete '{op.path}': does not exist")
            result[op.path] = None
        elif op.action == "update":
            content = _current(op.path)
            if content is None:
                raise PatchError(f"update '{op.path}': does not exist")
            for hunk in (op.hunks or []):
                content = _apply_hunk(content, hunk)
            if op.move_to:
                result[op.path] = None           # remove old path
                result[op.move_to] = content     # write new path
            else:
                result[op.path] = content
    return result


class StreamingBundleParser:
    """Accumulate streamed chunks; parse once the full patch envelope arrives.

    Lets the agent apply a bundle that the model emits token-by-token: feed chunks
    as they stream, and `ready()` flips true once both the bundle header and footer
    are present. `ops()` then parses the buffered envelope with the same parse_patch
    used for whole bundles. Pure -> unit-tested.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> bool:
        """Append a chunk; return True once a complete envelope is buffered."""
        self._buf += chunk or ""
        return self.ready()

    def ready(self) -> bool:
        return _BEGIN in self._buf and _END in self._buf

    def envelope(self) -> str:
        """The buffered text trimmed to the Begin..End envelope (or '')."""
        if not self.ready():
            return ""
        start = self._buf.index(_BEGIN)
        end = self._buf.index(_END, start) + len(_END)
        return self._buf[start:end]

    def ops(self) -> list:
        """Parse the completed envelope into FileOps (raises PatchError if not ready)."""
        if not self.ready():
            raise PatchError("patch envelope not complete yet")
        return parse_patch(self.envelope())
