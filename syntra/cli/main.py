"""Syntra CLI.

Run without installing:

    python3 -m syntra verify
    python3 -m syntra catalog
    python3 -m syntra route planner

ponytail: 5.8K-line monolith. Each `syntra <subcommand>` should live in
`syntra/cli/commands/<name>.py` when the file exceeds 6K.

Commands:
    verify                            run all smoke checks at once
    catalog                           show model catalog
    catalog refresh                   pull artificialanalysis.ai numbers
    route <role>                      show what the router would pick + skipped candidates
    providers                         list configured providers
    run <goal>                        run one task end-to-end (planner -> executor -> reviewer)
    task <task-id>                    show stored state for a task
    tasks                             list all tasks
    route-health                      show route health records
    route-health-clear [provider]     clear route health (all or one provider)
    route-blacklist <model>           blacklist a model id (optional --provider X)
    route-penalty <model> <0..1>      penalize a model (optional --provider X)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import __version__
from ..core.catalog import Catalog, CatalogError
from ..core.router import Router, TaskProfile, NoModelAvailable
from ..core.state import TaskStore
from ..core.loop import Loop, LoopConfig
from ..core.registry import ProviderRegistry, ProviderRegistryError
from ..core.aa_refresh import refresh_catalog, AARefreshError
from ..core.route_health import RouteHealth
from ..core.overrides import Overrides
from ..core import pricing
from ..core.redact import redact as _redact_output
from ..providers.openai_compat import ProviderError


def _state_root() -> Path:
    explicit = os.environ.get("SYNTRA_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    # Global state by default (~/.config/syntra/) so sessions are accessible
    # from any directory. Set SYNTRA_STATE_DIR=.syntra for folder-local mode.
    global_dir = Path.home() / ".config" / "syntra" / "state"
    try:
        global_dir.mkdir(parents=True, exist_ok=True)
        return global_dir
    except OSError:
        return (Path.cwd() / ".syntra" / "state").resolve()


def _load_cost_mode() -> str:
    """T5: the persisted default cost mode (budget|im-a-millionaire|pennies), set via /mode."""
    from ..core import cost_modes
    return cost_modes.load_mode(_state_root())


def _load_context_relay() -> bool:
    """T6: the persisted context mode set via /context. True = the smart brief relay (default,
    context stays flat as the chat grows); False = send the WHOLE conversation to every role
    (higher cost, nothing summarized). Stored as <state>/context_relay ("brief"|"full")."""
    try:
        return (_state_root() / "context_relay").read_text().strip() != "full"
    except OSError:
        return True


def _save_context_relay(relay: bool) -> None:
    """Persist the context mode ("brief" = relay on, "full" = whole conversation)."""
    try:
        root = _state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "context_relay").write_text("brief" if relay else "full")
    except OSError:
        pass


def _load_plan_review() -> bool:
    """Whether tasks PAUSE for plan approval before executing. ON by default (user: the plan
    should be vetted; turn it off from the approval modal — "Approve always" — or /plan-review).
    Stored as <state>/plan_review ("on"|"off"); absent → ON."""
    try:
        return (_state_root() / "plan_review").read_text().strip() != "off"
    except OSError:
        return True


def _save_plan_review(on: bool) -> None:
    """Persist plan-review across restarts ("Approve always" / /plan-review off → off)."""
    try:
        root = _state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "plan_review").write_text("on" if on else "off")
    except OSError:
        pass


_COMMIT_STYLES = ("off", "minimal", "neutral", "branded")


def _load_commit_style() -> str:
    """How the AGENT formats its git commit messages — the app-user's choice (asked once, then
    persisted). Values: off|minimal|neutral|branded. Absent → "" (UNSET → treated as off until the
    user picks, and the first agent commit asks). Stored as <state>/commit_style."""
    try:
        val = (_state_root() / "commit_style").read_text().strip()
        return val if val in _COMMIT_STYLES else ""
    except OSError:
        return ""


def _save_commit_style(style: str) -> None:
    """Persist the agent commit-message style across restarts (unknown value → ignored)."""
    if style not in _COMMIT_STYLES:
        return
    try:
        root = _state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "commit_style").write_text(style)
    except OSError:
        pass


def _catalog_path() -> Path:
    from ..core.paths import default_catalog_path
    return default_catalog_path()


def _route_health() -> RouteHealth:
    return RouteHealth(_state_root() / "route-health.json")


def _route_stats():
    from ..core.stats import RouteStats
    return RouteStats.load(_state_root() / "routes.json")


def _reliability_label(stats, role: str, provider: str, model_id: str) -> str:
    """Human reliability signal for a route, from learned stats (demo UX, §2)."""
    rec = stats.records.get(f"{role}|{provider}|{model_id}")
    if rec is None or rec.samples < stats.min_samples:
        n = rec.samples if rec else 0
        return f"unknown ({n} run{'s' if n != 1 else ''})"
    rate = rec.pass_rate()
    band = "High" if rate >= 0.8 else "Medium" if rate >= 0.5 else "Low"
    return f"{band} ({rate*100:.0f}% over {rec.samples})"


def _print(s: str = "") -> None:
    # Never send credential-shaped values to terminal output or the TUI transcript.
    # Known provider keys are also redacted at provider/network error boundaries.
    safe = _redact_output(str(s))
    if _OUTPUT_SINK is not None:
        _OUTPUT_SINK(safe)
        return
    sys.stdout.write(safe + "\n")
    sys.stdout.flush()


# When set (e.g. by the TUI), all _print output is routed here instead of
# stdout so slash-command results are visible inside the curses screen.
_OUTPUT_SINK = None


def set_output_sink(sink) -> None:
    """Redirect _print to `sink(text)` (or back to stdout when sink is None)."""
    global _OUTPUT_SINK
    _OUTPUT_SINK = sink


import contextlib


@contextlib.contextmanager
def capture_output():
    """Capture all _print + raw print()/stdout into a list of lines.

    Used by the TUI to run a slash-command and show its output in the
    transcript instead of losing it behind the curses screen.
    """
    import io
    lines: list[str] = []
    prev = _OUTPUT_SINK
    set_output_sink(lambda s: lines.extend(str(s).split("\n")))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield lines
    finally:
        set_output_sink(prev)
        captured = buf.getvalue()
        if captured:
            lines.extend(captured.rstrip("\n").split("\n"))


def _build_router(cat: Catalog, registry: ProviderRegistry | None, overrides: Overrides | None, rh: RouteHealth | None) -> Router:
    def resolve(mid):
        # Resolve to the HEALTHIEST provider serving the model (not just the first),
        # so a model stays routable while any provider is healthy (matches loop._resolve).
        if not registry:
            return None
        eps = registry.find_all_for_model(mid)
        if not eps:
            return None
        if rh is None:
            return eps[0].name
        return max(eps, key=lambda ep: rh.cooldown_factor(ep.name, mid)).name
    blk = (lambda mid, prov: overrides.is_blacklisted(mid, prov)) if overrides else None
    cool = (lambda prov, mid: rh.cooldown_factor(prov, mid)) if rh else None
    pen = (lambda mid, prov: overrides.penalty_for(mid, prov)) if overrides else None
    specs = (lambda mid: overrides.extra_specialties(mid)) if overrides else None
    roles = (lambda mid: overrides.role_edits_for(mid)) if overrides else None
    pin = (lambda role: overrides.pinned_model_for(role)) if overrides else None
    return Router(
        cat,
        route_resolver=resolve,
        is_blacklisted=blk,
        cooldown_factor=cool,
        extra_penalty=pen,
        extra_specialties=specs,
        role_edits=roles,
        pinned_model=pin,
        cooldown_threshold=0.65,
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive config wizard: write a chmod-600 providers.json. Never echoes keys.
    With --agents, instead scaffolds an AGENTS.md project guide at the project root. [A]"""
    if getattr(args, "agents", False):
        from ..core.project_instructions import find_project_root, write_agents_md
        root = find_project_root(Path.cwd())
        wrote, msg = write_agents_md(root, force=getattr(args, "force", False))
        _print(("✓ " if wrote else "") + msg
               + ("\n  edit the TODOs to teach agents your build/test/conventions." if wrote else ""))
        return 0 if wrote else 1
    from ..core.registry import write_providers_config, default_config_path, ProviderRegistryError

    dest = Path(args.path).expanduser() if args.path else default_config_path()
    _print("=== syntra init ===")
    _print(f"This writes a provider config to: {dest}")
    _print("Keys are stored chmod-600 and never printed back. Ctrl-C to abort.")
    _print("")

    providers: list[dict] = []
    try:
        while True:
            name = input("provider name (blank to finish): ").strip()
            if not name:
                break
            base_url = input("  base_url (OpenAI-compatible /v1): ").strip()
            if not base_url:
                _print("  (base_url required; skipping this provider)")
                continue
            use_env = input("  read key from env var instead of storing it? [y/N] ").strip().lower() in ("y", "yes")
            row: dict = {"name": name, "display_name": name, "base_url": base_url}
            if use_env:
                env = input("  env var name (e.g. OPENROUTER_API_KEY): ").strip()
                row["api_key_env"] = env
            else:
                import getpass
                key = getpass.getpass("  api key (hidden; blank = no-auth/local): ").strip()
                row["api_key"] = key or "no-auth"
            allowed = input("  allowed_models (comma-separated; blank = any): ").strip()
            if allowed:
                row["allowed_models"] = [m.strip() for m in allowed.split(",") if m.strip()]
            # Validate (offline) and WARN — never block; it's the user's config (E4).
            from ..core.provider_validate import validate_provider_config
            problems = validate_provider_config(row)
            for prob in problems:
                _print(f"  warning: {prob}")
            providers.append(row)
            _print(f"  added {name} (key {'via env' if use_env else 'stored, hidden'})"
                   + ("  [has warnings]" if problems else ""))
            _print("")
    except (EOFError, KeyboardInterrupt):
        _print("\naborted.")
        return 1

    if not providers:
        _print("no providers entered; nothing written.")
        return 1

    try:
        out = write_providers_config(dest, providers, overwrite=args.force)
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        _print("hint: pass --force to overwrite, or choose a different --path")
        return 2
    _print(f"wrote {len(providers)} provider(s) to {out} (chmod 600)")
    _print('run `syntra doctor` to validate, then `syntra run "<goal>"`.')
    return 0


