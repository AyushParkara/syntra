"""Free / token-saver provider presets (Track T2, B2).

Quality-preserving starting points the user can add without hunting for base_urls.
Per our no-hardcoded-model-lists rule, a preset is a PROVIDER TEMPLATE (endpoint +
how to authenticate) — NOT a fixed list of model ids. Models are still discovered
dynamically from the catalog/provider, so presets never go stale or pin quality.

`list_presets()` -> names+notes for display; `preset_config(name)` -> a row ready
to validate (core.provider_validate) and write into providers.json.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    display_name: str
    base_url: str
    api_key_env: str
    note: str


# Endpoint templates only. `allowed_models` is intentionally omitted so discovery
# picks up whatever free/cheap models the provider currently offers.
_PRESETS: list[Preset] = [
    Preset(
        name="openrouter-free",
        display_name="OpenRouter (free tier)",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        note="OpenRouter exposes many ':free' models; discovery + routing pick them by quality.",
    ),
    Preset(
        name="groq",
        display_name="Groq (fast free tier)",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        note="Fast inference with a generous free tier; OpenAI-compatible.",
    ),
    Preset(
        name="ollama-local",
        display_name="Ollama (local, no key)",
        base_url="http://localhost:11434/v1",
        api_key_env="",
        note="Local models, zero cost; requires a running Ollama daemon.",
    ),
]


def list_presets() -> list[Preset]:
    return list(_PRESETS)


def preset(name: str) -> Preset | None:
    for p in _PRESETS:
        if p.name == name:
            return p
    return None


def preset_config(name: str) -> dict | None:
    """A providers.json row for a preset (api_key omitted -> set via env/no-auth)."""
    p = preset(name)
    if not p:
        return None
    row: dict = {"name": p.name, "display_name": p.display_name, "base_url": p.base_url}
    if p.api_key_env:
        row["api_key_env"] = p.api_key_env
    else:
        row["api_key"] = "no-auth"      # local endpoint
    return row
