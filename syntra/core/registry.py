"""Provider registry.

Loads the user's provider configuration from a config file and matches
catalog model ids to the endpoint that can serve them.

Config search order:
  1. $SYNTRA_PROVIDERS_FILE if set
  2. $XDG_CONFIG_HOME/syntra/providers.json (defaults to ~/.config/syntra/providers.json)
  3. ./.syntra/providers.json
  4. <package>/data/providers.example.json (placeholders, no live keys)

Config file shape (mirrors OpenAI-compatible gateways):

    {
      "providers": [
        {
          "name": "openrouter",
          "display_name": "OpenRouter",
          "base_url": "https://openrouter.ai/api/v1",
          "api_key_env": "OPENROUTER_API_KEY",     // or api_key directly
          "extra_headers": { "HTTP-Referer": "https://example.com" },
          "allowed_models": ["anthropic/claude-opus-4.5", "openai/gpt-5"]   // optional
        },
        { ... }
      ]
    }

A provider with no `allowed_models` is treated as a wildcard gateway that
can serve any model id. Provider order in the file is precedence order.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..providers.openai_compat import (
    OpenAICompatibleProvider,
    ProviderEndpoint,
    ProviderError,
)


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_USER_CONFIG = Path.home() / ".config" / "syntra" / "providers.json"
_REPO_LOCAL_CONFIG = Path.cwd() / ".syntra" / "providers.json"

# Folder-local-config trust gating. OFF by default (tests/library/non-CLI callers
# are unaffected); the CLI turns it on via enable_trust_enforcement() so an untrusted
# ./.syntra/providers.json isn't loaded without a one-time y/N confirmation.
_trust_enforced = False


def enable_trust_enforcement() -> None:
    """Turn on folder-local-config trust gating (the CLI calls this at startup)."""
    global _trust_enforced
    _trust_enforced = True


def _summarize_providers(path) -> list:
    """One 'name → base_url' line per provider in a config file (for the trust prompt)."""
    try:
        raw = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 - summary is best-effort
        return []
    return [f"{row.get('name', '?')} → {row.get('base_url', '(no base_url)')}"
            for row in raw.get("providers", [])]


def preflight_repo_local_trust(prompt):
    """If THIS folder has its own ./.syntra/providers.json (and you didn't pick an
    explicit config via SYNTRA_PROVIDERS_FILE), ask ONCE whether to trust it and
    remember the answer — trust OR decline — per folder. A trusted folder config
    then takes precedence over your global config for this folder; declining keeps
    the global. Re-asked if the file changes. Returns True/False (your decision) or
    None (not applicable). Call once at CLI startup, BEFORE curses, so the prompt
    uses plain input."""
    if os.environ.get("SYNTRA_PROVIDERS_FILE"):
        return None                       # explicit config chosen -> folder-local is moot
    repo = _REPO_LOCAL_CONFIG
    if not repo.exists():
        return None
    from .trust import trust_status, record_trust, record_decline
    status = trust_status(repo)
    if status != "unknown":
        return status == "trusted"        # already decided + file unchanged -> no prompt
    try:
        ok = bool(prompt(str(repo), _summarize_providers(repo)))
    except Exception:  # noqa: BLE001 - any prompt failure -> treat as declined
        ok = False
    (record_trust if ok else record_decline)(repo)
    return ok


def _example_config() -> Path:
    from .paths import providers_example_path
    return providers_example_path()


def default_config_path() -> Path:
    """Where `init` writes by default (user XDG config)."""
    return _DEFAULT_USER_CONFIG


def resolved_secrets_path(path: Path | str | None = None) -> Path:
    """The browser-login token store, kept beside the resolved providers config."""
    return ProviderRegistry._resolve_config_path(path).parent / "secrets.json"


def _default_token_lookup(secrets_dir: Path):
    """Build a token_lookup(name)->access_token backed by the chmod-600 store."""
    from .secrets import SecretStore
    store = SecretStore(secrets_dir / "secrets.json")

    def lookup(name: str):
        rec = store.get(name)
        return rec.access_token if rec else None

    return lookup


def _quarantine_corrupt_config(path: Path | str) -> tuple[str, str]:
    """#210: move a corrupt config aside to `<path>.corrupt` (preserved for the user),
    and report any restorable `<path>.bak`. Returns (quarantine_path, backup_path) as
    strings ("" when a step couldn't happen). Best-effort — never raises."""
    p = Path(path)
    quarantined = ""
    try:
        if p.exists():
            import os
            dst = p.with_suffix(p.suffix + ".corrupt")
            os.replace(str(p), str(dst))     # atomic move; keeps 0600 perms + exact bytes
            quarantined = str(dst)
    except OSError:
        quarantined = ""
    bak = p.with_suffix(p.suffix + ".bak")
    restorable = str(bak) if bak.exists() else ""
    return quarantined, restorable


def write_providers_config(path: Path | str, providers: list[dict],
                           *, overwrite: bool = False) -> Path:
    """Write a providers.json with chmod 600. Refuses to clobber unless overwrite.

    `providers` is a list of dicts like:
      {"name","display_name","base_url","api_key"|"api_key_env","allowed_models"?}
    The file is created 0600 (owner read/write only) so secrets aren't world/group
    readable (PLAN Section 6 #10). Returns the resolved path.
    """
    import os
    p = Path(path).expanduser()
    if p.exists() and not overwrite:
        raise ProviderRegistryError(f"refusing to overwrite existing config: {p} (use overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    # #210: rotate a backup of the CURRENT config before overwriting it, so a bad write /
    # typo / bad edit is recoverable (never a silent lose-the-old-config). The backup is
    # a byte copy kept at 0600 (it holds secrets too). Best-effort — a backup failure must
    # not block the write itself.
    if p.exists() and overwrite:
        bak = p.with_suffix(p.suffix + ".bak")
        try:
            bfd = os.open(str(bak), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(bfd, "wb") as bf:
                bf.write(p.read_bytes())
            os.chmod(str(bak), 0o600)
        except OSError:
            pass
    doc = {"providers": providers}
    # Shared hardened primitive (#258): temp+fsync+os.replace + O_NOFOLLOW symlink refusal, 0600
    # from creation (no brief world-readable window) — providers.json holds secrets.
    from . import fsutil
    fsutil.write_atomic(p, json.dumps(doc, indent=2), mode=0o600)
    return p


def remove_key_by_tail(raw: dict, provider: str, tail: str) -> tuple[dict, list[str], list[str]]:
    """Remove the API key(s) selected by `tail` from `provider` in a providers.json
    dict. PURE (no I/O) so it's fully testable. Returns (new_raw, removed, notes):
      - removed: human descriptions of what was removed (never the full key)
      - notes:   advisories (provider not found, env-managed keys, etc.)
    Handles literal `api_key` (str) and `api_keys` (list). Env-var fields
    (`api_key_env`/`api_key_envs`) hold VARIABLE NAMES, not the key, so they can't
    be matched by tail -> noted, not touched. Deep-copies so the input is untouched."""
    import copy
    tail = (tail or "").strip().lstrip("… ").strip()
    new = copy.deepcopy(raw)
    removed: list[str] = []
    notes: list[str] = []
    provs = new.get("providers", [])
    target = next((p for p in provs if p.get("name") == provider), None)
    if target is None:
        notes.append(f"provider {provider!r} not found in config")
        return new, removed, notes
    if not tail:
        notes.append("no credential suffix given")
        return new, removed, notes

    # api_keys: list -> drop matching elements
    if isinstance(target.get("api_keys"), list):
        kept = [k for k in target["api_keys"] if not (isinstance(k, str) and k.endswith(tail))]
        dropped = len(target["api_keys"]) - len(kept)
        if dropped:
            removed.append(f"removed {dropped} credential(s) from {provider}.api_keys")
            if kept:
                target["api_keys"] = kept
            else:
                target.pop("api_keys")   # don't leave an empty list
    # api_key: single string
    if isinstance(target.get("api_key"), str) and target["api_key"].endswith(tail):
        target.pop("api_key")
        removed.append(f"removed a credential from {provider}.api_key")
    # env-managed keys: can't match by tail (field holds the VAR NAME)
    if target.get("api_key_env") or target.get("api_key_envs"):
        notes.append(f"{provider} also has env-var key(s) (api_key_env*); those hold "
                     f"variable names, not the key — remove the env var yourself if needed")
    if not removed and not notes:
        notes.append(f"no matching literal credential found for {provider}")
    return new, removed, notes


def add_key(raw: dict, provider: str, key: str, *, base_url: str = "",
            display_name: str = "") -> tuple[dict, str, list[str]]:
    """Add an API key to `provider` in a providers.json dict. PURE (no I/O) so it's fully
    testable and never echoes the secret. Returns (new_raw, summary, notes):
      - summary: a human line describing what changed without credential text
      - notes:   advisories (created a new provider, key already present, missing base_url…)

    Keys accumulate in `api_keys` (the list the registry already rotates through for backups),
    so adding a second key to an existing provider gives it a fallback. A brand-new provider is
    created from (provider, base_url); without base_url it can't be reached, which is noted.
    Deep-copies so the input is untouched. Dedups — re-adding the same key is a no-op + note."""
    import copy
    new = copy.deepcopy(raw)
    notes: list[str] = []
    key = (key or "").strip()
    provider = (provider or "").strip()
    if not provider:
        return new, "", ["no provider name given"]
    if not key:
        return new, "", ["no key given"]
    new.setdefault("providers", [])
    target = next((p for p in new["providers"] if p.get("name") == provider), None)
    created = False
    if target is None:
        target = {"name": provider, "display_name": display_name or provider.title()}
        if base_url:
            target["base_url"] = base_url
        else:
            notes.append(f"new provider {provider!r} has no base_url — set one so it can be reached")
        new["providers"].append(target)
        created = True
    elif base_url and not target.get("base_url"):
        target["base_url"] = base_url

    # collect the keys already present (literal single + list), so we dedup and accumulate.
    existing = []
    if isinstance(target.get("api_key"), str) and target["api_key"]:
        existing.append(target["api_key"])
    if isinstance(target.get("api_keys"), list):
        existing.extend(k for k in target["api_keys"] if isinstance(k, str))
    if key in existing:
        return new, "", [f"that credential is already configured for {provider}"]

    # Normalize onto api_keys (the list form the rotator uses). Fold a pre-existing single
    # api_key into the list so both old + new are tried.
    merged = existing + [key]
    target.pop("api_key", None)
    target["api_keys"] = merged
    summary = (f"added credential to {provider}"
               + (" (new provider)" if created else f" ({len(merged)} key(s) total)"))
    return new, summary, notes


def add_allowed_models(raw: dict, provider: str,
                       model_ids) -> tuple[dict, list, list]:
    """Union `model_ids` into a provider's `allowed_models` list. PURE (no I/O), deep-copies
    so the input is untouched, dedups while preserving order. Returns (new_raw, added, notes):
      - added: the ids that were NEWLY added (already-present ones are skipped).
      - notes: advisories — notably the WILDCARD→GATED change: a provider with no
        `allowed_models` serves ANY model; once this field is written it ONLY serves what's
        listed. Callers surface that so the user can decline. Unknown provider / no ids = no-op."""
    import copy
    new = copy.deepcopy(raw)
    ids = [str(m).strip() for m in (model_ids or []) if str(m).strip()]
    if not (provider or "").strip() or not ids:
        return new, [], []
    target = next((p for p in new.get("providers", []) if p.get("name") == provider), None)
    if target is None:
        return new, [], [f"no provider {provider!r} to add models to"]
    notes: list = []
    was_wildcard = "allowed_models" not in target or target.get("allowed_models") in (None, [])
    existing = list(target.get("allowed_models") or [])
    seen = set(existing)
    added: list = []
    for mid in ids:
        if mid not in seen:
            existing.append(mid)
            seen.add(mid)
            added.append(mid)
    if not added:
        return new, [], notes
    target["allowed_models"] = existing
    if was_wildcard:
        notes.append(f"{provider} was a wildcard (served any model); it now serves ONLY its "
                     f"allowed_models list")
    return new, added, notes


@dataclass
class ProviderRegistry:
    endpoints: list[ProviderEndpoint]
    source_path: str
    # L6: user-declared local model specs (providers.json `local_models`). Parsed at load but
    # NOT spawned — a LocalModelManager.ensure()s a spec lazily on first use. Default-empty so
    # every existing ProviderRegistry(endpoints=..., source_path=...) call is unaffected.
    local_model_specs: list = field(default_factory=list)

    # ------------------------------------------------------------------ loaders

    @classmethod
    def load(cls, path: Path | str | None = None, *, token_lookup=None) -> "ProviderRegistry":
        """Load providers. `token_lookup(name) -> str|None` supplies a browser-login
        bearer token for a provider that has an `oauth` block and no static key
        (D1-D2); when it returns a token, that token becomes the endpoint's api_key."""
        config_path = cls._resolve_config_path(path)
        try:
            # #210: decode utf-8-SIG so a UTF-8 BOM (from a Windows editor) is stripped
            # rather than choking json.loads — a BOM'd config shouldn't brick startup.
            raw = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError as e:
            raise ProviderRegistryError(
                f"No provider config found. Tried: {config_path}. "
                f"Create {_DEFAULT_USER_CONFIG} or set SYNTRA_PROVIDERS_FILE."
            ) from e
        except json.JSONDecodeError as e:
            # #210: QUARANTINE the corrupt file (move it aside, preserved) instead of
            # leaving it in place — so the user keeps the broken copy to inspect AND a
            # fresh init won't refuse-to-overwrite it. Point them at the quarantine + any
            # rotated .bak they can restore.
            quarantined, restorable = _quarantine_corrupt_config(config_path)
            hint = ""
            if quarantined:
                hint = f" It has been quarantined to {quarantined}."
            if restorable:
                hint += f" A previous good config is available at {restorable} — restore it or run `syntra init`."
            else:
                hint += " Recreate it (e.g. `syntra init`) or fix the JSON."
            raise ProviderRegistryError(
                f"Provider config at {config_path} is not valid JSON: {e}.{hint}"
            ) from e
        except OSError as e:
            raise ProviderRegistryError(
                f"Could not read provider config at {config_path}: {e}."
            ) from e

        endpoints: list[ProviderEndpoint] = []
        for row in raw.get("providers", []):
            # F18: a hand-edited providers.json can omit a required key. Fail with a CLEAR
            # ProviderRegistryError naming the field, instead of a raw KeyError from the
            # row["name"]/row["base_url"] accesses below.
            if not isinstance(row, dict) or not row.get("name") or not row.get("base_url"):
                raise ProviderRegistryError(
                    f"Invalid provider entry in {config_path}: each provider needs a 'name' and "
                    f"'base_url'. Offending entry: {row!r}"
                )
            # Collect one or more API keys (precedence order) for this provider.
            # Supports: "api_key" (single), "api_key_env" (single from env),
            # "api_keys" (list of literals), "api_key_envs" (list of env vars).
            keys: list[str] = []
            single = row.get("api_key", "")
            single_env = row.get("api_key_env", "")
            if single:
                keys.append(single)
            # NOT elif: a provider may set both a literal key AND an env-var key
            # (e.g. primary inline, backup in env) — both should be available for failover.
            if single_env:
                v = os.environ.get(single_env, "")
                if v and v not in keys:
                    keys.append(v)
            for k in (row.get("api_keys") or []):
                if k and k not in keys:
                    keys.append(k)
            for env in (row.get("api_key_envs") or []):
                v = os.environ.get(env, "")
                if v and v not in keys:
                    keys.append(v)
            # Browser-login providers: no static key, but an oauth block + a stored token.
            if not keys and row.get("oauth"):
                if token_lookup is not None:
                    tok = token_lookup(row["name"]) or ""
                else:
                    # B6: auto-refresh a stored browser-login token if it has expired,
                    # using this provider's own oauth block (token_url + client_id).
                    from .oauth import config_from_oauth_block, ensure_fresh_token
                    from .secrets import SecretStore
                    _store = SecretStore(config_path.parent / "secrets.json")
                    tok = ensure_fresh_token(_store, row["name"],
                                             config_from_oauth_block(row["oauth"])) or ""
                if tok:
                    keys.append(tok)
            if not keys:
                keys.append("")  # keyless endpoint (e.g. local/no-auth)
            extra = row.get("extra_headers") or None
            allowed = row.get("allowed_models")
            # One endpoint per key. Multiple keys → same name, tried in order
            # (the loop fails over to the next key on quota exhaustion).
            endpoints.extend(ProviderEndpoint(
                name=row["name"],
                display_name=row.get("display_name", row["name"]),
                base_url=row["base_url"],
                api_key=key,
                credential_state=("no-auth" if key.lower() == "no-auth"
                                  else "keyed" if key else "missing"),
                timeout=float(row.get("timeout", 60.0)),
                extra_headers=extra,
                allowed_models=tuple(allowed) if allowed else None,
                kind=(row.get("kind") or "openai").lower(),
            ) for key in keys)
        # L6: parse (do NOT spawn) any user-declared local models. Best-effort — a malformed
        # local_models block must never brick provider loading.
        try:
            from .local_models import parse_local_model_specs
            specs = parse_local_model_specs(raw)
        except Exception:  # noqa: BLE001
            specs = []
        return cls(endpoints=endpoints, source_path=str(config_path), local_model_specs=specs)

    @staticmethod
    def _resolve_config_path(explicit: Path | str | None) -> Path:
        if explicit:
            return Path(explicit).expanduser()
        env = os.environ.get("SYNTRA_PROVIDERS_FILE")
        if env:
            return Path(env).expanduser()
        repo = _REPO_LOCAL_CONFIG
        # A TRUSTED folder-local config wins over the global (per-project config); an
        # untrusted/declined one is ignored. Gating is CLI-only (_trust_enforced);
        # OFF -> the original precedence, so tests/library callers are unaffected.
        repo_trusted = None
        if _trust_enforced and repo.exists():
            from .trust import is_trusted
            repo_trusted = is_trusted(repo)
            if repo_trusted:
                return repo
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            candidate = Path(xdg).expanduser() / "syntra" / "providers.json"
            if candidate.exists():
                return candidate
        if _DEFAULT_USER_CONFIG.exists():
            return _DEFAULT_USER_CONFIG
        if repo.exists() and repo_trusted is not False:
            return repo
        return _example_config()

    # --------------------------------------------------------------- queries

    def find_for_model(self, model_id: str) -> ProviderEndpoint | None:
        """Return the highest-precedence endpoint that can serve this model id."""
        for ep in self.endpoints:
            if ep.serves(model_id):
                return ep
        return None

    def find_all_for_model(self, model_id: str) -> list[ProviderEndpoint]:
        """All endpoints that can serve this model, in precedence order.

        When a provider has multiple API keys, each key is its own endpoint here,
        so the loop can fail over key→key (and provider→provider) on quota
        exhaustion. The first entry is the primary; the rest are backups.
        """
        return [ep for ep in self.endpoints if ep.serves(model_id)]

    def by_name(self, name: str) -> ProviderEndpoint | None:
        for ep in self.endpoints:
            if ep.name == name:
                return ep
        return None

    def ready_endpoints(self) -> list[ProviderEndpoint]:
        out = []
        for ep in self.endpoints:
            # An endpoint is "ready" if it has a base url. A key (or explicit "no-auth" for
            # local/self-hosted) is fine either way — F40: both branches used to append `ep`
            # identically, so it collapses to a single append after the base_url check.
            if not ep.base_url:
                continue
            out.append(ep)
        return out

    def all_served_model_ids(self) -> set[str]:
        """Union of every model id any endpoint can serve.

        Endpoints with no allowed_models (wildcard) contribute nothing to this
        set since we cannot enumerate "any model". Callers who want to allow
        wildcards must use find_for_model() per pick.
        """
        out: set[str] = set()
        for ep in self.endpoints:
            if ep.allowed_models:
                out.update(ep.allowed_models)
        return out

    # --------------------------------------------------------- adapter factory

    def get_adapter(self, model_id: str):
        endpoint = self.find_for_model(model_id)
        if endpoint is None:
            raise ProviderError(
                f"No configured provider can serve model id {model_id!r}. "
                f"Add it to {self.source_path} or remove the pin/route."
            )
        return self.adapter_for_endpoint(endpoint)

    def adapter_for_endpoint(self, endpoint: "ProviderEndpoint"):
        """Build the right adapter for a specific endpoint (one key). Used for
        key/provider failover where the caller picks which endpoint to try."""
        if not endpoint.base_url:
            raise ProviderError(
                f"Provider {endpoint.name} has no base_url configured."
            )
        if endpoint.kind == "anthropic":
            from .native_providers import AnthropicProvider
            return AnthropicProvider(endpoint)
        if endpoint.kind == "gemini":
            from .native_providers import GeminiProvider
            return GeminiProvider(endpoint)
        if endpoint.kind == "responses":
            from .native_providers import ResponsesProvider
            return ResponsesProvider(endpoint)
        return OpenAICompatibleProvider(endpoint)


class ProviderRegistryError(RuntimeError):
    """Provider config not found or malformed."""
