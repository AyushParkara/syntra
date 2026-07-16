"""User-facing error taxonomy (Phase 5).

The single source for turning an internal failure kind (route_health.FailureKind)
into a clear, actionable message + next-step hint. Keeps user-facing wording in
one place so messages stay consistent and never leak internals or secrets.

Pure + deterministic -> unit-tested.
"""

from __future__ import annotations

from .route_health import classify_provider_error


# kind -> (one-line explanation, next-step hint)
_TAXONOMY: dict[str, tuple[str, str]] = {
    "auth": (
        "authentication failed (the provider rejected your key)",
        "check the api key for this provider in your providers.json; run `syntra doctor`.",
    ),
    "quota": (
        "rate-limited (HTTP 429); the provider is throttling requests",
        "wait and retry, lower --quality-bias to use cheaper routes, or spread load across providers.",
    ),
    "billing": (
        "payment required / out of credits (HTTP 402)",
        "top up the provider balance or switch to a free-preset provider; syntra routes around it meanwhile.",
    ),
    "tls": (
        "TLS certificate problem reaching the provider (bad/altname/self-signed cert or raw-IP TLS)",
        "fix the provider base_url (use the correct hostname, not a raw IP) or its certificate; "
        "syntra routes around it and will NEVER silently disable certificate checks.",
    ),
    "tool_incapable": (
        "this model can't do tool/function calling, which the task needs",
        "pick a tool-capable model for this role; syntra avoids this route for tool tasks.",
    ),
    "server": (
        "the provider had a server error (5xx)",
        "usually transient; retry. syntra will fail over to another route if one is available.",
    ),
    "network": (
        "could not reach the provider (network error or timeout)",
        "check connectivity and the provider base_url; run `syntra doctor --probe`.",
    ),
    "warming": (
        "a local model is still loading (cold start)",
        "give it a moment — syntra waits for local models to finish loading instead of routing away; the next call will be fast.",
    ),
    "malformed": (
        "the provider returned a response syntra could not parse",
        "the model/endpoint may be misconfigured; try another model or check the base_url path.",
    ),
    "empty": (
        "the provider returned an empty reply",
        "often transient or a too-small token budget; syntra retries and escalates automatically.",
    ),
    "truncated": (
        "the reply was cut off (hit the output token limit)",
        "raise --max-output-tokens or use a model with a larger max_output.",
    ),
    "budget": (
        "the run hit its token/cost budget and stopped",
        "raise --max-tokens / --max-steps, or split the task into smaller goals.",
    ),
    "blocked": (
        "the action was blocked by a safety rule",
        "the command/path is dangerous or outside the workspace; revise it.",
    ),
    "approval-required": (
        "this action needs your approval before it can run",
        "re-run with --execute to be prompted, or --auto-approve (dangerous) to skip prompts.",
    ),
    "user_penalty": (
        "this route is disabled by your overrides",
        "see `syntra route-penalty` / `route-blacklist`, or clear the override.",
    ),
}

_FALLBACK = ("an unexpected error occurred", "run `syntra doctor` to check your setup.")


def explain_kind(kind: str) -> tuple[str, str]:
    """Return (message, hint) for a taxonomy kind. Unknown -> generic fallback."""
    return _TAXONOMY.get((kind or "").lower(), _FALLBACK)


def explain_provider_error(err: Exception) -> str:
    """Classify a provider error and format a one-paragraph user-facing message.

    Never includes secrets (the underlying ProviderError is already redacted at
    the provider layer); this only adds the friendly explanation + hint.
    """
    kind = classify_provider_error(err)
    message, hint = explain_kind(kind)
    return f"{message}\n  hint: {hint}"