# --------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    """Deep foundation health check. Validates config, catalog hygiene,
    routing, and optionally probes endpoint reachability.

    Complements ``verify`` (which shows routing decisions). ``doctor``
    diagnoses *why* things might be broken and prints actionable fixes.
    """
    import platform

    problems: list[str] = []
    warnings: list[str] = []
    ok_items: list[str] = []

    def _ok(label: str, detail: str = "") -> None:
        msg = f"  [ok]   {label}"
        if detail:
            msg += f"  ({detail})"
        _print(msg)
        ok_items.append(label)

    def _warn(label: str, detail: str) -> None:
        msg = f"  [warn] {label}: {detail}"
        _print(msg)
        warnings.append(f"{label}: {detail}")

    def _fail(label: str, detail: str) -> None:
        msg = f"  [FAIL] {label}: {detail}"
        _print(msg)
        problems.append(f"{label}: {detail}")

    _print("================ syntra doctor ================")

    # 1. Python version
    vi = sys.version_info
    if vi >= (3, 10):
        _ok("python", f"{vi.major}.{vi.minor}.{vi.micro} on {platform.system()}")
    else:
        _fail("python", f"{vi.major}.{vi.minor}.{vi.micro} — requires >= 3.10")

    # 2. Catalog
    cat = None
    try:
        cat = Catalog.load(args.catalog_path or _catalog_path())
        _ok("catalog", f"{len(cat.models)} models loaded")
    except FileNotFoundError:
        _fail("catalog", f"file not found: {_catalog_path()}")
    except Exception as e:
        _fail("catalog", str(e))

    # 3. Providers
    registry = None
    try:
        registry = ProviderRegistry.load()
        ready = registry.ready_endpoints()
        keyed = sum(1 for e in ready if e.credential_state == "keyed")
        noauth = sum(1 for e in ready if e.credential_state == "no-auth")
        no_key = sum(1 for e in registry.endpoints if e.credential_state == "missing")
        _ok("providers", f"{len(registry.endpoints)} configured, {keyed} keyed, {noauth} no-auth")
        if no_key > 0:
            _warn("providers", f"{no_key} endpoint(s) have empty api_key (not 'no-auth', not set)")
        _print(f"         config: {registry.source_path}")
    except ProviderRegistryError as e:
        _fail("providers", str(e))
        _print("         hint: run `syntra init` or copy the packaged syntra/data/providers.example.json template")

    # 4. Overrides
    try:
        ov = Overrides.load()
        _ok("overrides", f"{len(ov.blacklists)} blacklists, {len(ov.penalties)} penalties")
    except Exception as e:
        _warn("overrides", str(e))
        ov = Overrides()

    # 5. Route health
    rh = _route_health()
    _ok("route-health", f"{len(rh.records)} routes tracked")

    # 6. Catalog hygiene — servable models must have specialties + context_window.
    # Servability is decided per-model via find_for_model() below (it also resolves wildcard
    # endpoints, which all_served_model_ids() can't enumerate), so no precomputed id set is needed.
    if cat and registry:
        missing_specs: list[str] = []
        missing_ctx: list[str] = []
        for m in cat.models:
            if not registry.find_for_model(m.id):
                continue  # not servable, skip
            if not m.specialties:
                missing_specs.append(m.id)
            if m.context_window <= 0:
                missing_ctx.append(m.id)
        if missing_specs:
            _warn("catalog-tags", f"{len(missing_specs)} servable model(s) missing specialties")
            for mid in missing_specs[:5]:
                _print(f"           - {mid}")
            if len(missing_specs) > 5:
                _print(f"           ... +{len(missing_specs) - 5} more")
        else:
            _ok("catalog-tags/specialties", "all servable models have specialties")

        if missing_ctx:
            _warn("catalog-tags", f"{len(missing_ctx)} servable model(s) have context_window=0 (unknown)")
            for mid in missing_ctx[:5]:
                _print(f"           - {mid}")
            if len(missing_ctx) > 5:
                _print(f"           ... +{len(missing_ctx) - 5} more")
        else:
            _ok("catalog-tags/context", "all servable models have known context_window")

    # 7. Routing — each role should produce a pick
    if cat:
        router = _build_router(cat, registry, ov, rh)
        for role in ("planner", "executor", "reviewer"):
            try:
                dec = router.pick(TaskProfile(role=role))
                _ok(f"route/{role}", f"{dec.model.id} via {dec.provider or '?'}")
            except NoModelAvailable as e:
                _fail(f"route/{role}", str(e))

    # 7b. Route-down reminders (C6): surface persistently-failing routes.
    if rh is not None:
        from ..core.route_health import route_down_reminder
        for rec in rh.all_records():
            reminder = route_down_reminder(rec)
            if reminder:
                _warn("route-down", reminder)

    # 8. --probe: bounded endpoint reachability
    if getattr(args, "probe", False) and registry:
        _print("")
        _print("---- probe (endpoint reachability) ----")
        import urllib.request
        import urllib.error

        for ep in registry.endpoints:
            url = ep.base_url.rstrip("/") + "/models"
            headers = {"User-Agent": "syntra-doctor/0.1"}
            if ep.api_key and ep.api_key.lower() != "no-auth":
                headers["Authorization"] = f"Bearer {ep.api_key}"
            if ep.extra_headers:
                headers.update(ep.extra_headers)
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
                _ok(f"probe/{ep.name}", f"{url} -> HTTP {status}")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    _fail(f"probe/{ep.name}", f"{url} -> HTTP {e.code} (auth problem)")
                elif e.code == 402:
                    _fail(f"probe/{ep.name}", f"{url} -> HTTP 402 (billing/quota)")
                elif e.code == 404:
                    # /models may not exist, but the server is reachable
                    _ok(f"probe/{ep.name}", f"{url} -> HTTP 404 (reachable, /models not supported)")
                else:
                    _warn(f"probe/{ep.name}", f"{url} -> HTTP {e.code}")
            except urllib.error.URLError as e:
                _fail(f"probe/{ep.name}", f"{url} -> {e.reason}")
            except Exception as e:
                _fail(f"probe/{ep.name}", f"{url} -> {type(e).__name__}: {e}")

    # 9. --probe-models: live preflight of the router's actual per-role picks.
    # Sends a tiny real chat to each role's top candidates ACROSS their providers,
    # records health (so future routing avoids dead models), and reports which work.
    if getattr(args, "probe_models", False) and registry and cat:
        _print("")
        _print("---- probe-models (preflight: do the picked models actually work?) ----")
        from ..core.preflight import preflight_roles
        router = _build_router(cat, registry, ov, rh)
        try:
            reports = preflight_roles(router, registry, rh)
        except Exception as e:  # noqa: BLE001
            _fail("probe-models", f"{type(e).__name__}: {e}")
            reports = []
        for rep in reports:
            for at in rep.attempts:
                if at.status == "ok":
                    _ok(f"probe-models/{rep.role}", f"{at.model_id} via {at.label} -> ok ({at.latency_ms}ms)")
                elif at.status == "inconclusive":
                    _ok(f"probe-models/{rep.role}", f"{at.model_id} via {at.label} -> {at.detail}")
                else:
                    # An attempt that failed but was RECOVERED (the role still found a
                    # working route) is a warning, not a hard problem -- failover did
                    # its job. Only a role with NO working route is a FAIL (below).
                    msg = f"{at.model_id} via {at.label} -> {at.kind or at.status}: {at.detail}"
                    (_warn if rep.healthy else _fail)(f"probe-models/{rep.role}", msg)
            if rep.healthy:
                w = rep.working
                _print(f"  => {rep.role}: using {w.model_id} via {w.label}")
            else:
                _fail(f"probe-models/{rep.role}", f"NO working model among top picks (picked {rep.picked})")

    # Summary
    _print("")
    if problems:
        _print(f"result: {len(problems)} problem(s) found")
        for p in problems:
            _print(f"  - {p}")
    elif warnings:
        _print(f"result: OK with {len(warnings)} warning(s)")
    else:
        _print("result: OK")
    _print("================ end doctor ================")
    return 1 if problems else 0


# --------------------------------------------------------------------- verify


def cmd_verify(args: argparse.Namespace) -> int:
    """One-shot health/sanity report. No API calls. Safe to run anytime."""
    ok = True
    _print("================ syntra verify ================")

    # Catalog
    try:
        cat = Catalog.load(args.catalog_path or _catalog_path())
        _print(f"catalog: OK  ({len(cat.models)} models from AA, dynamic scoring active)")
    except Exception as e:
        _print(f"catalog: FAIL  {e}")
        return 2

    # Providers
    try:
        registry = ProviderRegistry.load()
        ready = registry.ready_endpoints()
        keyed = sum(1 for e in ready if e.credential_state == "keyed")
        noauth = sum(1 for e in ready if e.credential_state == "no-auth")
        _print(f"providers: OK  ({len(registry.endpoints)} configured, {keyed} keyed, {noauth} no-auth)")
        _print(f"  config:  {registry.source_path}")
    except ProviderRegistryError as e:
        _print(f"providers: FAIL  {e}")
        registry = None
        ok = False

    # Overrides
    try:
        ov = Overrides.load()
        _print(f"overrides: OK  ({len(ov.blacklists)} blacklists, {len(ov.penalties)} penalties)")
        _print(f"  file:    {ov.source_path}")
    except Exception as e:
        _print(f"overrides: WARN  {e}")
        ov = Overrides()

    # Route health
    rh = _route_health()
    _print(f"route-health: OK  ({len(rh.records)} routes tracked)")
    _print(f"  file:    {rh.path}")

    # Router decisions for each role
    _print("")
    _print("---------------- routing ----------------")
    router = _build_router(cat, registry, ov, rh)
    for role in ("planner", "executor", "reviewer"):
        try:
            dec = router.pick(TaskProfile(role=role))
            _print(f"  {role:<9} -> {dec.model.id:<42} via {dec.provider or '(none)':<18} [{dec.strategy}]  score={dec.score}")
            if dec.deliberation:
                # Verify output should be readable: summarize skips by reason.
                no_provider = [s for s in dec.deliberation if s.reason == "no provider"]
                other = [s for s in dec.deliberation if s.reason != "no provider"]

                if no_provider:
                    msg = f"            skipped {len(no_provider)} candidates (no provider)"
                    if other:
                        msg += f"; {len(other)} other skips"
                    _print(msg)

                if other:
                    grouped: dict[str, list] = {}
                    for s in other:
                        grouped.setdefault(s.reason, []).append(s)
                    reasons = sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)
                    summary = ", ".join(f"{reason} ({len(items)})" for reason, items in reasons[:6])
                    more = f"; +{len(reasons) - 6} more reasons" if len(reasons) > 6 else ""
                    _print(f"            skips: {len(other)} total; {summary}{more}")
                    # Show a few concrete examples to make the summary actionable.
                    shown = 0
                    for reason, items in reasons[:4]:
                        for s in items[:2]:
                            if shown >= 8:
                                break
                            _print(f"            example {s.model_id:<40} reason: {s.reason}{(' / ' + s.detail) if s.detail else ''}")
                            shown += 1
                        if shown >= 8:
                            break
        except NoModelAvailable as e:
            _print(f"  {role:<9} -> NO MODEL AVAILABLE  ({e})")
            ok = False

    _print("")
    _print("================ end verify ================")
    _print(f"result: {'OK' if ok else 'PROBLEMS DETECTED'}")
    return 0 if ok else 1


# --------------------------------------------------------------------- catalog


def cmd_catalog(args: argparse.Namespace) -> int:
    cat = Catalog.load(args.catalog_path or _catalog_path())
    _print(f"Catalog: {len(cat.models)} models")
    _print(f"Source:  {cat.source[:100]}")
    _print("")
    _print(f"{'MODEL':40} {'INTEL':>5} {'TPS':>5} {'$IN':>6} {'$OUT':>6}  ROLES         SPECIALTIES")
    for m in sorted(cat.models, key=lambda x: -x.intelligence_index):
        roles = ",".join(m.best_roles)
        specs = ",".join(m.specialties[:5])
        _print(f"{m.id:40} {m.intelligence_index:>5g} {m.speed_tps:>5g} {m.price_input:>6.2f} {m.price_output:>6.2f}  {roles:<13} {specs}")
    _print("")
    _print("Role score weights (from catalog, no hardcoded lists):")
    for role, weights in cat.role_score_weights.items():
        items = [f"{k}={v}" for k, v in weights.items()]
        _print(f"  {role}: {', '.join(items)}")
    return 0


def cmd_catalog_refresh(args: argparse.Namespace) -> int:
    path = Path(args.catalog_path or _catalog_path())
    _print(f"refreshing {path}")
    if args.dry_run:
        _print("dry-run: no file will be written")
    try:
        result = refresh_catalog(catalog_path=path, dry_run=args.dry_run)
    except AARefreshError as e:
        _print(f"error: {e}")
        _print("hint: keep using the seeded catalog (works fine) or get a key at https://artificialanalysis.ai")
        return 2
    _print(f"  fetched {result.fetched} rows from artificialanalysis.ai")
    _print(f"  matched {result.matched_existing} existing models (numbers refreshed)")
    _print(f"  added   {result.added_new} new models (need manual role/specialty tags)")
    return 0


# --------------------------------------------------------------------- route


def cmd_route(args: argparse.Namespace) -> int:
    cat = Catalog.load(args.catalog_path or _catalog_path())
    profile = TaskProfile(
        role=args.role,
        quality_bias=args.quality_bias,
        needs_tool_use=args.needs_tool_use,
        needs_long_context=args.needs_long_context,
    )

    registry = None
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError:
        pass
    ov = Overrides.load()
    rh = _route_health()
    router = _build_router(cat, registry, ov, rh)

    try:
        dec = router.pick(profile)
    except NoModelAvailable as e:
        _print(f"error: {e}")
        return 2

    _print(f"role:     {dec.role}")
    _print(f"strategy: {dec.strategy}")
    if dec.deliberation:
        _print("deliberation (top of candidate list first):")
        for s in list(dec.deliberation)[:40]:
            extra = f" ({s.detail})" if s.detail else ""
            _print(f"  skip  {s.model_id:<42} {s.reason}{extra}")
        if len(dec.deliberation) > 40:
            _print(f"  ... (+{len(dec.deliberation) - 40} more skips)")
    _print(f"PICK ->   {dec.model.id}")
    _print(f"served:   {dec.provider or '(no provider configured)'}")
    _print(f"score:    {dec.score}")
    _print(f"reason:   {dec.reason}")
    return 0


# --------------------------------------------------------------------- run


# Approval/sandbox flag values are accepted in friendly dashed form on the CLI
# (`on-request`, `read-only`) but the engine's matrix uses underscores. [A]
def _norm_policy(value: str) -> str:
    return (value or "").strip().replace("-", "_")


def _parse_config_override(item: str) -> tuple[str, object]:
    """Parse one `-c key=value` override. The value is interpreted as JSON when it
    parses (so numbers/bools/lists/quoted strings work), else taken as a literal
    string. Returns (key, value). [A]"""
    if "=" not in item:
        raise ValueError(f"bad -c override (expected key=value): {item!r}")
    key, _, raw = item.partition("=")
    key, raw = key.strip(), raw.strip()
    if not key:
        raise ValueError(f"bad -c override (empty key): {item!r}")
    import json as _json
    try:
        val = _json.loads(raw)
    except Exception:
        val = raw
    return key, val


def _apply_config_overrides(config: LoopConfig, items) -> None:
    """Apply `-c key=value` overrides onto a LoopConfig, coercing each value to the
    target field's existing type. Unknown keys warn and are skipped (never crash a
    run). [A]"""
    import dataclasses as _dc
    fields = {f.name for f in _dc.fields(config)}
    for item in items or []:
        key, val = _parse_config_override(item)
        if key not in fields:
            _print(f"warning: -c unknown config key '{key}' (ignored)")
            continue
        cur = getattr(config, key)
        if isinstance(cur, bool):
            if not isinstance(val, bool):
                val = str(val).strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            try:
                val = int(val)
            except (TypeError, ValueError):
                pass
        elif isinstance(cur, float):
            try:
                val = float(val)
            except (TypeError, ValueError):
                pass
        elif isinstance(cur, tuple) and isinstance(val, list):
            val = tuple(val)
        setattr(config, key, val)


def _loop_config_from_args(args: argparse.Namespace, *, workspace: str,
                           web_backend=None, research: bool = False) -> LoopConfig:
    """Build a LoopConfig from a run/exec namespace. Shared by `run` and `exec` so
    both honor the same knobs. Optional/newer flags are read via getattr so older
    namespaces (session_dispatch, _run_args) keep working. [A]"""
    # `--model` is a convenience that pins all three roles when the per-role pins
    # aren't given (per-role flags still win).
    model = getattr(args, "model", "") or ""
    config = LoopConfig(
        quality_bias=getattr(args, "quality_bias", 0.8),
        pin_planner=(getattr(args, "planner", "") or model or ""),
        pin_executor=(getattr(args, "executor", "") or model or ""),
        pin_reviewer=(getattr(args, "reviewer", "") or model or ""),
        max_output_tokens=getattr(args, "max_output_tokens", 8192),
        max_steps=getattr(args, "max_steps", 20),
        max_tokens=getattr(args, "max_tokens", 500_000),
        max_repeated_failures=getattr(args, "max_repeated_failures", 2),
        wait_for_limits=float(getattr(args, "wait_for_limits", 0.0) or 0.0),  # #165: opt-in headless "wait out 429s"
        proof_only=getattr(args, "proof_only", False),
        reasoning=getattr(args, "reasoning", "") or "",
        constraints=tuple(getattr(args, "constraint", None) or ()),
        execute=getattr(args, "execute", False),
        auto_approve=getattr(args, "auto_approve", False),
        agent=getattr(args, "agent", False),
        stream=getattr(args, "stream", False),
        librarian=getattr(args, "learn", True),  # T12: CLI runs accumulate memory like the TUI (off via --no-learn)
        cost_mode=(getattr(args, "mode", "") or _load_cost_mode()),  # T5: flag wins, else persisted default
        context_relay=_load_context_relay(),  # T6: brief relay (default) vs full conversation (/context)
        permission_ask=_make_permission_ask() if getattr(args, "agent", False) else None,
        question_ask=_make_question_ask() if getattr(args, "agent", False) else None,
        # Git commit-message style — app-user's choice (asked once on the first agent commit when
        # interactive, else stays off), persisted + followed thereafter.
        commit_style=_load_commit_style(),
        commit_style_persist=_save_commit_style,
        clarify_ambiguous=getattr(args, "agent", False),  # F40: ask before guessing on a vague goal
        agent_brain=getattr(args, "agent_brain", "") or "",  # F44: any installed agent as the role brain
        mcp_clients=_spawn_mcp_clients(_configured_mcp_servers() + (getattr(args, "mcp", None) or [])),
        hooks=_load_lifecycle_hooks(),  # B5: pre/post tool-use lifecycle hooks from hooks.json
        lsp_client=_spawn_lsp_client(getattr(args, "lsp", "") or "", workspace),
        initial_images=_load_initial_images(getattr(args, "image", None) or []),
        verify_command=getattr(args, "verify_command", "") or "",
        direct_chat=not getattr(args, "no_direct", False),
        direct_quality_bias=getattr(args, "direct_bias", 0.4),
        plan_council=getattr(args, "council", 1),
        research=research,
        web_search=web_backend,
        approval_policy=_norm_policy(getattr(args, "approval_policy", "") or ""),
        sandbox_mode=_norm_policy(getattr(args, "sandbox", "") or ""),
    )
    _apply_config_overrides(config, getattr(args, "config_overrides", None) or [])
    return config


def cmd_run(args: argparse.Namespace) -> int:
    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())

    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        _print("hint: run `syntra init` or copy the packaged syntra/data/providers.example.json template")
        return 2

    rh = _route_health()
    ov = Overrides.load()

    # Pre-run cost estimate, grounded in the actual goal length (core/pricing.py).
    router = _build_router(cat, registry, ov, rh)
    try:
        planner_dec = router.pick(TaskProfile(role="planner", quality_bias=args.quality_bias))
        executor_dec = router.pick(TaskProfile(role="executor", quality_bias=args.quality_bias))
        reviewer_dec = router.pick(TaskProfile(role="reviewer", quality_bias=args.quality_bias))
    except Exception as e:
        _print(f"error: routing pre-check failed: {e}")
        return 2

    estimate = pricing.estimate_run(
        args.goal,
        planner=planner_dec.model,
        executor=executor_dec.model,
        reviewer=reviewer_dec.model,
        planner_provider=planner_dec.provider or "",
        executor_provider=executor_dec.provider or "",
        reviewer_provider=reviewer_dec.provider or "",
        expected_steps=args.expected_steps,
    )

    verbose = getattr(args, "verbose", False) or args.dry_run
    if verbose:
        _print("=== run plan ===")
        _print(f"goal: {args.goal}")
        _print(f"workspace: {args.workspace}")
        _print(f"state: {_state_root()}")
        _print("")
        _print(f"{'ROLE':9} {'MODEL':42} {'PROVIDER':16} {'CALLS':>5} {'~IN':>8} {'~OUT':>7} {'~$':>9}")
        for r in estimate.roles:
            _print(f"{r.role:9} {r.model_id:42} {(r.provider or '?'):16} {r.calls:>5} {r.input_tokens:>8} {r.output_tokens:>7} {r.cost_usd:>9.5f}")
        _print(f"{'TOTAL':9} {'':42} {'':16} {'':>5} {estimate.total_input_tokens:>8} {estimate.total_output_tokens:>7} {estimate.total_usd:>9.5f}")
        _print(f"estimated cost: ${estimate.total_usd:.5f}  (approx; assumes ~{estimate.expected_steps} steps. actuals in cost.json)")
        # Reliability signal per role from learned route stats (demo UX, §2).
        _stats = _route_stats()
        _print("reliability (from past runs):")
        for role, dec in (("planner", planner_dec), ("executor", executor_dec), ("reviewer", reviewer_dec)):
            _print(f"  {role:9} {dec.model.id:42} {_reliability_label(_stats, role, dec.provider or '', dec.model.id)}")
    else:
        # Quiet default: cost stays visible (Pillar 3) but as one compact line.
        _print(f"~${estimate.total_usd:.4f} est · working… (verbose off; /verbose or --verbose for details)")
    if args.dry_run:
        _print("(dry-run: stopping before any provider call)")
        return 0
    if not args.yes:
        try:
            ans = input("proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            _print("aborted.")
            return 0

    from ..core.search import build_search_backend
    _research = getattr(args, "deep_research", False)
    # E3/T2: /web dispatches a "search the web for: …" goal through this line-REPL path. It
    # previously got NO backend unless --agent/--research, so /web returned "no backend
    # configured". Build the backend for an explicit web-search goal too, not just agent mode.
    _is_web_goal = (getattr(args, "goal", "") or "").lower().startswith("search the web for")
    _web_backend = (build_search_backend()
                    if (getattr(args, "agent", False) or _research or _is_web_goal) else None)
    if _is_web_goal and not getattr(args, "agent", False):
        args.agent = True                     # the websearch tool runs in agent mode
    if _research:
        args.proof_only = True               # research grounds claims; flag uncited ones
    config = _loop_config_from_args(args, workspace=args.workspace,
                                    web_backend=_web_backend, research=_research)

    loop = Loop(catalog=cat, store=store, registry=registry, route_health=rh, overrides=ov,
                progress=(_run_progress if verbose else _quiet_progress),
                approval=_make_approval(args), route_stats=_route_stats())

    # Live steering (req F5): background stdin reader feeds the inbox the loop
    # polls between steps. Prefix '!' = instant steer; plain line = queued follow-up.
    steering = None
    if getattr(args, "steer", False):
        from ..core.steering import SteeringInbox
        steering = SteeringInbox()
        loop.steering = steering
        _start_steering_reader(steering)
        _print("steering ON: type a line + Enter to QUEUE a follow-up; prefix with '!' to inject INSTANTLY.")

    try:
        # Feed the Librarian's learned durable memory into the CLI run too, so `syntra run`
        # honors the SAME durable facts the interactive cockpit does (CLI --constraint flags
        # still merge on top).
        _sess_mem = _load_session_memory()
        if getattr(args, "autopilot", 1) and args.autopilot > 1:
            result = loop.autopilot(args.goal, workspace_root=args.workspace,
                                    config=config, max_iterations=args.autopilot,
                                    session_memory=_sess_mem)
        else:
            result = loop.run(args.goal, workspace_root=args.workspace, config=config,
                              session_memory=_sess_mem)
    except ProviderError as e:
        from ..core.errors import explain_provider_error
        _print(f"provider error: {explain_provider_error(e)}")
        return 3

    # T12: `syntra run` now ACCUMULATES durable memory like the interactive cockpit (was
    # inject-only). A single-shot CLI run has no rolling conversation, so we pass an ephemeral
    # state dict — only the learning-extraction half (B) of the librarian fires (the summary
    # refresh is a no-op with empty history). Honors the same novelty + cheap-model gates.
    _run_librarian({"history": [], "summary": "", "last_extract_sig": ""},
                   args.goal, result, loop, config, lambda *a, **k: None)

    return _print_run_result(result)


def _start_steering_reader(inbox) -> None:
    """Spawn a daemon thread that reads stdin lines into a SteeringInbox.

    Lines starting with '!' are instant steering; all other non-empty lines are
    queued follow-ups. Daemon thread: dies with the process when the run ends.
    Blocking readline is fine here; non-TTY/EOF just ends the thread cleanly.
    """
    import threading

    def _reader() -> None:
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("!"):
                    if inbox.steer(line[1:]):
                        _print("  [steer] instant instruction accepted")
                else:
                    if inbox.queue(line):
                        _print("  [steer] follow-up queued")
        except Exception:
            pass

    t = threading.Thread(target=_reader, name="syntra-steering", daemon=True)
    t.start()


def _make_approval(args):
    """Build an approval callback for execute-mode edits.

    Returns None when not in execute mode (loop stays propose-only). With
    --auto-approve the loop applies without asking, so this callback is only
    consulted for interactive approval: show the diff, prompt y/N per edit.
    """
    if not getattr(args, "execute", False):
        return None
    if getattr(args, "auto_approve", False):
        return None  # loop applies directly; no per-edit prompt

    def _approve(payload: dict) -> bool:
        _print("")
        _print(f"  proposed {payload['kind']} -> {payload['path']}")
        diff = payload.get("diff", "")
        for line in diff.splitlines()[:60]:
            _print(f"    {line}")
        if len(diff.splitlines()) > 60:
            _print("    ... (diff truncated)")
        try:
            ans = input(f"  apply this edit to {payload['path']}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        return ans in ("y", "yes")

    return _approve


def _run_progress(kind: str, payload: dict) -> None:
    """Shared progress renderer for run + resume."""
    if kind == "route":
        _print(f"  [route] {payload['role']:<8}  -> {payload['model']}")
        _print(f"          provider: {payload.get('provider', '?')}  strategy: {payload.get('strategy','?')}")
    elif kind == "retry":
        _print(f"  [retry] {payload['role']:<8}  failed -> trying {payload['next_model']} via {payload['next_provider']}")
        _print(f"          reason: {payload['error']}")
    elif kind == "provider_failover":
        model = payload.get("model", "?").split("/")[-1]
        _print(f"  [switch] {model} via {payload.get('from','?')} failed "
               f"({payload.get('kind','?')}) -> {payload.get('to','?')}")
    elif kind == "key_failover":
        model = payload.get("model", "?").split("/")[-1]
        _print(f"  [switch] {model} via {payload.get('from','?')} exhausted -> key {payload.get('to','?')}")
    elif kind == "capability_degrade":
        model = payload.get("model", "?").split("/")[-1]
        _print(f"  [degrade] {model} via {payload.get('provider','?')} rejected "
               f"{payload.get('capability','a feature')} -> retried without it")
    elif kind == "quality_reroute":
        model = payload.get("from", "?").split("/")[-1]
        _print(f"  [reroute] {model} response looked off ({payload.get('reason','low quality')}) "
               f"-> re-asking a different model")
    elif kind == "credential_help":
        prov = payload.get("provider", "?")
        if payload.get("kind") == "billing":
            _print(f"  [credits] a credential for {prov} is OUT OF CREDITS — add credits, or remove it:")
        else:
            _print(f"  [bad key] a credential for {prov} was REJECTED — fix it, or remove it:")
        _print(f"            syntra providers remove-key {prov} <key-suffix>")
    elif kind == "usage":
        cache = f"  cache✓{payload['cache_read']}" if payload.get("cache_read") else ""
        _print(f"  [usage] {payload['role']:<8}  {payload['model']}  in={payload['in']} out={payload['out']}{cache}  ${payload['usd']:.4f}")
    elif kind == "loop_halted":
        _print(f"  [HALT]  loop guard stopped the run: {payload['reason']}")
        _print(f"          tokens used: {payload.get('tokens_used', '?')}  at step: {payload.get('step_id', '?')}")
    elif kind == "verification":
        if not payload.get("passed", True):
            _print(f"  [GATE]  {payload['role']:<8}  {payload.get('step_id','')}  FAILED verification")
            for e in payload.get("errors", []):
                _print(f"          - {e}")
        elif payload.get("warnings"):
            _print(f"  [gate]  {payload['role']:<8}  {payload.get('step_id','')}  warnings: {', '.join(payload['warnings'])}")
    elif kind == "reasoning":
        _print(f"  [think] {payload['role']:<8}  {payload['model']}  reasoning_effort={payload['effort']}")
    elif kind == "steering":
        if payload.get("mode") == "instant":
            _print(f"  [steer] injected {payload['count']} instruction(s) into step {payload.get('step_id','?')}")
        else:
            _print(f"  [steer] {payload['count']} follow-up(s) queued -> added as new step(s)")
    elif kind == "edit":
        status = payload.get("status", "?")
        path = payload.get("path", "?")
        if status == "applied":
            _print(f"  [edit]  applied {path}  (checkpoint {payload.get('checkpoint_id','?')})")
        elif status == "proposed":
            _print(f"  [edit]  proposed {path}  (not applied -- propose-only)")
        else:
            _print(f"  [edit]  {status} {path}: {payload.get('reason','')}")
    elif kind == "verify_command":
        _print(f"  [verify] running: {payload['command']}")
    elif kind == "verify_result":
        mark = "PASS" if payload.get("ok") else "FAIL"
        _print(f"  [verify] {mark}  exit={payload.get('exit_code','?')}")
    elif kind == "repair":
        _print(f"  [repair] added step {payload.get('step_id','?')} -> resume to fix: {payload.get('reason','')}")
    elif kind == "autopilot":
        _print(f"  [auto]  pass {payload.get('iteration','?')}: still unmet {payload.get('unmet', [])} -> resuming")
    elif kind == "council":
        if "chosen" in payload:
            _print(f"  [council] picked {payload['chosen']} from {payload['of']} candidate plans")
        else:
            _print(f"  [council] plan from {payload.get('member','?')} ({payload.get('steps','?')} steps)")


def _quiet_progress(kind: str, payload: dict) -> None:
    """Default (verbose OFF): show only events the user must see; hide telemetry.

    Surfaces halts, edit proposals/approvals, real-verify failures, repair/auto
    steps, and steering acks. Suppresses route/usage/retry/reasoning/verification
    chatter (that's what --verbose / /verbose is for).
    """
    if kind == "loop_halted":
        _print(f"  [HALT] {payload.get('reason','?')}")
    elif kind == "edit":
        status = payload.get("status", "?")
        if status in ("applied", "proposed", "failed"):
            _print(f"  [edit] {status} {payload.get('path','?')}")
    elif kind == "verify_result" and not payload.get("ok"):
        _print(f"  [verify] FAIL (exit {payload.get('exit_code','?')})")
    elif kind == "repair":
        _print("  [repair] added a fix-up step")
    elif kind == "autopilot":
        _print(f"  [auto] retrying (pass {payload.get('iteration','?')})")
    elif kind == "steering":
        mode = payload.get("mode")
        _print(f"  [steer] {'injected now' if mode == 'instant' else 'queued'}")
    elif kind == "council" and "chosen" in payload:
        _print(f"  [council] picked best of {payload['of']} plans ({payload['chosen']})")


def _print_run_result(result) -> int:
    """Shared result renderer for run + resume. Returns the CLI exit code."""
    # THE ANSWER FIRST. The whole point: show the deliverable the user asked for,
    # not just background telemetry.
    done = [s for s in result.state.plan if s.status == "done" and (s.result or "").strip()]
    _print("")
    _print("================= answer =================")
    if not done:
        _print("(no output produced)")
    elif len(done) == 1:
        _print(done[0].result.strip())
    else:
        for s in done:
            _print(f"--- {s.id}: {s.description}")
            _print((s.result or "").strip())
            _print("")
    _print("==========================================")
    # Compact footer: status + cost, details on demand (no wall of telemetry).
    in_tok, out_tok = result.state.total_tokens()
    verdict_mark = "ok" if result.verdict == "pass" else f"NEEDS WORK ({result.verdict})"
    _print(f"[{verdict_mark}]  cost ${result.state.total_cost_usd():.4f}  "
           f"tokens {in_tok}+{out_tok}  task {result.state.task_id}")
    if result.issues:
        for i in result.issues:
            _print(f"  ! {i}")
    _print(f"details: syntra task {result.state.task_id}")
    return 0 if result.verdict == "pass" else 1


# ----------------------------------------------------------- exec (headless)


def _answer_from_state(state) -> str:
    """The run's deliverable as plain text: the done steps' raw results joined.
    Reads only persisted task state, so it works on a loaded task too (used by
    `/raw`)."""
    done = [s for s in state.plan if s.status == "done" and (s.result or "").strip()]
    if not done:
        return ""
    if len(done) == 1:
        return done[0].result.strip()
    return "\n\n".join(f"--- {s.id}: {s.description}\n{(s.result or '').strip()}" for s in done)


def _collect_answer(result) -> str:
    """The run's deliverable as plain text. Shared by `exec` (stdout / JSON answer)
    and schema validation. [A]"""
    return _answer_from_state(result.state)


class _JsonlStream:
    """Serialize a run to line-delimited JSON for automation (`exec --json`).

    One record per line: ``{"seq": N, "type": "...", ...}``. The engine's own event
    kinds are already generic, so progress events pass straight through as
    ``{"type": <kind>, "data": <payload>}``; terminal records use ``start`` /
    ``done`` / ``error`` / ``schema``. Writes to any file object (stdout in prod, a
    buffer in tests). [A]"""

    def __init__(self, out):
        self._out = out
        self._seq = 0

    def event(self, type_: str, **fields) -> None:
        import json as _json
        self._seq += 1
        rec = {"seq": self._seq, "type": type_}
        rec.update(fields)
        line = _json.dumps(rec, default=str, ensure_ascii=False)
        # #181: with ensure_ascii=False, U+2028 (LINE SEP) / U+2029 (PARA SEP) go out RAW
        # — valid JSON, but they split a record for JS / `splitlines()`-style NDJSON readers
        # (the second half then fails to parse). Escape them back to \u-form so every record
        # stays exactly ONE line, without paying full ASCII-escaping of all other unicode.
        if " " in line or " " in line:
            line = line.replace(" ", "\\u2028").replace(" ", "\\u2029")
        self._out.write(line + "\n")
        try:
            self._out.flush()
        except Exception:
            pass

    def progress(self, kind: str, payload: dict) -> None:
        self.event(kind, data=(payload or {}))


def _load_output_schema(path: str) -> dict:
    """Load a user-supplied JSON Schema file (fails fast on a bad path/JSON). [A]"""
    import json as _json
    p = Path(path)
    if not p.exists():
        raise ValueError(f"--output-schema file not found: {path}")
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"--output-schema is not valid JSON: {e}") from e


def _validate_output(answer: str, schema: dict) -> tuple[bool, str]:
    """Validate the run's final answer against a JSON Schema for automation/CI.

    The answer is expected to be JSON; we parse it then check the schema (full
    validation when `jsonschema` is installed, a minimal top-level-type check
    otherwise). Engine-side *constraining* (forcing the model to emit schema-valid
    JSON) is a NEEDS-FROM-B item; this is the CLI-side check. Returns (ok, message).
    [A]"""
    import json as _json
    try:
        obj = _json.loads(answer)
    except Exception as e:  # noqa: BLE001
        return False, f"final answer is not JSON: {e}"
    try:
        import jsonschema  # type: ignore
    except Exception:
        want = schema.get("type")
        if want == "object" and not isinstance(obj, dict):
            return False, "expected a JSON object at the top level"
        if want == "array" and not isinstance(obj, list):
            return False, "expected a JSON array at the top level"
        return True, "validated (basic; install jsonschema for full schema checks)"
    try:
        jsonschema.validate(obj, schema)
        return True, "schema OK"
    except jsonschema.ValidationError as e:  # type: ignore
        return False, f"schema mismatch: {e.message}"
    except jsonschema.SchemaError as e:  # type: ignore
        return False, f"invalid schema: {e.message}"


def cmd_exec(args: argparse.Namespace) -> int:
    """Non-interactive one-shot run for automation/CI: no TUI, no spend prompt.

    Prints the final answer to stdout, or with ``--json`` emits a line-delimited
    JSON event stream (one record per engine event + a terminal ``done``/``error``).
    Exit codes: 0 success, 1 verdict not pass, 2 setup/usage error, 3 provider
    error, 4 output-schema mismatch. [A]"""
    import sys as _sys
    jsonl = getattr(args, "json", False)
    stream = _JsonlStream(_sys.stdout) if jsonl else None

    def _fail(msg: str, code: int) -> int:
        if jsonl:
            stream.event("error", message=msg)
        else:
            _print(f"error: {msg}")
        return code

    workspace = getattr(args, "cd", "") or getattr(args, "workspace", "") or str(Path.cwd())

    # Load the output schema up front so a bad path fails before we spend anything.
    schema = None
    if getattr(args, "output_schema", ""):
        try:
            schema = _load_output_schema(args.output_schema)
        except ValueError as e:
            return _fail(str(e), 2)

    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        return _fail(str(e), 2)
    rh = _route_health()
    ov = Overrides.load()

    from ..core.search import build_search_backend
    research = getattr(args, "deep_research", False)
    web_backend = build_search_backend() if (getattr(args, "agent", False) or research) else None
    if research:
        args.proof_only = True

    try:
        config = _loop_config_from_args(args, workspace=workspace,
                                        web_backend=web_backend, research=research)
    except ValueError as e:
        return _fail(str(e), 2)
    if schema is not None:
        config.output_schema = schema   # constrain the answer at generation (degrades if unsupported)

    progress = (stream.progress if jsonl
                else (_run_progress if getattr(args, "verbose", False) else _quiet_progress))
    loop = Loop(catalog=cat, store=store, registry=registry, route_health=rh, overrides=ov,
                progress=progress, approval=_make_approval(args), route_stats=_route_stats())

    if jsonl:
        stream.event("start", goal=args.goal, workspace=workspace)
    try:
        result = loop.run(args.goal, workspace_root=workspace, config=config,
                          session_memory=_load_session_memory())
    except ProviderError as e:
        from ..core.errors import explain_provider_error
        return _fail(f"provider error: {explain_provider_error(e)}", 3)

    answer = _collect_answer(result)
    exit_code = 0 if result.verdict == "pass" else 1

    # Output-schema validation (automation contract).
    schema_ok, schema_msg = True, ""
    if schema is not None:
        schema_ok, schema_msg = _validate_output(answer, schema)
        if not schema_ok:
            exit_code = 4

    if jsonl:
        in_tok, out_tok = result.state.total_tokens()
        if schema is not None:
            stream.event("schema", ok=schema_ok, message=schema_msg)
        stream.event("done", verdict=result.verdict,
                     cost_usd=round(result.state.total_cost_usd(), 6),
                     tokens={"input": in_tok, "output": out_tok},
                     task=result.state.task_id, answer=answer,
                     issues=list(result.issues or []))
        return exit_code

    rc = _print_run_result(result)
    if schema is not None and not schema_ok:
        _print(f"[schema] FAILED: {schema_msg}")
        return 4
    return rc


def _git_diff(workspace: str, paths: list, staged: bool) -> tuple[str, str]:
    """Return (diff_text, error). Empty diff_text + empty error means a clean tree."""
    import subprocess
    cmd = ["git", "-C", workspace or ".", "diff"]
    if staged:
        cmd.append("--cached")
    if paths:
        cmd += ["--", *paths]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "", "git is not installed"
    except subprocess.TimeoutExpired:
        return "", "git diff timed out"
    if r.returncode != 0:
        return "", (r.stderr or "git diff failed").strip()
    return r.stdout, ""


def cmd_review(args: argparse.Namespace) -> int:  # [A]
    """Review the working-tree changes through the engine — a one-shot reviewer over
    `git diff`. Reuses the exec machinery, so --json / --output-schema / every run flag
    behave identically. Read-only by intent: the goal asks for findings, not edits.

    NEEDS-FROM-B: a dedicated engine review entrypoint (e.g. ``Loop.review(diff)``) would
    let this skip the full plan->execute->review cycle; today it composes the existing loop.
    """
    workspace = getattr(args, "cd", "") or getattr(args, "workspace", "") or str(Path.cwd())
    diff, err = _git_diff(workspace, list(getattr(args, "paths", []) or []),
                          getattr(args, "staged", False))
    if err:
        _print(f"error: {err}")
        return 2
    if not diff.strip():
        _print("no changes to review (clean working tree)"
               + (" — staged is empty; try without --staged" if getattr(args, "staged", False) else ""))
        return 0
    _CAP = 60_000   # keep a huge change from blowing the context window
    if len(diff) > _CAP:
        diff = diff[:_CAP] + "\n…[diff truncated]"
    args.goal = (
        "You are reviewing a code change. Find real correctness bugs, security issues, "
        "and quality problems — be specific with file and line. Do NOT modify any files; "
        "output findings only, most important first. If it looks good, say so plainly.\n\n"
        "```diff\n" + diff + "\n```")
    return cmd_exec(args)


def cmd_apply(args: argparse.Namespace) -> int:  # [A]
    """Apply a unified-diff patch file to the working tree, validated first. Pairs with
    `review` (review -> get a suggested patch -> apply). ``--check`` validates only.

    NEEDS-FROM-B: applying a *task's* proposed changes (rather than a patch file) needs an
    engine entrypoint that materializes a task's edits as a patch; this handles files today.
    """
    import subprocess
    workspace = getattr(args, "cd", "") or str(Path.cwd())
    patch = (getattr(args, "patch", "") or "").strip()
    if not patch:
        _print("usage: syntra apply <patch-file> [--cd DIR] [--check]")
        return 2
    p = Path(patch).expanduser()
    if not p.is_file():
        _print(f"error: patch file not found: {patch}")
        return 2
    base = ["git", "-C", workspace, "apply"]
    try:
        chk = subprocess.run(base + ["--check", str(p)], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        _print("error: git is not installed")
        return 2
    except subprocess.TimeoutExpired:
        _print("error: git apply timed out")
        return 1
    if chk.returncode != 0:
        _print("✗ patch does not apply cleanly:\n" + (chk.stderr or "").strip())
        return 1
    if getattr(args, "check", False):
        _print("✓ patch applies cleanly (check only — not applied)")
        return 0
    res = subprocess.run(base + [str(p)], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        _print("✗ apply failed:\n" + (res.stderr or "").strip())
        return 1
    _print(f"✓ applied {p.name} to {workspace}")
    return 0


# --------------------------------------------------------------- resume


def cmd_resume(args: argparse.Namespace) -> int:
    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())

    # No id given -> continue your MOST RECENT session, so you never have to copy the
    # hex id from the screen (a terminal where drag-copy doesn't work shouldn't block you).
    task_id = (args.task_id or "").strip()
    if not task_id:
        tasks_dir = _state_root() / "tasks"
        dirs = [d for d in tasks_dir.iterdir() if (d / "task.json").exists()] if tasks_dir.exists() else []
        if not dirs:
            _print("No saved sessions yet — nothing to resume.")
            return 2
        task_id = max(dirs, key=lambda d: d.stat().st_mtime).name
        _print(f"Continuing your most recent session: {task_id}\n")

    try:
        state = store.load(task_id)
    except FileNotFoundError:
        _print(f"error: no task {task_id} under {_state_root()}")
        return 2

    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2

    rh = _route_health()
    ov = Overrides.load()

    done = sum(1 for s in state.plan if s.status == "done")
    _print("=== resume ===")
    _print(f"task_id:  {state.task_id}")
    _print(f"goal:     {state.goal}")
    _print(f"progress: {done}/{len(state.plan)} steps already done")
    if done == len(state.plan) and state.plan:
        _print("(all steps already done; will re-review only)")

    # Same ROOT-CAUSE FIX as the TUI /resume path: a resumed task that needs to read/view/run
    # something must take the tool-using agent phase, not the toolless text pipeline. Honor the
    # persisted access mode (Plan/Ask/Edit/Auto) and turn on auto_tools so _wants_tools() can
    # route a file/view/run task to tools — otherwise CLI `syntra resume` would refuse with
    # "I don't have file-system access tools" exactly like the TUI did.
    from ..core.access_modes import load_access_state
    _access = load_access_state(_state_root() / "access.json")
    _acc_policy, _acc_sandbox = _access.map_to_policy()
    config = LoopConfig(
        quality_bias=args.quality_bias,
        pin_planner=args.planner or "",
        pin_executor=args.executor or "",
        pin_reviewer=args.reviewer or "",
        max_output_tokens=args.max_output_tokens,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_repeated_failures=args.max_repeated_failures,
        proof_only=args.proof_only,
        reasoning=args.reasoning,
        constraints=tuple(getattr(args, "constraint", None) or ()),
        hooks=_load_lifecycle_hooks(),  # B5: pre/post tool-use lifecycle hooks on resume too
        auto_tools=True,
        auto_approve=_access.is_auto_approve(),
        approval_policy=_acc_policy,
        sandbox_mode=_acc_sandbox,
        access_mode=_access.mode,
        access_overrides=dict(_access.overrides),
    )

    loop = Loop(catalog=cat, store=store, registry=registry, route_health=rh, overrides=ov,
                progress=_run_progress, route_stats=_route_stats())
    try:
        result = loop.resume(task_id, config=config,
                             session_memory=_load_session_memory())
    except ProviderError as e:
        from ..core.errors import explain_provider_error
        _print(f"provider error: {explain_provider_error(e)}")
        return 3

    return _print_run_result(result)


# --------------------------------------------------------------------- providers / tasks


def cmd_providers(args: argparse.Namespace) -> int:
    # --free: show the quality-preserving free/token-saver presets (B2).
    if getattr(args, "free", False):
        from ..core.free_presets import list_presets
        _print("Free / token-saver presets (templates; models discovered dynamically):")
        _print(f"{'NAME':18} {'DISPLAY':28} {'BASE_URL':40} AUTH")
        for p in list_presets():
            auth = p.api_key_env or "no-auth (local)"
            _print(f"{p.name:18} {p.display_name[:28]:28} {p.base_url[:40]:40} {auth}")
            _print(f"  - {p.note}")
        _print("\nAdd one: copy the template into your providers.json (or `syntra init`), "
               "set its api key env var, then `syntra doctor`.")
        return 0
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    _print(f"Provider config: {registry.source_path}")
    _print(f"{'NAME':22} {'DISPLAY':28} {'BASE_URL':45} KEY    ALLOWED")
    for ep in registry.endpoints:
        if ep.credential_state == "no-auth":
            key_marker = "noauth"
        elif ep.credential_state == "keyed":
            key_marker = "yes"
        else:
            key_marker = "MISSING"
        allowed = "*" if ep.allowed_models is None else str(len(ep.allowed_models))
        _print(f"{ep.name:22} {ep.display_name[:28]:28} {ep.base_url[:45]:45} {key_marker:6} {allowed}")
    return 0


def cmd_local(args: argparse.Namespace) -> int:
    """L6: manage user-declared LOCAL model servers (providers.json `local_models`).

    `syntra local` / `local list`  -> list declared local models.
    `syntra local status`          -> which are declared (running state; nothing runs until used).
    `syntra local start <model_id>` -> spawn+register it now (health-polls until ready).
    """
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    specs = list(getattr(registry, "local_model_specs", []) or [])
    action = getattr(args, "local_action", None) or "list"

    if not specs and action in ("list", "status"):
        _print("No local models declared. Add a `local_models` array to your providers.json:")
        _print('  "local_models": [{"model_id": "qwen2.5-coder", '
               '"cmd": "llama-server -m ${MODEL_ID} --port ${PORT}", "port": 5801, "ttl_s": 600}]')
        _print(f"(config: {registry.source_path})")
        return 0

    if action in ("list", "status"):
        _print(f"Declared local models (config: {registry.source_path}):")
        _print(f"{'MODEL_ID':24} {'PORT':6} {'TTL_s':7} CMD")
        for s in specs:
            port = str(s.port) if s.port else "auto"
            ttl = str(int(s.ttl_s)) if s.ttl_s else "never"
            _print(f"{s.model_id[:24]:24} {port:6} {ttl:7} {s.cmd[:60]}")
        if action == "status":
            _print("\nState: not started (Syntra spawns a local model on first use, or via "
                   "`syntra local start <model_id>`).")
        else:
            _print("\nStart one now: `syntra local start <model_id>`  "
                   "(or just run a task — it spawns on demand).")
        return 0

    if action == "start":
        mid = getattr(args, "model_id", "")
        spec = next((s for s in specs if s.model_id == mid), None)
        if spec is None:
            _print(f"error: no local model {mid!r} declared. Run `syntra local` to see declared models.")
            return 2
        from ..core.local_models import LocalModelManager
        mgr = LocalModelManager()
        _print(f"Starting local model {mid!r} (cmd: {spec.cmd}) — waiting for readiness…")
        try:
            ep = mgr.ensure(spec, registry)
        except Exception as e:  # noqa: BLE001 - surface a spawn/readiness failure cleanly
            _print(f"error: could not start {mid!r}: {e}")
            return 2
        _print(f"ready: {ep.name} serving {mid} at {ep.base_url} (registered as a local provider).")
        _print("Note: this manager instance is per-invocation; a normal `syntra run` spawns/"
               "manages local models within its own session.")
        return 0

    _print(f"error: unknown `local` action {action!r}")
    return 2


def cmd_providers_remove_key(args: argparse.Namespace) -> int:
    """Remove an API key selected by a private suffix from a provider in
    providers.json. DRY-RUN by default; needs --yes to write. Backs up first and
    never prints credential text. This is the action the 'out of credits / bad key'
    alert suggests."""
    import json as _json, os as _os, shutil as _shutil, time as _time
    from ..core.registry import remove_key_by_tail
    path = ProviderRegistry._resolve_config_path(None)
    try:
        raw = _json.loads(Path(path).read_text())
    except Exception as e:  # noqa: BLE001
        _print(f"error: cannot read {path}: {e}")
        return 2
    new, removed, notes = remove_key_by_tail(raw, args.provider, args.tail)
    for n in notes:
        _print(f"  note: {n}")
    if not removed:
        _print("nothing to remove.")
        return 1 if notes else 0
    _print(f"would remove from {path}:")
    for r in removed:
        _print(f"  - {r}")
    if not args.yes:
        _print(f"\nDRY RUN (nothing changed). Apply with:\n"
               f"  syntra providers remove-key {args.provider} <key-suffix> --yes")
        return 0
    # Apply: back up, then atomic chmod-600 write of the FULL dict (preserve structure).
    bak = f"{path}.bak-{int(_time.time())}"
    _shutil.copy2(path, bak)
    tmp = Path(str(path) + ".tmp")
    fd = _os.open(str(tmp), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    try:
        with _os.fdopen(fd, "w") as f:
            _json.dump(new, f, indent=2)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _os.replace(str(tmp), str(path))
    try:
        _os.chmod(str(path), 0o600)
    except OSError:
        pass
    _print(f"removed. backup saved: {bak}")
    _print("run `syntra doctor --probe-models` to re-check routing.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Check PyPI for a newer Syntra and upgrade it. notify -> consent -> apply,
    or snooze ('remind me in N days'). Detects the install method so it upgrades
    correctly (uv tool / pipx / pip) or, on a dev checkout, tells you to update
    the source. `--check` only reports; `--yes` upgrades without prompting."""
    import subprocess as _sp, time as _time
    from ..core import updates as up
    root = _state_root()
    st = up.load_state(root)
    installed = up.installed_version()
    _print(f"Syntra {installed} (installed)")

    latest = up.latest_version()
    st.last_check_ts = _time.time()
    if latest:
        st.last_seen_latest = latest
    up.save_state(root, st)

    if latest is None:
        _print("Couldn't check for updates — offline, or Syntra isn't published to PyPI yet.")
        return 0
    if not up.is_newer(latest, installed):
        _print(f"Up to date (latest is {latest}).")
        return 0

    _print(f"Update available: {latest}  (you have {installed})")
    method = up.detect_install_method()
    cmd = up.upgrade_command(method)
    if cmd is None:
        _print(f"This is a '{method}' (dev) install — update your source checkout "
               f"(pull / re-sync). No package upgrade to run.")
        return 0
    if getattr(args, "check", False):
        _print(f"To upgrade: syntra update   (runs: {' '.join(cmd)})")
        return 0

    do_it = getattr(args, "yes", False)
    if not do_it:
        try:
            ans = input(f"Update now via {method}? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        do_it = ans in ("y", "yes")
    if not do_it:
        try:
            raw = input("No problem — remind me in how many days? [7] ").strip()
        except EOFError:
            raw = ""
        days = 7.0
        if raw:
            try:
                days = float(raw)
            except ValueError:
                days = 7.0
        up.snooze_for_days(st, days, _time.time())
        up.save_state(root, st)
        _print(f"OK — I'll remind you again in {days:g} day(s).")
        return 0

    _print(f"Running: {' '.join(cmd)}")
    try:
        rc = _sp.call(cmd)
    except FileNotFoundError:
        _print(f"error: '{cmd[0]}' not found — upgrade manually: {' '.join(cmd)}")
        return 2
    if rc == 0:
        _print(f"Updated to {latest}. Restart syntra to use it.")
    else:
        _print(f"Upgrade exited {rc}. Try manually: {' '.join(cmd)}")
    return rc


def _provider_oauth_config(provider: str):
    """Read a provider's `oauth` block from providers.json.
    ponytail: OAuth device-code flow (core/oauth.py) deleted. API keys cover 100% at v0.1.0."""
    return None


def cmd_login(args: argparse.Namespace) -> int:
    """Browser login for a provider via the OAuth device-code flow.
    ponytail: OAuth device-code flow (core/oauth.py) deleted. Use API keys instead."""
    _print("error: OAuth login was removed (over-engineered for v0.1.0). Use API keys in providers.json.")
    return 1
    if config is None:
        _print(f"error: provider {provider!r} has no usable 'oauth' block in providers.json.")
        _print("  add: \"oauth\": {\"device_auth_url\": ..., \"token_url\": ..., \"client_id\": ..., \"scope\": ...}")
        return 2

    store = SecretStore(resolved_secrets_path())
    if getattr(args, "refresh", False):
        rec = store.get(provider)
        if not rec or not rec.refresh_token:
            _print(f"error: no stored refresh token for {provider}; run `syntra login {provider}` first.")
            return 2
        try:
            new = DeviceLogin(config).refresh(rec.refresh_token)
        except Exception as e:  # noqa: BLE001
            _print(f"error: refresh failed: {e}")
            return 2
        store.set(provider, new)
        _print(f"refreshed token for {provider} (chmod-600 store).")
        return 0

    try:
        run_device_login(config, store, provider, emit=_print)
    except OAuthError as e:
        _print(f"error: {e}")
        return 2
    except KeyboardInterrupt:
        _print("\naborted.")
        return 1
    _print(f"run `syntra doctor` to confirm {provider} now routes.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Delete a provider's stored browser-login token."""
    from ..core.secrets import SecretStore
    from ..core.registry import resolved_secrets_path
    store = SecretStore(resolved_secrets_path())
    if store.delete(args.provider):
        _print(f"removed stored token for {args.provider}.")
        return 0
    _print(f"no stored token for {args.provider}.")
    return 1


def cmd_task(args: argparse.Namespace) -> int:
    store = TaskStore(_state_root())
    try:
        state = store.load(args.task_id)
    except FileNotFoundError:
        _print(f"error: no task {args.task_id} under {_state_root()}")
        return 2

    # --handoff shows the continuity handoff (regenerated fresh from typed state).
    if getattr(args, "handoff", False):
        from ..core import compaction as _compaction
        # Prefer the persisted file; fall back to regenerating from state.
        text = store.read_handoff(args.task_id) or _compaction.build_handoff(state)
        _print(text)
        return 0

    # --step requests a single step's full output
    if args.step:
        match = next((s for s in state.plan if s.id == args.step), None)
        if not match:
            _print(f"error: task {args.task_id} has no step {args.step}")
            _print(f"available: {', '.join(s.id for s in state.plan)}")
            return 2
        _print(f"step:        {match.id}")
        _print(f"status:      {match.status}")
        _print(f"description: {match.description}")
        if match.failure_reason:
            _print(f"failure:     {match.failure_reason}")
        _print("---")
        _print(match.result or "(no result)")
        return 0

    _print(f"task_id:   {state.task_id}")
    _print(f"goal:      {state.goal}")
    _print(f"status:    {state.status}")
    _print(f"plan:      {len(state.plan)} steps")
    for s in state.plan:
        marker = {"done": "+", "running": ".", "failed": "x", "pending": "-", "skipped": "~"}.get(s.status, "?")
        out_chars = len(s.result or "")
        _print(f"  [{marker}] {s.id}  {s.description[:70]}  ({out_chars} chars)")
    # Loop guard ledger (if present): shows halts + budget usage.
    ledger = store.load_loop_ledger(state.task_id)
    if ledger:
        if ledger.get("halted"):
            _print(f"loop:      HALTED ({ledger.get('halt_reason', '?')})  "
                   f"steps={ledger.get('steps_started', 0)}  tokens={ledger.get('tokens_used', 0)}")
        else:
            _print(f"loop:      ok  steps={ledger.get('steps_started', 0)}  tokens={ledger.get('tokens_used', 0)}")
    if state.failures:
        _print(f"failures:  {len(state.failures)} recorded attempt(s)")
    reports = store.load_verification(state.task_id)
    if reports:
        gate_fails = sum(1 for r in reports if not r.get("passed", True))
        warn_count = sum(len([f for f in r.get("findings", []) if f.get("severity") == "warning"]) for r in reports)
        _print(f"verify:    {len(reports)} report(s), {gate_fails} gate-fail(s), {warn_count} warning(s)")
    in_tok, out_tok = state.total_tokens()
    _print(f"tokens:    in={in_tok}  out={out_tok}")
    _print(f"cost:      ${state.total_cost_usd():.4f}")
    _print(f"dir:       {state.task_dir}")
    if state.summary:
        _print("---")
        _print(state.summary)
    _print("")
    _print(f"hint: `syntra task {state.task_id} --step <step-id>` to see full step output")
    return 0


def cmd_rollout(args: argparse.Namespace) -> int:
    """Replay a task's rollout, or branch a new task from a point in it."""
    from ..core import rollout
    root = _state_root()
    if getattr(args, "branch_at", None) is not None:
        new_id = rollout.branch(root, args.task_id, at=args.branch_at)
        _print(f"branched new task {new_id} from {args.task_id} at event #{args.branch_at}")
        _print(f"lineage: {' -> '.join(rollout.lineage(root, new_id))}")
        _print(f'resume/continue it with: syntra resume {new_id}')
        return 0
    _print(rollout.replay(root, args.task_id))
    chain = rollout.lineage(root, args.task_id)
    if len(chain) > 1:
        _print(f"\nlineage: {' -> '.join(chain)}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:  # [A]
    """Hide a session from the default listings/pickers without deleting it."""
    ok = TaskStore(_state_root()).set_archived(args.task_id, True)
    _print(f"✓ archived {args.task_id} — hidden from listings (`syntra unarchive {args.task_id}` to restore)"
           if ok else f"error: no task {args.task_id}")
    return 0 if ok else 2


def cmd_unarchive(args: argparse.Namespace) -> int:  # [A]
    """Restore an archived session to the default listings/pickers."""
    ok = TaskStore(_state_root()).set_archived(args.task_id, False)
    _print(f"✓ unarchived {args.task_id}" if ok else f"error: no task {args.task_id}")
    return 0 if ok else 2


def cmd_delete(args: argparse.Namespace) -> int:  # [A]
    """Permanently delete a session's stored state (confirms unless --yes)."""
    if not getattr(args, "yes", False):
        try:
            ans = input(f"permanently delete task {args.task_id} (cannot be undone)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            _print("aborted.")
            return 0
    ok = TaskStore(_state_root()).delete_task(args.task_id)
    _print(f"✓ deleted {args.task_id}" if ok else f"error: no task {args.task_id}")
    return 0 if ok else 2


def cmd_completion(args: argparse.Namespace) -> int:  # [A]
    """Print a shell-completion script for `syntra` (bash or zsh). Subcommand names are
    introspected from the parser so they stay in sync. Usage:
      bash: eval "$(syntra completion bash)"   ·   zsh: syntra completion zsh > _syntra"""
    shell = (getattr(args, "shell", "") or "bash").lower()
    names: list[str] = []
    for a in build_parser()._actions:
        if isinstance(a, argparse._SubParsersAction):
            names = sorted(a.choices.keys())
            break
    words = " ".join(names)
    if shell == "zsh":
        _print(f"#compdef syntra\n_arguments '1:command:({words})' '*::arg:->_args'")
    else:  # bash (default)
        _print(
            "_syntra_complete() {\n"
            '  local cur="${COMP_WORDS[COMP_CWORD]}"\n'
            '  if [ "$COMP_CWORD" -eq 1 ]; then\n'
            f'    COMPREPLY=( $(compgen -W "{words}" -- "$cur") )\n'
            "  fi\n"
            "}\n"
            "complete -F _syntra_complete syntra"
        )
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    store = TaskStore(_state_root())
    rows = store.list_tasks(include_archived=getattr(args, "all", False))
    if not rows:
        _print(f"(no tasks under {_state_root()})")
        return 0
    # A one-shot CLI runs one task then exits: a persisted "running" status means
    # the task was INTERRUPTED (process ended mid-run), not active now. Relabel so
    # the listing doesn't imply many things are concurrently running.
    rows = sorted(rows, key=lambda r: r.get("updated", 0), reverse=True)
    _print(f"{'TASK_ID':14} {'STATUS':12} GOAL")
    for r in rows:
        status = r.get("status", "?")
        shown = "interrupted" if status == "running" else status
        _print(f"{r['task_id']:14} {shown:12} {r['goal'][:78]}")
    interrupted = sum(1 for r in rows if r.get("status") == "running")
    if interrupted:
        _print(f"\n{interrupted} interrupted (ended mid-run) — `syntra resume <id>` to continue one.")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:  # [A]
    """List built-in + installed skills, show one's body, or rank them against a goal."""
    from ..core.plugin_loader import (bundled_skills, discover_plugins,
                                      get_skill, match_skills)
    name = (getattr(args, "name", "") or "").strip()
    if name:  # `syntra skills <name>` — show the full skill
        s = get_skill(name)
        if not s:
            _print(f"no skill named '{name}' (run `syntra skills` to list)")
            return 2
        _print(f"# {s.name}  ({s.plugin})")
        if s.description:
            _print(f"description: {s.description}")
        if getattr(s, "when_to_use", ""):
            _print(f"when to use: {s.when_to_use}")
        _print("")
        _print(s.content)
        return 0
    builtin = bundled_skills()
    plugin_skills = [s for p in discover_plugins() for s in p.skills]
    match = (getattr(args, "match", "") or "").strip()
    if match:  # `syntra skills --match "<goal>"` — implicit description matching
        hits = match_skills(match, builtin + plugin_skills)
        if not hits:
            _print(f"no skill matches: {match!r}")
            return 0
        _print(f"skills matching {match!r} (best first):")
        for s in hits:
            _print(f"  {s.name:24} {s.description[:60]}")
        return 0
    if builtin:
        _print("BUILT-IN:")
        for s in builtin:
            _print(f"  {s.name:24} {s.description[:64]}")
    if plugin_skills:
        _print("\nPLUGINS:")
        for s in plugin_skills:
            _print(f"  {s.name:24} {s.description[:52]} (from {s.plugin})")
    if not builtin and not plugin_skills:
        _print("no skills found. Add one at syntra/skills/<name>/SKILL.md, "
               "or install a plugin: `syntra install <src>`.")
    return 0


def cmd_install(args: argparse.Namespace) -> int:  # [A]
    """Install agents/skills from a local folder, a .md file, or a git/http URL.
    With no source, list what's already installed."""
    from ..core import installer
    from ..core.plugin_loader import discover_plugins, plugin_summary
    src = (getattr(args, "source", "") or "").strip()
    if not src:
        _print(plugin_summary(discover_plugins()))
        return 0
    ok, msg = installer.install(src)
    _print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_plugins(args: argparse.Namespace) -> int:  # [A]
    """List installed plugins (agents/skills/commands) + enabled state, or toggle one
    with --enable/--disable. A disabled plugin stays on disk but is skipped at discovery."""
    from ..core import installer
    enable = (getattr(args, "enable", "") or "").strip()
    disable = (getattr(args, "disable", "") or "").strip()
    if enable or disable:
        ok, msg = installer.set_enabled(enable or disable, enabled=bool(enable))
        _print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1
    rows = installer.list_installed()
    if not rows:
        _print("no plugins installed. Add one: `syntra install <folder|file.md|git-url>`.")
        return 0
    for r in rows:
        mark = "●" if r["enabled"] else "○"
        state = "" if r["enabled"] else "  (disabled)"
        _print(f"  {mark} {r['name']:22}{state}  {r['summary']}")
    return 0


# ---------------------------------------------------------------- route-health


def cmd_models(args: argparse.Namespace) -> int:
    """Show or set per-role model assignments (Auto/Manual). Mirrors the TUI board.

    `syntra models`                       -> show the assignment board
    `syntra models <role> <model_id>`     -> pin that role (MANUAL)
    `syntra models <role> auto`           -> clear the pin (AUTO)
    Pins persist to overrides.json; sub-agents are configurable too.
    """
    roles = ("planner", "executor", "reviewer", "subagent")
    desc = {"planner": "breaks the task into steps", "executor": "does each step",
            "reviewer": "checks the work", "subagent": "handles delegated sub-tasks"}
    ov = Overrides.load()
    role = (getattr(args, "role", "") or "").lower()
    model = (getattr(args, "model", "") or "").strip()
    # `syntra models detect <provider>` — query that provider's /models with its key, validate
    # each model, and make the working ones routable (allowed_models + minimal catalog overlay).
    if role == "detect":
        provider = model
        if not provider:
            _print("usage: syntra models detect <provider>")
            return 2
        from ..core import model_discovery as _md
        from ..core.registry import (ProviderRegistry, add_allowed_models,
                                      write_providers_config, default_config_path)
        from ..core.paths import default_catalog_overlay_path
        reg = ProviderRegistry.load()
        ep = reg.by_name(provider)
        if ep is None:
            _print(f"no configured provider '{provider}'. add a key first: syntra key {provider} <key>")
            return 2
        cat = Catalog.load(_catalog_path())
        rep = _md.discover_for_endpoint(
            ep, registry=reg, catalog_ids=frozenset(m.id for m in cat.models),
            existing_allowed=tuple(getattr(ep, "allowed_models", None) or ()),
            on_event=lambda kind, p: (
                _print(f"  {p.get('id','?')}: {p.get('status','?')}"
                       + (f" · tools={p['tool_use']}" if p.get('tool_use') not in (None, 'unknown') else ""))
                if kind == "discovery_model" else None))
        if rep.fetch_error:
            _print(f"could not list models for {provider}: {rep.fetch_error}")
            return 1
        import json as _json
        path = getattr(reg, "source_path", None) or default_config_path()
        if rep.added_to_allowed:
            try:
                raw = _json.loads(Path(path).read_text())
            except (OSError, ValueError):
                raw = {"providers": []}
            raw, _a, _n = add_allowed_models(raw, provider, list(rep.added_to_allowed))
            write_providers_config(path, raw.get("providers", []), overwrite=True)
            for _note in _n:
                _print(f"  note: {_note}")
        if rep.uncatalogued_added:
            ov_path = default_catalog_overlay_path()
            try:
                ovd = _json.loads(Path(ov_path).read_text())
            except (OSError, ValueError):
                ovd = {"models": []}
            _by = {d.id: d for d in rep._discovered}
            _vr = {v.model_id: v for v in rep.validation}
            for _mid in rep.uncatalogued_added:
                _dm = _by.get(_mid)
                if _dm is not None:
                    ovd, _ = _md.merge_overlay_row(
                        ovd, _md.build_minimal_catalog_row(_dm, provider, _vr.get(_mid)))
            _md.write_catalog_overlay(ov_path, ovd)
        _print(f"{provider}: {len(rep.fetched)} models · {len(rep.validated_ok)} validated · "
               f"{len(rep.added_to_allowed)} allowed · {len(rep.uncatalogued_added)} newly catalogued")
        return 0
    if role:
        if role not in roles:
            _print(f"unknown role '{role}'. pick one of: {', '.join(roles)}")
            return 2
        if not model:
            _print(f"usage: syntra models {role} <model_id|auto>")
            return 2
        if model.lower() == "auto":
            ov.unpin_role(role); ov.save()
            _print(f"{role} → AUTO  (best-fit routing)")
            return 0
        cat = Catalog.load(_catalog_path())
        if not any(m.id == model for m in cat.models):
            _print(f"model '{model}' is not in the catalog. run `syntra catalog` to see ids.")
            return 2
        ov.pin_role(role, model); ov.save()
        _print(f"{role} → MANUAL  {model}")
        return 0
    # no args -> show the board
    _print("Model assignments  (MANUAL = your pin · AUTO = Syntra routes the best fit):")
    for r in roles:
        pinned = ov.pinned_model_for(r)
        mode = f"MANUAL → {pinned}" if pinned else "AUTO  (best-fit routing)"
        _print(f"  {r:<9} {mode:<36} · {desc[r]}")
    _print("")
    _print("Pin a model:   syntra models <role> <model_id>")
    _print("Back to AUTO:  syntra models <role> auto")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Show learned per-route outcome stats (routes.json)."""
    stats = _route_stats()
    rows = stats.rows()
    if not rows:
        _print("(no route stats yet; run some tasks first)")
        _print(f"file: {stats.path}")
        return 0
    if getattr(args, "json", False):
        import json as _json
        _print(_json.dumps({"routes": [r.to_dict() for r in rows]}, indent=2))
        return 0
    _print(f"Route stats: {len(rows)} routes  (min_samples={stats.min_samples} before quality bites)")
    _print(f"{'ROLE':9} {'MODEL':38} {'PROVIDER':16} {'N':>4} {'PASS%':>6} {'QUAL':>6} {'AVG$':>9}")
    for r in rows:
        _print(f"{r.role:9} {r.model_id[:38]:38} {r.provider[:16]:16} {r.samples:>4} "
               f"{r.pass_rate()*100:>5.0f}% {r.quality_factor(min_samples=stats.min_samples):>6.3f} {r.avg_cost():>9.5f}")
    suggested, reason = stats.suggest_quality_bias()
    _print(f"quality_bias suggestion (advisory): {reason}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Cost-per-success benchmark: routed stack vs one pinned model (live calls)."""
    from ..core import benchmark as bench
    cat = Catalog.load(args.catalog_path or _catalog_path())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    rh = _route_health()
    ov = Overrides.load()

    tasks = args.task or [
        "Write a Python function that returns the nth Fibonacci number.",
        "Explain the difference between a process and a thread in two sentences.",
        "Write a regex that matches a valid IPv4 address.",
    ]
    baseline_model = args.baseline_model
    _print(f"benchmark: {len(tasks)} task(s)  routed-vs-{baseline_model}")
    _print("(this makes real provider calls; ctrl-c to abort)")

    def make_loop():
        store = TaskStore(_state_root())
        return Loop(catalog=cat, store=store, registry=registry, route_health=rh,
                    overrides=ov, route_stats=_route_stats())

    routed_cfg = LoopConfig(quality_bias=args.quality_bias)
    baseline_cfg = LoopConfig(
        quality_bias=args.quality_bias,
        pin_planner=baseline_model, pin_executor=baseline_model, pin_reviewer=baseline_model,
    )
    try:
        outcomes = bench.run_suite(make_loop, tasks, workspace_root=args.workspace,
                                   routed_config=routed_cfg, baseline_config=baseline_cfg)
    except ProviderError as e:
        from ..core.errors import explain_provider_error
        _print(f"provider error: {explain_provider_error(e)}")
        return 3
    report = bench.build_report(outcomes)
    _print("")
    _print(report.render())
    return 0


def cmd_route_health(args: argparse.Namespace) -> int:
    rh = _route_health()
    if not rh.records:
        _print("(no route health records yet; nothing has failed or succeeded yet)")
        _print(f"file: {rh.path}")
        return 0
    _print(f"Route health: {len(rh.records)} routes tracked")
    _print(f"{'PROVIDER':22} {'MODEL':42} SUCCESS  FAILS  COOL")
    for r in sorted(rh.records.values(), key=lambda r: r.cooldown_factor()):
        _print(f"{r.provider:22} {r.model_id:42} {r.successes:>7}  {len(r.failures):>5}  {r.cooldown_factor():.2f}")
    return 0


def cmd_route_health_clear(args: argparse.Namespace) -> int:
    rh = _route_health()
    n = rh.clear(provider=args.provider, model_id=args.model)
    _print(f"cleared {n} route health records")
    return 0


def cmd_route_blacklist(args: argparse.Namespace) -> int:
    ov = Overrides.load()
    ov.add_blacklist(model_id=args.model, provider=args.provider, reason=args.reason or "")
    ov.save()
    _print(f"blacklisted model={args.model}" + (f" provider={args.provider}" if args.provider else " (any provider)"))
    _print(f"wrote {ov.source_path}")
    return 0


def cmd_route_penalty(args: argparse.Namespace) -> int:
    if args.multiplier < 0.0:
        _print("error: penalty multiplier must be >= 0.0")
        return 2
    ov = Overrides.load()
    ov.add_penalty(
        model_id=args.model,
        provider=args.provider,
        penalty=args.multiplier,
        reason=args.reason or "",
    )
    ov.save()
    _print(f"added penalty model={args.model} mult={args.multiplier}" + (f" provider={args.provider}" if args.provider else " (any provider)"))
    _print(f"wrote {ov.source_path}")
    return 0


# ---------------------------------------------------------------- session


def _integration_dispatch(line: str):
    """Let an optional out-of-tree integration handle an extra slash command.
    Returns (action, arg), or None (not its command / no integration). Never raises."""
    try:
        from ..core.tools import load_integration_module
        mod = load_integration_module()
        if mod is not None and hasattr(mod, "dispatch_slash"):
            return mod.dispatch_slash(line)
    except Exception:  # noqa: BLE001
        return None
    return None


def _git_diff_text(max_chars: int = 8000) -> str:
    """Return the working-tree diff (falls back to staged), capped. '' when git is
    absent or the tree is clean. Used by /review, /code-review, /simplify so they
    operate on REAL changes, not a hint."""
    import subprocess, shutil
    if not shutil.which("git"):
        return ""
    try:
        r = subprocess.run(["git", "diff"], capture_output=True, text=True, timeout=10)
        if not r.stdout.strip():
            r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=10)
        return r.stdout[:max_chars]
    except Exception:  # noqa: BLE001 - a git hiccup just yields no diff
        return ""


def _review_goal(kind: str, arg: str) -> tuple[str, str]:
    """Build a (action, goal) for /review | /code-review | /simplify so the line-REPL RUNS a
    real review of the current diff (was: print a hint). Returns ('goal', <prompt>), or a
    ('noop', msg) sentinel when there's nothing to review."""
    diff = _git_diff_text()
    if not diff.strip():
        return ("noop", "no changes to review (working tree + staging area clean)")
    extra = (arg or "").strip()
    if kind == "simplify":
        ask = ("Review this diff for cleanup-only simplifications (readability, dead code, "
               "duplication, naming) — do NOT hunt for new bugs or add features.")
    else:
        ask = "Review this git diff for correctness bugs, security issues, and regressions."
    if extra:
        ask += f"\nFocus: {extra}"
    return ("goal", f"{ask}\n\n```diff\n{diff}\n```")


def session_dispatch(line: str) -> tuple[str, str]:
    """Classify one interactive-session input line into (action, arg). Pure.

    actions: empty | exit | help | tasks | resume | proof-goal | goal |
             verify | doctor | models | agent | verbose
    Slash-commands control the session; anything else is a task goal.
    """
    s = (line or "").strip()
    if not s:
        return ("empty", "")
    if s.startswith("!"):
        return ("shell", s[1:].strip())
    if s in ("/exit", "/quit", "/q", ":q"):
        return ("exit", "")
    # TUI-interactive commands (handled as modals in the TUI; routed here so the
    # palette/help can list them and the no-dead-buttons audit passes). Checked
    # before /resume so "/resume-question" doesn't get swallowed by "/resume".
    if s == "/trace":
        return ("trace", "")
    if s == "/wizard":
        return ("wizard", "")
    if s == "/resume-question":
        return ("resume_question", "")
    if s in ("/help", "/?", "?"):
        return ("help", "")
    if s in ("/tasks", "/ls"):
        return ("tasks", "")
    if s in ("/verbose", "/v"):
        return ("verbose", "")
    if s in ("/verify",):
        return ("verify", "")
    if s in ("/doctor",):
        return ("doctor", "")
    if s in ("/models", "/model", "/catalog"):
        return ("models", "")
    if s.startswith("/models ") or s.startswith("/model "):
        return ("models", s.split(" ", 1)[1].strip())
    if s == "/spin" or s.startswith("/spin "):   # F24: demo the live multi-agent panel
        return ("spin", s[len("/spin"):].strip())
    if s in ("/compact",):
        return ("compact", "")
    if s in ("/clear", "/new"):
        return ("clear", "")
    if s == "/copy" or s.startswith("/copy "):
        return ("copy", s[len("/copy"):].strip())
    if s == "/attach" or s.startswith("/attach "):
        # Image attach is handled in the TUI (chip + drag-drop + clipboard); this routes the
        # command so it's discoverable in the palette and not flagged dead by the coverage guard.
        return ("attach", s[len("/attach"):].strip())
    if s in ("/plan",):
        # Re-open the current plan as a click-to-expand card in the chat (TUI-handled).
        return ("plan", "")
    if s in ("/blocks",):
        return ("blocks", "")
    if s.startswith("/mcp"):
        return ("mcp", s[len("/mcp"):].strip())
    if s in ("/status",):
        return ("status", "")
    if s == "/stats" or s.startswith("/stats "):
        # TUI-handled usage dashboard (#218): activity heatmap + streaks + cost/day.
        # Routed here so the palette lists it + the coverage guard passes; arg = window days.
        return ("stats", s[len("/stats"):].strip())
    if s == "/diff" or s.startswith("/diff "):
        # #141: bare /diff = this turn's diff (TUI); `/diff git` = working-tree. Pass the arg.
        return ("diff", s[len("/diff"):].strip())
    if s in ("/review",):
        return ("review", "")
    # /permissions [ask|auto|locked|normal|<perm> <auto|ask|off>] — opens the access popup when
    # bare, or applies a quick setting when an arg is given (the arg was DROPPED before, so
    # `/permissions auto` fell through to ('unknown', …) and the handler's auto/ask branches
    # were unreachable dead code). /access is the same command under a clearer name.
    if s == "/permissions" or s.startswith("/permissions "):
        return ("permissions", s[len("/permissions"):].strip())
    if s == "/permission" or s.startswith("/permission "):
        return ("permissions", s[len("/permission"):].strip())
    if s == "/access" or s.startswith("/access "):
        return ("permissions", s[len("/access"):].strip())
    if s == "/auto" or s.startswith("/auto "):
        return ("auto", s[len("/auto"):].strip())
    # #173: /keymap takes bind/unbind args now — match the prefix + carry the arg. MUST be
    # checked before the /key prefix below (else "/keymap …" is swallowed by "/key").
    if s == "/keymap" or s == "/hotkeys" or s.startswith("/keymap ") or s.startswith("/hotkeys "):
        _rest = s.split(None, 1)
        return ("keymap", _rest[1].strip() if len(_rest) > 1 else "")
    if s == "/login" or s.startswith("/login "):
        return ("login", s[len("/login"):].strip())
    if s == "/logout" or s.startswith("/logout "):
        return ("logout", s[len("/logout"):].strip())
    if s in ("/sessions",):
        return ("sessions", "")
    # T17: /session was a near-useless one-liner (last task id only) and confusingly close to
    # /sessions — folded into /sessions (the real picker).
    if s in ("/session",):
        return ("sessions", "")
    if s == "/title" or s.startswith("/title ") or s.startswith("/rename"):
        # /title [name]  ·  /rename [name] (alias) — name the current session
        _arg = s.split(" ", 1)[1].strip() if " " in s else ""
        return ("title", _arg)
    if s.startswith("/themes") or s.startswith("/colors"):
        rest = s.split(None, 1)
        return ("themes", rest[1].strip() if len(rest) > 1 else "")
    if s.startswith("/layout"):
        rest = s.split(None, 1)
        return ("layout", rest[1].strip() if len(rest) > 1 else "")
    if s in ("/changelog",):
        return ("changelog", "")
    if s in ("/debug",):
        return ("debug", "")
    if s in ("/commands", "/command-info"):
        return ("commands", "")
    if s in ("/memories",):
        return ("memories", "")
    if s in ("/tree",):
        return ("tree", "")
    if s in ("/find", "/file"):
        return ("find", "")
    if s in ("/config",):
        return ("config", "")
    # T17: /multi-agents was info-text only (it just listed /compare /council /swarm) — cut;
    # /help carries that now. /init was a "run syntra init" reminder — cut (the real `syntra
    # init` CLI command stays). /session folded into /sessions above.
    if s in ("/plugins", "/extensions"):             # [A] installed-plugins picker (A4)
        return ("plugins", "")
    if s == "/skills" or s.startswith("/skills "):   # [A] /skills <query> -> match (A4)
        return ("skills", s[len("/skills"):].strip())
    if s.startswith("/skill-create"):
        return ("skill-create", s[len("/skill-create"):].strip())
    if s.startswith("/skill"):
        return ("skill", s[len("/skill"):].strip())
    if s.startswith("/mode"):
        return ("mode", s[len("/mode"):].strip())
    if s.startswith("/context"):
        return ("context", s[len("/context"):].strip())
    if s.startswith("/key"):
        return ("key", line[len("/key"):].strip())   # raw line (keys are case/space-sensitive)
    if s.startswith("/goal"):
        return ("goal-set", s[len("/goal"):].strip())
    if s.startswith("/memory-drop"):
        return ("memory-drop", s[len("/memory-drop"):].strip())
    if s.startswith("/memory-update"):
        return ("memory-update", s[len("/memory-update"):].strip())
    if s.startswith("/use-proxy"):
        return ("use-proxy", s[len("/use-proxy"):].strip())
    if s.startswith("/fork"):
        return ("fork", s[len("/fork"):].strip())
    if s in ("/agents",):
        return ("agents", "")
    if s.startswith("/agent ") or s == "/agent":
        return ("agent", s[len("/agent"):].strip())   # /agent <goal> -> agentic run
    if s.startswith("/resume"):
        return ("resume", s[len("/resume"):].strip())
    if s.startswith("/proof"):
        return ("proof-goal", s[len("/proof"):].strip())
    if s.startswith("/export"):
        return ("export", s[len("/export"):].strip())
    if s.startswith("/effort"):
        return ("effort", s[len("/effort"):].strip())
    if s.startswith("/web"):
        rest = s[len("/web"):].strip()
        return ("goal", f"search the web for: {rest}") if rest else ("goal", "web search")
    if s.startswith("/browse"):
        return ("browse", s[len("/browse"):].strip())
    if s.startswith("/vision"):
        rest = s[len("/vision"):].strip()
        # T2: actually SEE the image — instruct the agent to load it with the real `view_image`
        # tool (tools.py), not just relabel a goal with the filename as plain text.
        if rest:
            return ("agent", f"Use the view_image tool to load the image at '{rest}', then "
                             f"describe and analyze what it shows in detail.")
        return ("goal", "To analyze an image, pass its path: /vision <path-to-image>")
    if s == "/view" or s.startswith("/view "):
        return ("view", s[len("/view"):].strip())
    if s == "/preview" or s.startswith("/preview "):
        return ("preview", s[len("/preview"):].strip())
    if s == "/open" or s.startswith("/open "):
        return ("open", s[len("/open"):].strip())
    if s.startswith("/imagine"):
        rest = s[len("/imagine"):].strip()
        # Generate an image from a prompt via the real generate_image tool (saved to the
        # workspace, then rendered inline). Route as an agent goal so it goes through the tool
        # path + permission gate, not a plain text answer.
        if rest:
            return ("agent", f"Use the generate_image tool to create an image for this prompt, "
                             f"then briefly say where you saved it: {rest}")
        return ("goal", "To generate an image, pass a prompt: /imagine <prompt>")
    if s.startswith("/image"):
        return ("image", s[len("/image"):].strip())
    if s.startswith("/undo"):
        return ("undo", s[len("/undo"):].strip())
    if s.startswith("/rollback"):
        return ("rollback", s[len("/rollback"):].strip())
    if s in ("/cost",):
        return ("cost", "")
    if s in ("/providers",):
        return ("providers", "")
    if s in ("/route",):
        return ("route-info", "")
    if s.startswith("/pin"):
        return ("pin", s[len("/pin"):].strip())
    if s.startswith("/unpin"):
        return ("unpin", s[len("/unpin"):].strip())
    if s in ("/stop",):
        return ("stop", "")
    if s in ("/raw",):
        return ("raw", "")
    if s.startswith("/code-review"):
        return ("code-review", s[len("/code-review"):].strip())
    if s.startswith("/simplify"):
        return ("simplify", s[len("/simplify"):].strip())
    if s in ("/context",):
        return ("context", "")
    if s.startswith("/bg"):
        return ("bg", s[len("/bg"):].strip())
    if s in ("/jobs",):
        return ("jobs", "")
    if s.startswith("/todo"):
        return ("todo", s[len("/todo"):].strip())
    if s.startswith("/grep"):
        return ("grep", s[len("/grep"):].strip())
    if s == "/search" or s.startswith("/search "):
        # TUI-handled workspace-search overlay (#217); routed so the palette lists it + the
        # coverage guard passes. In the plain CLI it maps to the same text grep behavior.
        return ("search", s[len("/search"):].strip())
    if s.startswith("/watch"):
        return ("watch", s[len("/watch"):].strip())
    if s in ("/unwatch",):
        return ("unwatch", "")
    if s.startswith("/feature"):
        return ("feature", s[len("/feature"):].strip())
    if s in ("/benchmark",):
        return ("benchmark", "")
    if s.startswith("/ask"):
        return ("goal", s[len("/ask"):].strip() or "answer my question")
    if s.startswith("/git"):
        return ("git", s[len("/git"):].strip())
    # /history: a read-only task list WITH the per-task $cost column (restored — the T17 fold
    # into /sessions dropped the cost view, which is /history's whole point vs the session picker).
    if s in ("/history",):
        return ("history", "")
    # Message navigator: list my past messages + jump to one (TUI overlay/rail; keyboard alias).
    if s in ("/msgs", "/messages"):
        return ("msgs", "")
    if s.startswith("/rules"):
        return ("rules", s[len("/rules"):].strip())
    if s in ("/hardware",):
        return ("hardware", "")
    if s.startswith("/download-model"):
        return ("download-model", s[len("/download-model"):].strip())
    if s.startswith("/btw"):
        return ("btw", s[len("/btw"):].strip())
    if s in ("/usage",):
        return ("usage", "")
    if s.startswith("/agent-create"):
        return ("agent-create", s[len("/agent-create"):].strip())
    if s.startswith("/loop"):
        return ("loop", s[len("/loop"):].strip())
    if s.startswith("/batch"):
        return ("batch", s[len("/batch"):].strip())
    if s.startswith("/rewind"):
        return ("rewind", s[len("/rewind"):].strip())
    if s in ("/hooks",):
        return ("hooks", "")
    if s.startswith("/hook-add"):
        return ("hook-add", s[len("/hook-add"):].strip())
    if s.startswith("/hook-remove"):
        return ("hook-remove", s[len("/hook-remove"):].strip())
    if s in ("/map",):
        return ("map", "")
    # /symbols <name>: a real NAMED-SYMBOL lookup (where a class/fn is DEFINED) via the repo
    # map — semantically distinct from /grep (text search). The T17 fold to grep lost this.
    if s.startswith("/symbols"):
        return ("symbols", s[len("/symbols"):].strip())
    if s.startswith("/deep-research"):
        return ("deep-research", s[len("/deep-research"):].strip())
    if s.startswith("/council"):
        return ("council", s[len("/council"):].strip())
    if s.startswith("/compare"):
        return ("compare", s[len("/compare"):].strip())
    if s.startswith("/plan-review"):
        return ("plan-review", s[len("/plan-review"):].strip())
    if s.startswith("/commit-style"):
        return ("commit-style", s[len("/commit-style"):].strip())
    if s.startswith("/ab-handoff"):
        return ("ab_handoff", s[len("/ab-handoff"):].strip())
    if s.startswith("/spend"):
        return ("spend", s[len("/spend"):].strip())
    if s.startswith("/replay"):
        return ("replay", s[len("/replay"):].strip())
    # An optional out-of-tree integration may handle extra slash commands.
    ext = _integration_dispatch(s)
    if ext is not None:
        return ext
    if s.startswith("/"):
        return ("unknown", s)
    return ("goal", s)


_SESSION_INTRO = (
    "syntra — smart model coordinator. type a goal and press Enter; it routes "
    "plan/execute/review to the best models and shows you the answer.\n"
    "commands: /help  /verify  /doctor  /models  /agent <goal>  /tasks  /resume <id>  "
    "/proof <goal>  /verbose  /exit"
)


def _update_notice() -> str | None:
    """Throttled 'a newer Syntra is available' notice (its own once/day cadence,
    independent of the health check). Fail-silent — never blocks or breaks startup."""
    try:
        from ..core import updates as up
        return up.check_for_notice(_state_root())
    except Exception:  # noqa: BLE001
        return None


def _startup_health_summary() -> str | None:
    """Smart, infrequent health check + update notice. Returns a one-line summary
    to SHOW, or None when nothing to say. See core/healthcheck.py for the decision."""
    from ..core import healthcheck as hc
    from ..core.overrides import Overrides as _Ov
    # Update notice runs on its OWN throttle, even when the health check is fresh.
    upd = _update_notice()

    def _resume_hint() -> str | None:
        """Crash-safe recovery nudge: if the most-recently-touched task is still unfinished
        (its own exit-time save never ran — hard kill / power loss / SIGKILL), remind the user
        on the NEXT launch that it can be resumed. Complements the exit-time save hint, which
        can't fire when the process dies abnormally."""
        try:
            store = TaskStore(_state_root())
            tasks_dir = _state_root() / "tasks"
            if not tasks_dir.exists():
                return None
            dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
            if not dirs:
                return None
            tid = max(dirs, key=lambda d: d.stat().st_mtime).name
            st = store.load(tid)
            unfinished = any(s.status in ("pending", "failed", "running") for s in (st.plan or []))
            if st.status in ("running", "pending") or unfinished:
                return f"[resume] unfinished task {tid} — run `/resume {tid}` (or `syntra resume {tid}`)"
        except Exception:  # noqa: BLE001 - a hint must never block launch
            return None
        return None

    hint = _resume_hint()
    cache_path = _state_root() / "verify-cache.json"
    try:
        providers_path = ProviderRegistry.load().source_path
    except ProviderRegistryError:
        providers_path = None
    fp = hc.config_fingerprint(
        providers_path=providers_path,
        catalog_path=_catalog_path(),
        overrides_path=_Ov.load().source_path,
    )
    cache = hc.load_cache(cache_path)
    do_it, reason = hc.should_check(cache, fp)

    def _combine(*lines):
        joined = "\n".join(l for l in lines if l)
        return joined or None

    if not do_it:
        return _combine(upd, hint)      # health fresh, but still surface update + resume notices
    ok, detail = True, ""
    try:
        cat = Catalog.load(_catalog_path())
        registry = ProviderRegistry.load()
        router = _build_router(cat, registry, _Ov.load(), _route_health())
        router.pick(TaskProfile(role="planner"))
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)[:120]
    hc.save_cache(cache_path, fingerprint=fp, ok=ok)
    if ok:
        return _combine(upd, hint, f"[health] checked ({reason}) — ready.")
    return _combine(upd, hint, f"[health] problem ({reason}): {detail}  ·  run `syntra doctor`")


def _handle_models(arg: str) -> bool:
    """`/models` — list catalog + current role pins, or pin/unpin a role.

    Usage shown to the user:
      /models                       list models + show current pins
      /models pin <role> <model>    assign a model to a role
      /models unpin <role>          clear a role pin (back to AUTO)
    where <role> is one of: planner | executor | reviewer | subagent
    """
    import argparse as _ap
    parts = arg.split() if arg else []
    roles = ("planner", "executor", "reviewer", "subagent")

    # --- pin ---
    if parts and parts[0] == "pin":
        if len(parts) < 3:
            _print(f"usage: /models pin <{'|'.join(roles)}> <model_id>")
            return True
        role, model_id = parts[1].lower(), " ".join(parts[2:])
        if role not in roles:
            _print(f"unknown role '{role}'. pick one of: {', '.join(roles)}")
            return True
        cat = Catalog.load(_catalog_path())
        if not any(m.id == model_id for m in cat.models):
            _print(f"model '{model_id}' is not in the catalog. run /models to see ids.")
            return True
        ov = Overrides.load()
        ov.pin_role(role, model_id)
        ov.save()
        _print(f"pinned {role} -> {model_id}  (saved to {ov.source_path})")
        _print("the router will now use this exact model for that role.")
        return True

    # --- unpin ---
    if parts and parts[0] == "unpin":
        if len(parts) < 2 or parts[1].lower() not in roles:
            _print(f"usage: /models unpin <{'|'.join(roles)}>")
            return True
        role = parts[1].lower()
        ov = Overrides.load()
        if ov.pinned_model_for(role) is None:
            _print(f"{role} was not pinned.")
            return True
        ov.unpin_role(role)
        ov.save()
        _print(f"unpinned {role}. it will be auto-routed again.")
        return True

    # --- default: list catalog + the Auto/Manual assignment board + usage ---
    cmd_catalog(_ap.Namespace(catalog_path=None))
    ov = Overrides.load()
    _role_desc = {"planner": "breaks the task into steps",
                  "executor": "does each step",
                  "reviewer": "checks the work",
                  "subagent": "handles delegated sub-tasks"}
    _print("")
    _print("Model assignments  (MANUAL = your pin · AUTO = Syntra routes the best fit):")
    for role in roles:
        pinned = ov.pinned_model_for(role)
        mode = f"MANUAL → {pinned}" if pinned else "AUTO  (best-fit routing)"
        _print(f"  {role:<9} {mode:<34} · {_role_desc.get(role, '')}")
    _print("")
    _print(f"Pin a model (MANUAL):  /models pin <{'|'.join(roles)}> <model_id>")
    _print("Back to AUTO:          /models unpin <role>")
    return True


def _handle_undo(action: str, arg: str, last_task_id: str) -> bool:
    """Real /undo + /rollback: restore files via the last task's checkpoint ledger.

    /undo            undo the single most recent applied edit (LIFO)
    /rollback        undo every applied edit from the last task
    /rollback <id>   undo edits from newest back to and including checkpoint <id>
    add --force (or !) to override the dirty-file guard.
    """
    from ..core import edits as _edits
    tokens = (arg or "").replace("!", " ").split()
    force = any(t in ("--force", "-f") for t in tokens) or (arg or "").strip().endswith("!")
    cp_id = next((t for t in tokens if not t.startswith("-")), "")

    if not last_task_id:
        _print("  no task yet — run something in --execute mode first."); return True
    try:
        store = TaskStore(_state_root())
        state = store.load(last_task_id)
    except Exception:  # noqa: BLE001
        _print(f"  cannot load last task {last_task_id!r}."); return True

    applier = _edits.EditApplier.from_checkpoints(
        state.workspace_root, state.task_dir / "checkpoints")
    if not applier.applied:
        _print("  nothing to undo (no checkpointed edits in the last task)."); return True

    try:
        if action == "undo":
            done = applier.undo_last(allow_dirty=force)
            _print(f"  undid {done.kind} of {done.path}  ({done.checkpoint_id})")
        elif cp_id:
            done = applier.rollback_to(cp_id, allow_dirty=force)
            _print(f"  rolled back {len(done)} edit(s) to before {cp_id}:")
            for e in done:
                _print(f"    - {e.kind} {e.path}")
        else:
            n = applier.rollback_all(allow_dirty=force)
            _print(f"  rolled back all {n} edit(s) from task {last_task_id}.")
    except _edits.EditError as e:
        _print(f"  {e}")
        _print("  (re-run with --force / ! to override the dirty-file guard.)")
    return True


def _configured_mcp_servers() -> list:
    """Server specs the user has saved (mcp.json). Auto-attached to every run."""
    try:
        from ..core import mcp_config
        from ..core import folder_trust as _ft
        cfg = mcp_config.config_path(_state_root())
        # #201(c): a repo-local mcp.json auto-spawns subprocesses. Don't honor a cloned
        # repo's server list unless the folder is trusted.
        if not _ft.repo_local_exec_allowed(str(cfg)):
            _print(f"  ⚠ skipped untrusted repo-local MCP config: {cfg} (run from a trusted folder to enable)")
            return []
        return mcp_config.load_servers(_state_root())
    except Exception:  # noqa: BLE001 - config is a convenience, never break a run
        return []


def _load_lifecycle_hooks():
    """B5: build a HookRegistry from the first hooks.json found, or None.

    Lifecycle hooks live under a ``hooks`` key (the pattern-rule engine uses
    ``rules``), so both can share one file. Locations, first match wins:
    $SYNTRA_HOOKS, <state>/hooks.json, ~/.config/syntra/hooks.json.
    """
    try:
        from ..core.hooks import load_hooks
        candidates = []
        env = os.environ.get("SYNTRA_HOOKS")
        if env:
            candidates.append(Path(env))
        candidates.append(_state_root() / "hooks.json")
        candidates.append(Path.home() / ".config" / "syntra" / "hooks.json")
        from ..core import folder_trust as _ft
        for p in candidates:
            # #201(c): a hooks.json can run shell=True on the first tool call. If it's
            # repo-local (folder-local mode) and the folder isn't trusted, REFUSE to load it —
            # opening a cloned repo must not silently run its hooks on the host.
            if not _ft.repo_local_exec_allowed(str(p)):
                _print(f"  ⚠ skipped untrusted repo-local hooks: {p} (run from a trusted folder to enable)")
                continue
            reg = load_hooks(p)
            if reg is not None:
                return reg
    except Exception:  # noqa: BLE001 - hooks are optional; never block a run
        pass
    return None


def _handle_mcp(arg: str) -> bool:
    """Real /mcp: manage + probe the persistent MCP server registry.

    /mcp                list configured servers and probe each for live tool count
    /mcp add <spec>     add a server ('<cmd>' for stdio, 'https://host/mcp [tok]' for http)
    /mcp remove <spec>  remove a server
    Configured servers auto-attach to every `syntra run --agent`.
    """
    from ..core import mcp_config
    parts = (arg or "").split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "add":
        if not rest:
            _print("  usage: /mcp add <command-or-url>"); return True
        added = mcp_config.add_server(_state_root(), rest)
        _print(f"  {'added' if added else 'already configured'}: {rest}")
        return True
    if sub in ("remove", "rm", "del"):
        if not rest:
            _print("  usage: /mcp remove <command-or-url>"); return True
        removed = mcp_config.remove_server(_state_root(), rest)
        _print(f"  {'removed' if removed else 'no match for'}: {rest}")
        return True

    servers = mcp_config.load_servers(_state_root())
    if not servers:
        _print("  no MCP servers configured.")
        _print("  add one:  /mcp add 'npx -y @modelcontextprotocol/server-github'")
        _print("  or http:  /mcp add 'https://host/mcp [bearer-token]'")
        _print("  (configured servers auto-attach to every  syntra run --agent)")
        return True

    _print(f"  configured MCP servers ({len(servers)}) — probing for tools:")
    for spec in servers:
        # _spawn_mcp_clients prints a connect line (name + tool count) on success
        # or a warning on failure, for each spec.
        clients = _spawn_mcp_clients([spec])
        for c in clients:
            try:
                c._t.close()
            except Exception:  # noqa: BLE001
                pass
    return True


def _handle_compare(arg: str) -> bool:
    """TUI /compare <question>: ask N models, print every answer + the synthesized best.
    Synchronous text version; the rich clickable side-by-side view is the TUI's job."""
    q = (arg or "").strip()
    if not q:
        _print("  usage: /compare <question>   — asks several models + shows + synthesizes")
        return True
    try:
        cat = Catalog.load(_catalog_path())
        registry = ProviderRegistry.load()
    except Exception as e:  # noqa: BLE001
        _print(f"  compare unavailable: {str(e)[:160]}"); return True
    loop = Loop(catalog=cat, store=TaskStore(_state_root()), registry=registry,
                route_health=_route_health(), overrides=Overrides.load(),
                progress=_quiet_progress)
    _print(f"  comparing models on: {q}")
    out = loop.compare(q, 3, config=LoopConfig())
    cands = out.get("candidates", [])
    if not cands:
        _print(f"  {out.get('rationale', 'no models available')}"); return True
    for i, c in enumerate(cands):
        _print(f"\n  ── candidate {i + 1}: {c['model']} via {c['provider']} ──")
        _print("  " + (c["text"] or "(no output)").replace("\n", "\n  "))
    best = out.get("best_index", -1)
    _print("\n  ══ synthesized best ══")
    if 0 <= best < len(cands):
        _print(f"  (judge picked candidate {best + 1} — {out.get('rationale', '')})")
    _print("  " + (out.get("synthesis") or "(no synthesis)").replace("\n", "\n  "))
    return True


def _handle_rules(arg: str) -> bool:
    """Real /rules: set + list the INVIOLABLE rules Syntra injects into every run.

    /rules                      list global + project rules
    /rules <rule>               add a PROJECT rule (.syntra/rules.md, this repo)
    /rules global <rule>        add a GLOBAL rule (every session)
    /rules remove <text>        remove matching rule(s) from global + project
    Rules are injected first, with override-everything priority, into every role
    prompt; the reviewer fails any output that breaks one.
    """
    from ..core import rules as _rules
    from ..core.memory import looks_like_injection
    sr = _state_root()
    proj_path = Path.cwd() / ".syntra" / "rules.md"
    parts = (arg or "").split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("-g", "--global", "global"):
        if not rest:
            _print("  usage: /rules global <rule>"); return True
        ok = _rules.add_global_rule(sr, rest)
        _print(f"  {'added GLOBAL rule (applies to every session)' if ok else 'not added (duplicate, blank, or blocked as injection)'}: {rest}")
        return True
    if sub in ("rm", "remove", "del", "delete"):
        if not rest:
            _print("  usage: /rules remove <text>"); return True
        gn = _rules.remove_global_rule(sr, rest)
        pn = 0
        if proj_path.is_file():
            cur = _rules._parse(proj_path.read_text(encoding="utf-8"))
            kept = [r for r in cur if r != rest and rest not in r]
            pn = len(cur) - len(kept)
            if pn:
                proj_path.write_text("# project rules\n" + "".join(f"- {r}\n" for r in kept), encoding="utf-8")
        _print(f"  removed {gn} global + {pn} project rule(s) matching: {rest}")
        return True
    if arg:
        if looks_like_injection(arg):
            _print("  rejected: that reads like a prompt-injection, not a rule."); return True
        proj_path.parent.mkdir(parents=True, exist_ok=True)
        with open(proj_path, "a") as f:
            f.write(f"- {arg}\n")
        _print(f"  added project rule (this repo): {arg}"); return True

    glob = _rules.load_global_rules(sr)
    proj = _rules._parse(proj_path.read_text(encoding="utf-8")) if proj_path.is_file() else []
    if not glob and not proj:
        _print("  no rules set. Rules are absolute — injected into every run, enforced by the reviewer.")
        _print("  global (every session):  /rules global <rule>")
        _print("  project (this repo):     /rules <rule>")
        return True
    if glob:
        _print(f"  GLOBAL rules — every session ({len(glob)}):")
        for r in glob:
            _print(f"    - {r}")
    if proj:
        _print(f"  PROJECT rules — {proj_path} ({len(proj)}):")
        for r in proj:
            _print(f"    - {r}")
    _print("  (injected first, override-everything, into every role; reviewer fails violations.)")
    return True


def _handle_route_info(last_task_id: str) -> bool:
    """Real /route: show the last run's routing decision per role from its audit log."""
    import json as _json
    if not last_task_id:
        _print("  no routing yet — run something first."); return True
    try:
        events_path = TaskStore(_state_root()).load(last_task_id).task_dir / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        _print(f"  no audit log for task {last_task_id!r}."); return True

    # Keep the LAST route event per role (a role can re-route on retry).
    latest: dict[str, dict] = {}
    order: list[str] = []
    for ln in lines:
        try:
            ev = _json.loads(ln)
        except ValueError:
            continue
        if ev.get("kind") != "route":
            continue
        p = ev.get("payload", {})
        role = p.get("role", "?")
        if role not in latest:
            order.append(role)
        latest[role] = p
    if not latest:
        _print(f"  no routing decisions recorded for task {last_task_id}."); return True

    _print(f"  routing for task {last_task_id}:")
    for role in order:
        p = latest[role]
        prov = p.get("provider", "?")
        score = p.get("score")
        score_s = f"{score:.1f}" if isinstance(score, (int, float)) else "?"
        _print(f"    {role:<9} {p.get('model', '?')} via {prov}  "
               f"(score {score_s}, {p.get('strategy', '?')})")
        if p.get("reason"):
            _print(f"              {str(p['reason'])[:120]}")
        skipped = p.get("skipped") or []
        if skipped:
            names = ", ".join(s.get("model", "?") for s in skipped[:4])
            _print(f"              skipped: {names}")
    return True


def _handle_session_action(action: str, arg: str, last_task_id: str) -> bool:
    """Execute a slash-command action with REAL behavior. Returns True if handled.

    Each command does actual work (modeled on what mature agent CLIs do): /diff
    runs git, /status shows model+cost+dir, /models shows the catalog, etc. No
    dead buttons — an unhandled action returns False so the caller can fall through.
    """
    import argparse as _ap
    if action == "help":
        from ..core.commands import SLASH_COMMANDS
        _print("syntra commands:")
        for c in SLASH_COMMANDS:
            _print(f"  {c.name:20s} {c.desc}")
        return True
    if action == "verbose":
        _print("verbose: use /verbose to toggle detail in the line REPL"); return True
    if action == "spin":
        _print("the live multi-agent panel is a full-screen view — launch the TUI with "
               "`syntra` (or `syntra --tui`) and type /spin to watch agents work.")
        return True
    if action == "msgs":
        # Message navigator: a TUI overlay + right-edge rail (hover-expand, click to jump). In
        # the line-REPL there's no scrollback to jump to, so it's a TUI feature — be honest.
        _print("  /msgs opens the message navigator in the TUI: a list of your past messages —")
        _print("  hover the right-edge rail (or run /msgs) and click one to scroll the chat to it.")
        return True
    if action == "tasks":
        cmd_tasks(_ap.Namespace()); return True
    if action == "verify":
        cmd_verify(_ap.Namespace(catalog_path=None)); return True
    if action == "doctor":
        cmd_doctor(_ap.Namespace(catalog_path=None, probe=False)); return True
    if action == "models":
        return _handle_models(arg)
    if action == "diff":
        import subprocess, shutil
        if shutil.which("git") is None:
            _print("git not installed"); return True
        tracked = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True)
        if tracked.returncode != 0:
            _print("not a git repo (or no changes)"); return True
        unt = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                             capture_output=True, text=True)
        _print(tracked.stdout.strip() or "(no tracked changes)")
        if unt.stdout.strip():
            _print("untracked:\n  " + "\n  ".join(unt.stdout.split()))
        return True
    if action == "status":
        try:
            reg = ProviderRegistry.load()
            cat = Catalog.load(_catalog_path())
            dec = _build_router(cat, reg, Overrides.load(), _route_health()).pick(TaskProfile(role="executor"))
            _print(f"  model (executor):  {dec.model.id} via {dec.provider or '?'}")
            _print(f"  providers:         {len(reg.endpoints)} configured")
            _print(f"  catalog:           {len(cat.models)} models")
        except Exception as e:  # noqa: BLE001
            _print(f"  status unavailable: {e}")
        _print(f"  workspace:         {Path.cwd()}")
        _print(f"  state dir:         {_state_root()}")
        return True
    if action == "stats":
        # #218: plain-REPL text usage summary (the TUI renders the rich dashboard overlay).
        # Same pure aggregation as the cockpit, so both surfaces agree.
        import time as _t
        from ..core.tui_model import usage_stats, render_heatmap, sparkline
        from ..core.spend import Ledger, _LEDGER_REL
        try:
            _days = max(7, min(365, int(arg.strip()))) if arg.strip() else 30
        except ValueError:
            _days = 30
        try:
            entries = Ledger(_state_root().joinpath(*_LEDGER_REL))._read()
        except Exception:  # noqa: BLE001
            entries = []
        st = usage_stats(entries, now=_t.time(), days=_days)
        if st.get("total_runs", 0) == 0:
            _print(f"  no usage recorded in the last {_days} days — run some tasks first")
            return True
        _print(f"  usage · last {_days} days")
        _print(f"    runs {st['total_runs']}  ·  spent ${st['total_usd']:.2f}  ·  "
               f"active {st['active_days']}d  ·  streak {st['current_streak']}d (best {st['longest_streak']})")
        for row in render_heatmap(st["heatmap"], width=max(20, _days)):
            _print(f"    {row}")
        _print("    less ·░▒▓█ more")
        _pd = st.get("per_day_usd", {}) or {}
        if _pd:
            _series = [_pd.get(o, 0.0) for o in range(_days - 1, -1, -1)]
            _print(f"    cost/day {sparkline(_series)}  (peak ${max(_series):.2f}, today ${st['today_usd']:.2f})")
        return True
    if action == "memories":
        import json as _json
        p = _state_root() / "session-memory.json"
        data = {}
        try:
            loaded = _json.loads(p.read_text())
            data = loaded if isinstance(loaded, dict) else {}
        except (FileNotFoundError, ValueError):
            pass
        cons = data.get("constraints", []) if isinstance(data.get("constraints"), list) else []
        convs = data.get("conventions", []) if isinstance(data.get("conventions"), list) else []
        repo = data.get("repo_map", []) if isinstance(data.get("repo_map"), list) else []
        arch = str(data.get("architecture", "") or "")
        lines: list = []
        if cons:
            lines.append("constraints:"); lines += [f"  {c}" for c in cons]
        if convs:
            lines.append("conventions:"); lines += [f"  {v}" for v in convs]
        if repo:
            lines.append("repo map:"); lines += [f"  {r}" for r in repo]
        if arch:
            lines.append(f"architecture: {arch}")
        _print("durable memory:\n" + "\n".join(lines) if lines else "(no durable memory yet)")
        return True
    if action == "memory-update":
        if not arg:
            _print("usage: /memory-update <constraint>"); return True
        import json as _json
        p = _state_root() / "session-memory.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = _json.loads(p.read_text())
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        constraints = data.get("constraints") if isinstance(data.get("constraints"), list) else []
        if arg not in constraints:
            constraints.append(arg)
        data["constraints"] = constraints
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(data, indent=2))
        except OSError as e:
            _print(f"could not update durable memory: {e}"); return True
        _print(f"added durable constraint: {arg}"); return True
    if action == "memory-drop":
        import json as _json
        p = _state_root() / "session-memory.json"
        try:
            data = _json.loads(p.read_text())
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        constraints = data.get("constraints") if isinstance(data.get("constraints"), list) else []
        before = len(constraints)
        data["constraints"] = [c for c in constraints if arg not in c]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(data, indent=2))
        except OSError as e:
            _print(f"could not update durable memory: {e}"); return True
        _print(f"dropped {before - len(data['constraints'])} matching constraint(s)")
        return True
    if action == "keymap":
        from ..core.keymap import Keymap
        km = Keymap.load(_state_root() / "keymap.json")
        for a, keys in km.bindings.items():
            _print(f"  {a:16} {', '.join(keys)}")
        return True
    if action == "permissions":
        from ..core.access_modes import load_access_state, save_access_state, MODES, PERMISSIONS, _SETTINGS
        _ap = _state_root() / "access.json"
        st = load_access_state(_ap)
        parts = (arg or "").split()
        # Per-permission set FIRST: `<perm> <auto|ask|off>`. Checked before the single-token mode
        # match because some names (e.g. "edit") are BOTH a mode and a permission — a 2-token
        # form with a valid setting is unambiguously "set this permission", so it must win over
        # treating token1 as a mode and silently dropping the setting.
        if len(parts) == 2 and parts[1] in _SETTINGS:        # /permissions <perm> <auto|ask|off>
            st.set_perm(parts[0], parts[1]); save_access_state(_ap, st)
            _print(f"  {parts[0]} → {st.effective(parts[0])}  ({st.summary()})")
            return True
        if len(parts) == 1 and parts[0] in MODES:            # /permissions plan|ask|edit|auto
            st.set_mode(parts[0]); save_access_state(_ap, st)
            _print(f"  mode: {st.summary()}")
            return True
        # bare (or unrecognized) -> show the real posture. The TUI opens the popup; the CLI lists.
        _print(f"  mode: {st.summary()}")
        for key, label, _h in PERMISSIONS:
            _print(f"    {label:14} {st.effective(key)}")
        _print("  modes: plan (read-only) · ask (confirm each) · edit (files only) · auto (everything)")
        _print("  set: /permissions <mode>  ·  /permissions <perm> auto|ask|off")
        _print("  secrets (.env/.git/keys) always ask — even on auto. This cannot be disabled.")
        return True
    if action == "config":
        from ..core.registry import ProviderRegistry as _PR
        _print(f"  providers: {_PR._resolve_config_path(None)}")
        _print(f"  catalog:   {_catalog_path()}")
        _print(f"  state:     {_state_root()}")
        return True
    if action == "tree":
        from ..core.tools import default_tools, ToolContext, dispatch
        from ..providers.openai_compat import ToolCall
        out = dispatch(ToolCall("t", "repo_overview", "{}"),
                       default_tools(), ToolContext(workspace_root=str(Path.cwd())))
        _print(out); return True
    if action == "find":
        _print("  use: /find then type a query in the file picker (Ctrl-T), "
               "or run with --agent and ask it to find_file"); return True
    if action == "mcp":
        return _handle_mcp(arg)
    if action == "themes":
        from ..core.themes import list_themes, current_theme, set_theme
        if arg:
            if set_theme(arg):
                _print(f"theme set: {arg}")
            else:
                _print(f"unknown theme {arg!r}. available: {', '.join(list_themes())}")
            return True
        _print(f"  current: {current_theme()}")
        _print("  available: " + ", ".join(list_themes()))
        _print("  switch: /themes <name>  (or set SYNTRA_THEME)")
        return True
    if action == "use-proxy":
        if arg:
            os.environ["HTTPS_PROXY"] = arg; os.environ["HTTP_PROXY"] = arg
            _print(f"proxy set for provider calls: {arg}")
        else:
            cur = os.environ.get("HTTPS_PROXY", "")
            _print(f"current proxy: {cur or '(none)'}  ·  usage: /use-proxy http://host:port")
        return True
    if action == "sessions":
        cmd_tasks(_ap.Namespace()); return True
    if action == "fork":
        if not arg:
            _print("usage: /fork <task-id> [at-event-#]  (branch a past session)"); return True
        parts = arg.split()
        from ..core import rollout
        new_id = rollout.branch(_state_root(), parts[0],
                                at=int(parts[1]) if len(parts) > 1 else 10**9)
        _print(f"forked {new_id} from {parts[0]} — resume with: /resume {new_id}")
        return True
    if action == "login":
        cmd_login(_ap.Namespace(provider=arg, refresh=False)) if arg else _print("usage: /login <provider>")
        return True
    if action == "logout":
        cmd_logout(_ap.Namespace(provider=arg)) if arg else _print("usage: /logout <provider>")
        return True
    if action == "changelog":
        # Show the tail of the build log if we can find it (package-relative, then cwd).
        cands = [Path(__file__).resolve().parents[2] / "docs" / "PLAN.md",
                 Path.cwd() / "docs" / "PLAN.md", Path.cwd() / "CHANGELOG.md"]
        src = next((p for p in cands if p.is_file()), None)
        if src is None:
            _print("  no changelog/build-log file found (looked for docs/PLAN.md, CHANGELOG.md)."); return True
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
        _print(f"  {src.name} (last {min(30, len(lines))} lines):")
        for ln in lines[-30:]:
            _print("  " + ln)
        return True
    if action == "debug":
        _print("  debug: use --verbose / /verbose for telemetry detail."); return True
    if action in ("undo", "rollback"):
        return _handle_undo(action, arg, last_task_id)
    if action == "cost":
        tasks_dir = _state_root() / "tasks"
        total = 0.0
        if tasks_dir.exists():
            ts = TaskStore(_state_root())
            for tid in sorted(tasks_dir.iterdir())[-10:]:
                try:
                    st = ts.load(tid.name)
                    c = st.total_cost_usd()
                    total += c
                    if c > 0:
                        _print(f"  {tid.name[:8]}  ${c:.4f}  ({st.goal[:40]})")
                except Exception:
                    pass
        _print(f"  total (recent): ${total:.4f}")
        return True
    if action == "providers":
        reg = ProviderRegistry.load()
        for ep in reg.endpoints:
            credential = getattr(ep, "credential_state", "missing")
            auth = "configured" if credential == "keyed" else (
                "no-auth" if credential == "no-auth" else "missing"
            )
            _print(f"  {auth:10s} {ep.name:16s}  {ep.base_url[:50]}")
        return True
    if action == "route-info":
        return _handle_route_info(last_task_id)
    if action == "pin":
        if not arg:
            _print("  usage: /pin <role> <model_id>  (e.g. /pin planner <provider>/<model-id>)"); return True
        parts = arg.split(None, 1)
        if len(parts) < 2:
            _print("  usage: /pin <role> <model_id>"); return True
        ov = Overrides.load()
        ov.pin_role(parts[0], parts[1])
        ov.save()
        _print(f"  pinned {parts[0]} → {parts[1]}"); return True
    if action == "unpin":
        role = arg.strip() if arg else ""
        if not role:
            _print("  usage: /unpin <role>  (e.g. /unpin planner)"); return True
        ov = Overrides.load()
        ov.pin_role(role, "")
        ov.save()
        _print(f"  unpinned {role}"); return True
    if action == "stop":
        _print("  use Ctrl+K to stop a running task"); return True
    if action == "raw":
        if not last_task_id:
            _print("  raw: no run yet — the raw model output shows after a task completes."); return True
        try:
            state = TaskStore(_state_root()).load(last_task_id)
        except Exception:  # noqa: BLE001
            _print(f"  raw: cannot load last task {last_task_id!r}."); return True
        txt = _answer_from_state(state)
        _print(txt if txt else "  raw: the last task produced no completed-step output yet.")
        return True
    if action == "code-review":
        import subprocess, shutil
        if not shutil.which("git"):
            _print("  git not installed"); return True
        r = subprocess.run(["git", "diff"], capture_output=True, text=True, timeout=10)
        if not r.stdout.strip():
            r = subprocess.run(["git", "diff", "--staged"], capture_output=True, text=True, timeout=10)
        if not r.stdout.strip():
            _print("  no changes to review (working tree + staging area clean)"); return True
        effort = arg if arg in ("low", "medium", "high") else "medium"
        _print(f"  {len(r.stdout)} chars of diff detected (effort: {effort}).")
        _print("  Run a real reviewer pass with:")
        _print('    syntra run "review the current git diff for bugs and risks"')
        return True
    if action == "grep":
        if not arg:
            _print("  usage: /grep <pattern>"); return True
        import subprocess, shutil
        tool = "rg" if shutil.which("rg") else "grep"
        try:
            if tool == "rg":
                r = subprocess.run(["rg", "-n", "--max-count", "3", arg, "."],
                                   capture_output=True, text=True, timeout=15)
            else:
                r = subprocess.run(["grep", "-rn", "--max-count=3", arg, "."],
                                   capture_output=True, text=True, timeout=15)
            out = r.stdout.strip()
            if out:
                lines = out.split("\n")[:30]
                _print("\n".join(f"  {ln}" for ln in lines))
                if len(out.split("\n")) > 30:
                    _print(f"  ... +{len(out.split(chr(10))) - 30} more")
            else:
                _print(f"  no matches for '{arg}'")
        except subprocess.TimeoutExpired:
            _print("  search timed out")
        except Exception as e:
            _print(f"  search error: {e}")
        return True
    if action == "search":
        # #217: the TUI opens a picker overlay; the plain REPL prints the parsed hits as
        # `path:line  text` (same rg parse) so the command isn't dead outside the cockpit.
        if not arg:
            _print("  usage: /search <pattern>"); return True
        import subprocess, shutil
        from ..core.tui_model import parse_rg_lines, search_result_label
        tool = "rg" if shutil.which("rg") else "grep"
        try:
            cmd = (["rg", "-n", "--max-count", "3", "--", arg, "."] if tool == "rg"
                   else ["grep", "-rn", "--max-count=3", "--", arg, "."])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            hits = parse_rg_lines(r.stdout)[:40]
            if hits:
                for h in hits:
                    _print("  " + search_result_label(h, 100))
            else:
                _print(f"  no matches for '{arg}'")
        except subprocess.TimeoutExpired:
            _print("  search timed out")
        except Exception as e:  # noqa: BLE001
            _print(f"  search error: {e}")
        return True
    if action == "todo":
        import json as _json
        todo_path = _state_root() / "todos.json"
        try:
            todos = _json.loads(todo_path.read_text()) if todo_path.exists() else []
        except (ValueError, OSError):
            todos = []
        parts = (arg.split(None, 1) or [""]) if arg.strip() else [""]
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if sub == "add" and rest:
            todos.append({"text": rest, "done": False})
            todo_path.parent.mkdir(parents=True, exist_ok=True)
            todo_path.write_text(_json.dumps(todos))
            _print(f"  added: {rest}")
        elif sub == "done" and rest.isdigit():
            idx = int(rest) - 1
            if 0 <= idx < len(todos):
                todos[idx]["done"] = True
                todo_path.write_text(_json.dumps(todos))
                _print(f"  done: {todos[idx]['text']}")
        else:
            if todos:
                _print("todos:")
                for i, t in enumerate(todos, 1):
                    mark = "■" if t["done"] else "□"
                    _print(f"  {mark} {i}. {t['text']}")
            else:
                _print("  no todos. /todo add <text>")
        return True
    if action in ("watch", "unwatch"):
        _print(f"  /{action} is TUI-only"); return True
    if action == "simplify":
        _print("  /simplify reviews the diff for cleanup-only improvements (no bug hunt)")
        _print("  run it in the TUI for a live review"); return True
    if action == "browse":
        # ponytail: browser.py deleted (YAGNI at v0.1.0).
        _print("  /browse removed — was over-engineered for v0.1.0. Restore when needed.")
        return True
    if action == "preview":
        # ponytail: browser_preview.py deleted (YAGNI at v0.1.0).
        _print("  /preview removed — was over-engineered for v0.1.0. Restore when needed.")
        return True
        _print(f"  {'rendered → ' + str(_out) if ok else 'could not preview: ' + msg}")
        return True
    if action == "image":
        if not arg:
            _print("  usage: /image <path>  — attach an image for the next message"); return True
        from pathlib import Path as _P
        if _P(arg).exists():
            _print("  /image is best in the TUI (attaches the image to your next message)")
        else:
            _print(f"  file not found: {arg}")
        return True
    if action == "feature":
        _print("  /feature runs the multi-agent workflow (explore→architect→review)")
        _print(f"  use it in the TUI, or: syntra run --agent '{arg or '<goal>'}'")
        return True
    if action == "benchmark":
        reg = ProviderRegistry.load()
        _print("benchmark: testing provider response times...")
        import time as _t
        for ep in reg.endpoints[:8]:
            try:
                from ..providers.openai_compat import OpenAICompatibleProvider, ChatMessage
                model = ep.allowed_models[0] if ep.allowed_models else ""
                if not model:
                    continue
                start = _t.time()
                OpenAICompatibleProvider(ep).chat(
                    model, [ChatMessage("user", "say hello")], max_tokens=10,
                )
                elapsed = _t.time() - start
                _print(f"  ✓ {ep.name:16s}  {model[:20]:20s}  {elapsed:.1f}s")
            except Exception as e:
                _print(f"  ✗ {ep.name:16s}  {str(e)[:40]}")
        return True
    if action == "git":
        import subprocess, shutil
        if not shutil.which("git"):
            _print("  git not installed"); return True
        sub = (arg.split(None, 1) or [""]) if arg.strip() else [""]
        cmd = sub[0].lower()
        rest = sub[1] if len(sub) > 1 else ""
        if cmd == "commit":
            diff = subprocess.run(["git", "diff", "--staged", "--stat"], capture_output=True, text=True)
            if not diff.stdout.strip():
                _print("  nothing staged. Use `git add` first."); return True
            msg = rest or "syntra: auto-commit"
            r = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True, timeout=10)
            _print(r.stdout or r.stderr)
        elif cmd == "push":
            r = subprocess.run(["git", "push"] + (rest.split() if rest else []),
                               capture_output=True, text=True, timeout=30)
            _print(r.stdout or r.stderr or "pushed")
        elif cmd == "pr":
            if shutil.which("gh"):
                title = rest or "syntra: changes"
                r = subprocess.run(["gh", "pr", "create", "--title", title, "--fill"],
                                   capture_output=True, text=True, timeout=30)
                _print(r.stdout or r.stderr)
            else:
                _print("  gh CLI not installed (brew install gh)")
        elif cmd == "log":
            r = subprocess.run(["git", "log", "--oneline", "-10", "--no-color"],
                               capture_output=True, text=True, timeout=5)
            _print(r.stdout or "(no commits)")
        elif cmd == "stash":
            r = subprocess.run(["git", "stash"] + (rest.split() if rest else []),
                               capture_output=True, text=True, timeout=10)
            _print(r.stdout or r.stderr or "stashed")
        else:
            _print("  /git commit [msg]  — commit staged changes")
            _print("  /git push          — push to remote")
            _print("  /git pr [title]    — create PR (needs gh CLI)")
            _print("  /git log           — recent commits")
            _print("  /git stash         — stash changes")
        return True
    if action == "rules":
        return _handle_rules(arg)
    if action == "title":
        name = (arg or "").strip()
        if not last_task_id:
            _print("  no active session yet — start a task first, then /title <name>")
            return True
        try:
            ok = TaskStore(_state_root()).set_title(last_task_id, name)
        except Exception as e:  # noqa: BLE001
            _print(f"  title error: {e}"); return True
        if name:
            _print(f"  session titled: {name}" if ok else "  could not set title")
        else:
            _print("  session title cleared (using the goal)")
        return True
    if action == "compare":
        return _handle_compare(arg)
    if action == "hardware":
        import platform, shutil
        lines = ["system hardware:"]
        lines.append(f"  OS:     {platform.system()} {platform.release()}")
        lines.append(f"  CPU:    {platform.processor() or platform.machine()}")
        try:
            import os as _os
            mem = _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES")
            lines.append(f"  RAM:    {mem // (1024**3)} GB")
        except Exception:
            lines.append("  RAM:    unknown")
        # Check for GPU
        if shutil.which("nvidia-smi"):
            import subprocess
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                                    "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
                for gpu_line in r.stdout.strip().split("\n"):
                    if gpu_line.strip():
                        lines.append(f"  GPU:    {gpu_line.strip()}")
            except Exception:
                lines.append("  GPU:    nvidia-smi available but query failed")
        else:
            lines.append("  GPU:    none detected (nvidia-smi not found)")
        # Recommend models based on RAM
        try:
            ram_gb = mem // (1024**3)
            lines.append("")
            lines.append("recommended local models:")
            if ram_gb >= 64:
                lines.append("  ✓ llama3:70b, qwen2.5:72b, deepseek-r1:70b")
            if ram_gb >= 32:
                lines.append("  ✓ llama3:8b, qwen3:30b, deepseek-r1:14b, gemma4:26b")
            if ram_gb >= 16:
                lines.append("  ✓ llama3.2:3b, gemma3:12b, deepseek-r1:latest")
            if ram_gb >= 8:
                lines.append("  ✓ gemma3:1b, llama3.2:1b, phi3:mini")
        except Exception:
            pass
        # Check Ollama
        if shutil.which("ollama"):
            lines.append("")
            lines.append("  ollama: ✓ installed")
            try:
                r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    lines.append("  local models:")
                    for ml in r.stdout.strip().split("\n")[:5]:
                        lines.append(f"    {ml}")
            except Exception:
                pass
        else:
            lines.append("  ollama: ✗ not installed (brew install ollama)")
        _print("\n".join(lines)); return True
    if action == "download-model":
        if not arg:
            _print("  usage: /download-model <model-name>  (e.g. llama3:8b)"); return True
        import shutil, subprocess
        if not shutil.which("ollama"):
            _print("  ollama not installed. Install: brew install ollama"); return True
        _print(f"  downloading {arg} via ollama...")
        try:
            r = subprocess.run(["ollama", "pull", arg], capture_output=True, text=True, timeout=600)
            _print(r.stdout or r.stderr or "done")
        except subprocess.TimeoutExpired:
            _print("  download timed out (10 min limit)")
        return True
    if action == "btw":
        _print(f"  quick question: {arg or '(ask something)'}"); return True
    if action == "usage":
        tasks_dir = _state_root() / "tasks"
        if tasks_dir.exists():
            ts = TaskStore(_state_root())
            total_cost = 0.0
            role_costs: dict[str, float] = {}
            role_tokens: dict[str, int] = {}
            for td in sorted(tasks_dir.iterdir())[-5:]:
                try:
                    st = ts.load(td.name)
                    for c in st.costs:
                        total_cost += c.cost_usd
                        role_costs[c.role] = role_costs.get(c.role, 0.0) + c.cost_usd
                        role_tokens[c.role] = role_tokens.get(c.role, 0) + c.input_tokens + c.output_tokens
                except Exception:
                    pass
            _print("usage (recent sessions):")
            for role in ("planner", "executor", "reviewer"):
                c = role_costs.get(role, 0)
                t = role_tokens.get(role, 0)
                _print(f"  {role:10s}  ${c:.4f}  {t} tokens")
            _print(f"  {'total':10s}  ${total_cost:.4f}")
        else:
            _print("  no usage data")
        return True
    if action == "skills":
        from ..core.plugin_loader import bundled_skills, discover_plugins
        _print("skills:")
        for s in bundled_skills():
            _print(f"  ● {s.name:12s} {s.description[:50]}")
        for p in discover_plugins():
            for s in p.skills:
                _print(f"  ○ {s.name:12s} ({p.name}) {s.description[:40]}")
        return True
    if action == "skill-create":
        if not arg:
            _print("  usage: /skill-create <name>  — creates a skill template"); return True
        skill_dir = Path.home() / ".config" / "syntra" / "plugins" / "user-skills" / "skills" / arg
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            skill_md.write_text(
                f"---\nname: {arg}\ndescription: Describe when this skill triggers\nmodel: inherit\n---\n\n"
                f"You are performing the '{arg}' task. Describe the method here.\n"
            )
        _print(f"  created skill template: {skill_md}\n  edit it, then /skills to verify it loads")
        return True
    if action == "agent-create":
        if not arg:
            _print("  usage: /agent-create <name>  — creates an agent template"); return True
        agent_dir = Path.home() / ".config" / "syntra" / "plugins" / "user-skills" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_md = agent_dir / f"{arg}.md"
        if not agent_md.exists():
            agent_md.write_text(
                f"---\nname: {arg}\ndescription: Use this agent when...\nmodel: inherit\ncolor: blue\ntools: [\"Read\", \"Grep\"]\n---\n\n"
                f"You are the '{arg}' agent. Describe your role and method here.\n"
            )
        _print(f"  created agent template: {agent_md}\n  edit it, then /plugins to verify it loads")
        return True
    if action == "plugins":
        from ..core.plugin_loader import discover_plugins, plugin_summary
        plugins = discover_plugins()
        _print(plugin_summary(plugins)); return True
    if action == "loop":
        _print(f"  /loop is TUI-only — runs '{arg or 'command'}' on a recurring interval"); return True
    if action == "batch":
        _print(f"  /batch is TUI-only — runs '{arg or 'task'}' across git worktrees in parallel"); return True
    if action == "rewind":
        _print(f"  /rewind is TUI-only — removes last {arg or '1'} messages from conversation"); return True
    if action == "hooks":
        from ..core.hook_engine import HookEngine, DEFAULT_HOOKS
        engine = HookEngine()
        engine.load()
        hooks = engine.list_hooks() or DEFAULT_HOOKS
        _print("hooks:")
        for h in hooks:
            status = "✓" if h.enabled else "○"
            _print(f"  {status} {h.name:20s}  {h.event:6s}  {h.action:5s}  {h.pattern[:30]}")
        return True
    if action == "hook-add":
        parts = arg.split(None, 4)
        if len(parts) < 5:
            _print("  usage: /hook-add <name> <event> <pattern> <warn|block> <message>"); return True
        from ..core.hook_engine import HookEngine, Hook
        engine = HookEngine()
        engine.load()
        engine.add(Hook(name=parts[0], event=parts[1], pattern=parts[2],
                        action=parts[3], message=parts[4]))
        engine.save()
        _print(f"  added hook: {parts[0]}"); return True
    if action == "hook-remove":
        if not arg:
            _print("  usage: /hook-remove <name>"); return True
        from ..core.hook_engine import HookEngine
        engine = HookEngine()
        engine.load()
        if engine.remove(arg):
            engine.save()
            _print(f"  removed: {arg}")
        else:
            _print(f"  hook '{arg}' not found")
        return True
    if action == "map":
        from ..core.repo_map import build_repo_map
        rm = build_repo_map(str(Path.cwd()))
        _print(rm.summary(30)); return True
    if action == "symbols":
        # Named-symbol lookup: WHERE a class/function is defined (not a text search).
        q = (arg or "").strip()
        if not q:
            _print("  usage: /symbols <name>   (finds where a class/function is defined; "
                   "partial match, e.g. /symbols login)")
            return True
        from ..core.repo_map import build_repo_map
        rm = build_repo_map(str(Path.cwd()))
        hits = rm.find_symbols(q)
        if not hits:
            _print(f"  no symbol matching {q!r} found in the workspace "
                   f"(searched {rm.file_count} files). Try /grep for a text search.")
            return True
        _print(f"symbols matching {q!r} ({len(hits)} definition{'s' if len(hits) != 1 else ''}):")
        for h in hits[:50]:
            _print(f"  {h.symbol:28} {h.path} ({h.language})")
        if len(hits) > 50:
            _print(f"  ... +{len(hits) - 50} more")
        return True
    if action == "history":
        # Read-only task list WITH the per-task $cost column (the reason /history exists apart
        # from the /sessions picker).
        store = TaskStore(_state_root())
        rows = store.list_tasks()
        if not rows:
            _print(f"(no tasks under {_state_root()})")
            return True
        rows = sorted(rows, key=lambda m: float(m.get("updated", 0.0) or 0.0), reverse=True)
        _print(f"{'TASK_ID':22} {'STATUS':9} {'COST':>9}  GOAL")
        for m in rows[:40]:
            tid = str(m.get("task_id", ""))
            cost = store.task_cost(tid)
            goal = str(m.get("goal", ""))[:56]
            _print(f"{tid[:22]:22} {str(m.get('status',''))[:9]:9} ${cost:>8.4f}  {goal}")
        return True
    if action == "bg":
        _print("  /bg is TUI-only — run a goal in background while you keep working"); return True
    if action == "jobs":
        _print("  /jobs is TUI-only — shows background tasks"); return True
    if action == "deep-research":
        _print("  deep research runs in the TUI (`syntra`, then `/deep-research <topic>`) "
               "or via `syntra research <topic>`."); return True
    if action == "council":
        _print("  /council runs a goal with several agents in parallel — use it in the TUI "
               "(`syntra`, then `/council <goal>`) and watch them in the agents panel."); return True
    if action == "plan-review":
        _print("  /plan-review (TUI) toggles whether tasks pause for plan approval before "
               "running. Default OFF — a task you give just runs."); return True
    if action == "commit-style":
        # How the AGENT formats the git commits it makes. off=plain message only · minimal=+ key
        # decision · neutral=+ Task/Decision/Rejected (no product name) · branded=+ Syntra-* trailers.
        want = (arg or "").strip().lower()
        if want in _COMMIT_STYLES:
            _save_commit_style(want)
            _print(f"  commit style: {want}  (git commits the agent makes will use this)")
        elif want:
            _print(f"  unknown style '{want}'. Use: {' | '.join(_COMMIT_STYLES)}")
        else:
            cur = _load_commit_style() or "(unset — you'll be asked on the first agent commit)"
            _print(f"  commit style: {cur}")
            _print(f"  set with: /commit-style {' | '.join(_COMMIT_STYLES)}")
        return True
    if action == "replay":
        from ..core import rollout
        parts = (arg or "").split()
        want_json = "json" in parts or "--json" in parts
        parts = [p for p in parts if p not in ("json", "--json")]
        tid = (parts[0] if parts else "").strip() or last_task_id
        if not tid:
            _print("  usage: /replay [json] <task-id>  (or run a task first)"); return True
        if want_json:
            import json as _json
            _print(_json.dumps(rollout.report(_state_root(), tid), ensure_ascii=False, indent=2))
        else:
            _print(rollout.replay(_state_root(), tid))
        return True
    if action == "ab_handoff":
        from ..core import ab as _ab
        parts = (arg or "").split()
        tid = (parts[0].strip() if parts else "") or last_task_id
        if not tid:
            _print("  usage: /ab-handoff [task-id] [step-id]  (or run a task first)"); return True
        step = parts[1].strip() if len(parts) > 1 else ""
        try:
            rep = _ab.compare_handoff(TaskStore(_state_root()), tid, step_id=step or None)
        except Exception as e:  # noqa: BLE001
            _print(f"  error: {e}"); return True
        _print(f"  ab-handoff: task {rep.task_id} step {rep.step_id}")
        _print(f"    A ({rep.a_mode}): {len(rep.a_text)} chars")
        _print(f"    B ({rep.b_mode}): {len(rep.b_text)} chars")
        try:
            st = TaskStore(_state_root()).load(rep.task_id)
            _print(f"    wrote: {Path(st.task_dir) / 'ab_handoff.json'}")
        except Exception:  # noqa: BLE001
            pass
        return True
    if action == "spend":
        from ..core import spend as _spend
        import time as _time
        parts = (arg or "").split()
        days = int(parts[0]) if parts and parts[0].isdigit() else 7
        try:
            rep = _spend.spend_report(_state_root(), days=days, now=_time.time(), limit=10)
            _print(f"  spend (last {rep['window_days']}d): ${rep['total_usd']:.4f}  "
                   f"({rep.get('entries', 0)} ledger entries)")
            for row in rep.get("tasks", []) or []:
                label = (row.get("title") or row.get("goal") or "").strip()
                tail = (f"  — {label}" if label else "")
                _print(f"    {row.get('task_id','')[:8]}  ${float(row.get('usd', 0.0)):.4f}{tail}")
        except Exception as e:  # noqa: BLE001
            _print(f"  spend: unavailable ({e})")
        return True
    if action == "effort":
        valid = ("low", "medium", "high", "xhigh")
        if arg in valid:
            os.environ["SYNTRA_REASONING_EFFORT"] = arg
            _print(f"  reasoning effort: {arg}")
        else:
            cur = os.environ.get("SYNTRA_REASONING_EFFORT", "(auto)")
            _print(f"  current: {cur}  ·  options: {', '.join(valid)}")
        return True
    if action in ("trace", "wizard", "resume_question"):
        _label = {"trace": "fold/unfold the background activity trace",
                  "wizard": "open the interactive question wizard",
                  "resume_question": "reopen a paused question"}[action]
        _print(f"  {action.replace('_', '-')}: {_label} — interactive in the TUI")
        return True
    if action == "init":
        _print("  project setup: run `syntra init` to write a chmod-600 providers.json,")
        _print("  and create .syntra/rules.md for project rules (auto-injected into runs).")
        return True
    if action == "agents":
        try:
            cat = Catalog.load(_catalog_path())
            registry = ProviderRegistry.load()
            ov = Overrides.load()
            rh = _route_health()
            router = _build_router(cat, registry, ov, rh)
            _print("agents (routed per role):")
            for role in ("planner", "executor", "reviewer"):
                try:
                    dec = router.pick(TaskProfile(role=role))
                    short = dec.model.id.split("/")[-1] if "/" in dec.model.id else dec.model.id
                    _print(f"  {role:<9}  {short}  via {dec.provider or '?'}")
                except NoModelAvailable:
                    _print(f"  {role:<9}  (no model available)")
            # sub-agents: show the pin if set, else they follow the executor's model
            sub_pin = ov.pinned_model_for("subagent")
            if sub_pin:
                _sub = sub_pin.split("/")[-1] if "/" in sub_pin else sub_pin
                _print(f"  {'subagent':<9}  {_sub}  (pinned)")
            else:
                _print(f"  {'subagent':<9}  (same as executor)")
            _print("")
            _print("  /models pin <role> <model_id>  to assign a specific model")
            _print("  /agent <goal>                  to run in agentic mode")
        except Exception as e:  # noqa: BLE001
            _print(f"  agents: {e}")
        return True
    if action == "mode":
        from ..core import cost_modes
        if arg:
            # T5/T17: /mode <name> sets the persisted COST mode (budget|im-a-millionaire|pennies).
            if cost_modes.is_known(arg):
                m = cost_modes.save_mode(_state_root(), arg)
                _print(f"  cost mode set: {m}  (governs all runs until changed)")
                if m == cost_modes.BUDGET:
                    _print("    budget: cheapest-strong-enough executor+reviewer; may ask to raise on hard tasks")
                elif m == cost_modes.MILLIONAIRE:
                    _print("    im-a-millionaire: quality-max — frontier models allowed everywhere")
                else:
                    _print("    pennies: max saving — cheapest viable across all roles + chat")
                return True
            _print(f"  unknown mode '{arg}'. cost modes: budget | im-a-millionaire | pennies")
            return True
        _print(f"  cost mode: {cost_modes.load_mode(_state_root())}  "
               f"(set with /mode budget|im-a-millionaire|pennies)")
        _print("    budget            balanced — cheapest-strong-enough, asks before frontier spend")
        _print("    im-a-millionaire  quality-max — frontier allowed everywhere")
        _print("    pennies           max saving — cheapest viable everywhere")
        _print("")
        _print("  run modes:")
        _print("    plain        syntra run <goal>             — planner → executor → reviewer")
        _print("    agent        /agent <goal> | run --agent   — tool-using executor (reads/writes/runs)")
        _print("    research     /deep-research | run --deep-research — adversarial multi-model research")
        _print("    compare      /compare <q>                  — N models answer, compared + synthesized")
        _print("    proof-only   run --proof-only              — only verified, evidence-backed claims")
        return True
    if action == "context":
        # T6: /context full | brief — flip how much conversation each role sees.
        a = (arg or "").strip().lower()
        if a in ("full", "whole", "all"):
            _save_context_relay(False)
            _print("  context: FULL — the whole conversation is sent to every role")
            _print("    (higher cost, nothing summarized; use for a turn where the brief drops something)")
            return True
        if a in ("brief", "relay", "smart", "on"):
            _save_context_relay(True)
            _print("  context: BRIEF — a concise relay replaces history (cost stays flat as the chat grows)")
            return True
        if a:
            _print(f"  unknown context mode '{arg}'. use: /context full | /context brief")
            return True
        cur = "brief" if _load_context_relay() else "full"
        _print(f"  context: {cur}  (set with /context full | /context brief)")
        _print("    brief  a crafted relay replaces history — flat cost as the conversation grows (default)")
        _print("    full   send the WHOLE conversation to planner/executor/reviewer — costlier, nothing summarized")
        return True
    if action == "key":
        # /key <provider> <key> [base_url] — add an API key without editing config files. Saved
        # chmod-600; never echoed. Same persistence the TUI seam uses.
        parts = (arg or "").split()
        if len(parts) < 2:
            _print("usage: /key <provider> <key> [base_url]")
            _print("  e.g.  /key openrouter sk-or-... https://openrouter.ai/api/v1")
            _print("  the key is saved with mode 600 and is never shown in output.")
            return True
        # NOTE: do NOT re-import ProviderRegistry here — it is already module-level (line 35).
        # A local re-import binds the name function-locally, which made the earlier /providers
        # and /benchmark branches raise "ProviderRegistry referenced before assignment".
        from ..core.registry import (add_key, write_providers_config,
                                      default_config_path)
        prov, key = parts[0], parts[1]
        burl = parts[2] if len(parts) > 2 else ""
        try:
            reg = ProviderRegistry.load()
            path = getattr(reg, "source_path", None) or default_config_path()
            import json as _json
            try:
                raw = _json.loads(Path(path).read_text())
            except (OSError, ValueError):
                raw = {"providers": []}
            new, summary, notes = add_key(raw, prov, key, base_url=burl)
            if not summary:
                _print("  " + (notes[0] if notes else "no key added"))
                return True
            write_providers_config(path, new.get("providers", []), overwrite=True)
            _print(f"  ✓ {summary}")
            for n in notes:
                _print(f"    note: {n}")
            _print(f"    saved to {path} (chmod 600)")
        except Exception as e:  # noqa: BLE001
            _print(f"  could not save key: {str(e)[:160]}")
        return True
    if action == "skill":
        if not arg:
            _print("usage: /skill <name>  (loads a skill by name)"); return True
        import json as _json
        # First try a bundled/plugin skill
        from ..core.plugin_loader import get_skill
        sk = get_skill(arg)
        if sk:
            _print(f"skill: {sk.name}\n{sk.content[:1500]}"); return True
        from ..core.tools import default_tools, ToolContext, dispatch
        from ..providers.openai_compat import ToolCall
        out = dispatch(ToolCall("s", "skill", _json.dumps({"name": arg})),
                       default_tools(), ToolContext(workspace_root=str(Path.cwd())))
        _print(out); return True
    if action == "layout":
        _print(f"  layout: {arg or '(use in the TUI: /layout default|focus|coding|review)'}")
        _print("  available: default, full, focus, coding, review")
        _print("  /layout save <name>  save current layout")
        _print("  /layout load <name>  load a saved layout")
        return True
    if action == "goal-set":
        if arg:
            (_state_root()).mkdir(parents=True, exist_ok=True)
            (_state_root() / "goal.txt").write_text(arg)
            _print(f"session goal set: {arg}")
        else:
            g = (_state_root() / "goal.txt")
            _print(f"current goal: {g.read_text() if g.exists() else '(none)'}")
        return True
    if action == "shell":
        import subprocess
        if not arg:
            _print("  usage: !<command>"); return True
        try:
            r = subprocess.run(arg, shell=True, capture_output=True, text=True, timeout=120)
            _print(((r.stdout or "") + (r.stderr or "")).rstrip() or f"  (exit {r.returncode})")
        except Exception as e:  # noqa: BLE001
            _print(f"  shell error: {e}")
        return True
    if action == "auto":
        _print("  /auto toggles autopilot in the TUI (bare `syntra`); in this line mode "
               "pass `--autopilot N` to `syntra run`.")
        return True
    if action == "export":
        if arg:
            _print(f"  export: run `syntra rollout {arg}` to replay that session timeline.")
        else:
            _print("  export: use `syntra rollout <task-id>` to replay a session timeline.")
        return True
    if action == "compact":
        _print("  context auto-compacts during agent runs at ~75% of the model window.")
        return True
    if action == "copy":
        _print("  in the TUI, press Ctrl-Y to select a message and 'c' to copy it.")
        return True
    if action == "attach":
        _print("  /attach stages an image for your next message (vision input).")
        _print("  use it in the `syntra` TUI: /attach <path>, drag a file in, or Ctrl+V for a")
        _print("  copied screenshot. The run auto-routes to a vision-capable model.")
        return True
    if action == "plan":
        # The TUI re-opens the plan card; from the line-REPL, point at the plan file on disk.
        if last_task_id:
            from pathlib import Path as _P
            _pf = _P.cwd() / ".syntra" / "plans" / f"plan-{last_task_id}.md"
            if _pf.is_file():
                _print(f"  plan: {_pf}")
                _print(_pf.read_text(encoding="utf-8"))
                return True
        _print("  /plan: no plan yet — run a task first (the plan is shown as a card in the TUI).")
        return True
    if action == "review":
        # The line-REPL intercepts /review BEFORE this handler and runs a real diff review;
        # this branch is only reached from non-REPL callers. Keep it honest + actionable.
        diff = _git_diff_text()
        if not diff.strip():
            _print("  /review: no changes to review (working tree + staging area clean)")
        else:
            _print("  /review runs a skeptical reviewer over your current git diff.")
            _print("  run it from the interactive session (or `syntra` TUI) to execute it.")
        return True
    if action == "clear":
        _print("\n" * 2 + "[new session — context cleared]")
        return True
    return False


