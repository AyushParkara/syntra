"""Native provider adapters: Anthropic Messages + Google Gemini.

We default to the OpenAI-compatible protocol, but Claude (`/v1/messages`) and
Gemini (`generateContent`) use different request/response shapes for messages,
tools, and images. These adapters translate our `ChatMessage`/tools/images
to/from each native API and expose the SAME `chat()` surface as the OpenAI
adapter, so routing treats them identically.

Translation (build_*/parse_*) is PURE and the HTTP POST is injected, so the
shape-mapping is unit-tested with fake responses (no network). These map to the
public API specifications, implemented independently.
"""

from __future__ import annotations

import json
import urllib.request

from ..providers.openai_compat import ChatResult, ToolCall, ProviderError
from .redact import redact as _redact


# ----------------------------------------------------------------- helpers

def _data_url_parts(url: str):
    """('image/png', '<b64>') from a data: URL, else (None, None)."""
    if url.startswith("data:") and ";base64," in url:
        head, b64 = url.split(";base64,", 1)
        return head[len("data:"):], b64
    return None, None


# ----------------------------------------------------------------- Anthropic

def anthropic_build_body(model_id, messages, *, max_tokens, temperature, tools=None) -> dict:
    system_parts, conv = [], []
    for m in messages:
        if m.role == "system":
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == "tool":
            conv.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]})
            continue
        blocks = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        for img in m.images or ():
            mime, b64 = _data_url_parts(img)
            if b64:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": img}})
        for tc in m.tool_calls or ():
            try:
                inp = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                inp = {}
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": inp})
        conv.append({"role": m.role, "content": blocks or m.content})
    body = {"model": model_id, "messages": conv,
            "max_tokens": max_tokens, "temperature": temperature}
    if system_parts:
        # T6: cache the STABLE system block (role prompt + rules + brief) so the repeated
        # planner/executor/reviewer prefix is billed at ~0.1x on the warm read. The native
        # Anthropic API takes cache_control as a tag on a system text BLOCK (not a bare
        # string), so we send the structured form. Killable via SYNTRA_NO_PROMPT_CACHE.
        import os as _os
        sys_text = "\n\n".join(system_parts)
        if _os.environ.get("SYNTRA_NO_PROMPT_CACHE"):
            body["system"] = sys_text
        else:
            body["system"] = [{"type": "text", "text": sys_text,
                               "cache_control": {"type": "ephemeral"}}]
    if tools:
        body["tools"] = [{"name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters", {})}
                         for t in tools]
    return body


def anthropic_parse(resp: dict, model_id: str, provider: str) -> ChatResult:
    text_parts, tool_calls = [], []
    for block in resp.get("content", []) or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                                       arguments=json.dumps(block.get("input", {}))))
    usage = resp.get("usage", {}) or {}
    # T6: parse cache accounting so _record_cost reprices cached reads (~0.1x) / writes (~1.25x)
    # on the NATIVE Anthropic path too (the OpenAI-compat path already did; this closes the gap so
    # direct Anthropic calls are no longer billed at full input rate for the repeated prefix).
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    # IMPORTANT: the native Anthropic API reports usage.input_tokens as the UNCACHED REMAINDER
    # (total = input_tokens + cache_read + cache_write), whereas the OpenAI-compat `prompt_tokens`
    # INCLUDES the cached tokens. _record_cost (shared) subtracts cache from input_tokens, so it
    # expects the INCLUSIVE convention — report the full prompt total here so the repricing math
    # (full_input = input_tokens - cache_read - cache_write) lands on the truly-uncached count.
    input_tokens = int(usage.get("input_tokens", 0)) + cache_read + cache_write
    return ChatResult(
        text="".join(text_parts),
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens", 0)),
        model_id=resp.get("model", model_id), provider=provider, raw=resp,
        finish_reason=resp.get("stop_reason", "") or "",
        tool_calls=tuple(tool_calls),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


# ----------------------------------------------------------------- Gemini

def gemini_build_body(messages, *, max_tokens, temperature, tools=None) -> dict:
    sys_parts, contents = [], []
    for m in messages:
        if m.role == "system":
            if m.content:
                sys_parts.append(m.content)
            continue
        if m.role == "tool":
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": m.name or m.tool_call_id,
                                      "response": {"result": m.content}}}]})
            continue
        role = "model" if m.role == "assistant" else "user"
        parts = []
        if m.content:
            parts.append({"text": m.content})
        for img in m.images or ():
            mime, b64 = _data_url_parts(img)
            if b64:
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        for tc in m.tool_calls or ():
            try:
                args = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            parts.append({"functionCall": {"name": tc.name, "args": args}})
        contents.append({"role": role, "parts": parts or [{"text": m.content}]})
    body = {"contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
    if sys_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(sys_parts)}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "parameters": t["function"].get("parameters", {})} for t in tools]}]
    return body


