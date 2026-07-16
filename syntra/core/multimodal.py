"""Multimodal input support (images) for vision-capable models.

Vision models on the OpenAI-compatible API take image inputs as content "parts":
a message's content becomes a list of {"type":"text"} / {"type":"image_url"}
entries. This module builds those parts safely:

- sniff the real image type from magic bytes (png/jpeg/gif/webp);
- turn a local file into a base64 `data:` URL (size-capped) or pass an http(s) URL;
- reject unsupported types and oversized images.

Pure + deterministic -> unit-tested. The provider layer serializes these parts;
routing must send images only to vision-capable models (caller's responsibility).
"""

from __future__ import annotations

import base64
from pathlib import Path

SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024      # 10 MB per image


class ImageError(Exception):
    pass


def sniff_mime(data: bytes) -> str | None:
    """Detect image type from magic bytes. Returns a MIME or None if unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def data_url_from_bytes(data: bytes, *, mime: str | None = None) -> str:
    """Build a base64 data: URL from raw image bytes (mime sniffed if not given)."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"image too large ({len(data)} bytes > {MAX_IMAGE_BYTES})")
    mime = mime or sniff_mime(data)
    if mime not in SUPPORTED_MIME:
        raise ImageError(f"unsupported image type ({mime or 'unknown'}); "
                         f"supported: {', '.join(sorted(SUPPORTED_MIME))}")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def data_url_from_file(path: str | Path) -> str:
    """Read a local image file -> base64 data: URL (type sniffed, size-capped)."""
    p = Path(path)
    if not p.is_file():
        raise ImageError(f"no such image file: {path}")
    return data_url_from_bytes(p.read_bytes())


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _clean_dropped_path(text: str) -> str | None:
    """Normalize a single dragged/pasted path (quoted / file:// / shell-escaped spaces).
    Returns the cleaned string, or None if `text` isn't a single-line path-like token."""
    s = (text or "").strip()
    if not s or "\n" in s:
        return None
    # strip a surrounding pair of single/double quotes
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    # file:// URL → local path (percent-decode + drop the scheme/host)
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        s = unquote(urlparse(s).path)
    # un-escape shell-escaped spaces some terminals add on drag (e.g. "my\ pic.png")
    return s.replace("\\ ", " ")


def looks_like_image_path(text: str) -> str | None:
    """If `text` is a single path to an existing image file, return the cleaned path; else None.

    This backs drag-and-drop: dragging a file onto a terminal pastes its PATH (often
    quoted, or `file://`-prefixed, sometimes with backslash-escaped spaces). We treat a
    paste as an image ONLY when it resolves to one existing image file — so normal text
    that merely mentions ".png" is never mistaken for an attachment.
    """
    s = _clean_dropped_path(text)
    if not s or not s.lower().endswith(_IMAGE_EXTS):
        return None
    p = Path(s).expanduser()
    return str(p) if p.is_file() else None


def looks_like_file_path(text: str) -> str | None:
    """If `text` is a single path to an EXISTING file (any type), return the cleaned path; else
    None (#127). Backs drag-and-drop for non-image files. Requires the file to actually exist so
    ordinary prose that merely contains a word with a dot isn't mistaken for an attachment; also
    rejects multi-token strings (a real dropped path is one token) unless it resolves as-is."""
    s = _clean_dropped_path(text)
    if not s:
        return None
    p = Path(s).expanduser()
    return str(p) if p.is_file() else None


# ── general (non-image) file attachments (#127) ──────────────────────────────────────────
# Attaching a file should work for ANY file the model can use, not only images. Images ride the
# vision channel (initial_images); text-ish files (code, prose, config, logs, data) are read and
# injected into the goal as a fenced context block that every model can read — no vision needed.
MAX_TEXT_ATTACH_BYTES = 256 * 1024        # 256 KB of text per file (keeps the prompt bounded)
_TEXT_EXTS = (
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".sh", ".bash", ".zsh", ".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".go", ".rb", ".java",
    ".kt", ".swift", ".php", ".pl", ".lua", ".sql", ".html", ".htm", ".css", ".scss", ".xml",
    ".env", ".conf", ".gitignore", ".dockerfile", ".makefile", ".tf", ".gradle", ".properties",
)


class AttachmentError(Exception):
    pass


_DOC_EXTS = (".pdf", ".docx")            # extractable documents (text pulled out, if a lib exists)


def classify_attachment(path: str | Path) -> str:
    """Return 'image', 'text', 'document', or 'binary' for a local file (#127). Images go to the
    vision channel; text + extracted document text are injected as context; other binary is
    refused with a clear message."""
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise AttachmentError(f"no such file: {path}")
    low = p.name.lower()
    if low.endswith(_IMAGE_EXTS):
        return "image"
    if low.endswith(_DOC_EXTS):
        return "document"
    if low.endswith(_TEXT_EXTS) or p.suffix == "":   # extensionless (README, Makefile) → try text
        # confirm it's actually decodable text (not a binary with a text-ish name)
        return "text" if _is_probably_text(p) else "binary"
    # unknown extension: sniff — many code/data files have odd suffixes but are UTF-8.
    return "text" if _is_probably_text(p) else "binary"


