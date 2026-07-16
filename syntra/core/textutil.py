"""Text shaping helpers shared across core modules."""


def clip(text: str, limit: int) -> str:
    """Truncate to ~limit chars at a word/line boundary, not mid-token.

    Blind slicing (``text[:n]``) cuts mid-word and mid-identifier, which confuses
    the consuming model. This backs up to the last space/newline within a small
    window and appends a clear marker with the dropped count."""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    sp = max(cut.rfind(" "), cut.rfind("\n"))
    if sp > 0 and sp >= limit // 2:
        cut = cut[:sp]
    dropped = len(text) - len(cut)
    return cut.rstrip() + f" …[+{dropped} chars]"