def cmd_session(args: argparse.Namespace) -> int:
    """Interactive session (full-screen modern-CLI style). Bare `syntra` lands here."""
    msg = _startup_health_summary()
    if msg:
        _print(msg)
    _print(_SESSION_INTRO)
    _print("")
    last_task_id = ""
    verbose = False  # background telemetry OFF by default (toggle with /verbose)
    while True:
        try:
            line = input("syntra> ")
        except (EOFError, KeyboardInterrupt):
            _print("")
            break
        action, arg = session_dispatch(line)
        if action == "empty":
            continue
        if action == "exit":
            break
        if action == "help":
            _print(_SESSION_INTRO)
            continue
        if action == "verbose":
            verbose = not verbose
            _print(f"verbose background detail: {'ON' if verbose else 'OFF'}")
            continue
        if action == "tasks":
            cmd_tasks(argparse.Namespace())
            continue
        if action == "unknown":
            _print(f"unknown command: {arg}  (try /help)")
            continue
        if action == "resume":
            tid = arg or last_task_id
            if not tid:
                _print("usage: /resume <task-id>  (or run a task first)")
                continue
            cmd_resume(_run_args(task_id=tid, verbose=verbose))
            continue
        # /review, /code-review, /simplify: RUN a real review of the current diff (T2/T17 —
        # was a printed hint). Build the goal here (impure git read) and submit it like any goal.
        if action in ("review", "code-review", "simplify"):
            _act, _payload = _review_goal(action, arg)
            if _act == "noop":
                _print(f"  {_payload}")
                continue
            cmd_run(_run_args(goal=_payload, yes=True, verbose=verbose))
            continue
        # Real handlers for the slash-commands (each does actual work).
        if _handle_session_action(action, arg, last_task_id):
            continue
        if action == "agent":
            cmd_run(_run_args(goal=arg, yes=True, verbose=verbose, agent=True))
            continue
        # goal / proof-goal -> run it
        proof = (action == "proof-goal")
        cmd_run(_run_args(goal=arg, proof_only=proof, yes=True, verbose=verbose))

    if last_task_id:
        _print(f"(resume later: syntra resume {last_task_id})")
    _print("bye.")
    return 0