def gemini_parse(resp: dict, model_id: str, provider: str) -> ChatResult:
    text_parts, tool_calls = [], []
    cands = resp.get("candidates", []) or []
    finish = ""
    if cands:
        finish = cands[0].get("finishReason", "") or ""
        for i, part in enumerate(cands[0].get("content", {}).get("parts", []) or []):
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(ToolCall(id=f"call_{i}", name=fc.get("name", ""),
                                           arguments=json.dumps(fc.get("args", {}))))
    usage = resp.get("usageMetadata", {}) or {}
    return ChatResult(
        text="".join(text_parts),
        input_tokens=int(usage.get("promptTokenCount", 0)),
        output_tokens=int(usage.get("candidatesTokenCount", 0)),
        model_id=model_id, provider=provider, raw=resp,
        finish_reason=finish, tool_calls=tuple(tool_calls),
    )


# ----------------------------------------------------------------- adapters

def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - https provider URL
        return json.loads(r.read().decode("utf-8"))


class AnthropicProvider:
    """Claude Messages API adapter (same chat() surface as the OpenAI adapter)."""

    def __init__(self, endpoint, post=_post_json):
        self.endpoint = endpoint
        self._post = post

    @property
    def name(self):
        return self.endpoint.name

    def is_ready(self):
        return bool(self.endpoint.base_url)

    def chat(self, model_id, messages, *, max_tokens=2048, temperature=0.2,
             extra_body=None, tools=None, tool_choice=None, max_retries=2, sleep=None) -> ChatResult:
        body = anthropic_build_body(model_id, messages, max_tokens=max_tokens,
                                    temperature=temperature, tools=tools)
        if extra_body:
            body.update({k: v for k, v in extra_body.items() if v not in (None, "")})
        headers = {"x-api-key": self.endpoint.api_key, "anthropic-version": "2023-06-01"}
        if self.endpoint.extra_headers:
            headers.update(self.endpoint.extra_headers)
        url = self.endpoint.base_url.rstrip("/") + "/v1/messages"
        try:
            resp = self._post(url, body, headers, self.endpoint.timeout)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(_redact(f"Anthropic call failed ({self.endpoint.name}): {e}",
                                        [self.endpoint.api_key])) from e
        return anthropic_parse(resp, model_id, self.endpoint.name)


class GeminiProvider:
    """Google Gemini generateContent adapter."""

    def __init__(self, endpoint, post=_post_json):
        self.endpoint = endpoint
        self._post = post

    @property
    def name(self):
        return self.endpoint.name

    def is_ready(self):
        return bool(self.endpoint.base_url)

    def chat(self, model_id, messages, *, max_tokens=2048, temperature=0.2,
             extra_body=None, tools=None, tool_choice=None, max_retries=2, sleep=None) -> ChatResult:
        body = gemini_build_body(messages, max_tokens=max_tokens, temperature=temperature, tools=tools)
        if extra_body:
            body.update({k: v for k, v in extra_body.items() if v not in (None, "")})
        key = self.endpoint.api_key
        url = f"{self.endpoint.base_url.rstrip('/')}/models/{model_id}:generateContent?key={key}"
        headers = dict(self.endpoint.extra_headers or {})
        try:
            resp = self._post(url, body, headers, self.endpoint.timeout)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(_redact(f"Gemini call failed ({self.endpoint.name}): {e}",
                                        [self.endpoint.api_key])) from e
        return gemini_parse(resp, model_id, self.endpoint.name)


