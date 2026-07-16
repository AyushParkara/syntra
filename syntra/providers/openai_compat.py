"""OpenAI-compatible chat provider.

Almost every modern LLM service exposes a `/chat/completions` endpoint that
accepts the OpenAI Chat Completions JSON shape. This single adapter speaks
that protocol against any base_url + api_key pair.

Used for: hosted gateways, native vendor endpoints with OpenAI-compatible
modes, self-hosted inference servers, local model servers. One class, many
endpoints, configured by the provider registry.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from ..core.redact import redact as _redact


# Statuses worth retrying on the SAME route before failing over (transient).
_RETRYABLE_STATUS = {429, 503}


def parse_retry_after(value, *, now: float | None = None, cap: float = 30.0) -> float:
    """Parse a Retry-After header into seconds-to-wait (bounded, never negative).

    Accepts an integer-seconds form ("5") or an HTTP-date form. Unparseable or
    missing -> 0.0. Result is clamped to [0, cap].
    """
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    # Integer seconds form.
    try:
        return max(0.0, min(cap, float(int(s))))
    except (TypeError, ValueError):
        pass
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime
        import time as _t
        dt = parsedate_to_datetime(s)
        if dt is None:
            return 0.0
        target = dt.timestamp()
        base = now if now is not None else _t.time()
        return max(0.0, min(cap, target - base))
    except Exception:
        return 0.0


@dataclass(frozen=True)
class ToolCall:
    """One tool/function call requested by the model (OpenAI tools shape)."""
    id: str
    name: str
    arguments: str           # raw JSON string of arguments, as the model emitted it


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    # assistant messages may carry tool calls; tool messages answer a call by id.
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    name: str = ""           # optional tool name (for tool-result messages)
    images: tuple[str, ...] = ()   # data:/http(s) image URLs for vision models


@dataclass(frozen=True)
class ChatResult:
    text: str
    input_tokens: int
    output_tokens: int
    model_id: str
    provider: str
    raw: dict | None = None
    # finish_reason from provider: "stop", "length", "content_filter", "tool_calls", etc.
    finish_reason: str = ""
    # tool calls the model requested this turn (empty when none).
    tool_calls: tuple[ToolCall, ...] = ()
    # Rate-limit signal from response headers, when the provider sends them.
    # {"remaining": int|None, "limit": int|None, "reset": str|None}. Lets the
    # caller switch keys PROACTIVELY before hitting a hard 429.
    rate_limit: dict | None = None
    # Chain-of-thought from reasoning models (DeepSeek `reasoning_content`,
    # OpenRouter `reasoning`), separate from `text`. "" when the model/provider
    # doesn't expose it. The TUI shows it as a dim, collapsible "thinking" block.
    reasoning: str = ""
    # Prompt-cache accounting (when the provider reports it). cache_read = input
    # tokens served from a warm cache (~0.1x price); cache_write = tokens written
    # to the cache this call (~1.25x). Both 0 when caching is off/unreported.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def parse_rate_limit_headers(headers) -> dict | None:
    """Extract common rate-limit headers into a small dict, or None if absent.

    Handles the de-facto standards used by OpenRouter, OpenAI, Anthropic:
      x-ratelimit-remaining[-requests|-tokens], x-ratelimit-limit*, x-ratelimit-reset*
    """
    if headers is None:
        return None
    def _get(*names):
        for n in names:
            try:
                v = headers.get(n) if hasattr(headers, "get") else None
            except Exception:
                v = None
            if v is not None:
                return v
        return None
    remaining = _get("x-ratelimit-remaining-requests", "x-ratelimit-remaining",
                     "X-RateLimit-Remaining-Requests", "X-RateLimit-Remaining")
    limit = _get("x-ratelimit-limit-requests", "x-ratelimit-limit",
                 "X-RateLimit-Limit-Requests", "X-RateLimit-Limit")
    reset = _get("x-ratelimit-reset-requests", "x-ratelimit-reset",
                 "X-RateLimit-Reset-Requests", "X-RateLimit-Reset")
    if remaining is None and limit is None and reset is None:
        return None
    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    return {"remaining": _int(remaining), "limit": _int(limit), "reset": reset}


class ProviderError(RuntimeError):
    """Provider call failed (network, auth, quota, malformed)."""


# #205/#163: recognize the "prompt is over the context window" 400 so the caller can
# re-compact + retry ONCE, instead of the generic-error path pointlessly failing over
# to another model (which has the same limit) and then killing the turn.
_OVERFLOW_PHRASES = (
    "context length exceeded", "context_length_exceeded",
    "maximum context length", "context window",
    "prompt is too long", "prompt too long",
    "too many tokens", "reduce the length",
    "exceed context", "exceeds the context",
    "input is too long", "too large for context",
)


def is_context_overflow_error(err: Exception) -> bool:
    """True when a provider error means the request exceeded the model's context window.

    Distinct from a rate limit (429) or a generic 5xx — an overflow is not fixed by
    retrying the same request or a different model; it's fixed by shrinking the prompt.
    Matched by phrase (providers word this many ways); deliberately does NOT fire on
    unrelated 400s (auth/tool-incapable/malformed) which carry their own signatures."""
    msg = str(err).lower()
    return any(p in msg for p in _OVERFLOW_PHRASES)


def parse_overflow_tokens(message: str) -> "tuple[int, int] | None":
    """Extract (have, limit) token counts from an overflow message when the provider
    includes them (e.g. 'prompt is too long: 137500 tokens > 135000 maximum'), so the
    caller can trim by the exact gap. Returns None when no such pair is present — the
    caller then falls back to a fractional trim. Pure."""
    import re
    m = re.search(r"(\d[\d,]{2,})\s*(?:tokens?)?\s*>\s*(\d[\d,]{2,})", message)
    if not m:
        return None
    try:
        have = int(m.group(1).replace(",", ""))
        limit = int(m.group(2).replace(",", ""))
    except ValueError:
        return None
    return (have, limit)


def repair_tool_pairing(messages: Sequence["ChatMessage"]) -> list["ChatMessage"]:
    """#204: make a message list tool-call-consistent before it hits the wire.

    OpenAI-compatible providers reject a request (400) unless every assistant
    ``tool_calls`` entry is answered by exactly one ``tool`` message with the
    matching ``tool_call_id``, and no ``tool`` message references an id that was
    never requested. A transcript replayed on resume/fork can violate this — the
    saved rollout may hold an assistant tool_use whose result was never persisted
    (the "session stuck / 400 on resume" class), or a duplicate/orphaned result.

    This pure pass repairs three defects, without mutating the input list or any
    message object (valid, already-paired lists pass through as the SAME objects):
      1. dangling tool_use  -> insert a synthetic error ``tool`` result right after
         the assistant message, so the model sees the call was answered;
      2. orphaned tool result (id never requested by a prior tool_call) -> drop it;
      3. duplicate tool result for one id -> keep the first, drop the rest.

    Order is preserved: a synthetic result is inserted immediately after the
    assistant message that requested it (before any later message).
    """
    # Every tool_call id the assistant has requested so far, in first-seen order.
    requested: set[str] = set()
    answered: set[str] = set()
    out: list["ChatMessage"] = []

    for m in messages:
        if m.role == "tool":
            tcid = m.tool_call_id
            if not tcid or tcid not in requested or tcid in answered:
                # orphaned (never requested) or a duplicate answer -> drop it.
                continue
            answered.add(tcid)
            out.append(m)
            continue

        if m.role == "assistant" and m.tool_calls:
            # Before recording this turn's requests, flush synthetic results for any
            # PRIOR requested-but-unanswered ids (a new assistant turn means the
            # provider will never see their real answer now).
            for pending in [i for i in requested if i not in answered]:
                out.append(_synthetic_tool_result(pending))
                answered.add(pending)
            out.append(m)
            for tc in m.tool_calls:
                requested.add(tc.id)
            continue

        # Any other message (user / plain assistant / system): before it, flush
        # synthetic results for still-unanswered tool calls so the assistant
        # tool_calls block is immediately followed by tool results on the wire.
        for pending in [i for i in requested if i not in answered]:
            out.append(_synthetic_tool_result(pending))
            answered.add(pending)
        out.append(m)

    # Trailing unanswered tool calls at end-of-list (nothing follows) still need
    # synthetic results, else the wire ends on a dangling tool_use.
    for pending in [i for i in requested if i not in answered]:
        out.append(_synthetic_tool_result(pending))
        answered.add(pending)

    return out


def _synthetic_tool_result(tool_call_id: str) -> "ChatMessage":
    """A placeholder tool result for a call whose real result was lost (resume/fork)."""
    return ChatMessage(
        role="tool",
        content="[tool result unavailable — the call did not complete or its result "
                "was not saved; treat as no output and continue]",
        tool_call_id=tool_call_id,
    )


def _serialize_message(m: "ChatMessage") -> dict:
    """OpenAI wire form for a ChatMessage, incl. tool_calls / tool results / images."""
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict = {"role": m.role, "content": m.content}
    # Vision: when images are attached, content becomes a multi-part array.
    if m.images:
        from ..core.multimodal import content_parts
        out["content"] = content_parts(m.content, m.images)
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in m.tool_calls
        ]
        # OpenAI requires content to be null (not "") when only tool_calls are present.
        if not m.content and not m.images:
            out["content"] = None
    return out


# ---- prompt caching (provider-aware, additive) ---------------------------------
# Providers whose OpenAI-compatible API passes Anthropic-style `cache_control`
# blocks through to the model. Data-driven + extensible. We ONLY inject the marker
# where it is known-safe, so every OTHER provider's request is byte-identical to
# before (their own automatic caching, e.g. DeepSeek/OpenAI, still applies untouched).
_CACHE_PASSTHROUGH_PROVIDERS = ("openrouter", "anthropic", "dashscope", "alibaba")


def _is_anthropic_family(model_id: str) -> bool:
    m = (model_id or "").lower()
    return "claude" in m or "anthropic" in m


def _cache_dialect(endpoint, model_id: str) -> str | None:
    """Prompt-cache dialect for this (endpoint, model), or None to leave the request
    untouched. Currently only the Anthropic `cache_control` block dialect — honored
    natively and passed through by OpenRouter/DashScope for Claude models. Killable
    via SYNTRA_NO_PROMPT_CACHE=1."""
    import os
    if os.environ.get("SYNTRA_NO_PROMPT_CACHE"):
        return None
    name = (getattr(endpoint, "name", "") or "").lower()
    kind = getattr(endpoint, "kind", "") or ""
    if _is_anthropic_family(model_id) and (
        kind == "anthropic" or any(p in name for p in _CACHE_PASSTHROUGH_PROVIDERS)
    ):
        return "anthropic"
    return None


def _apply_cache_breakpoints(serialized: list[dict], dialect: str | None) -> list[dict]:
    """Mark the STABLE PREFIX (the last system message) as a cache breakpoint in the
    given dialect. PURE — returns a new list; the input is untouched. The role system
    prompt is byte-identical across every call of that role, so caching it cuts the
    repeated planner/executor/reviewer input cost (40-80%). No-op when dialect is None
    or there is no system message."""
    if not dialect:
        return serialized
    idx = None
    for i, msg in enumerate(serialized):
        if msg.get("role") == "system":
            idx = i  # the LAST system message = the full stable instruction block
    if idx is None:
        return serialized
    out = [dict(m) for m in serialized]
    target = out[idx]
    content = target.get("content")
    if dialect == "anthropic":
        # OpenRouter/Anthropic openai-compat form: content becomes a parts array with
        # cache_control on the last text part.
        if isinstance(content, str):
            target["content"] = [{"type": "text", "text": content,
                                  "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            parts = [dict(p) for p in content]
            parts[-1] = {**parts[-1], "cache_control": {"type": "ephemeral"}}
            target["content"] = parts
    return out


def _parse_cache_tokens(usage: dict) -> tuple[int, int]:
    """(cache_read, cache_write) from a usage dict across provider shapes: Anthropic/
    OpenRouter use cache_read_input_tokens/cache_creation_input_tokens; OpenAI uses
    prompt_tokens_details.cached_tokens (read only)."""
    usage = usage or {}
    read = int(usage.get("cache_read_input_tokens", 0) or 0)
    write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    if not read:
        det = usage.get("prompt_tokens_details") or {}
        read = int(det.get("cached_tokens", 0) or 0)
    return read, write


def _parse_tool_calls(message: dict) -> tuple["ToolCall", ...]:
    """Extract tool calls from a response message (OpenAI tools shape)."""
    calls = message.get("tool_calls") or []
    out: list[ToolCall] = []
    for c in calls:
        c = c or {}                         # a provider may send a null element
        fn = c.get("function", {}) or {}
        out.append(ToolCall(
            id=c.get("id", ""),
            name=fn.get("name", ""),
            arguments=fn.get("arguments", "") or "",
        ))
    return tuple(out)


@dataclass(frozen=True)
class ProviderEndpoint:
    """Static description of one configured provider."""

    name: str            # short id used in config and routing
    display_name: str    # human label
    base_url: str        # full base, e.g. https://api.example.com/v1
    api_key: str         # bearer token; "" means none required
    timeout: float = 60.0
    extra_headers: dict[str, str] | None = None
    # If set, restricts which models this endpoint is allowed to serve.
    allowed_models: tuple[str, ...] | None = None
    kind: str = "openai"  # "openai" | "anthropic" | "gemini" -> selects the adapter

    def serves(self, model_id: str) -> bool:
        return self.canonical_id(model_id) is not None

    def canonical_id(self, model_id: str) -> str | None:
        """Translate a catalog model id to the provider-native model id.

        Returns the matched provider-side name, or the input unchanged for
        wildcard gateways, or None when this endpoint cannot serve the model.
        """
        if self.allowed_models is None:
            return model_id
        if model_id in self.allowed_models:
            return model_id
        # Also match without provider prefix (e.g. catalog "xai/grok-4-3" vs provider "grok-4.3")
        if "/" in model_id:
            short = model_id.split("/", 1)[1]
            if short in self.allowed_models:
                return short
        # Normalize dashes/dots/underscores for fuzzy match (e.g. grok-4-3 vs grok-4.3)
        def _norm(s: str) -> str:
            return s.replace("-", "_").replace(".", "_").lower()

        n = _norm(model_id)
        for am in self.allowed_models:
            if _norm(am) == n:
                return am
        if "/" in model_id:
            short_n = _norm(model_id.split("/", 1)[1])
            for am in self.allowed_models:
                if _norm(am) == short_n:
                    return am
        # #213: family / version-prefix WILDCARDS. Only entries that actually contain a
        # glob char are treated as patterns — a plain id stays an exact match (so
        # `openai/gpt-5` never matches `gpt-5-mini`). A match returns the model_id (the
        # provider serves it under its own id); patterns are matched against the full id
        # and the provider-prefix-stripped short id.
        import fnmatch
        short_id = model_id.split("/", 1)[1] if "/" in model_id else model_id
        for am in self.allowed_models:
            if ("*" in am or "?" in am or "[" in am) and (
                    fnmatch.fnmatchcase(model_id, am) or fnmatch.fnmatchcase(short_id, am)):
                return model_id
        return None


class OpenAICompatibleProvider:
    """Thin HTTP adapter for OpenAI-shaped chat completions."""

    def __init__(self, endpoint: ProviderEndpoint):
        self.endpoint = endpoint

    @property
    def name(self) -> str:
        return self.endpoint.name

    def is_ready(self) -> bool:
        # "no-auth" endpoints (local/self-hosted) are still ready.
        return bool(self.endpoint.base_url)

    def chat(
        self,
        model_id: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        extra_body: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice=None,
        max_retries: int = 2,
        sleep=None,
    ) -> ChatResult:
        if not self.endpoint.base_url:
            raise ProviderError(f"provider {self.endpoint.name}: base_url not set")

        canonical = self.endpoint.canonical_id(model_id)
        if canonical is None:
            raise ProviderError(
                f"provider {self.endpoint.name} cannot serve model {model_id!r}"
            )

        # #204: repair tool_use/tool_result pairing before the wire — a resumed/forked
        # transcript can carry a dangling tool_use (result never saved) or an orphaned/
        # duplicate tool result, which an OpenAI-compatible provider rejects with a 400.
        _paired = repair_tool_pairing(messages)
        _serialized = _apply_cache_breakpoints(
            [_serialize_message(m) for m in _paired],
            _cache_dialect(self.endpoint, canonical),
        )
        body = {
            "model": canonical,
            "messages": _serialized,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Tool/function-calling: only sent when the caller supplies tools, so
        # tool-less calls (chat, plan, review-without-tools) are byte-identical
        # to before and never confuse a tool-incapable model.
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        # Merge optional provider params (e.g. reasoning_effort) only when given,
        # so models that don't support them never receive the field.
        if extra_body:
            body.update({k: v for k, v in extra_body.items() if v not in (None, "")})

        headers = {
            "Content-Type": "application/json",
        }
        if self.endpoint.api_key and self.endpoint.api_key.lower() != "no-auth":
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        if self.endpoint.extra_headers:
            headers.update(self.endpoint.extra_headers)

        url = self.endpoint.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        if sleep is None:
            import time as _time
            sleep = _time.sleep

        attempt = 0
        rate_limit = None
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.endpoint.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    rate_limit = parse_rate_limit_headers(getattr(resp, "headers", None))
                break
            except urllib.error.HTTPError as e:
                # Transient 429/503: honor Retry-After and retry the SAME route a
                # bounded number of times before giving up (then failover happens
                # one level up in the loop).
                if e.code in _RETRYABLE_STATUS and attempt < max_retries:
                    hdr = e.headers.get("Retry-After") if e.headers else None
                    if hdr is not None:
                        wait = parse_retry_after(hdr)        # honor server hint (incl. 0)
                    else:
                        wait = min(8.0, 0.5 * (2 ** attempt))  # exponential backoff floor
                    attempt += 1
                    sleep(wait)
                    continue
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:500]
                except Exception:
                    pass
                # Redact in case the provider echoes our Authorization header / key.
                detail = _redact(detail, [self.endpoint.api_key])
                raise ProviderError(
                    f"HTTP {e.code} from {self.endpoint.name} ({url}): {detail}"
                ) from e
            except urllib.error.URLError as e:
                raise ProviderError(_redact(f"Network error from {self.endpoint.name}: {e}",
                                            [self.endpoint.api_key])) from e

        try:
            message = raw["choices"][0]["message"]
            text = message.get("content") or ""
        except (KeyError, IndexError) as e:
            raise ProviderError(
                f"Malformed response from {self.endpoint.name}: {raw!r}"
            ) from e
        # Non-streaming reasoning models return CoT in the message too.
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""

        tool_calls = _parse_tool_calls(message)

        usage = raw.get("usage", {}) or {}
        cache_read, cache_write = _parse_cache_tokens(usage)
        # Extract finish_reason from the first choice
        finish_reason = ""
        try:
            finish_reason = raw["choices"][0].get("finish_reason", "")
        except (KeyError, IndexError):
            pass
        return ChatResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_id=raw.get("model", model_id),
            provider=self.endpoint.name,
            raw=raw,
            finish_reason=finish_reason or "",
            tool_calls=tool_calls,
            rate_limit=rate_limit,
            reasoning=str(reasoning),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def generate_image(
        self,
        model_id: str,
        prompt: str,
        *,
        size: str = "1024x1024",
        timeout: float | None = None,
    ) -> bytes:
        """Generate an image from a text prompt; return the raw image BYTES.

        Two provider shapes are supported, tried in order, since the OpenAI-compatible fleet
        splits on this:
          A. `/images/generations` (OpenAI gpt-image / DALL·E, many gateways) → JSON with
             `data[0].b64_json` (gpt-image always b64) or `data[0].url` (DALL·E default).
          B. `/chat/completions` with `modalities:["image","text"]` (OpenRouter Gemini/Grok
             "image" models) → the image rides back as a data URL in
             `choices[0].message.images[0].image_url.url` — which is already our multimodal
             format. We try A first; on a 404/400 (endpoint absent) we fall back to B.
        Reuses the endpoint's auth + base_url; decodes b64 or fetches a returned URL to bytes."""
        canonical = self.endpoint.canonical_id(model_id) or model_id

        # ---- Path A: /images/generations ----
        try:
            raw = self._post_json("/images/generations",
                                  {"model": canonical, "prompt": prompt, "size": size,
                                   "n": 1, "response_format": "b64_json"},
                                  timeout=timeout)
            return self._image_bytes_from_response(raw)
        except ProviderError as e:
            # endpoint missing / bad request for this model → try the chat-modalities path.
            if not any(c in str(e) for c in ("HTTP 404", "HTTP 400", "HTTP 405")):
                raise

        # ---- Path B: chat-completions with image modality ----
        raw = self._post_json("/chat/completions",
                              {"model": canonical,
                               "messages": [{"role": "user", "content": prompt}],
                               "modalities": ["image", "text"]},
                              timeout=timeout)
        return self._image_bytes_from_response(raw)

    def _image_bytes_from_response(self, raw: dict) -> bytes:
        """Pull image bytes out of either response shape (b64_json / url / chat-image data URL)."""
        import base64 as _b64
        # Path A shape
        data = raw.get("data")
        if isinstance(data, list) and data:
            d0 = data[0] or {}
            if d0.get("b64_json"):
                return _b64.b64decode(d0["b64_json"])
            if d0.get("url"):
                return self._fetch_url_bytes(d0["url"])
        # Path B shape: choices[0].message.images[0].image_url.url (data: or http URL)
        try:
            imgs = raw["choices"][0]["message"].get("images") or []
            if imgs:
                u = imgs[0].get("image_url", {}).get("url", "")
                if u.startswith("data:"):
                    return _b64.b64decode(u.split(",", 1)[1])
                if u:
                    return self._fetch_url_bytes(u)
        except (KeyError, IndexError, TypeError):
            pass
        raise ProviderError(f"{self.endpoint.name}: no image in response: {str(raw)[:300]}")

    def _fetch_url_bytes(self, url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=self.endpoint.timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"{self.endpoint.name}: could not fetch generated image URL: {e}") from e

    def _post_json(self, path: str, body: dict, *, timeout: float | None = None) -> dict:
        """POST JSON to base_url+path with the endpoint's auth; return parsed JSON. Shared by the
        image-gen paths (the chat() call has its own richer retry/usage handling)."""
        if not self.endpoint.base_url:
            raise ProviderError(f"provider {self.endpoint.name}: base_url not set")
        headers = {"Content-Type": "application/json"}
        if self.endpoint.api_key and self.endpoint.api_key.lower() != "no-auth":
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        headers.update(self.endpoint.extra_headers or {})
        url = self.endpoint.base_url.rstrip("/") + path
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.endpoint.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = _redact(e.read().decode("utf-8")[:300], [self.endpoint.api_key])
            except Exception:  # noqa: BLE001
                pass
            raise ProviderError(f"HTTP {e.code} from {self.endpoint.name} ({url}): {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(_redact(f"Network error from {self.endpoint.name}: {e}",
                                        [self.endpoint.api_key])) from e

    def count_tokens(self, model_id: str, text: str, *, post=None) -> "int | None":
        """#166: ask the provider for an ACCURATE token count of `text`, or None if it can't.

        Tries the provider's count-tokens endpoint (`/messages/count_tokens`, the OpenAI-adjacent
        convention) and reads the common response shapes (`input_tokens` / `totalTokens` /
        `total_tokens` / `tokens`). Returns None — never raises — when there's no base_url, the
        endpoint doesn't exist (404), the network fails, or the body has no recognizable count,
        so the caller (pricing.count_tokens) transparently falls back to the char heuristic.
        `post(path, body) -> dict` is injectable for testing (defaults to `self._post_json`)."""
        if not self.endpoint.base_url:
            return None
        _post = post or self._post_json
        body = {"model": model_id, "messages": [{"role": "user", "content": text}]}
        try:
            data = _post("/messages/count_tokens", body)
        except Exception:  # noqa: BLE001 - any failure => fall back to the heuristic
            return None
        if not isinstance(data, dict):
            return None
        for key in ("input_tokens", "totalTokens", "total_tokens", "tokens"):
            n = data.get(key)
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                return n
        return None

    def chat_stream(
        self,
        model_id: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        extra_body: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice=None,
        on_text=None,
        on_reasoning=None,
        open_lines=None,
    ) -> ChatResult:
        """Streaming chat: reassembles SSE deltas into a ChatResult, calling
        on_text(chunk) as text arrives and on_reasoning(chunk) as chain-of-thought
        arrives (reasoning models only). `open_lines(url, body, headers, timeout)`
        is injectable (returns an iterable of raw SSE lines) so this is testable
        without a network."""
        from ..core.streaming import parse_sse

        if not self.endpoint.base_url:
            raise ProviderError(f"provider {self.endpoint.name}: base_url not set")
        canonical = self.endpoint.canonical_id(model_id)
        if canonical is None:
            raise ProviderError(f"provider {self.endpoint.name} cannot serve model {model_id!r}")

        # #204: repair tool_use/tool_result pairing before the wire (see chat()).
        _paired = repair_tool_pairing(messages)
        body = {
            "model": canonical,
            "messages": _apply_cache_breakpoints(
                [_serialize_message(m) for m in _paired],
                _cache_dialect(self.endpoint, canonical),
            ),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if extra_body:
            body.update({k: v for k, v in extra_body.items() if v not in (None, "")})

        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.endpoint.api_key and self.endpoint.api_key.lower() != "no-auth":
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        if self.endpoint.extra_headers:
            headers.update(self.endpoint.extra_headers)
        url = self.endpoint.base_url.rstrip("/") + "/chat/completions"

        opener = open_lines or self._default_open_lines
        try:
            lines = opener(url, body, headers, self.endpoint.timeout)
            acc = parse_sse(lines, on_text=on_text, on_reasoning=on_reasoning)
        except urllib.error.HTTPError as e:
            detail = _redact((e.read().decode("utf-8")[:500] if hasattr(e, "read") else str(e)),
                             [self.endpoint.api_key])
            raise ProviderError(f"HTTP {e.code} from {self.endpoint.name} ({url}): {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(_redact(f"Network error from {self.endpoint.name}: {e}",
                                        [self.endpoint.api_key])) from e

        text = acc.text()
        tool_calls = acc.tool_calls()
        if not text.strip() and not tool_calls:
            raise ProviderError(f"{self.endpoint.name} returned empty stream for {model_id}")
        return ChatResult(
            text=text, input_tokens=0, output_tokens=0,
            model_id=canonical, provider=self.endpoint.name, raw=None,
            finish_reason=acc.finish_reason or ("tool_calls" if tool_calls else "stop"),
            tool_calls=tool_calls,
            reasoning=acc.reasoning_text(),
        )

    def _default_open_lines(self, url, body, headers, timeout):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return (line for line in resp)
