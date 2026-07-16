"""Central secret redaction (Phase 5 security hardening).

One place that masks secrets before they can reach logs, events, error
messages, or packed context (PLAN Section 6 #10: no secret in any log/output;
target secret-leak count == 0). Two layers:

1. exact-match : caller-supplied known secrets (e.g. a provider's configured
   api_key) are masked literally -- the strongest guarantee, since we mask the
   real value even if a provider echoes it in an error body.
2. pattern     : shapes that look like secrets (sk-..., AKIA..., Bearer ...,
   key=value) are masked even when we don't know the literal -- conservative,
   prefers a false mask over a leak.

Pure + deterministic -> unit-tested (incl. a fuzz test).
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-<REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<REDACTED>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer <REDACTED>"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b(\s*[:=]\s*)"
                r"['\"]?[^\s'\"]{6,}['\"]?"), r"\1\2<REDACTED>"),
)

# api_key values shorter than this aren't treated as exact secrets (avoids
# masking "" or sentinels like "no-auth").
_MIN_SECRET_LEN = 6


# #248 — DETECTION patterns (distinct from the redaction patterns above, which MASK for display).
# These REPORT that content contains a credential, so a caller can refuse to WRITE it into a
# durable file (memory / .syntra) — a secret in MEMORY.md is re-injected into every step. Curated
# provider-token shapes + a generic assignment; tuned to avoid firing on ordinary prose/code.
_SECRET_SCAN: tuple[tuple[str, re.Pattern], ...] = (
    ("openai/anthropic-key",  re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("aws-access-key-id",     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token",          re.compile(r"\bgh[posru]_[A-Za-z0-9]{16,}\b")),
    ("gitlab-token",          re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b")),
    ("slack-token",           re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-oauth",          re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}")),
    ("google-api-key",        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-key",            re.compile(r"\b[rs]k_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("bearer-token",          re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("private-key-block",     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt",                   re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}")),
    # generic `secret/password/token/api_key = <value>` with a real-looking value (>=8 chars,
    # not a placeholder word). Requires quotes or a non-trivial value to avoid prose false hits.
    ("generic-assignment",    re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9+/_\-]{8,})['\"]?")),
)
# Placeholder values that the generic-assignment rule must NOT treat as a real secret.
_SECRET_PLACEHOLDERS = frozenset({
    "your_api_key", "yourapikey", "changeme", "example", "placeholder", "xxxxxxxx",
    "redacted", "none", "null", "true", "false", "todo", "value", "secret", "password",
})


def scan_secrets(text: str) -> list[str]:
    """Return the list of secret RULE NAMES whose pattern matches `text` (empty = clean). Used to
    refuse writing a credential into a durable file. Detection, not masking. Pure — unit-tested."""
    if not text:
        return []
    s = str(text)
    hits = []
    for name, pat in _SECRET_SCAN:
        m = pat.search(s)
        if not m:
            continue
        if name == "generic-assignment":
            val = (m.group(2) or "").strip().strip("'\"").lower()
            if val in _SECRET_PLACEHOLDERS or len(val) < 8:
                continue                      # a placeholder / too-short → not a real secret
        hits.append(name)
    return hits


def contains_secret(text: str) -> bool:
    """True if `text` contains any recognizable credential shape (#248 write-gate)."""
    return bool(scan_secrets(text))


def redact(text: str, secrets=()) -> str:
    """Mask secrets in ``text``. Exact known secrets first, then patterns.

    ``secrets`` is an iterable of literal secret strings (e.g. configured API
    keys) to mask wherever they appear. Non-string / short / sentinel values are
    ignored. Always returns a string.
    """
    if text is None:
        return ""
    out = str(text)
    for s in secrets or ():
        if not isinstance(s, str):
            continue
        sv = s.strip()
        if len(sv) < _MIN_SECRET_LEN or sv.lower() == "no-auth":
            continue
        out = out.replace(sv, "<REDACTED>")
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out
