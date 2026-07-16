"""Preflight model health gate.

After the router selects models per role, this probes the top-N candidates with a
TINY real chat call and records the outcome into route_health -- so the very next
`pick_top_n` routes AROUND models that don't actually work (router.py already skips
routes whose cooldown_factor < threshold). The point: never discover a dead model
mid-run ("blind waiting"); find it up front, fail over to an alternate PROVIDER for
the same model (e.g. deepseek->nvidia), then to the next-ranked model, and SHOW it.

Design decisions (researched, not guessed):
- Budget = 64 tokens, NOT a token or two. Reasoning models (DeepSeek-R1, o-series)
  spend the budget THINKING; a tiny budget makes them return empty + finish=length,
  which is a budget trap -- not a model failure. 64 leaves room to emit something.
- A model counts as WORKING if it returns non-empty `text` OR non-empty `reasoning`
  OR any tool_calls. A reasoning-only ping is still a reachable, working model.
- empty + finish_reason in {length, max_tokens} -> INCONCLUSIVE: do NOT record a
  failure (the false-positive guard for reasoning models).
- empty + finish_reason in {stop, end_turn, ""} (the real silent-failure case, e.g.
  a 200 OK with no content) -> record "empty".
- A ProviderError (400/402/429/5xx/...) is classified and recorded via the same
  `classify_provider_error` the live loop uses.

Pure logic + an injectable `chat` callable so it is fully unit-testable without
network. The only side effect is recording into the passed-in RouteHealth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..providers.openai_compat import ChatMessage, ProviderError
from .route_health import RouteHealth, classify_provider_error


# A tiny prompt that any chat model can answer in a token or two. We keep it
# trivial so the probe is cheap and so a healthy model produces *some* text.
_PING_MESSAGES = (ChatMessage("user", "Reply with the single word: OK"),)

# finish_reasons that mean "the model ran out of budget before answering" -- for a
# reasoning model on a tiny budget this is expected, NOT a failure.
_BUDGET_FINISH = frozenset({"length", "max_tokens", "model_length"})
# finish_reasons that mean "the model stopped normally" -- so an empty body here is
# a genuine silent failure (provider returned 200 with no content).
_TERMINAL_FINISH = frozenset({"stop", "end_turn", "eos", ""})


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one (model, provider) health probe."""
    role: str
    model_id: str
    provider: str
    status: str                  # "ok" | "empty" | "error" | "inconclusive"
    kind: str | None = None      # FailureKind when status == "error"/"empty"
    detail: str = ""
    latency_ms: int = 0

    @property
    def label(self) -> str:
        """Safe provider-only label for user-visible progress."""
        return self.provider