def _run_args(*, goal: str = "", task_id: str = "", proof_only: bool = False,
              yes: bool = False, verbose: bool = False, agent: bool = False) -> argparse.Namespace:
    """Build a Namespace with run/resume defaults for session-driven calls."""
    return argparse.Namespace(
        catalog_path=None, goal=goal, task_id=task_id,
        workspace=str(Path.cwd()), quality_bias=0.8,
        planner="", executor="", reviewer="",
        max_output_tokens=8192, expected_steps=5,
        max_steps=20, max_tokens=500_000, max_repeated_failures=2,
        proof_only=proof_only, reasoning="", constraint=[],
        execute=False, autopilot=1, auto_approve=False, verify_command="",
        steer=False, dry_run=False, yes=yes, verbose=verbose,
        no_direct=False, direct_bias=0.4, council=1, agent=agent, stream=False,
        mcp=[], lsp="", image=[],
    )


# ---------------------------------------------------------------- parser


def _add_policy_flags(p: argparse.ArgumentParser) -> None:
    """Automation/safety flags shared by `run` and `exec`, mapped into LoopConfig by
    `_loop_config_from_args`. Kept in one place so the two stay consistent. [A]"""
    p.add_argument("--model", default="",
                   help="pin one model for all roles (per-role --planner/--executor/--reviewer still win)")
    p.add_argument("--approval-policy", dest="approval_policy", default="",
                   choices=["", "untrusted", "on-request", "on-failure", "never"],
                   help="when the agent must ask before tools/edits (engine approval×sandbox matrix)")
    p.add_argument("--sandbox", default="",
                   choices=["", "read-only", "workspace-write", "danger-full-access"],
                   help="what agent shell/file actions may do: read-only auto-runs; "
                        "workspace-write gates mutations; danger-full-access lifts confinement")
    p.add_argument("-c", "--config", dest="config_overrides", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="override a config knob (repeatable), e.g. -c quality_bias=0.9 -c max_steps=10")


