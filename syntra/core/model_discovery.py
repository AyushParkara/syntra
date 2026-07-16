"""Discover which models a provider API key actually unlocks.

After a key is added, query the provider's ``/models`` endpoint with that key (a REAL HTTP
GET, not a hardcoded list), then validate each returned model with a tiny real chat call so
we know it actually works (and whether it supports tools), and finally make the working ones
ROUTABLE — by unioning them into the provider's ``allowed_models`` AND adding a minimal
catalog row (in the user overlay) for any id the bundled catalog doesn't already know.

Why both: the router only ever picks models that are in the catalog, then re-checks the
provider's ``allowed_models``. A discovered id written to only one of the two is invisible to
routing — so discovery must touch both, and clearly FLAG which ids it newly catalogued.

Network I/O sits behind injectable seams (``transport`` for the GET, ``chat`` for the
validation ping) so the logic is fully unit-testable offline. Persistence is NOT done here —
``discover_for_endpoint`` only PLANS the delta; the caller writes providers.json + the overlay
with the pure helpers in registry/this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .catalog import _infer_tier


# ── data ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DiscoveredModel:
    """One model id returned by a provider's /models, plus any caps it advertised.
    0 / "" means UNKNOWN (the response didn't say) — never an invented number."""
    id: str
    raw: dict = field(default_factory=dict)
    context_window: int = 0
    max_output: int = 0
    price_input: float = 0.0     # USD per 1M tokens (converted from per-token when given)
    price_output: float = 0.0
    modality: str = ""


@dataclass(frozen=True)
class ValidationResult:
    model_id: str
    status: str                  # "ok" | "empty" | "error" | "inconclusive"
    tool_use: str = "unknown"    # "yes" | "no" | "unknown"
    detail: str = ""
    latency_ms: int = 0


# ── parse: model-agnostic /models response ─────────────────────────────────────
def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _per_token_to_per_million(v) -> float:
    """Providers report pricing per-token (a tiny float/str like '0.000003'); the catalog
    stores USD per 1M tokens. Convert; 0.0 when absent/garbage."""
    return _num(v) * 1_000_000.0


def parse_models_response(body: dict) -> list:
    """Parse an OpenAI-compatible ``/models`` body (``{"data":[{"id":...}]}``) OR a Gemini-style
    ``{"models":[{"name":...}]}`` into a list[DiscoveredModel]. Mines optional caps when the
    provider includes them (openrouter/deepseek/nvidia/xai use these standard fields); leaves
    them UNKNOWN otherwise. Pure; tolerant of garbage (returns [] rather than raising)."""
    if not isinstance(body, dict):
        return []
    rows = body.get("data")
    id_key = "id"
    if not isinstance(rows, list):
        rows = body.get("models")          # Gemini shape uses `models[].name`
        id_key = "name"
    if not isinstance(rows, list):
        return []
    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get(id_key) or row.get("id") or row.get("name")
        if not isinstance(mid, str) or not mid.strip():
            continue
        mid = mid.strip()
        # context window: several standard field names across providers
        ctx = (row.get("context_length") or row.get("context_window")
               or (row.get("top_provider") or {}).get("context_length") or 0)
        try:
            ctx = int(ctx or 0)
        except (TypeError, ValueError):
            ctx = 0
        max_out = (row.get("max_output_tokens") or row.get("max_tokens")
                   or (row.get("top_provider") or {}).get("max_completion_tokens") or 0)
        try:
            max_out = int(max_out or 0)
        except (TypeError, ValueError):
            max_out = 0
        pricing = row.get("pricing") or {}
        pin = _per_token_to_per_million(pricing.get("prompt")) if isinstance(pricing, dict) else 0.0
        pout = _per_token_to_per_million(pricing.get("completion")) if isinstance(pricing, dict) else 0.0
        modality = ""
        arch = row.get("architecture")
        if isinstance(arch, dict):
            modality = str(arch.get("modality") or "")
        if not modality:
            modality = str(row.get("modality") or "")
        out.append(DiscoveredModel(id=mid, raw=row, context_window=ctx, max_output=max_out,
                                   price_input=pin, price_output=pout, modality=modality))
    return out


# ── fetch: real authenticated GET {base_url}/models ─────────────────────────────
def _default_transport(req, timeout):
    """Issue the real GET and return the raw response body bytes. Isolated so tests inject
    a fake transport(req, timeout) -> bytes and never touch the network."""
    import urllib.request
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_models(endpoint, *, timeout: float = 8.0, transport=None):
    """GET ``{base_url}/models`` with the endpoint's bearer key + extra headers (mirrors the
    doctor probe). Returns ``(list[DiscoveredModel], error)`` where error is None on success or
    a short reason: "auth" (401/403), "billing" (402), "unsupported" (404 / not a models
    endpoint), "unreachable" (network), "http:<code>" (other). `transport(req, timeout) -> bytes`
    is injectable for tests."""
    import urllib.error
    import urllib.request

    base = (getattr(endpoint, "base_url", "") or "").rstrip("/")
    if not base:
        return [], "unreachable"
    url = base + "/models"
    headers = {"User-Agent": "syntra-discovery/0.1"}
    api_key = getattr(endpoint, "api_key", "") or ""
    if api_key and api_key.lower() != "no-auth":
        headers["Authorization"] = f"Bearer {api_key}"
    extra = getattr(endpoint, "extra_headers", None)
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers, method="GET")
    send = transport or _default_transport
    try:
        raw = send(req, getattr(endpoint, "timeout", None) or timeout)
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0)
        if code in (401, 403):
            return [], "auth"
        if code == 402:
            return [], "billing"
        if code == 404:
            return [], "unsupported"
        return [], f"http:{code}"
    except urllib.error.URLError:
        return [], "unreachable"
    except Exception:  # noqa: BLE001 - a discovery probe must never crash the caller
        return [], "unreachable"
    try:
        body = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError):
        return [], "unsupported"     # reachable but not a JSON models list
    return parse_models_response(body), None