@dataclass(frozen=True)
class RolePreflight:
    """Per-role preflight summary: what was picked, what actually works."""
    role: str
    picked: str                                  # router's rank-1 model_id
    working: ProbeResult | None = None           # first model/provider that returned ok
    attempts: tuple[ProbeResult, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.working is not None


def _default_chat(adapter, model_id: str, *, max_tokens: int):
    """The real chat call. max_retries=0: failover is the router's job, not the
    adapter's -- we want each (model, provider) probed exactly once."""
    return adapter.chat(
        model_id, _PING_MESSAGES,
        max_tokens=max_tokens, temperature=0.0, max_retries=0,
    )


def _classify_result(result) -> tuple[str, str | None, str]:
    """Map a ChatResult to (status, kind, detail). Pure -- no I/O.

    Returns one of:
      ("ok", None, "")               -- text/reasoning/tool_calls present
      ("inconclusive", None, detail) -- empty but ran out of budget (reasoning trap)
      ("empty", "empty", detail)     -- empty with a terminal finish_reason (real silent fail)
    """
    text = (getattr(result, "text", "") or "").strip()
    reasoning = (getattr(result, "reasoning", "") or "").strip()
    tool_calls = getattr(result, "tool_calls", ()) or ()
    if text or reasoning or tool_calls:
        return ("ok", None, "")

    finish = (getattr(result, "finish_reason", "") or "").strip().lower()
    if finish in _BUDGET_FINISH:
        # Reasoning model burned the budget thinking; not a failure. Don't penalize.
        return ("inconclusive", None, f"empty but finish_reason={finish or '∅'} (budget trap)")
    # Empty with a terminal/normal finish -> genuine silent failure.
    return ("empty", "empty", f"200 OK but empty text (finish_reason={finish or '∅'})")


def preflight_roles(
    router,
    registry,
    route_health: RouteHealth | None,
    *,
    roles: Sequence[str] = ("planner", "executor", "reviewer"),
    per_role_candidates: int = 2,
    quality_bias: float = 0.8,
    max_tokens: int = 64,
    emit: Callable[[str, dict], None] | None = None,
    chat: Callable | None = None,
) -> list[RolePreflight]:
    """Probe the top candidates per role; record health; return per-role reports.

    - For each role, ask the router for the top `per_role_candidates` models.
    - For each candidate model, try its DISTINCT providers/keys (via
      registry.find_all_for_model), one per (provider,key), until one works.
    - Record success/failure into route_health so the next routing pick avoids
      the bad ones. Stop at the first working (model, provider) per role.
    - `emit(kind, payload)` surfaces each attempt live (the TUI renders it).
    - `chat` is injectable for tests: chat(adapter, model_id, max_tokens=) -> ChatResult.
    """
    # Local import to avoid a module cycle (router imports are heavy).
    from .router import TaskProfile

    do_chat = chat or _default_chat
    _emit = emit or (lambda kind, payload: None)
    reports: list[RolePreflight] = []

    for role in roles:
        top = router.pick_top_n(
            TaskProfile(role=role, quality_bias=quality_bias),
            n=max(1, per_role_candidates),
        )
        picked = top[0].model.id if top else "(none)"
        attempts: list[ProbeResult] = []
        working: ProbeResult | None = None

        for dec in top:
            model_id = dec.model.id
            endpoints = registry.find_all_for_model(model_id)
            if not endpoints:
                # Should not happen (router resolves providers), but be safe.
                pr = ProbeResult(role, model_id, dec.provider, "error",
                                 kind="network", detail="no provider endpoint")
                attempts.append(pr)
                _emit("preflight_probe", _probe_payload(pr))
                continue

            # Probe one configured endpoint at a time. Key-level failover is the
            # runtime loop's job; credential identifiers never enter progress data.
            seen: set[tuple[str, str]] = set()
            model_ok = False
            for ep in endpoints:
                api_key = getattr(ep, "api_key", "")
                ident = (ep.name, api_key[-6:] if api_key else "")
                if ident in seen:
                    continue
                seen.add(ident)

                t0 = time.time()
                try:
                    adapter = registry.adapter_for_endpoint(ep)
                    result = do_chat(adapter, model_id, max_tokens=max_tokens)
                except ProviderError as e:
                    kind = classify_provider_error(e)
                    dt = int((time.time() - t0) * 1000)
                    if route_health is not None:
                        route_health.record_failure(ep.name, model_id, kind, detail=str(e))
                    pr = ProbeResult(role, model_id, ep.name, "error",
                                     kind=kind, detail=str(e)[:160], latency_ms=dt)
                    attempts.append(pr)
                    _emit("preflight_probe", _probe_payload(pr))
                    continue
                except Exception as e:  # noqa: BLE001 -- a probe must never crash the run
                    dt = int((time.time() - t0) * 1000)
                    if route_health is not None:
                        route_health.record_failure(ep.name, model_id, "network", detail=str(e))
                    pr = ProbeResult(role, model_id, ep.name, "error",
                                     kind="network", detail=f"{type(e).__name__}: {e}"[:160],
                                     latency_ms=dt)
                    attempts.append(pr)
                    _emit("preflight_probe", _probe_payload(pr))
                    continue

                dt = int((time.time() - t0) * 1000)
                status, kind, detail = _classify_result(result)
                pr = ProbeResult(role, model_id, ep.name, status,
                                 kind=kind, detail=detail, latency_ms=dt)
                attempts.append(pr)
                _emit("preflight_probe", _probe_payload(pr))

                if status == "ok":
                    if route_health is not None:
                        route_health.record_success(ep.name, model_id)
                    working = pr
                    model_ok = True
                    break  # a working provider for this model is enough
                if status == "empty":
                    if route_health is not None:
                        route_health.record_failure(ep.name, model_id, "empty", detail=detail)
                    # try the next provider for the same model
                    continue
                # "inconclusive" (budget trap): do NOT record; try next provider but
                # don't hold it against the model either.
                continue

            if model_ok:
                break  # this role has a working model; don't probe lower-ranked ones

        report = RolePreflight(role=role, picked=picked, working=working,
                               attempts=tuple(attempts))
        reports.append(report)
        _emit("preflight_summary", {
            "role": role,
            "picked": picked,
            "working_model": working.model_id if working else None,
            "working_provider": working.provider if working else None,
            "healthy": report.healthy,
        })

    return reports


def _probe_payload(pr: ProbeResult) -> dict:
    return {
        "role": pr.role,
        "model": pr.model_id,
        "provider": pr.provider,
        "status": pr.status,
        "kind": pr.kind,
        "detail": pr.detail,
        "latency_ms": pr.latency_ms,
    }