def _add_exec_flags(p: argparse.ArgumentParser) -> None:
    """The full one-shot run flag set shared by `exec` and `review` (everything except
    the positional goal). Mapped into LoopConfig by `_loop_config_from_args`. Kept in
    one place so the two stay byte-for-byte consistent. [A]"""
    p.add_argument("--cd", default="", metavar="DIR",
                   help="working directory / workspace root for the run (default: current dir)")
    p.add_argument("--workspace", default="", help=argparse.SUPPRESS)   # accepted alias of --cd
    p.add_argument("--json", action="store_true",
                   help="emit a line-delimited JSON event stream (one record per engine event + terminal done/error)")
    p.add_argument("--output-schema", dest="output_schema", default="", metavar="FILE",
                   help="validate the final answer against a JSON Schema file (exit 4 on mismatch)")
    p.add_argument("--planner", default="")
    p.add_argument("--executor", default="")
    p.add_argument("--reviewer", default="")
    p.add_argument("--quality-bias", type=float, default=0.8)
    p.add_argument("--max-output-tokens", type=int, default=8192)
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--max-tokens", type=int, default=500_000)
    p.add_argument("--max-repeated-failures", type=int, default=2)
    p.add_argument("--wait-for-limits", type=float, default=0.0, metavar="SECONDS",
                   help="#165: on a rate-limit (429/quota), WAIT out the limit and retry the same model "
                        "(up to this many total seconds) instead of failing/downgrading. 0 = off (default)")
    p.add_argument("--reasoning", default="", choices=["", "low", "medium", "high", "xhigh"])
    p.add_argument("--constraint", action="append", default=[],
                   help="durable constraint injected into every step (repeatable)")
    p.add_argument("--proof-only", action="store_true",
                   help="block ungrounded factual claims + over-certainty")
    p.add_argument("--agent", action="store_true",
                   help="agentic tool-using executor (read/grep/edit/run), governed by --approval-policy/--sandbox")
    p.add_argument("--execute", action="store_true", help="apply executor edit blocks (approval-gated)")
    p.add_argument("--auto-approve", action="store_true",
                   help="DANGEROUS: apply edits/tools without per-action approval")
    p.add_argument("--no-direct", action="store_true",
                   help="force full plan->execute->review (no one-call direct answers)")
    p.add_argument("--direct-bias", type=float, default=0.4)
    p.add_argument("--council", type=int, default=1, metavar="N",
                   help="get a plan from N models and judge-pick the best")
    p.add_argument("--mcp", action="append", default=[], metavar="CMD")
    p.add_argument("--lsp", default="", metavar="CMD")
    p.add_argument("--image", action="append", default=[], metavar="PATH")
    p.add_argument("--verify-command", default="",
                   help="command run after execution to ground the verdict (e.g. 'pytest -q')")
    p.add_argument("--deep-research", action="store_true", dest="deep_research")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="human-readable progress lines (ignored under --json)")
    _add_policy_flags(p)   # [A] --model/--approval-policy/--sandbox/-c


