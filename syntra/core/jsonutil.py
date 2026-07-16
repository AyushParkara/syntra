"""Shared best-effort JSON extraction from model output.

Consolidates the two historical copies (loop._extract_json / TaskAnalyzer._extract_json).
Default: raise ValueError when no JSON is found. Pass `default=` to get a fallback
value instead (matches the analyzer's original `{}`-on-failure contract).
"""
import json
import re

# Object-only by design: both callers expect a JSON object (they immediately do
# result.get(...)). We intentionally match `{...}` and not top-level `[...]` arrays.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_RAISE = object()


def extract_json(text: str, *, default=_RAISE) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, c in enumerate(text[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    if default is _RAISE:
        raise ValueError("No JSON found in model output")
    return default