def read_document_attachment(path: str | Path) -> str:
    """Extract text from a PDF/.docx for injection as context (#127). Uses whatever extractor is
    ALREADY importable (pypdf for PDF, python-docx for .docx) — never forces a new install. If
    none is available, raises AttachmentError with a plain, no-pitch message so the caller can
    tell the user how to proceed (attach the text some other way)."""
    import importlib
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise AttachmentError(f"no such file: {path}")
    low = p.name.lower()
    text = ""
    if low.endswith(".pdf"):
        pypdf = None
        for mod in ("pypdf", "PyPDF2"):
            try:
                pypdf = importlib.import_module(mod)
                break
            except Exception:  # noqa: BLE001
                continue
        if pypdf is None:
            raise AttachmentError("can't read PDFs here (no PDF text extractor available) — "
                                  "paste the text, or convert it to .txt/.md first")
        try:
            reader = pypdf.PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as e:  # noqa: BLE001
            raise AttachmentError(f"could not extract text from {p.name}: {str(e)[:80]}") from e
    elif low.endswith(".docx"):
        try:
            docx = importlib.import_module("docx")
        except Exception:  # noqa: BLE001
            raise AttachmentError("can't read .docx here (python-docx not available) — "
                                  "save it as .txt/.md and attach that") from None
        try:
            doc = docx.Document(str(p))
            text = "\n".join(par.text for par in doc.paragraphs)
        except Exception as e:  # noqa: BLE001
            raise AttachmentError(f"could not extract text from {p.name}: {str(e)[:80]}") from e
    else:
        raise AttachmentError(f"not an extractable document: {p.name}")
    text = text.strip()
    if not text:
        raise AttachmentError(f"{p.name} has no extractable text (scanned/empty?)")
    if len(text) > MAX_TEXT_ATTACH_BYTES:
        text = text[:MAX_TEXT_ATTACH_BYTES] + f"\n… [truncated at {MAX_TEXT_ATTACH_BYTES // 1024} KB]"
    return text


def _is_probably_text(p: Path, sniff: int = 4096) -> bool:
    """Heuristic: a file is text if its first chunk decodes as UTF-8 and has no NUL bytes.
    Reads only the first `sniff` bytes — never the whole file (a multi-GB log must not be
    slurped into memory just to classify it, #139)."""
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(sniff)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        # a valid UTF-8 sequence can be split at the sniff boundary — ignore a trailing
        # partial multibyte char so we don't misclassify real text as binary.
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start >= sniff - 4    # only a truncated final char → still text


def read_text_attachment(path: str | Path) -> str:
    """Read a text-ish file's content (size-capped) for injection as context (#127). Reads AT MOST
    MAX_TEXT_ATTACH_BYTES+1 bytes — never the whole file — so attaching a multi-GB log can't slurp
    it all into memory (#139). Reads one extra byte to detect (and mark) truncation."""
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise AttachmentError(f"no such file: {path}")
    try:
        with open(p, "rb") as fh:
            data = fh.read(MAX_TEXT_ATTACH_BYTES + 1)
    except OSError as e:
        raise AttachmentError(f"could not read {p.name}: {str(e)[:80]}") from e
    truncated = len(data) > MAX_TEXT_ATTACH_BYTES
    if truncated:
        data = data[:MAX_TEXT_ATTACH_BYTES]
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n… [truncated at {MAX_TEXT_ATTACH_BYTES // 1024} KB]"
    return text


def _safe_fence(content: str) -> str:
    """A code fence GUARANTEED longer than any backtick run inside `content`, so a file that
    itself contains ``` (Markdown, fenced code, this very docstring) can't close the fence early
    and break the attachment boundary the model sees (#127). CommonMark: a fence must be longer
    than any backtick run it wraps."""
    import re
    longest = max((len(m) for m in re.findall(r"`+", content or "")), default=0)
    return "`" * max(3, longest + 1)


def context_block_from_texts(texts) -> str:
    """Build a fenced context block from [(name, content), …] to prepend to a goal (#127).
    Empty string when there are no text attachments. Each file is wrapped in a fence sized to
    survive any backticks in its own content; the filename on the header line is flattened to a
    single line so an adversarially-named file (embedded newline + fake fence) can't inject framing
    OUTSIDE the fence the block relies on (#140)."""
    parts = []
    for name, content in texts or ():
        safe_name = " ".join(str(name).splitlines()).strip() or "attachment"
        fence = _safe_fence(content)
        parts.append(f"📎 Attached file: {safe_name}\n{fence}\n{content}\n{fence}")
    if not parts:
        return ""
    return "The user attached the following file(s) as context:\n\n" + "\n\n".join(parts) + "\n\n"


def image_part(url_or_data: str) -> dict:
    """An OpenAI image content part for a chat message."""
    return {"type": "image_url", "image_url": {"url": url_or_data}}


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def content_parts(text: str, images) -> list:
    """Assemble a multi-part content array: the text first, then each image.

    `images` is an iterable of data:/http(s) URLs. http(s) URLs are passed through;
    anything else must already be a data: URL (build it via data_url_from_*).
    """
    parts: list = []
    if text:
        parts.append(text_part(text))
    for img in images or ():
        s = str(img)
        if not (s.startswith("data:") or s.startswith("http://") or s.startswith("https://")):
            raise ImageError("image must be a data: or http(s) URL (use data_url_from_file)")
        parts.append(image_part(s))
    return parts