def cmd_mcp_server(args: argparse.Namespace) -> int:  # [B]
    """Expose Syntra AS an MCP server over stdio (B3): other tools drive Syntra via the
    run_goal / continue_session JSON-RPC tools. stdout is the protocol channel, so progress
    is SILENCED and every notice goes to stderr — anything else on stdout would corrupt it."""
    import sys as _sys
    import json as _json
    from ..core.mcp_server import LoopRunner, serve_stdio

    def _err(m):
        print(m, file=_sys.stderr, flush=True)

    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _err(f"error: {e}")
        return 2
    rh = _route_health()
    ov = Overrides.load()
    loop = Loop(catalog=cat, store=store, registry=registry, route_health=rh, overrides=ov,
                progress=lambda *a, **k: None)        # SILENT: stdout carries JSON-RPC frames
    config = LoopConfig(auto_approve=getattr(args, "auto_approve", False))
    workspace = getattr(args, "workspace", "") or str(Path.cwd())

    def _history_for(sid: str):
        """Load a prior session's conversation so continue_session threads it (same
        transcript the TUI resume reads). None if absent/unreadable."""
        try:
            data = _json.loads((_state_root() / "transcripts" / f"{sid}.json").read_text())
            return [(m.get("role", ""), m.get("text", "")) for m in data
                    if m.get("role") in ("user", "assistant")]
        except Exception:  # noqa: BLE001
            return None

    runner = LoopRunner(loop, config, workspace_root=workspace, history_for=_history_for)
    _err("syntra mcp-server: ready — JSON-RPC over stdio (Content-Length framed)")
    serve_stdio(runner)
    return 0


def cmd_swarm(args: argparse.Namespace) -> int:  # [B]
    """F24: fan out N parallel agents on a goal and print each answer — the direct 'spin up N
    agents' path, instead of having the planner write a loop."""
    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    loop = Loop(catalog=cat, store=store, registry=registry, route_health=_route_health(),
                overrides=Overrides.load(),
                progress=(_run_progress if getattr(args, "verbose", False) else _quiet_progress))
    n = max(1, min(int(getattr(args, "n", 3) or 3), 16))
    _print(f"spinning up {n} agents on: {args.goal}")
    for label, text in loop.swarm(args.goal, n, config=LoopConfig()):
        _print(f"\n──── {label} ────")
        _print(text or "(no output)")
    return 0


def cmd_campaign(args: argparse.Namespace) -> int:
    """Fan-out kind (ii): run N INDEPENDENT jobs in parallel, each its OWN full
    plan->execute->review pipeline (campaign style). One job per `--job` (repeatable) or one
    per line from a `--jobs-file`."""
    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    jobs = list(getattr(args, "job", None) or [])
    jf = getattr(args, "jobs_file", "") or ""
    if jf:
        try:
            jobs += [ln.strip() for ln in Path(jf).read_text().splitlines() if ln.strip()]
        except OSError as e:
            _print(f"error: cannot read --jobs-file {jf!r}: {e}")
            return 2
    jobs = [j for j in jobs if j]
    if not jobs:
        _print("usage: syntra campaign --job '<goal>' [--job '<goal>' ...]  (or --jobs-file FILE)")
        return 2
    loop = Loop(catalog=cat, store=store, registry=registry, route_health=_route_health(),
                overrides=Overrides.load(),
                progress=(_run_progress if getattr(args, "verbose", False) else _quiet_progress))
    _print(f"running {len(jobs)} independent jobs (each its own plan→execute→review)…")
    failures = 0
    for label, res in loop.campaign(jobs, workspace_root=str(Path.cwd()), config=LoopConfig()):
        verdict = getattr(res, "verdict", "error") if res is not None else "error"
        if verdict != "pass":
            failures += 1
        _print(f"\n──── {label}: {verdict} ────")
        if res is not None:
            _print((getattr(res.state, "summary", "") or "(no summary)")[:600])
    return 1 if failures else 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Ask N different models the same question, print every answer side by side, then the
    judge's synthesized best (or a manually-picked candidate via --pick)."""
    cat = Catalog.load(args.catalog_path or _catalog_path())
    store = TaskStore(_state_root())
    try:
        registry = ProviderRegistry.load()
    except ProviderRegistryError as e:
        _print(f"error: {e}")
        return 2
    loop = Loop(catalog=cat, store=store, registry=registry, route_health=_route_health(),
                overrides=Overrides.load(),
                progress=(_run_progress if getattr(args, "verbose", False) else _quiet_progress))
    n = max(2, min(int(getattr(args, "n", 3) or 3), 8))
    _print(f"comparing {n} models on: {args.question}")
    out = loop.compare(args.question, n, config=LoopConfig())
    cands = out.get("candidates", [])
    if not cands:
        _print(f"  {out.get('rationale', 'no models available')}")
        return 2
    for i, c in enumerate(cands):
        tail = c["provider"]
        _print(f"\n──── candidate {i + 1}: {c['model']} via {tail} ────")
        _print((c["text"] or "(no output)"))
    pick = int(getattr(args, "pick", 0) or 0)
    if 1 <= pick <= len(cands):                     # manual override: show the chosen one
        _print(f"\n════ your pick — candidate {pick} ════")
        _print(cands[pick - 1]["text"] or "(no output)")
        return 0
    best = out.get("best_index", -1)
    _print("\n════ synthesized best ════")
    if 0 <= best < len(cands):
        _print(f"  (judge picked candidate {best + 1}: {cands[best]['model']} — {out.get('rationale','')})")
    _print(out.get("synthesis") or "(no synthesis)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="syntra", description="Smart model coordinator.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}",
                   help="show the installed Syntra version and exit")
    p.add_argument("--catalog-path", default=None)
    p.add_argument("--tui", action="store_true", help="force the full-screen curses TUI (default for bare `syntra` on a real terminal)")
    p.add_argument("--plain", action="store_true", help="force the simple line-based session instead of the full-screen TUI")
    p.add_argument("--inline", action="store_true", help="inline mode: finalized turns go to the terminal's NATIVE scrollback (native wheel/find/copy), live region pinned at the bottom (opt-in; SYNTRA_INLINE=1 also enables)")
    sub = p.add_subparsers(dest="command", required=False)

    sub.add_parser("verify", help="one-shot health/sanity check (no API calls)").set_defaults(func=cmd_verify)

    s = sub.add_parser("init", help="interactive wizard: write a chmod-600 providers.json (keys never echoed)")
    s.add_argument("--path", default="", help="config path (default: ~/.config/syntra/providers.json)")
    s.add_argument("--force", action="store_true", help="overwrite an existing config / AGENTS.md")
    s.add_argument("--agents", action="store_true", help="scaffold an AGENTS.md project guide at the repo root")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="deep foundation health check with actionable fixes")
    s.add_argument("--probe", action="store_true", help="test endpoint reachability (makes network calls)")
    s.add_argument("--probe-models", action="store_true",
                   help="preflight: send a tiny real chat to each role's top picks (across "
                        "their providers) and report which actually work (makes network calls)")
    s.set_defaults(func=cmd_doctor)

    # catalog
    s = sub.add_parser("catalog", help="show model catalog or refresh from artificialanalysis.ai")
    cat_sub = s.add_subparsers(dest="catalog_action")
    cat_sub.add_parser("show", help="show catalog (default)").set_defaults(func=cmd_catalog)
    refresh = cat_sub.add_parser("refresh", help="pull updates from artificialanalysis.ai")
    refresh.add_argument("--dry-run", action="store_true")
    refresh.set_defaults(func=cmd_catalog_refresh)
    s.set_defaults(func=cmd_catalog)

    # route
    s = sub.add_parser("route", help="show what the router would pick for a role")
    s.add_argument("role", choices=["planner", "executor", "reviewer"])
    s.add_argument("--quality-bias", type=float, default=0.8)
    s.add_argument("--needs-tool-use", action="store_true")
    s.add_argument("--needs-long-context", action="store_true")
    s.set_defaults(func=cmd_route)

    # run
    s = sub.add_parser("run", help="run one task through planner -> executor -> reviewer")
    s.add_argument("goal")
    s.add_argument("--workspace", default=str(Path.cwd()))
    s.add_argument("--quality-bias", type=float, default=0.8)
    s.add_argument("--planner", default="")
    s.add_argument("--executor", default="")
    s.add_argument("--reviewer", default="")
    s.add_argument("--max-output-tokens", type=int, default=8192)
    s.add_argument("--expected-steps", type=int, default=5, help="cost preflight: assumed plan step count for the estimate")
    s.add_argument("--max-steps", type=int, default=20, help="loop guard: hard ceiling on plan steps executed")
    s.add_argument("--max-tokens", type=int, default=500_000, help="loop guard: total token budget for the run (0 = unlimited)")
    s.add_argument("--max-repeated-failures", type=int, default=2, help="loop guard: halt after hitting the same wall this many times")
    s.add_argument("--wait-for-limits", type=float, default=0.0, metavar="SECONDS",
                   help="#165: on a rate-limit (429/quota), WAIT out the limit and retry the same model "
                        "(up to this many total seconds) instead of failing/downgrading. 0 = off (default)")
    s.add_argument("--proof-only", action="store_true", help="block ungrounded factual claims + over-certainty (anti-hallucination)")
    s.add_argument("--reasoning", default="", choices=["", "low", "medium", "high", "xhigh"], help="base reasoning effort (escalated by task risk; only sent to capable models)")
    s.add_argument("--constraint", action="append", default=[], help="durable constraint injected into every step + persisted (repeatable), e.g. --constraint 'never use globals'")
    s.add_argument("--yes", "-y", action="store_true", help="skip confirmation prompt before spending")
    s.add_argument("--dry-run", action="store_true", help="show route+cost plan, do not call any provider")
    s.add_argument("--verbose", "-v", action="store_true", help="show background detail (routes, usage, cost table, reliability); default OFF")
    s.add_argument("--steer", action="store_true", help="accept live mid-run instructions from stdin: plain line = queued follow-up, '!'-prefixed = instant steer")
    s.add_argument("--execute", action="store_true", help="parse executor edit blocks and apply them (approval-gated; checkpointed for rollback)")
    s.add_argument("--autopilot", type=int, default=1, metavar="N", help="keep working up to N passes until the completion audit passes (req A10); bounded by LoopGuard")
    s.add_argument("--no-direct", action="store_true", help="disable one-call direct answers; force full plan->execute->review for everything")
    s.add_argument("--direct-bias", type=float, default=0.4, help="cost/quality knob for direct chat answers (0=cheapest capable, 1=best); default 0.4")
    s.add_argument("--council", type=int, default=1, metavar="N", help="get a plan from N different models and judge-pick the best (costs more; default 1 = off)")
    s.add_argument("--auto-approve", action="store_true", help="DANGEROUS: apply edits without per-edit approval (requires --execute)")
    s.add_argument("--agent", action="store_true", help="agentic executor: the model uses tools (read/grep/edit/run) to do the work directly; asks permission per risky tool (allow once/session/reject)")
    s.add_argument("--agent-brain", default="", metavar="NAME", help="use an INSTALLED agent's persona as the brain across planner/executor/reviewer (any installed agent by name)")
    s.add_argument("--stream", action="store_true", help="stream model tokens live (when the provider supports it)")
    s.add_argument("--mcp", action="append", default=[], metavar="CMD", help="add an MCP server and use its tools (repeatable). stdio: --mcp 'npx -y @modelcontextprotocol/server-github'  |  HTTP: --mcp 'https://host/mcp [bearer-token]' (or set SYNTRA_MCP_TOKEN)")
    s.add_argument("--lsp", default="", metavar="CMD", help="start a language server and feed its diagnostics to the reviewer, e.g. --lsp 'pyright-langserver --stdio'")
    s.add_argument("--image", action="append", default=[], metavar="PATH", help="attach an image (png/jpeg/gif/webp) to the goal for vision models (repeatable)")
    s.add_argument("--verify-command", default="", help="command run after execution to ground the verdict in a real check (e.g. 'pytest -q'); a failing check forces verdict=fail")
    s.add_argument("--deep-research", action="store_true", dest="deep_research", help="deep-research mode: decompose into angles (incl. a disconfirming one), search + read sources via web tools, synthesize a cited report, and cross-verify every citation with a different-family auditor")
    s.add_argument("--no-learn", dest="learn", action="store_false", help="don't let this run learn durable facts into session-memory.json (the Librarian; on by default)")
    s.add_argument("--mode", default="", help="cost mode: budget (default, balanced) | im-a-millionaire (quality-max, frontier allowed) | pennies (max saving). Governs ALL routing. Overrides the /mode persisted default for this run.")
    s.set_defaults(learn=True)
    _add_policy_flags(s)   # [A] --model/--approval-policy/--sandbox/-c
    s.set_defaults(func=cmd_run)

    # exec — non-interactive one-shot for automation/CI (headless; --json => JSONL)
    e = sub.add_parser("exec", aliases=["x"],
                       help="non-interactive one-shot run for automation/CI; --json emits a JSONL event stream")
    e.add_argument("goal", help="the task to run")
    _add_exec_flags(e)
    e.set_defaults(func=cmd_exec)

    # review — one-shot reviewer over the working-tree diff (reuses the exec machinery,
    # so --json / --output-schema / all run flags work the same). [A]
    r = sub.add_parser("review",
                       help="review your working-tree changes (git diff) through the engine")
    r.add_argument("paths", nargs="*",
                   help="limit the review to these paths (default: all changes)")
    r.add_argument("--staged", action="store_true",
                   help="review staged changes (git diff --cached)")
    _add_exec_flags(r)
    r.set_defaults(func=cmd_review)

    # apply — land a unified-diff patch file on the working tree (validated first). [A]
    ap = sub.add_parser("apply",
                        help="apply a unified-diff patch file to the working tree (validated first)")
    ap.add_argument("patch", nargs="?", default="", help="path to a .patch / .diff file")
    ap.add_argument("--cd", default="", metavar="DIR", help="working directory to apply in")
    ap.add_argument("--check", action="store_true", help="validate only — do not modify files")
    ap.set_defaults(func=cmd_apply)

    # resume
    s = sub.add_parser("resume", help="resume a session (no id = your most recent one)")
    s.add_argument("task_id", nargs="?", default="",
                   help="session id (optional — omit to continue your most recent session)")
    s.add_argument("--quality-bias", type=float, default=0.8)
    s.add_argument("--planner", default="")
    s.add_argument("--executor", default="")
    s.add_argument("--reviewer", default="")
    s.add_argument("--max-output-tokens", type=int, default=8192)
    s.add_argument("--max-steps", type=int, default=20)
    s.add_argument("--max-tokens", type=int, default=500_000)
    s.add_argument("--max-repeated-failures", type=int, default=2)
    s.add_argument("--wait-for-limits", type=float, default=0.0, metavar="SECONDS",
                   help="#165: on a rate-limit (429/quota), WAIT out the limit and retry the same model "
                        "(up to this many total seconds) instead of failing/downgrading. 0 = off (default)")
    s.add_argument("--proof-only", action="store_true")
    s.add_argument("--reasoning", default="", choices=["", "low", "medium", "high", "xhigh"])
    s.add_argument("--constraint", action="append", default=[], help="durable constraint (repeatable); merged with persisted memory")
    s.set_defaults(func=cmd_resume)

    # swarm — fan out N parallel agents on a goal (F24)  # [B]
    s = sub.add_parser("swarm", help="fan out N parallel agents on a goal (no planner loop)")
    s.add_argument("n", type=int, help="number of agents to spin up (1-16)")
    s.add_argument("goal", help="the goal each agent runs")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_swarm)

    s = sub.add_parser("campaign", help="run N INDEPENDENT jobs in parallel, each its own full "
                                        "plan→execute→review pipeline (vs swarm = workers on one goal)")
    s.add_argument("--job", action="append", help="a job goal (repeatable); each runs its own pipeline")
    s.add_argument("--jobs-file", default="", help="file with one job goal per line")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_campaign)

    s = sub.add_parser("compare", help="ask N different models the same question, show every "
                                       "answer side by side, and synthesize the best")
    s.add_argument("question", help="the question to ask every model")
    s.add_argument("-n", type=int, default=3, help="how many distinct models to compare (2-8)")
    s.add_argument("--pick", type=int, default=0, metavar="N",
                   help="skip the judge and just show candidate N as the answer (manual pick)")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_compare)

    # mcp-server — expose Syntra AS an MCP server over stdio (B3, local only)  # [B]
    s = sub.add_parser("mcp-server", help="expose Syntra as an MCP server over stdio (JSON-RPC)")
    s.add_argument("--workspace", default=str(Path.cwd()), help="working directory for run_goal")
    s.add_argument("--auto-approve", action="store_true", help="let run_goal use tools without prompting")
    s.set_defaults(func=cmd_mcp_server)

    # providers / tasks
    p_prov = sub.add_parser("providers", help="list configured providers")
    p_prov.add_argument("--free", action="store_true", help="show free/token-saver provider presets to add")
    p_prov.set_defaults(func=cmd_providers)
    prov_sub = p_prov.add_subparsers(dest="prov_action")
    rmk = prov_sub.add_parser("remove-key",
        help="remove an API key using a private suffix — dry-run unless --yes")
    rmk.add_argument("provider", help="provider name (e.g. openrouter, deepseek)")
    rmk.add_argument("tail", help="private key suffix used to select the credential")
    rmk.add_argument("--yes", action="store_true", help="actually write the change (backs up first)")
    rmk.set_defaults(func=cmd_providers_remove_key)

    # L6: manage user-declared local model servers (providers.json `local_models`).
    p_local = sub.add_parser("local", help="manage local model servers (providers.json local_models)")
    p_local.set_defaults(func=cmd_local)
    local_sub = p_local.add_subparsers(dest="local_action")
    local_sub.add_parser("list", help="list declared local models").set_defaults(func=cmd_local)
    local_sub.add_parser("status", help="show declared local models + their state").set_defaults(func=cmd_local)
    ls = local_sub.add_parser("start", help="spawn + register a declared local model now")
    ls.add_argument("model_id", help="the declared local model id to start")
    ls.set_defaults(func=cmd_local)

    s = sub.add_parser("update", help="check for a newer Syntra and upgrade (notify → consent → remind)")
    s.add_argument("--check", action="store_true", help="only check; don't upgrade")
    s.add_argument("--yes", action="store_true", help="upgrade without prompting")
    s.set_defaults(func=cmd_update)

    p_login = sub.add_parser("login", help="browser login for a provider (OAuth device-code)")
    p_login.add_argument("provider", help="provider name (must have an 'oauth' block in providers.json)")
    p_login.add_argument("--refresh", action="store_true", help="renew using the stored refresh token instead of a full login")
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="delete a provider's stored browser-login token")
    p_logout.add_argument("provider", help="provider name")
    p_logout.set_defaults(func=cmd_logout)
    s = sub.add_parser("task", help="show stored state for one task")
    s.add_argument("task_id")
    s.add_argument("--step", default="", help="show full output for a specific step (e.g. --step s1)")
    s.add_argument("--handoff", action="store_true", help="show the continuity handoff (goal + decisions + failures + current step)")
    s.set_defaults(func=cmd_task)
    s = sub.add_parser("tasks", help="list all tasks")
    s.add_argument("--all", action="store_true", help="include archived sessions")
    s.set_defaults(func=cmd_tasks)
    # session lifecycle [A]: archive (hide) / unarchive (restore) / delete (remove)
    s = sub.add_parser("archive", help="hide a session from listings (keeps its data)")
    s.add_argument("task_id")
    s.set_defaults(func=cmd_archive)
    s = sub.add_parser("unarchive", help="restore an archived session")
    s.add_argument("task_id")
    s.set_defaults(func=cmd_unarchive)
    s = sub.add_parser("delete", help="permanently delete a session's stored state")
    s.add_argument("task_id")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_delete)
    # shell completion [A]
    s = sub.add_parser("completion", help="print a shell-completion script (bash|zsh)")
    s.add_argument("shell", nargs="?", default="bash", choices=["bash", "zsh"],
                   help="target shell (default: bash)")
    s.set_defaults(func=cmd_completion)
    # skills / install (A4) — [A] CLI surface
    s = sub.add_parser("skills", help="list built-in + installed skills, show one, or match a goal")
    s.add_argument("name", nargs="?", default="", help="show this skill's full body")
    s.add_argument("--match", default="", metavar="GOAL",
                   help="rank skills by relevance to a free-text goal")
    s.set_defaults(func=cmd_skills)
    s = sub.add_parser("install", help="install agents/skills from a folder, .md file, or git URL")
    s.add_argument("source", nargs="?", default="",
                   help="local path, file.md, or git/http URL (omit to list installed)")
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("plugins", help="list installed plugins (agents/skills), or enable/disable one")
    s.add_argument("--enable", default="", metavar="NAME", help="re-enable a disabled plugin")
    s.add_argument("--disable", default="", metavar="NAME", help="disable a plugin (kept on disk)")
    s.set_defaults(func=cmd_plugins)
    s = sub.add_parser("models", help="show/set per-role model assignments (Auto/Manual)")
    s.add_argument("role", nargs="?", default="", help="planner|executor|reviewer|subagent (omit to show the board)")
    s.add_argument("model", nargs="?", default="", help="model id to pin, or 'auto' to clear the pin")
    s.set_defaults(func=cmd_models)
    s = sub.add_parser("rollout", help="replay a task's event timeline, or branch a new task from a point in it")
    s.add_argument("task_id")
    s.add_argument("--branch-at", type=int, default=None, metavar="N",
                   help="fork a new task keeping events [0:N] (re-run from that point)")
    s.set_defaults(func=cmd_rollout)

    # route health
    sub.add_parser("route-health", help="show route health records").set_defaults(func=cmd_route_health)
    s = sub.add_parser("stats", help="show learned per-route outcome stats (routes.json)")
    s.add_argument("--json", action="store_true", help="output raw JSON")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("bench", help="cost-per-success benchmark: routed stack vs one pinned model (live calls)")
    s.add_argument("--baseline-model", required=True, help="model id to pin for ALL roles in the baseline (the 'one expensive model' comparison)")
    s.add_argument("--task", action="append", default=[], help="benchmark task (repeatable); defaults to a built-in set")
    s.add_argument("--workspace", default=str(Path.cwd()))
    s.add_argument("--quality-bias", type=float, default=0.8)
    s.set_defaults(func=cmd_bench)
    s = sub.add_parser("route-health-clear", help="clear route health records")
    s.add_argument("--provider", default=None)
    s.add_argument("--model", default=None)
    s.set_defaults(func=cmd_route_health_clear)

    # overrides
    s = sub.add_parser("route-blacklist", help="add a model/route to the blacklist")
    s.add_argument("model"); s.add_argument("--provider", default=None); s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_route_blacklist)

    s = sub.add_parser("route-penalty", help="add a score penalty to a model/route")
    s.add_argument("model"); s.add_argument("multiplier", type=float, help=">=0.0 (1.0 = no change, <1 penalty, >1 boost)")
    s.add_argument("--provider", default=None); s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_route_penalty)

    return p