# ── validate: a tiny REAL call confirms the key can actually call the model ─────
# A trivial 1-tool schema for the tool-use probe. We only check IF the model emits a
# tool_call, not whether it's "correct" — so the tool is a no-op echo.
_PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "noop",
        "description": "Return the given text unchanged.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}]


def _default_validate_chat(adapter, model_id, *, max_tokens, tools=None):
    """The real validation call. max_retries=0 so each model is probed exactly once."""
    from .preflight import _PING_MESSAGES
    kw = {"max_tokens": max_tokens, "temperature": 0.0, "max_retries": 0}
    if tools is not None:
        kw["tools"] = tools
    return adapter.chat(model_id, _PING_MESSAGES, **kw)


def validate_model(adapter, model_id: str, *, max_tokens: int = 64,
                   probe_tools: bool = True, chat=None) -> ValidationResult:
    """Probe ONE model with a tiny real chat call. Reuses preflight._classify_result so the
    reasoning-model budget trap (empty + finish_reason=length) is INCONCLUSIVE, not a failure.
    When ``probe_tools`` and the base ping is ok, a second tiny call with a trivial tool schema
    sets tool_use="yes" if the model emits a tool_call, "no" if it answers in text, "unknown"
    on error. ``chat(adapter, model_id, max_tokens=, tools=)`` is injectable for tests."""
    import time
    from .preflight import _classify_result
    from ..providers.openai_compat import ProviderError

    do_chat = chat or _default_validate_chat
    t0 = time.time()
    try:
        result = do_chat(adapter, model_id, max_tokens=max_tokens)
    except ProviderError as e:
        return ValidationResult(model_id, "error", detail=str(e)[:160],
                                latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:  # noqa: BLE001 - a validation probe must never crash discovery
        return ValidationResult(model_id, "error", detail=f"{type(e).__name__}: {e}"[:160],
                                latency_ms=int((time.time() - t0) * 1000))
    status, _kind, detail = _classify_result(result)
    latency = int((time.time() - t0) * 1000)

    tool_use = "unknown"
    if probe_tools and status == "ok":
        try:
            tr = do_chat(adapter, model_id, max_tokens=max_tokens, tools=_PROBE_TOOL)
            tool_use = "yes" if (getattr(tr, "tool_calls", ()) or ()) else "no"
        except TypeError:
            # the injected/real chat doesn't accept tools= → can't probe; leave unknown
            tool_use = "unknown"
        except Exception:  # noqa: BLE001 - tool probe failure shouldn't fail the model
            tool_use = "unknown"
    return ValidationResult(model_id, status, tool_use=tool_use, detail=detail, latency_ms=latency)


# ── build a minimal catalog row + overlay persistence ───────────────────────────
def build_minimal_catalog_row(dm: DiscoveredModel, provider: str,
                              vr: "ValidationResult | None" = None) -> dict:
    """Build a catalog row (the shape catalog.Model parses) for a discovered model. Caps come
    from the discovered metadata when known, else safe defaults (0/0.0) — NEVER invented.
    intelligence_index=0.0 so the router scores it LAST: a discovered stub never outranks a
    real curated model. tier is inferred from the id. notes flags it as auto-detected."""
    note = "auto-detected from provider /models"
    if vr is not None and vr.tool_use in ("yes", "no"):
        note += f"; tools={vr.tool_use}"
    if vr is not None and vr.status and vr.status != "ok":
        note += f"; probe={vr.status}"
    return {
        "id": dm.id,
        "provider": provider,
        "display_name": dm.id,
        "intelligence_index": 0.0,
        "speed_tps": 0.0,
        "price_input": float(dm.price_input or 0.0),
        "price_output": float(dm.price_output or 0.0),
        "context_window": int(dm.context_window or 0),
        "max_output": int(dm.max_output or 0),
        "specialties": [],
        "best_roles": [],
        "notes": note,
        "tier": _infer_tier(dm.id),
    }


def merge_overlay_row(overlay_raw: dict, row: dict) -> tuple:
    """Append ``row`` to ``overlay_raw['models']`` if its id isn't already there. PURE,
    deep-copies (input untouched). Returns ``(new_raw, added)``."""
    import copy
    new = copy.deepcopy(overlay_raw) if isinstance(overlay_raw, dict) else {"models": []}
    models = new.setdefault("models", [])
    rid = row.get("id")
    if not rid or any(isinstance(m, dict) and m.get("id") == rid for m in models):
        return new, False
    models.append(copy.deepcopy(row))
    return new, True


def write_catalog_overlay(path, overlay_raw: dict) -> None:
    """Atomically write the catalog overlay via the shared hardened primitive (#258): temp+fsync+
    os.replace + O_NOFOLLOW symlink refusal. Mode 0o644 — it holds NO secrets (unlike
    providers.json's 0o600). Creates parent dirs."""
    from pathlib import Path
    from . import fsutil
    p = Path(path).expanduser()
    fsutil.write_atomic(p, json.dumps(overlay_raw, indent=2), mode=0o644)


# ── orchestration: fetch → validate → plan the allowed/overlay delta ────────────
@dataclass(frozen=True)
class DiscoveryReport:
    provider: str
    fetched: tuple = ()                 # all ids the /models endpoint returned
    fetch_error: str | None = None      # None on success, else "auth"/"billing"/…
    validated_ok: tuple = ()            # ids whose tiny call worked
    validation: tuple = ()              # ValidationResult per probed id
    added_to_allowed: tuple = ()        # ids newly added to the provider's allowed_models
    uncatalogued_added: tuple = ()      # ids NOT in the catalog → need a new overlay row (FLAG)
    skipped_existing: tuple = ()        # ids already in the catalog (no overlay row needed)
    _discovered: tuple = ()             # the DiscoveredModel objects (for building overlay rows)


def discover_for_endpoint(endpoint, *, validate: bool = True,
                          catalog_ids: frozenset = frozenset(),
                          existing_allowed: tuple = (),
                          registry=None, transport=None, chat=None,
                          on_event=None) -> DiscoveryReport:
    """PLAN discovery for one endpoint (no file writes — the caller persists):
      1. GET /models with the key (fetch_models).
      2. optionally validate each id with a tiny real call (validate_model).
      3. compute: ids to add to allowed_models (fetched − existing_allowed) and the subset
         NOT in the catalog (need a minimal overlay row, flagged in uncatalogued_added).
    Streams on_event("discovery_model", {...}) per id and a final ("discovery_done", {...}).
    `registry` resolves the real chat adapter for validation (the live path); `transport`/`chat`
    are injectable for tests. When validate=True and neither chat nor a registry-resolvable
    adapter is available, validation is skipped (status stays 'fetched')."""
    provider = getattr(endpoint, "name", "") or ""
    emit = on_event or (lambda kind, payload: None)

    discovered, err = fetch_models(endpoint, transport=transport)
    if err:
        emit("discovery_done", {"provider": provider, "error": err,
                                "summary": f"could not list models for {provider}: {err}"})
        return DiscoveryReport(provider=provider, fetch_error=err)

    fetched_ids = tuple(dm.id for dm in discovered)
    existing = set(existing_allowed or ())
    validations: list = []
    validated_ok: list = []
    # resolve a real chat adapter for validation when no fake chat is injected (the live path);
    # tests pass chat= and skip this. If neither is available, don't validate (avoid a crash).
    adapter = None
    if validate and chat is None and registry is not None:
        try:
            adapter = registry.adapter_for_endpoint(endpoint)
        except Exception:  # noqa: BLE001 - can't build an adapter → skip validation, still discover
            adapter = None
    do_validate = validate and (chat is not None or adapter is not None)

    for dm in discovered:
        vr = None
        if do_validate:
            vr = validate_model(adapter, dm.id, chat=chat)
            validations.append(vr)
            if vr.status in ("ok", "inconclusive"):   # inconclusive = reasoning budget trap, treat as reachable
                validated_ok.append(dm.id)
        emit("discovery_model", {
            "provider": provider, "id": dm.id,
            "status": (vr.status if vr else "fetched"),
            "tool_use": (vr.tool_use if vr else "unknown"),
        })

    # allowed_models delta: everything fetched that isn't already allowed
    added_allowed = tuple(i for i in fetched_ids if i not in existing)
    # of those, the ones the catalog doesn't know need a minimal overlay row (FLAG)
    uncatalogued = tuple(i for i in added_allowed if i not in catalog_ids)
    skipped = tuple(i for i in fetched_ids if i in catalog_ids)

    rep = DiscoveryReport(
        provider=provider, fetched=fetched_ids, fetch_error=None,
        validated_ok=tuple(validated_ok), validation=tuple(validations),
        added_to_allowed=added_allowed, uncatalogued_added=uncatalogued,
        skipped_existing=skipped, _discovered=tuple(discovered),
    )
    emit("discovery_done", {
        "provider": provider,
        "summary": (f"{provider}: {len(fetched_ids)} models, "
                    f"{len(validated_ok)} validated, {len(uncatalogued)} newly catalogued"),
    })
    return rep
