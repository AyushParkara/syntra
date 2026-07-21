"""Record/replay test fixtures for provider calls (#167) — dev-infra.

ponytail: moved from syntra/core/vcr.py. This is dev-infra, not runtime.
Production code should never import this module.

Provider tests want to exercise the REAL request/response shape without hitting the
network on every run (slow, flaky, costs tokens, needs a key). VCR records a real
provider `chat(...)` response to a hashed on-disk "cassette", then replays it offline
on the next identical request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class CassetteMiss(RuntimeError):
    """No cassette for this request and no recorder to make one (replay-only mode)."""


def _msg_repr(m) -> dict:
    return {
        "role": getattr(m, "role", ""),
        "content": getattr(m, "content", ""),
        "tool_call_id": getattr(m, "tool_call_id", ""),
        "name": getattr(m, "name", ""),
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in (getattr(m, "tool_calls", ()) or ())
        ],
    }


def cassette_key(model_id: str, messages, **knobs) -> str:
    payload = {
        "model": str(model_id or ""),
        "messages": [_msg_repr(m) for m in (messages or [])],
        "knobs": {k: knobs[k] for k in sorted(knobs)},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _to_dict(result) -> dict:
    return {
        "_schema": "syntra.vcr_cassette", "_schema_version": 1,
        "text": result.text,
        "input_tokens": int(result.input_tokens),
        "output_tokens": int(result.output_tokens),
        "model_id": result.model_id,
        "provider": result.provider,
        "finish_reason": getattr(result, "finish_reason", "") or "",
        "reasoning": getattr(result, "reasoning", "") or "",
        "cache_read_tokens": int(getattr(result, "cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(result, "cache_write_tokens", 0) or 0),
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in (getattr(result, "tool_calls", ()) or ())
        ],
    }


def _from_dict(d: dict):
    from syntra.providers.openai_compat import ChatResult, ToolCall
    return ChatResult(
        text=d.get("text", ""),
        input_tokens=int(d.get("input_tokens", 0) or 0),
        output_tokens=int(d.get("output_tokens", 0) or 0),
        model_id=d.get("model_id", ""),
        provider=d.get("provider", ""),
        finish_reason=d.get("finish_reason", "") or "",
        reasoning=d.get("reasoning", "") or "",
        cache_read_tokens=int(d.get("cache_read_tokens", 0) or 0),
        cache_write_tokens=int(d.get("cache_write_tokens", 0) or 0),
        tool_calls=tuple(
            ToolCall(id=t.get("id", ""), name=t.get("name", ""), arguments=t.get("arguments", ""))
            for t in (d.get("tool_calls") or [])
        ),
    )


def _path(dir_: "str | Path", key: str) -> Path:
    return Path(dir_) / f"{key}.json"


def load(dir_: "str | Path", key: str):
    try:
        d = json.loads(_path(dir_, key).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(d, dict):
        return None
    return _from_dict(d)


def record(dir_: "str | Path", key: str, result) -> None:
    from syntra.core.fsutil import write_atomic_bytes
    write_atomic_bytes(
        _path(dir_, key), json.dumps(_to_dict(result), indent=2, ensure_ascii=False).encode("utf-8"),
        mode=None)


class ReplayProvider:
    def __init__(self, cassette_dir: "str | Path", *, record_with=None):
        self.dir = Path(cassette_dir)
        self.record_with = record_with

    def chat(self, model_id: str, messages, **knobs):
        key = cassette_key(model_id, messages, **knobs)
        hit = load(self.dir, key)
        if hit is not None:
            return hit
        if self.record_with is None:
            raise CassetteMiss(
                f"no cassette for {model_id!r} (key {key}) and no recorder "
                f"run once with a real provider as record_with to capture it")
        result = self.record_with.chat(model_id, messages, **knobs)
        record(self.dir, key, result)
        return result