def _make_permission_ask():
    """Live 3-choice tool-permission prompt for --agent runs (allow once/session/reject).

    Returns a callable(name, danger, args) -> 'once' | 'session' | 'reject'. On a
    non-interactive stdin it denies (safe default); the user can pass --auto-approve
    to skip prompting entirely.
    """
    import sys as _sys
    from ..core.permissions import (ALLOW_ONCE, ALLOW_SESSION, ALLOW_ALWAYS,
                                    DENY_ONCE, DENY_ALL)

    def ask(name, danger, args):
        if not _sys.stdin.isatty():
            return DENY_ONCE
        detail = ""
        if name in ("bash", "run", "exec_command"):
            detail = f"  $ {args.get('command', '')}"
        elif name in ("write", "edit"):
            detail = f"  path: {args.get('path', '')}"
        _print(f"\n[permission] the agent wants to use '{name}' ({danger}).{detail}")
        _print("  [1] allow once   [2] allow for this session   [3] allow always (remember)   "
               "[4] deny once   [5] deny all (stop asking this)")
        try:
            choice = input("  choice [1/2/3/4/5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return DENY_ONCE
        return {"1": ALLOW_ONCE, "2": ALLOW_SESSION, "3": ALLOW_ALWAYS,
                "4": DENY_ONCE, "5": DENY_ALL}.get(choice, DENY_ONCE)

    return ask


def _make_question_ask():
    """Live prompt for the agent's `question` tool (clarifying questions).

    Returns callable(question)->answer. Non-interactive stdin -> empty answer
    (the tool then tells the agent to proceed with a stated assumption)."""
    import sys as _sys

    def ask(question):
        if not _sys.stdin.isatty():
            return ""
        _print(f"\n[agent asks] {question}")
        try:
            return input("  your answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    return ask


def _make_tui_question_ask(on_event, steering):
    """TUI clarifying-question callback (F40). The engine's _maybe_clarify calls this BLOCKING
    on the run's worker thread when the analyzer flags a genuinely under-specified goal. We:
      1. emit a `[question]` line → the TUI's _drain opens the interactive wizard,
      2. block this worker thread (the TUI keeps drawing on the main thread) until the user's
         answer arrives back through the steering inbox as an `[answers] {json}` steer — OR the
         user just types a plain follow-up, which we also accept as the answer,
      3. return the answer text so the engine folds Q+A into context before planning.
    Bounded by a deadline + the inbox stop signal so it can never hang the run forever."""
    import time as _t
    from ..core.question_wizard import format_question_request, parse_answers

    def ask(question: str) -> str:
        q = (question or "").strip()
        if not q or steering is None:
            return ""
        # open the wizard as ONE free-text clarifying step (the analyzer asks a single question).
        spec = {"title": "Quick clarification", "steps": [
            {"id": "clarify", "prompt": q, "kind": "text", "allow_chat": False}]}
        try:
            on_event(format_question_request(spec), "tool")
        except Exception:  # noqa: BLE001 - a UI emit hiccup must not break the run
            return ""
        deadline = 600.0       # hard ceiling: never block a run forever waiting on an answer
        waited = 0.0
        while waited < deadline:
            if steering.should_stop():     # user interrupted the whole run
                return ""
            for line in steering.drain_steering():
                if line.strip().startswith("[answers]"):
                    # the wizard responded — consume it via parse_answers ONLY (never treat an
                    # [answers] line as a plain follow-up, or a null/empty payload leaks raw).
                    ans = parse_answers(line)
                    if isinstance(ans, dict):
                        vals = [str(v) for v in ans.values() if str(v).strip()]
                        return " ".join(vals).strip()
                    return ""              # wizard cancelled / empty → no answer, engine proceeds
                if line.strip():           # a plain typed follow-up = the answer too
                    return line.strip()
            _t.sleep(0.05)
            waited += 0.05
        return ""

    return ask


def _load_initial_images(paths: list):
    """Convert each --image PATH to a data: URL for the agent's first message.
    ponytail: was from ..core.multimodal import data_url_from_file (deleted, YAGNI). Inlined."""
    import base64
    def _sniff(data):
        if data[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
        if data[:3] == b"\xff\xd8\xff": return "image/jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
        return None
    out = []
    for p in paths:
        try:
            data = Path(p).read_bytes()
            mime = _sniff(data) or "image/png"
            out.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
        except Exception as e:  # noqa: BLE001
            _print(f"  warning: could not load image {p!r}: {str(e)[:160]}")
    return tuple(out)


def _spawn_mcp_clients(commands: list):
    """Spawn each `--mcp <command>` MCP server, handshake, return connected clients.
    A server that fails to start is skipped with a warning (never kills the run)."""
    import shlex, os
    clients = []
    for raw in commands:
        raw = (raw or "").strip()
        if not raw:
            continue
        # HTTP MCP server: "<url>" (optionally "<url> <bearer-token>", else env
        # SYNTRA_MCP_TOKEN). Streamable-HTTP transport — reaches HOSTED MCP servers.
        if raw.startswith(("http://", "https://")):
            parts = raw.split()
            url = parts[0]
            token = parts[1] if len(parts) > 1 else os.environ.get("SYNTRA_MCP_TOKEN")
            name = url.rstrip("/").rsplit("/", 1)[-1] or "mcp-http"
            try:
                from ..core.mcp import MCPClient, HttpTransport
                client = MCPClient(HttpTransport(url, token=token), name=name)
                client.initialize()
                clients.append(client)
                _print(f"  [mcp] connected (http): {name} ({len(client.list_tools())} tools)")
            except Exception as e:  # noqa: BLE001
                _print(f"  warning: MCP HTTP server {url!r} failed: {str(e)[:160]}")
            continue
        # stdio MCP server: a command to spawn.
        cmd = shlex.split(raw)
        if not cmd:
            continue
        name = cmd[0].rsplit("/", 1)[-1].replace("mcp-", "").replace("-server", "") or "mcp"
        try:
            from ..core.mcp import MCPClient, StdioTransport
            client = MCPClient(StdioTransport(cmd), name=name)
            client.initialize()
            clients.append(client)
            _print(f"  [mcp] connected: {name} ({len(client.list_tools())} tools)")
        except Exception as e:  # noqa: BLE001
            _print(f"  warning: MCP server {raw!r} failed to start: {str(e)[:160]}")
    return tuple(clients)


def _spawn_lsp_client(command: str, workspace: str):
    """Spawn the `--lsp <command>` language server and initialize it.
    ponytail: LSP client (core/lsp.py) deleted. Compile errors via shell commands at v0.1.0."""
    if not command.strip():
        return None
    _print("  [lsp] not available — core/lsp.py was removed (over-engineered for v0.1.0)")
    return None


def unstreamed_step_results(step_results, streamed_answer: str) -> list:
    """BUG4: the plan-step replay must NOT re-print an answer already shown live.

    The final answer streams token-by-token (committed as one bubble). The post-run
    loop then replays each done step's ``result`` — for a run whose step result IS the
    streamed answer, that renders the SAME text a SECOND time. This returns only the
    step results NOT already contained in the streamed answer (whitespace-insensitive),
    dropping blanks. With no streamed answer it passes every non-blank result through
    (unchanged behavior for non-streamed / multi-step runs). Pure."""
    streamed_norm = " ".join((streamed_answer or "").split())
    out = []
    for r in step_results:
        s = (r or "").strip()
        if not s:
            continue
        rnorm = " ".join(s.split())
        # skip when the streamed answer already carries this step's text. F29: only treat a
        # SUBSTRING match as "already shown" when the step text is substantial (>=40 chars) —
        # otherwise a short, legitimate step result (e.g. "done", "42", a common phrase) that
        # merely happens to appear inside the streamed answer would be silently dropped.
        if streamed_norm and (rnorm == streamed_norm or (len(rnorm) >= 40 and rnorm in streamed_norm)):
            continue
        out.append(s)
    return out


def _record_turn(state: dict, goal: str, result, answer_buf: list) -> None:
    """Append (user, assistant) to the runner's rolling conversation history so the
    NEXT message is context-aware. Slash commands are skipped; never raises into the run.

    The hard cap here is a GENEROUS backstop (only bites if the Librarian is off), NOT the
    context bound: the Librarian folds-and-drops old turns post-turn (it runs AFTER this),
    so capping at the ~16-msg recent window would drop the oldest turns BEFORE they're
    summarized -> silent loss. We cap well above one window + a turn so it never pre-empts."""
    try:
        if not goal or goal.startswith("/"):
            return
        hist = state.setdefault("history", [])
        ans = "".join(answer_buf).strip()
        if not ans and result is not None:
            ans = "\n".join((s.result or "").strip() for s in result.state.plan
                            if s.status == "done" and (s.result or "").strip())
        hist.append(("user", goal))
        if ans:
            hist.append(("assistant", ans[:4000]))
        elif result is not None and getattr(result, "verdict", "") == "plan_pending":
            hist.append(("assistant", "(proposed a plan; awaiting your approval)"))
        if len(hist) > 64:                      # backstop only; Librarian is the real bound
            del hist[: len(hist) - 64]
    except Exception:  # noqa: BLE001 - memory bookkeeping must never break a run
        pass


def _extract_learnings(state: dict, goal: str, result, loop, cfg, on_event) -> None:
    """Librarian job B (runner-only): after a SUCCESSFUL real task, learn durable
    CONSTRAINTS into session-memory.json (the same flat file /memories shows + /memory-drop
    edits). Novelty-gated so a re-ask/resume doesn't re-extract. Never raises."""
    import json as _json, hashlib as _hl
    from ..core.memory import Memory
    rstate = getattr(result, "state", None)
    if rstate is None:
        return
    decs = "|".join(getattr(d, "description", "") for d in (getattr(rstate, "decisions", []) or []))
    sig = _hl.sha256(f"{goal}|{decs}|{getattr(result,'verdict','')}".encode()).hexdigest()[:16]
    if state.get("last_extract_sig") == sig:
        return
    state["last_extract_sig"] = sig
    p = _state_root() / "session-memory.json"
    try:
        current = _json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    for _k in ("constraints", "conventions", "repo_map"):
        if not isinstance(current.get(_k), list):
            current[_k] = []
    # Dedup against ALL durable fields the extractor produces -- conventions, repo_map and
    # the architecture summary were being silently discarded (only constraints persisted).
    existing = Memory(constraints=list(current["constraints"]),
                      conventions=list(current["conventions"]),
                      repo_map=list(current["repo_map"]),
                      architecture_summary=str(current.get("architecture", "") or ""))
    delta = loop.extract_learnings(rstate, state.get("history", []), existing, cfg)
    new_c = [c for c in delta.constraints if c not in existing.constraints]
    new_v = [v for v in delta.conventions if v not in existing.conventions]
    new_r = [r for r in delta.repo_map if r not in existing.repo_map]
    new_arch = (delta.architecture_summary
                if delta.architecture_summary and not existing.architecture_summary else "")
    if not (new_c or new_v or new_r or new_arch):
        return
    learned = ([f"constraint: {c}" for c in new_c]
               + [f"convention: {v}" for v in new_v]
               + [f"repo: {r}" for r in new_r]
               + ([f"architecture: {new_arch}"] if new_arch else []))
    if getattr(cfg, "learn_mode", "auto") == "propose":
        for m in learned:
            on_event(f"📚 would learn: {m}  (/memory-update to keep)", "tool")
        return
    # #215: union the new learnings under a CROSS-PROCESS lock so two concurrent runs
    # can't clobber each other's freshly-learned facts (same lost-update class as #209).
    try:
        from ..core.memory_store import append_learnings
        merged = append_learnings(p, constraints=new_c, conventions=new_v,
                                  repo_map=new_r, architecture=(new_arch or ""))
    except Exception:  # noqa: BLE001
        return
    if not merged:
        return
    current = merged   # re-sync so the consolidation block below counts the merged totals
    for _k in ("constraints", "conventions", "repo_map"):
        if not isinstance(current.get(_k), list):
            current[_k] = []
    for m in learned:
        on_event(f"📚 learned: {m}  (remove with /memory-drop)", "tool")
    # B4: periodic consolidation — once memory has grown past a threshold, run a cheap
    # merge/dedup/drop-superseded pass and rewrite the file. Best-effort; never breaks a run.
    _total = len(current["constraints"]) + len(current["conventions"]) + len(current["repo_map"])
    _thresh = int(os.environ.get("SYNTRA_MEMORY_CONSOLIDATE_AT", "30") or "30")
    if _total >= _thresh and hasattr(loop, "consolidate_memory"):
        try:
            mem = Memory(constraints=list(current["constraints"]),
                         conventions=list(current["conventions"]),
                         repo_map=list(current["repo_map"]),
                         architecture_summary=str(current.get("architecture", "") or ""))
            cleaned = loop.consolidate_memory(mem, cfg)
            if cleaned is not None and not cleaned.is_empty():
                from ..core.memory_store import replace_all
                replace_all(p, constraints=list(cleaned.constraints),
                            conventions=list(cleaned.conventions),
                            repo_map=list(cleaned.repo_map),
                            architecture=str(cleaned.architecture_summary or ""))
                _after = (len(cleaned.constraints) + len(cleaned.conventions)
                          + len(cleaned.repo_map))
                on_event(f"📚 consolidated memory ({_total}→{_after} entries)", "tool")
        except Exception:  # noqa: BLE001
            pass


def _load_session_memory():
    """Load the Librarian's durable learnings (session-memory.json) as a Memory so they are
    fed BACK into runs -- not just shown in /memories. The file's flat 'architecture' key
    maps to Memory.architecture_summary. Returns None when nothing durable exists yet."""
    import json as _json
    from ..core.memory import Memory
    p = _state_root() / "session-memory.json"
    try:
        d = _json.loads(p.read_text())
        if not isinstance(d, dict):
            return None
    except (FileNotFoundError, ValueError, OSError):
        return None
    m = Memory(
        constraints=list(d.get("constraints") or []),
        conventions=list(d.get("conventions") or []),
        repo_map=list(d.get("repo_map") or []),
        architecture_summary=str(d.get("architecture") or ""),
    )
    return None if m.is_empty() else m


def _run_librarian(state: dict, goal: str, result, loop, cfg, on_event) -> None:
    """Librarian post-answer pass (smart-but-RARE), invoked ONLY from the runner so
    loop.run() tests are untouched: (A) refresh the rolling summary when turns roll off
    the recent window; (B) learn durable facts from a SUCCESSFUL real task. Never raises."""
    if not getattr(cfg, "librarian", False) or not goal or goal.startswith("/"):
        return
    try:  # (A) rolling-summary refresh, against the post-turn history
        from ..core.loop import normalize_history
        loop._history = normalize_history(state.get("history", []))
        if loop._needs_summary_refresh():
            state["summary"] = loop.refresh_summary(cfg, state=getattr(result, "state", None))
            # The summary ABSORBED the oldest turns -> drop them from the canonical history
            # too, so the next turn neither re-shows them nor (worse) silently loses them.
            state["history"] = [(m.role, m.content) for m in loop._history]
    except Exception:  # noqa: BLE001
        pass
    try:  # (B) learning extraction: ANY successful, non-chat task (B4/F50 — broadened from
          # multi-step-only so real work actually accumulates). Safe to broaden: pure chat/one-shot
          # is excluded by analysis_conversational, slash commands by the goal guard above, the
          # extractor is strict (returns {} when nothing is durable) + novelty-gated, and it now
          # runs a CHEAP model — so this fills memory without noise or meaningful cost.
        if (getattr(result, "verdict", "") == "pass"
                and not getattr(result, "analysis_conversational", False)):
            _extract_learnings(state, goal, result, loop, cfg, on_event)
    except Exception:  # noqa: BLE001
        pass


def _make_tui_runner(resume_id: str = ""):
    """Build a run_goal(goal, on_event) callback that drives the real Loop and
    streams the answer back to the TUI. Loads providers LAZILY (on first goal)
    so launching the TUI on a tty-less shell still falls back cleanly.

    If resume_id is given, the prior task's goal + answer are preloaded so `syntra
    resume <id>` REOPENS the conversation in the TUI (not a silent CLI re-run)."""
    import threading as _threading
    state = {}
    # The Librarian (rolling-summary + memory learning) runs in the BACKGROUND after a turn
    # so it never blocks the turn from finishing (it was up to 2 model calls / 90s on the hot
    # path → the next queued message couldn't start = the "everything is laggy" report). This
    # lock SERIALIZES the background librarian against the START of the next turn: the next
    # run waits for a still-running librarian to finish folding the summary so it sees the
    # updated history (rare — the librarian only fires when the convo grows past the window).
    _librarian_lock = _threading.Lock()
    # Preload the resumed conversation (its last goal + answer) so the chat reopens
    # with context and the model continues from where it left off.
    _initial: list = []
    if resume_id:
        try:
            import json as _json
            # The WHOLE saved conversation (persisted by the TUI on exit) is the source
            # of truth; fall back to the single task's goal+answer only if it's missing.
            _tf = _state_root() / "transcripts" / f"{resume_id}.json"
            if _tf.exists():
                _initial = [(m.get("role", ""), m.get("text", ""))
                            for m in _json.loads(_tf.read_text())
                            if (m.get("text", "") or "").strip()]
            if not _initial:
                _st = TaskStore(_state_root()).load(resume_id)
                _ans = "\n\n".join((s.result or "").strip() for s in _st.plan
                                   if (getattr(s, "result", "") or "").strip())
                if not _ans:
                    _ans = (getattr(_st, "summary", "") or "").strip()
                _initial = [("user", _st.goal)] + ([("assistant", _ans)] if _ans else [])
        except Exception:  # noqa: BLE001 - a bad id just opens a fresh chat
            _initial = []
        # If this session is a branch, lead with a "forked from <id>" banner so it's
        # obvious you're on a fork (A2). System role -> shown in chat, kept out of _hist.
        try:
            _meta = _json.loads((_state_root() / "tasks" / resume_id / "task.json").read_text())
            _bf = str(_meta.get("branched_from", "")).strip()
            if _bf:
                _initial = [("system", f"⌥ forked from {_bf[:8]} · this is a branch")] + _initial
        except Exception:  # noqa: BLE001 - no/!branched task.json just skips the banner
            pass
    # The MODEL's history is only the real conversation turns (user/assistant) — system
    # banners/status lines are display-only and must not pollute the prompt.
    _hist = [(r, t) for r, t in _initial if r in ("user", "assistant", "assistant_stream")]
    # The TUI sets an interactive tool-permission prompt here (once/session/reject), so
    # agent tools (bash/curl/web) ask before running unless /auto-approve is on.
    _perm = {"ask": None}

    def run_goal(goal: str, on_event, steering=None):
        # Wait for a still-running background Librarian from the PREVIOUS turn to finish folding
        # the rolling summary before this turn reads state["history"]/["summary"] — otherwise a
        # fast next message could start on a half-updated history. Almost always uncontended
        # (the librarian only does real work when the convo has grown past the recent window).
        _librarian_lock.acquire()
        _librarian_lock.release()
        if "cat" not in state:
            state["cat"] = Catalog.load(_catalog_path())
            state["registry"] = ProviderRegistry.load()
            state["rh"] = _route_health()
            state["ov"] = Overrides.load()
            state["history"] = list(_hist)   # seed the MODEL with the resumed conversation
            state["summary"] = ""   # Librarian rolling summary (job A)
            state["last_extract_sig"] = ""  # Librarian learning novelty gate (job B)

        # Stream the Loop's progress events live into the TUI. Structured run
        # events feed the "Cockpit Trace" (core/activity_tree): mode blocks with
        # indented children + a dim, collapsible thinking node. We render the tree
        # and emit only the NEW rows since the last event (the chat is an
        # append-only queue), each tagged with its render role (mode/thinking/tool/
        # ok/error). Streamed answer tokens (kind="token") render live as "stream".
        from ..core.progress import tui_progress_line
        from ..core.activity_tree import ActivityTree
        from ..core.tui_model import activity_line, normalize_activity  # F53 glyph feed

        _last_route = {"sig": None}
        _tree = ActivityTree()
        _emitted = {"n": 0}
        _TREE_EVENTS = {"phase", "analysis", "mode", "plan_step", "plan_ready",
                        "step_start", "step_done", "edit", "verify_result",
                        "review_lens", "autopilot", "loop_halted"}

        def _trace_width() -> int:
            """Render the trace to the real terminal width (the default cockpit layout is
            full-width chat), NOT a hardcoded 72 that visibly cut the trace on wide
            terminals (P6). If a side panel narrows the chat pane the TUI clips cleanly."""
            try:
                import shutil
                return max(72, min(200, shutil.get_terminal_size().columns - 4))
            except Exception:  # noqa: BLE001
                return 100

        def _flush_tree() -> None:
            """Emit STRUCTURAL tree rows added since the last flush.

            The chat is append-only, so we only diff the discrete structural rows
            (mode headers + children). Live thinking is streamed separately below
            and excluded here, since it grows in place and would break the diff."""
            rows = _tree.lines(_trace_width(), thinking_collapsed=True)
            structural = [(t, r) for (t, r) in rows if r != "thinking"]
            for text, role in structural[_emitted["n"]:]:
                on_event(text, role)
            _emitted["n"] = len(structural)

        from ..core.tui_model import ReasoningLineBuffer
        _thinking = {"open": False, "last": "", "buf": ReasoningLineBuffer()}
        _answer = {"buf": []}   # accumulate the streamed answer for conversation memory
        _agent_tally = {"tok": {}, "usd": {}}   # per-role token/cost, summarized on agent_done
        _last_activity = {"s": ""}   # F53: dedup consecutive identical activity lines

        def _progress(kind: str, payload: dict) -> None:
            # T4 defense-in-depth: only the EXECUTOR's answer streams to chat. A reviewer's
            # raw JSON verdict or a sub-agent's intermediate text (role != executor) must
            # never render as the assistant answer. The engine already gates this at the
            # source; this is the renderer-side backstop.
            _role = payload.get("role")
            if kind in ("token", "reasoning_token") and _role not in (None, "", "executor"):
                return
            if kind == "token":
                chunk = payload.get("text", "")
                if chunk:
                    _answer["buf"].append(chunk)
                    if _thinking["open"]:      # answer started -> close the thinking block
                        for _ln in _thinking["buf"].flush():   # BUG1: emit the held CoT tail as ONE line
                            on_event("  " + _ln, "thinking")
                        _tree.end_thinking()
                        _thinking["open"] = False
                        _thinking["last"] = ""   # reset dedup so a new mode's first CoT isn't dropped
                    on_event(chunk, "stream")
                return
            if kind == "reasoning_token":
                chunk = payload.get("text", "")
                # Skip an identical consecutive block (some models echo the same CoT
                # per step); keeps the trace from showing duplicate thinking.
                if chunk and chunk != _thinking["last"]:
                    _thinking["last"] = chunk
                    if not _thinking["open"]:
                        on_event("✶ thinking", "thinking")
                        _thinking["open"] = True
                    _tree.feed_thinking(chunk)
                    # BUG1: coalesce a token/word-at-a-time reasoning stream into WHOLE lines
                    # before emitting (was one Message per chunk → "one word per line").
                    for ln in _thinking["buf"].feed(chunk):
                        on_event("  " + ln, "thinking")
                return
            # F53: a clean, VISIBLE glyph line for a file edit (the folded tree below still
            # records it for the panel). "applied" -> "✎ edited <file>"; else show the status.
            if kind == "edit":
                _path = str(payload.get("path", "") or "")
                _status = str(payload.get("status", "") or "")
                if _path and _status in ("applied", "proposed", "wrote", "created", "failed"):
                    _verb = "edited" if _status == "applied" else _status
                    _det = "" if _status == "applied" else _status
                    on_event(activity_line(_verb, _path.split("/")[-1], _det), "activity")
            if kind == "image":
                # IMGFIX: render an image inline in the user's terminal. Carry the path on a
                # dedicated 'show_image' role-line the TUI drain turns into an image_overlay.
                _ip = str(payload.get("path", "") or "")
                if _ip:
                    on_event(_ip, "show_image")
                return
            if kind == "knowledge_hit":
                # D4: the loop emits this when cross-session BM25 recall injects relevant past
                # notes/failures. Surface it as a visible activity line so the user SEES that
                # cross-session memory fired (the event was otherwise silently dropped).
                try:
                    _n = int(payload.get("count", 0) or 0)
                except Exception:  # noqa: BLE001
                    _n = 0
                if _n > 0:
                    on_event(activity_line("recall", f"{_n} hit(s)", "knowledge index"), "activity")
                return
            if kind in _TREE_EVENTS:
                if _thinking["open"]:
                    for _ln in _thinking["buf"].flush():   # BUG1: emit the held CoT tail before the block closes
                        on_event("  " + _ln, "thinking")
                    _tree.end_thinking()
                    _thinking["open"] = False
                    _thinking["last"] = ""       # reset dedup on a mode change (audit #9)
                _tree.feed(kind, payload)
                _flush_tree()
                return
            # Surface the routed model (drives the spinner's "working with
            # <model>"), but only when it CHANGES — avoids the planner/executor/
            # reviewer route spam. One quiet line per distinct model.
            if kind == "note":
                # A short, plain informational line from the engine (e.g. "routed to a vision
                # model for this turn", or a vision-miss warning). Surfaced as a quiet system line.
                _ntext = str(payload.get("text", "") or "")
                if _ntext:
                    on_event(_ntext, "system")
                return
            if kind == "command":
                # Gap 2: a shell command was executed. We surface it (never silent) — loudly
                # when it ran WITHOUT an OS sandbox, quietly otherwise (verbose mode). The text
                # is pre-built by the engine ("ran on host (no sandbox): X" / "ran: X").
                _ctext = payload.get("text", "")
                _style = "system" if payload.get("sandboxed", True) is False else "tool"
                on_event(("⚠ " if _style == "system" else "  ⎿ ") + _ctext, _style)
                return
            if kind == "turn_diff":
                # One coherent diff for the whole turn — show the summary in the
                # feed and stash the full diff so the diff panel/`/diff` can show it.
                summ = payload.get("summary", "")
                on_event(f"  ⎿ changes: {summ}", "tool")
                # A1 inline diff cell [A]: show the ACTUAL changed lines inline (colored
                # +/-/@@, folds with the trace), capped — not just a count. Full diff
                # stays in the diff panel / `/diff`.
                _diff = (payload.get("diff", "") or "")
                if _diff.strip():
                    _dlines = _diff.splitlines()
                    for _dl in _dlines[:24]:
                        on_event(_dl, "tool")
                    if len(_dlines) > 24:
                        on_event(f"  … +{len(_dlines) - 24} more diff lines — /diff for the full view", "tool")
                return
            if kind == "key_low":
                rem = payload.get("remaining", "?")
                on_event(f"⚠ {payload.get('provider','?')} rate-limit low ({rem} left) — {payload.get('action','')}", "tool")
                return
            if kind == "key_failover":
                model = payload.get("model", "?").split("/")[-1]
                on_event(f"⚠ {model} via {payload.get('from','?')} exhausted — switched to backup key", "tool")
                return
            if kind == "provider_failover":
                model = payload.get("model", "?").split("/")[-1]
                on_event(f"⚠ {model} via {payload.get('from','?')} failed "
                         f"({payload.get('kind','?')}) → switched to {payload.get('to','?')}", "tool")
                return
            if kind == "key_exhausted":
                n = payload.get("backups_remaining", 0)
                prov = payload.get("provider", "?")
                if n > 0:
                    on_event(f"⚠ {prov} credential limited — {n} backup credential(s) left, switching", "tool")
                else:
                    on_event(f"✗ {prov} credential limited and NO backups left — "
                             f"change model, add credits, or increase the daily limit", "tool")
                return
            # phase / plan_step / plan_ready / step_start / step_done are now owned
            # by the Cockpit Trace (_TREE_EVENTS above). `route` stays here: it adds
            # a quiet route child under the current mode, deduped per distinct model.
            if kind == "route":
                model = payload.get("model", "")
                role = payload.get("role", "")
                prov = payload.get("provider", "?")
                score = payload.get("score", 0)
                sig = (role, model, prov)
                if model and sig != _last_route["sig"]:
                    _last_route["sig"] = sig
                    short_model = model.split("/")[-1] if "/" in model else model
                    on_event(f"  ┄ {role} → {short_model} via {prov} ({score:.2f})", "tool")
                return
            if kind == "route_demote":
                # F29: visible, NOT in the collapsed trace (role 'system' stays shown) —
                # so the user sees that a weak model will be swapped on the next pass.
                _m = str(payload.get("model", "")).split("/")[-1]
                on_event(f"⚠ {_m} gave a weak result — preferring a different model on retry", "system")
                return
            # Per-agent telemetry → a clean "✓ role done · N tok · $cost"
            # row when each agent finishes (instead of per-call [usage] spam). The route
            # line above already announced role+model, so agent_start needs no extra row.
            if kind == "usage":
                _role = payload.get("role", "")
                _agent_tally["tok"][_role] = (_agent_tally["tok"].get(_role, 0)
                                              + int(payload.get("in", 0) or 0)
                                              + int(payload.get("out", 0) or 0))
                _agent_tally["usd"][_role] = (_agent_tally["usd"].get(_role, 0.0)
                                              + float(payload.get("usd", 0.0) or 0.0))
                return
            if kind == "agent_activity":
                # forward per-agent tool-count + tokens + live activity to the panel
                # (format: "role · N tools · N tok · <activity>"). Forward ONLY the keys
                # present -- tool-call and token-update events arrive separately and must
                # not clobber each other (the widget feed merges).
                import json as _json
                d = {"k": "agent_activity", "role": payload.get("role", "")}
                if "tools" in payload:
                    d["tools"] = int(payload.get("tools") or 0)
                if "activity" in payload:
                    d["activity"] = str(payload.get("activity", ""))[:48]
                if "tokens" in payload:
                    d["tokens"] = int(payload.get("tokens") or 0)
                if "result" in payload:                          # #84: what the finished tool got
                    d["result"] = str(payload.get("result", ""))[:80]
                    d["for_tool"] = str(payload.get("for_tool", ""))
                on_event(_json.dumps(d), "agent_evt")
                # F53: also surface the tool action as a clean, VISIBLE glyph line in the
                # main feed (read/run/web/ref/search), deduped vs the previous one.
                _act = str(payload.get("activity", "") or "")
                if _act and _act != _last_activity["s"]:
                    _last_activity["s"] = _act
                    _verb, _target = normalize_activity(_act)
                    if _verb and _verb not in ("edited", "edit", "wrote"):   # edits handled above
                        on_event(activity_line(_verb, _target[:60]), "activity")
                return
            if kind == "agent_output":
                # forward the agent's actual OUTPUT text so the panel drill-down can show
                # WHAT it did (F20/F21: click an agent -> read its work).
                import json as _json
                on_event(_json.dumps({"k": "agent_output", "role": payload.get("role", ""),
                                      "text": str(payload.get("text", ""))[:4000]}), "agent_evt")
                return
            if kind == "agent_start":
                # forward to the AGENTS panel (its designed feed() API) so council plan·X /
                # panel review·X / subagent sub·N surface as live agents ("Running N agents").
                import json as _json
                on_event(_json.dumps({"k": "agent_start", "role": payload.get("role", ""),
                                      "model": payload.get("model", ""),
                                      "task": str(payload.get("task", ""))[:80]}), "agent_evt")
                return
            if kind == "agent_done":
                import json as _json
                on_event(_json.dumps({"k": "agent_done", "role": payload.get("role", "")}),
                         "agent_evt")   # flip the panel agent to done (before the row-suppress)
                # Only summarize agents with their OWN token tally (planner/executor/
                # reviewer). Synthetic sub-roles (council plan·X, panel review·X, sub·N)
                # tally cost under the base role, so a per-member row would falsely read
                # "done" with no cost -> suppress those instead of printing a hollow row.
                _role = payload.get("role", "")
                _tok = _agent_tally["tok"].get(_role, 0)
                _usd = _agent_tally["usd"].get(_role, 0.0)
                if not (_tok or _usd):
                    return
                _bits = []
                if _tok:
                    _bits.append((f"{_tok / 1000:.1f}k" if _tok >= 1000 else str(_tok)) + " tok")
                if _usd:
                    _bits.append(f"${_usd:.4f}")
                on_event(f"  ✓ {_role} done  · " + " · ".join(_bits), "ok")
                return
            try:
                line = tui_progress_line(kind, payload, verbose=False)
            except Exception:  # noqa: BLE001 - never let formatting kill a run
                line = None
            if line:
                on_event(f"  ⎿ {line}", "tool")

        loop = Loop(catalog=state["cat"], store=TaskStore(_state_root()),
                    registry=state["registry"], route_health=state["rh"],
                    overrides=state["ov"], route_stats=_route_stats(),
                    steering=steering, progress=_progress)
        # Gap 5b: keep the live loop on run_goal so the TUI's /permissions view can read the
        # REAL PermissionStore (created when loop.run builds the tool context — None until then).
        # A snapshot would capture None, so the view reads run_goal._loop.permission_store live.
        run_goal._loop = loop

        # Access posture (B6) is read ONCE here, ABOVE the /resume branch, so a resumed/
        # plan-approved run gets the SAME tool-enabling config a fresh run does. These are pure
        # reads (no one-shot consumption), unlike research/council below which must stay under
        # /resume so resume doesn't eat them. The persisted run mode (Plan/Ask/Edit/Auto) is the
        # single source of truth for how freely the agent acts: it maps onto the shell exec-policy
        # gate AND decides auto-approve. Plan → read-only; Ask → prompt; Edit → files auto, no
        # commands; Auto → run everything (secret floor + sandbox still on).
        from ..core.access_modes import load_access_state
        _access = load_access_state(_state_root() / "access.json")
        _acc_policy, _acc_sandbox = _access.map_to_policy()
        # auto_approve comes from the mode (Auto, or bash flipped to auto); the legacy /auto
        # toggle can still force it on for the run.
        auto_approve = _access.is_auto_approve() or bool(state.get("auto_approve", False))
        autopilot_n = int(state.get("autopilot", 1) or 1)
        # Build the web backend ALWAYS so the tool-using executor (auto_tools) has web_search
        # too, not just research mode — and so a resumed run can fetch/search as well.
        from ..core.search import build_search_backend
        _web = build_search_backend()

        # Handle /resume <id> — continue a previously planned task
        if goal.startswith("/resume"):
            tid = goal[len("/resume"):].strip()
            if not tid:
                tid = state.get("_pending_task", "")
            if tid:
                on_event("▸ Resuming execution...", "tool")
                # ROOT-CAUSE FIX: a resumed/plan-approved task must take the SAME tool-vs-text
                # decision a fresh run does. The old `LoopConfig(stream=True, librarian=True)`
                # inherited auto_tools=False + access_mode="" -> _wants_tools()=False -> the
                # toolless text pipeline -> the executor truthfully said "I don't have file-system
                # access tools available in this turn" and the reviewer (correctly) failed it,
                # which then read as a "weak model". Give resume the tool-enabling posture (access
                # mode, auto_tools, web, hooks, permission prompt) — but NOT the run-only toggles
                # (research/council) and NOT plan_approval (resume IS the post-approval execution;
                # re-pausing would deadlock).
                # BUT attached images MUST ride the resume: they ARE the content of the question
                # being answered ("what is this image?"), only now executing. Excluding them made
                # the model answer with NO image → it grabbed a stale workspace image via view_image
                # and described the wrong picture (the "didn't check the attached image" bug).
                _resume_cfg = LoopConfig(
                    stream=True, librarian=True,   # T12: resumed runs learn too
                    auto_tools=True,
                    web_search=_web,
                    auto_approve=auto_approve,
                    approval_policy=_acc_policy,
                    sandbox_mode=_acc_sandbox,
                    access_mode=_access.mode,
                    access_overrides=dict(_access.overrides),
                    cost_mode=str(state.get("cost_mode", "") or _load_cost_mode()),
                    context_relay=bool(state.get("context_relay", _load_context_relay())),
                    verbose_commands=bool(state.get("verbose_commands", False)),
                    question_ask=_make_tui_question_ask(on_event, steering),
                    commit_style=_load_commit_style(),          # resumed runs commit too
                    commit_style_persist=_save_commit_style,
                    hooks=_load_lifecycle_hooks(),
                    permission_ask=_perm["ask"],
                    # carry the IMAGES the user attached for THIS request (one-shot). Resume has no
                    # goal string to inject text into, so any staged TEXT attachments are left on the
                    # queue to ride the next fresh turn rather than being silently dropped (#127/#128).
                    initial_images=tuple(
                        a["url"] for a in (state.get("_attachments", []) or [])
                        if a.get("kind") == "image"),
                )
                # #138: consume TEXT attachments into THIS resumed run via its history (resume has
                # no goal string to prepend to), then clear the WHOLE staged list. Previously text
                # attachments were kept but the TUI chip cleared on submit — so a staged file (e.g.
                # secrets.txt) silently leaked into the NEXT unrelated message. One-shot: gone after.
                _resume_hist = list(state.get("history", []))
                _resume_texts = [(a.get("name", ""), a.get("content", ""))
                                 for a in (state.get("_attachments", []) or [])
                                 if a.get("kind") == "text"]
                if _resume_texts:
                    block = "\n\n".join(
                        f"Attached file: {name}\n```\n{content}\n```" for name, content in _resume_texts)
                    _resume_hist.append(("user", block))
                state.pop("_attachments", None)   # consume ALL staged attachments this turn
                try:
                    result = loop.resume(tid, config=_resume_cfg,
                                         history=_resume_hist,
                                         summary=state.get("summary", ""),
                                         session_memory=_load_session_memory())
                except Exception as e:
                    on_event(f"resume failed: {e}", "tool")
                    return None
                # The pending plan is now consumed -> clear it so a second /resume doesn't
                # re-run a finished task.
                if state.get("_pending_task") == tid:
                    state["_pending_task"] = ""
                # BUG4: dedupe the plan-step replay against the live-streamed answer (same
                # duplicate-bubble fix as the run path).
                for _r in unstreamed_step_results(
                        [s.result for s in result.state.plan if s.status == "done"],
                        "".join(_answer["buf"])):
                    on_event(_r)
                # T12: a resumed task must ACCUMULATE memory like a normal run (was skipped —
                # this branch returned before the librarian pass below). Use the resumed task's
                # REAL goal, not the "/resume <id>" command string (the librarian skips slash goals).
                _real_goal = (getattr(result.state, "goal", "") or "").strip()
                if _real_goal:
                    _record_turn(state, _real_goal, result, [])
                    _run_librarian(state, _real_goal, result, loop, _resume_cfg, on_event)
                return result
            on_event("nothing to resume", "tool")
            return None

        # Access posture (_access/_acc_policy/_acc_sandbox/auto_approve/autopilot_n/_web) was
        # read ABOVE the /resume branch so both paths share it (B6). The permission posture
        # (🔒 ask / 🔓 auto) is shown PERSISTENTLY in the top status bar, so we do NOT spam a
        # "⚙ 🔒 ask" trace line on every single turn.
        research = bool(state.get("_research_next"))
        if research:
            state["_research_next"] = ""        # one-shot
        # /council — run this goal with N planner + reviewer agents in parallel so the
        # user can WATCH real agents in the panel (F24/F30). One-shot toggle.
        council_n = int(state.get("_council_next", 0) or 0)
        if council_n > 1:
            state["_council_next"] = 0
        _n = council_n if council_n > 1 else 1
        # Plan-approval is ON by default (user): the plan is shown and the run pauses so you can
        # vet it before it executes. You turn it OFF right from the approval modal — "Approve for
        # this session" (in-memory) or "Approve always" (persisted) — or via /plan-review. The
        # session toggle seeds from the persisted value, so a saved "off" survives restarts.
        # (auto/research/council runs never pause regardless.)
        _plan_default = _load_plan_review()
        _plan_review = bool(state.get("_plan_review", _plan_default)) and not auto_approve and not research and _n <= 1
        # #127/#128: consume the ONE ordered attachment list for this turn (one-shot). Split into
        # image data-URLs (vision channel) and text (name, content) pairs (injected into the goal).
        _atts = state.pop("_attachments", []) or []
        _att_images = tuple(a["url"] for a in _atts if a.get("kind") == "image")
        _att_texts = [(a.get("name", ""), a.get("content", "")) for a in _atts if a.get("kind") == "text"]
        cfg = LoopConfig(stream=True, auto_approve=auto_approve, librarian=True,
                         plan_approval=_plan_review,
                         research=research, proof_only=research, web_search=_web,
                         plan_council=_n, review_panel=_n,
                         cost_mode=str(state.get("cost_mode", "") or _load_cost_mode()),  # T5 mode (/mode persists)
                         context_relay=bool(state.get("context_relay", _load_context_relay())),  # T6 /context
                         auto_tools=True,   # tool-needing tasks DO the work (curl/bash/web)
                         # F40: a genuinely under-specified goal POPS the interactive clarifying-
                         # question wizard (instead of the model answering "could you clarify…" as
                         # prose). The callback blocks the worker thread on the steering inbox until
                         # the wizard's answer arrives. Was wired only in the CLI --agent path before.
                         clarify_ambiguous=True,
                         question_ask=_make_tui_question_ask(on_event, steering),
                         # Git commit-message style: the app-user's choice (asked once via the
                         # question wizard on the first agent commit, then persisted + followed).
                         commit_style=_load_commit_style(),
                         commit_style_persist=_save_commit_style,
                         hooks=_load_lifecycle_hooks(),  # B5: pre/post tool-use lifecycle hooks
                         approval_policy=_acc_policy,    # B6: access mode → live shell gate
                         sandbox_mode=_acc_sandbox,
                         access_mode=_access.mode,       # B6: per-tool gate (off/auto/ask) for non-bash tools
                         access_overrides=dict(_access.overrides),
                         verbose_commands=bool(state.get("verbose_commands", False)),  # Gap 2 toggle
                         # Images the user attached this turn (drag-drop / paste / /attach in the
                         # TUI). One-shot: read + clear so they ride ONLY this message. The loop
                         # auto-routes to a vision model when these are present.
                         initial_images=_att_images,
                         permission_ask=_perm["ask"])   # interactive once/session/reject prompt
        # #127: text/code files the user attached this turn are injected as a context block
        # PREPENDED to the goal (one-shot). Images go via initial_images (cfg above); text needs
        # no vision, so any model can use it.
        # F28: the augmented goal (with the full attachment text) is used ONLY for THIS run — the
        # ORIGINAL user text is what gets recorded into history, so a one-shot attachment isn't
        # baked into every subsequent turn (cost/context bloat that defeated the "one-shot" intent).
        # NB: named `augmented_goal`, NOT `run_goal` — `run_goal` is the TUI run-callback function
        # in this scope; a local of that name would shadow it and break earlier references.
        augmented_goal = goal
        if _att_texts:
            block = "\n\n".join(
                f"Attached file: {name}\n```\n{content}\n```" for name, content in _att_texts)
            augmented_goal = block + "\n\n" + goal
        if autopilot_n > 1:
            result = loop.autopilot(augmented_goal, workspace_root=str(Path.cwd()),
                                    config=cfg, max_iterations=autopilot_n,
                                    history=state.get("history", []),
                                    summary=state.get("summary", ""),
                                    session_memory=_load_session_memory())
        else:
            result = loop.run(augmented_goal, workspace_root=str(Path.cwd()), config=cfg,
                              history=state.get("history", []),
                              summary=state.get("summary", ""),
                              session_memory=_load_session_memory())
        _record_turn(state, goal, result, _answer["buf"])
        if result.verdict == "plan_pending":
            state["_pending_task"] = result.state.task_id
            return result
        # BUG4: only replay step results the live token-stream DIDN'T already show, so a
        # streamed final answer isn't printed a second time (the duplicate-bubble glitch).
        _streamed_ans = "".join(_answer["buf"])
        for _r in unstreamed_step_results(
                [s.result for s in result.state.plan if s.status == "done"], _streamed_ans):
            on_event(_r)
        # Librarian (smart-but-RARE, post-answer): rolling-summary refresh + memory learning.
        # Run it in the BACKGROUND so the turn FINISHES NOW — the next queued message starts
        # immediately instead of waiting on up-to-2 librarian model calls (the lag fix). The
        # lock serializes it against the next turn's start so the summary fold is never lost.
        def _bg_librarian():
            with _librarian_lock:
                _run_librarian(state, goal, result, loop, cfg, on_event)
        _threading.Thread(target=_bg_librarian, name="syntra-librarian", daemon=True).start()
        return result

    # Expose the toggle setter so the TUI's /auto and /permissions can flip it.
    run_goal.set_toggle = lambda key, val: state.__setitem__(key, val)  # type: ignore[attr-defined]
    run_goal.get_toggle = lambda key, default=None: state.get(key, default)  # type: ignore[attr-defined]

    def _attach_image(src):
        """Stage an attachment for the NEXT message (#127).
        ponytail: was from ..core import multimodal (deleted, YAGNI). Inlined.
        """
        import base64
        try:
            from pathlib import Path as _P
            atts = state.setdefault("_attachments", [])
            if isinstance(src, tuple):           # (bytes, mime) from the clipboard → image
                data, mime = src
                mime = mime or "image/png"
                b64 = base64.b64encode(data).decode("ascii")
                url = f"data:{mime};base64,{b64}"
                label = f"clipboard image ({(mime or 'image').split('/')[-1]}, {len(data)//1024} KB)"
                atts.append({"kind": "image", "label": label, "url": url})
                return (True, label)
            # a file path — classify and route by type
            kind = multimodal.classify_attachment(src)
            name = _P(str(src)).name
            if kind == "image":
                atts.append({"kind": "image", "label": name,
                             "url": multimodal.data_url_from_file(src)})
                return (True, name)
            if kind == "text":
                content = multimodal.read_text_attachment(src)
                kb = max(1, len(content) // 1024)
                label = f"{name} ({kb} KB text)"
                atts.append({"kind": "text", "label": label, "name": name, "content": content})
                return (True, label)
            if kind == "document":
                # PDF/.docx: extract the text (if an extractor is importable) and attach it as a
                # text context record. read_document_attachment raises a clear message if it can't.
                content = multimodal.read_document_attachment(src)
                kb = max(1, len(content) // 1024)
                label = f"{name} ({kb} KB text)"
                atts.append({"kind": "text", "label": label, "name": name, "content": content})
                return (True, label)
            return (False, f"{name} looks like a binary file — can't attach as context "
                            f"(only images, text/code, and PDF/.docx documents are supported)")
        except Exception as e:  # noqa: BLE001
            return (False, str(e)[:160])

    def _clear_all_attachments():
        state.pop("_attachments", None)

    def _remove_attachment(index):
        """Drop ONE staged attachment by 0-based index (#128). Returns its label, or None if the
        index is out of range."""
        atts = state.get("_attachments", []) or []
        if 0 <= index < len(atts):
            return (atts.pop(index) or {}).get("label", "")
        return None

    def _list_attachments():
        """Labels of the currently-staged attachments, in order (#128)."""
        return [a.get("label", "") for a in (state.get("_attachments", []) or [])]

    run_goal.attach_image = _attach_image  # type: ignore[attr-defined]
    run_goal.clear_attachments = _clear_all_attachments  # type: ignore[attr-defined]
    run_goal.remove_attachment = _remove_attachment       # type: ignore[attr-defined]
    run_goal.list_attachments = _list_attachments         # type: ignore[attr-defined]
    run_goal._initial_messages = _initial   # type: ignore[attr-defined]  (resume: preload chat)
    run_goal._resume_id = resume_id          # type: ignore[attr-defined]
    # Bug A / T3: on resume the header used to show $0.00 / 0 tok until a NEW run finished,
    # because nothing surfaced the task's ALREADY-accumulated spend at open time. The engine
    # state restores it (state.load reads cost.json), so expose the restored totals here for
    # the header to seed immediately. 0 when not resuming or the task can't be read.
    _resume_cost, _resume_tin, _resume_tout = 0.0, 0, 0
    if resume_id:
        try:
            _rst = TaskStore(_state_root()).load(resume_id)
            _resume_cost = float(_rst.total_cost_usd())
            _resume_tin, _resume_tout = _rst.total_tokens()   # (input, output)
        except Exception:  # noqa: BLE001 - a bad/missing id just leaves the header at 0
            pass
    run_goal._resume_cost = _resume_cost                  # type: ignore[attr-defined]
    run_goal._resume_tokens_in = int(_resume_tin)         # type: ignore[attr-defined]
    run_goal._resume_tokens_out = int(_resume_tout)       # type: ignore[attr-defined]
    run_goal.set_permission_ask = lambda cb: _perm.__setitem__("ask", cb)  # type: ignore[attr-defined]

    def add_provider_key(provider: str, key: str, base_url: str = "") -> tuple[bool, str]:
        """TUI seam: add/append an API key for a provider from inside the app, persist it to the
        active providers.json (chmod 600, secret never echoed), and RELOAD the registry so the
        new key is live for the next run. Returns (ok, message-for-the-user — tail only, no key)."""
        from ..core.registry import (add_key, write_providers_config, ProviderRegistry,
                                      default_config_path)
        try:
            reg = state.get("registry") or ProviderRegistry.load()
            path = getattr(reg, "source_path", None) or default_config_path()
            import json as _json
            try:
                raw = _json.loads(Path(path).read_text())
            except (OSError, ValueError):
                raw = {"providers": []}              # no config yet → start one
            new, summary, notes = add_key(raw, provider, key, base_url=base_url)
            if not summary:                          # nothing added (dupe / bad input)
                return False, (notes[0] if notes else "no key added")
            write_providers_config(path, new.get("providers", []), overwrite=True)
            state["registry"] = ProviderRegistry.load(path)   # reload so the key is live now
            msg = summary + (("  · " + "; ".join(notes)) if notes else "")
            return True, msg
        except Exception as e:  # noqa: BLE001 - never crash the TUI on a key edit
            return False, f"could not save key: {str(e)[:160]}"

    run_goal.add_provider_key = add_provider_key   # type: ignore[attr-defined]

    def detect_provider_models(provider: str, *, on_event=None,
                               gate: bool = True) -> tuple[bool, str]:
        """TUI/CLI seam: query a provider's /models with its configured key, validate each
        model with a tiny real call, then make the working ones ROUTABLE — union into the
        provider's allowed_models (when `gate`) AND add a minimal catalog OVERLAY row for any
        id the catalog doesn't know. Reloads registry + catalog so they're live this session.
        Streams on_event(kind,payload) per model. Returns (ok, summary). Never raises."""
        from ..core import model_discovery as _md
        from ..core.registry import (add_allowed_models, write_providers_config,
                                      ProviderRegistry, default_config_path)
        from ..core.catalog import Catalog
        from ..core.paths import default_catalog_overlay_path
        try:
            reg = state.get("registry") or ProviderRegistry.load()
            ep = reg.by_name(provider) if hasattr(reg, "by_name") else None
            if ep is None:
                return False, f"no configured provider {provider!r} to probe"
            cat = state.get("cat") or Catalog.load(_catalog_path())
            catalog_ids = frozenset(m.id for m in cat.models)
            existing_allowed = tuple(getattr(ep, "allowed_models", None) or ())
            report = _md.discover_for_endpoint(
                ep, registry=reg, catalog_ids=catalog_ids, existing_allowed=existing_allowed,
                on_event=on_event)
            if report.fetch_error:
                return False, f"{provider}: could not list models ({report.fetch_error})"
            # persist allowed_models (re-read right before write to minimize a concurrent-edit race)
            path = getattr(reg, "source_path", None) or default_config_path()
            import json as _json
            if gate and report.added_to_allowed:
                try:
                    raw = _json.loads(Path(path).read_text())
                except (OSError, ValueError):
                    raw = {"providers": []}
                raw, _added, _notes = add_allowed_models(raw, provider,
                                                         list(report.added_to_allowed))
                write_providers_config(path, raw.get("providers", []), overwrite=True)
            # persist minimal catalog overlay rows for the uncatalogued ids (so they ROUTE)
            if report.uncatalogued_added:
                ov_path = default_catalog_overlay_path()
                try:
                    ov = _json.loads(Path(ov_path).read_text())
                except (OSError, ValueError):
                    ov = {"models": []}
                _by_id = {d.id: d for d in report._discovered}
                _vr_by = {v.model_id: v for v in report.validation}
                for _mid in report.uncatalogued_added:
                    _dm = _by_id.get(_mid)
                    if _dm is None:
                        continue
                    ov, _ = _md.merge_overlay_row(
                        ov, _md.build_minimal_catalog_row(_dm, provider, _vr_by.get(_mid)))
                _md.write_catalog_overlay(ov_path, ov)
            # reload so discovered models are live for the next run
            state["registry"] = ProviderRegistry.load(path)
            state["cat"] = Catalog.load(_catalog_path())
            msg = (f"{provider}: {len(report.fetched)} models · "
                   f"{len(report.validated_ok)} validated · "
                   f"{len(report.added_to_allowed)} allowed · "
                   f"{len(report.uncatalogued_added)} newly catalogued")
            if report.uncatalogued_added:
                msg += " (" + ", ".join(report.uncatalogued_added[:6]) + ")"
            return True, msg
        except Exception as e:  # noqa: BLE001 - never crash the TUI on discovery
            return False, f"model detection failed: {str(e)[:160]}"

    run_goal.detect_provider_models = detect_provider_models   # type: ignore[attr-defined]

    def run_compare(question: str, n: int, on_event) -> dict:
        """Ask N models the same question and stream compare_* events to the TUI.
        Builds its own Loop (compare is read-only — no tools, no workspace writes) so
        it works even before the first goal has lazily-loaded the shared providers."""
        cat = state.get("cat") or Catalog.load(_catalog_path())
        registry = state.get("registry") or ProviderRegistry.load()
        loop = Loop(catalog=cat, store=TaskStore(_state_root()), registry=registry,
                    route_health=state.get("rh") or _route_health(),
                    overrides=state.get("ov") or Overrides.load(),
                    progress=_quiet_progress)
        return loop.compare(question, n, config=LoopConfig(), on_event=on_event)

    run_goal.run_compare = run_compare   # type: ignore[attr-defined]

    # T11: an engine-owned background scheduler the TUI's /bg + /jobs drive (replacing the
    # private daemon-thread + dict). Tracks N in-flight tasks, fires task_started/done/error +
    # a "running/waiting N" monitor event (the SEAM the panel renders), and auto-resumes via the
    # on_done continuation. One scheduler per session.
    from ..core.scheduler import BackgroundScheduler
    _scheduler = BackgroundScheduler()

    def run_bg(goal_text: str, on_event=None, *, task_id: str = "", label: str = ""):
        """Dispatch a goal to run in the BACKGROUND (non-blocking) via the scheduler, and LEARN
        from it on completion (T12 gap 3: bg work now accumulates memory too). Returns the
        scheduler's BgTask immediately. `on_event(kind,payload)` receives the scheduler SEAM."""
        import uuid as _uuid
        tid = task_id or f"bg-{_uuid.uuid4().hex[:8]}"
        sink = on_event or (lambda k, p: None)
        _sched = _scheduler
        if on_event is not None:                       # per-call event sink
            _sched = BackgroundScheduler(on_event=lambda k, p: sink(k, p))
            _sched._tasks = _scheduler._tasks          # share the in-flight registry
        bg_cfg = LoopConfig(librarian=True, auto_tools=True, auto_approve=bool(state.get("auto_approve", False)))
        bg_loop = Loop(catalog=state.get("cat") or Catalog.load(_catalog_path()),
                       store=TaskStore(_state_root()),
                       registry=state.get("registry") or ProviderRegistry.load(),
                       route_health=state.get("rh") or _route_health(),
                       overrides=state.get("ov") or Overrides.load(),
                       progress=lambda k, p: sink(k, {**p, "bg_task": tid}))

        def _work():
            res = bg_loop.run(goal_text, workspace_root=str(Path.cwd()), config=bg_cfg,
                              session_memory=_load_session_memory())
            # T12 gap 3: a finished bg task learns durable facts like a foreground run.
            _run_librarian({"history": [], "summary": "", "last_extract_sig": ""},
                           goal_text, res, bg_loop, bg_cfg, lambda *a, **k: None)
            return res

        return _sched.submit(tid, _work, label=label or goal_text[:48])

    run_goal.run_bg = run_bg              # type: ignore[attr-defined]
    run_goal.scheduler = _scheduler       # type: ignore[attr-defined]  (jobs status, waiting N)

    def message_index():
        """Message-navigator seam: the user's past messages as (turn_index, label) for the
        /msgs overlay + the right-edge minimap rail. Reads the live session history."""
        from ..core.loop import user_message_index
        return user_message_index(state.get("history", []))
    run_goal.message_index = message_index   # type: ignore[attr-defined]
    return run_goal


def _emit_mode(on_event, auto_approve: bool, autopilot_n: int) -> None:
    """Emit a one-line badge describing the run's permission + auto posture."""
    perm = "🔓 auto-approve" if auto_approve else "🔒 ask"
    auto = f" · ↻ auto×{autopilot_n}" if autopilot_n > 1 else ""
    on_event(f"⚙ {perm}{auto}", "tool")


def _cli_trust_prompt(config_path: str, summary: list) -> bool:
    """One-time y/N prompt for trusting a folder-local provider config."""
    _print(f"⚠ This folder has its own provider config: {config_path}")
    for line in summary:
        _print(f"    {line}")
    try:
        ans = input("Trust and use it for this folder? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Folder-local config trust: ask ONCE before using a repo's ./.syntra/providers.json
    # (remembered per-folder). Off for tests/library; the CLI opts in here, and the
    # prompt runs before any TUI/curses so it uses plain input.
    from ..core import registry as _reg
    from ..core import folder_trust as _ft
    _reg.enable_trust_enforcement()
    _ft.enable_enforcement()          # #201(c): also gate repo-local hooks/mcp/plugins exec surfaces
    _reg.preflight_repo_local_trust(_cli_trust_prompt)
    # #212: act on the stamped schema versions — run any pending config migrations
    # (e.g. remap a retired model id in the GLOBAL providers.json/overrides.json)
    # ONCE at startup, before any config is loaded. Fail-open + GLOBAL-config-only.
    from ..core import migrations as _mig
    _mig.run_migrations_if_needed()
    # No subcommand -> interactive. Default to the full-screen TUI on a real
    # terminal (modern full-screen style); --plain forces the line REPL; the TUI falls
    # back to the REPL automatically when there's no tty.
    if not getattr(args, "func", None):
        want_tui = getattr(args, "tui", False) or not getattr(args, "plain", False)
        # opt-in inline mode (native scrollback). Falls back to the REPL on no-tty just
        # like the full-screen TUI does.
        want_inline = getattr(args, "inline", False) or os.environ.get("SYNTRA_INLINE") in ("1", "true", "yes")
        if want_inline and not getattr(args, "plain", False):
            try:
                from .inline_tui import run_inline
                return run_inline(_make_tui_runner(), startup_note_fn=_startup_health_summary)
            except ProviderRegistryError as e:
                _print(f"error: {e}")
                return 2
            except RuntimeError:
                pass   # no tty -> fall through to the line REPL
        if want_tui:
            try:
                from .tui2 import run_tui2
                return run_tui2(_make_tui_runner(), startup_note_fn=_startup_health_summary)
            except ProviderRegistryError as e:
                _print(f"error: {e}")
                return 2
            except RuntimeError as e:
                # ONLY a legitimate "no real terminal / curses unavailable" reason
                # falls back to the line REPL silently. That's the correct, expected
                # degrade path (piped output, CI, no tty).
                msg = str(e)
                if "not a real terminal" in msg or "curses unavailable" in msg:
                    return cmd_session(args)
                # Any OTHER RuntimeError is a real TUI bug -> show it and STOP, so
                # it isn't hidden behind a degraded mode that looks "fine".
                _print(f"[tui] error: {msg}")
                _print("[tui] run `SYNTRA_TUI_DEBUG=1 syntra` for a full traceback,")
                _print("      or `syntra --plain` to use line mode on purpose.")
                if os.environ.get("SYNTRA_TUI_DEBUG"):
                    import traceback; traceback.print_exc()
                return 1
            except Exception as e:  # noqa: BLE001 - any TUI crash -> show it and STOP
                _print(f"[tui] crashed on launch: {type(e).__name__}: {e}")
                _print("[tui] run `SYNTRA_TUI_DEBUG=1 syntra` for a full traceback,")
                _print("      or `syntra --plain` to use line mode on purpose.")
                if os.environ.get("SYNTRA_TUI_DEBUG"):
                    import traceback; traceback.print_exc()
                return 1
        return cmd_session(args)
    # `syntra resume [id]` on a real terminal REOPENS the conversation in the TUI
    # (not a silent CLI re-run — user: "resume means open that chat again"). A missing
    # id resolves to the most recent session. --plain / no-tty keeps CLI cmd_resume.
    if (getattr(args, "func", None) is cmd_resume and not getattr(args, "plain", False)
            and sys.stdout.isatty() and sys.stdin.isatty()):
        _rid = (getattr(args, "task_id", "") or "").strip()
        if not _rid:
            _td = _state_root() / "tasks"
            _dirs = [d for d in _td.iterdir() if (d / "task.json").exists()] if _td.exists() else []
            if _dirs:
                _rid = max(_dirs, key=lambda d: d.stat().st_mtime).name
        if _rid:
            try:
                from .tui2 import run_tui2
                return run_tui2(_make_tui_runner(resume_id=_rid),
                                startup_note_fn=_startup_health_summary)
            except RuntimeError as e:
                if not ("not a real terminal" in str(e) or "curses unavailable" in str(e)):
                    raise   # real TUI bug; else fall through to CLI resume
    try:
        return args.func(args)
    except CatalogError as e:
        _print(f"error: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