# ----------------------------------------------------------------- Responses API

def responses_build_body(model_id, messages, *, max_tokens, temperature, tools=None) -> dict:
    """Translate our messages/tools to the OpenAI Responses API request shape.

    Request: { model, instructions, input:[items], max_output_tokens, tools }.
    `instructions` carries the system prompt; `input` is the conversation as typed
    items (message / function_call / function_call_output)."""
    instructions = []
    items = []
    for m in messages:
        if m.role == "system":
            if m.content:
                instructions.append(m.content)
            continue
        if m.role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": m.tool_call_id, "output": m.content})
            continue
        items.extend({"type": "function_call", "call_id": tc.id,
                      "name": tc.name, "arguments": tc.arguments} for tc in (m.tool_calls or ()))
        if m.content or not m.tool_calls:
            items.append({"type": "message", "role": m.role,
                          "content": [{"type": ("output_text" if m.role == "assistant" else "input_text"),
                                       "text": m.content or ""}]})
    body = {"model": model_id, "input": items,
            "max_output_tokens": max_tokens, "temperature": temperature}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if tools:
        body["tools"] = [{"type": "function", "name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "parameters": t["function"].get("parameters", {})} for t in tools]
    return body


def responses_parse(resp: dict, model_id: str, provider: str) -> ChatResult:
    """Parse a Responses API result -> ChatResult (text + tool_calls + usage)."""
    text_parts, tool_calls = [], []
    for item in resp.get("output", []) or []:
        itype = item.get("type")
        if itype == "message":
            text_parts.extend(c.get("text", "") for c in item.get("content", []) or []
                              if c.get("type") in ("output_text", "text"))
        elif itype == "function_call":
            tool_calls.append(ToolCall(id=item.get("call_id", item.get("id", "")),
                                       name=item.get("name", ""),
                                       arguments=item.get("arguments", "") or ""))
    usage = resp.get("usage", {}) or {}
    return ChatResult(
        text="".join(text_parts),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        model_id=resp.get("model", model_id), provider=provider, raw=resp,
        finish_reason=resp.get("status", "") or ("tool_calls" if tool_calls else "stop"),
        tool_calls=tuple(tool_calls),
    )


class ResponsesProvider:
    """OpenAI Responses API (/v1/responses) adapter, same chat() surface."""

    def __init__(self, endpoint, post=_post_json):
        self.endpoint = endpoint
        self._post = post

    @property
    def name(self):
        return self.endpoint.name

    def is_ready(self):
        return bool(self.endpoint.base_url)

    def chat(self, model_id, messages, *, max_tokens=2048, temperature=0.2,
             extra_body=None, tools=None, tool_choice=None, max_retries=2, sleep=None) -> ChatResult:
        body = responses_build_body(model_id, messages, max_tokens=max_tokens,
                                    temperature=temperature, tools=tools)
        if extra_body:
            body.update({k: v for k, v in extra_body.items() if v not in (None, "")})
        headers = {}
        if self.endpoint.api_key and self.endpoint.api_key.lower() != "no-auth":
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        if self.endpoint.extra_headers:
            headers.update(self.endpoint.extra_headers)
        url = self.endpoint.base_url.rstrip("/") + "/responses"
        try:
            resp = self._post(url, body, headers, self.endpoint.timeout)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(_redact(f"Responses API call failed ({self.endpoint.name}): {e}",
                                        [self.endpoint.api_key])) from e
        return responses_parse(resp, model_id, self.endpoint.name)
