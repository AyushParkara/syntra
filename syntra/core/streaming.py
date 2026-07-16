"""Streaming response support (SSE) for the OpenAI-compatible API.

A streamed completion arrives as Server-Sent Events: many `data: {json}` lines,
each carrying a partial `delta`, ending with `data: [DONE]`. Text comes
incrementally; tool calls arrive FRAGMENTED — split by an `index`, with the
function name and a slice of the JSON `arguments` string in different chunks.

`StreamAccumulator` reassembles a stream into a final (text, tool_calls). It is
pure (feed parsed delta dicts), so the hard reassembly logic is unit-tested
without any network. `iter_sse` turns raw SSE lines into delta dicts. The
provider wires a real HTTP stream into these; a callback surfaces text live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..providers.openai_compat import ToolCall


@dataclass
class StreamAccumulator:
    text_parts: list = field(default_factory=list)
    reasoning_parts: list = field(default_factory=list)
    # index -> {"id":..., "name":..., "args":[...]}
    _tools: dict = field(default_factory=dict)
    finish_reason: str = ""

    def feed_delta(self, delta: dict, *, finish_reason: str = "",
                   on_text=None, on_reasoning=None) -> None:
        """Apply one streamed choice delta.

        Reasoning models (DeepSeek-R1, OpenRouter `:thinking`, some OpenAI-compat
        gateways) stream their chain-of-thought in a SEPARATE delta field —
        `reasoning_content` (DeepSeek) or `reasoning` (OpenRouter) — alongside the
        normal `content`. We capture it so the TUI can show a live "thinking"
        block; it never mixes into the answer text. Absent that field, behavior is
        exactly as before (reasoning stays empty)."""
        if finish_reason:
            self.finish_reason = finish_reason
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            self.reasoning_parts.append(reasoning)
            if on_reasoning:
                on_reasoning(reasoning)
        content = delta.get("content")
        if content:
            self.text_parts.append(content)
            if on_text:
                on_text(content)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = self._tools.setdefault(idx, {"id": "", "name": "", "args": []})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"].append(fn["arguments"])

    def text(self) -> str:
        return "".join(self.text_parts)

    def reasoning_text(self) -> str:
        return "".join(self.reasoning_parts)

    def tool_calls(self) -> tuple:
        out = []
        for idx in sorted(self._tools):
            slot = self._tools[idx]
            out.append(ToolCall(id=slot["id"] or f"call_{idx}",
                                name=slot["name"], arguments="".join(slot["args"])))
        return tuple(out)


def parse_sse(lines, *, on_text=None, on_reasoning=None) -> StreamAccumulator:
    """Reassemble an SSE line iterable into a StreamAccumulator. Pure-ish.

    `on_reasoning(chunk)` (optional) fires for each chain-of-thought chunk from
    reasoning models, separate from `on_text` — so a caller can render a live
    "thinking" stream without it leaking into the answer."""
    acc = StreamAccumulator()
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if choices:
            ch = choices[0]
            acc.feed_delta(ch.get("delta") or {}, finish_reason=ch.get("finish_reason") or "",
                           on_text=on_text, on_reasoning=on_reasoning)
    return acc
