"""Progress-event -> one-line formatter for the TUI activity feed (Track T1, (a)).

The Loop already emits a rich stream of progress events via its `progress`
callback (core/loop.py `_emit`). The line CLI renders them in
cli/main._run_progress / _quiet_progress. This is the PURE, testable equivalent
for the TUI: map (kind, payload) -> a compact line (or None to suppress).

`verbose=False` (default) mirrors _quiet_progress: only events the user must see
(halts, edits, real-verify failures, repair/auto/steer/council). `verbose=True`
adds the routing/usage/verification telemetry. All payload access is defensive
(.get) so a missing key never raises inside the run thread.
"""

from __future__ import annotations


def tui_progress_line(kind: str, payload: dict, *, verbose: bool = False) -> str | None:
    p = payload or {}
    # ---- always-surfaced events (mirror _quiet_progress) ----
    # Phase transitions — make the plan→execute→review cycle visible
    if kind == "phase":
        phase = p.get("phase", "?")
        model = p.get("model", "")
        short = model.split("/")[-1] if "/" in model else model
        icons = {"analyzing": "⊙", "planning": "▣", "executing": "▸",
                 "reviewing": "◎", "done": "✓", "failed": "✗"}
        icon = icons.get(phase, "·")
        if short:
            return f"{icon} {phase.upper()} ({short})"
        return f"{icon} {phase.upper()}"
    if kind == "step_start":
        sid = p.get("step_id", "?")
        desc = p.get("description", "")[:50]
        return f"  ▸ step {sid}: {desc}" if desc else f"  ▸ step {sid}"
    if kind == "step_done":
        return f"  ✓ step {p.get('step_id', '?')} done"
    if kind == "plan_step":
        return f"  │ {p.get('step_id', '?')}: {p.get('description', '')[:60]}"
    if kind == "plan_ready":
        return f"● Plan ready ({p.get('steps', '?')} steps) — /resume to execute"
    if kind == "loop_halted":
        return f"[halt] {p.get('reason', '?')}"
    if kind == "edit":
        status = p.get("status", "?")
        if status in ("applied", "proposed", "failed"):
            return f"[edit] {status} {p.get('path', '?')}"
        return None
    if kind == "verify_result":
        if not p.get("ok"):
            return f"[verify] FAIL (exit {p.get('exit_code', '?')})"
        return f"[verify] ok (exit {p.get('exit_code', 0)})" if verbose else None
    if kind == "repair":
        return "[repair] added a fix-up step"
    if kind == "autopilot":
        return f"[auto] retrying (pass {p.get('iteration', '?')})"
    if kind == "steering":
        return f"[steer] {'injected now' if p.get('mode') == 'instant' else 'queued'}"
    if kind == "council":
        if "chosen" in p:
            return f"[council] picked best of {p.get('of', '?')} plans ({p.get('chosen')})"
        return f"[council] plan from {p.get('member', '?')} ({p.get('steps', '?')} steps)" if verbose else None
    if kind == "completion_audit" and not p.get("passed", True):
        return f"⚠ goal not fully met yet — {p.get('summary', '')}"
    if kind == "waiting_for_input":
        return f"[waiting] {p.get('message', 'awaiting user input')}"
    if kind == "input_received":
        return "[resumed] user input received"

    # ---- failover visibility (req: SHOW the switch, never blind-wait) ----
    if kind == "provider_failover":
        model = (p.get("model", "?")).split("/")[-1]
        frm, to = p.get("from", "?"), p.get("to", "?")
        kd = p.get("kind", "")
        return f"⚠ {model} via {frm} failed ({kd}) → switched to {to}"
    if kind == "key_failover":
        model = (p.get("model", "?")).split("/")[-1]
        return f"⚠ {model} via {p.get('from','?')} exhausted → switched key to {p.get('to','?')}"
    if kind == "capability_degrade":
        model = (p.get("model", "?")).split("/")[-1]
        cap = p.get("capability", "a feature")
        return f"⚠ {model} via {p.get('provider','?')} rejected {cap} → retried without it"
    if kind == "quality_reroute":
        model = (p.get("from", "?")).split("/")[-1]
        return f"⚠ {model} response looked off ({p.get('reason','low quality')}) → re-asking a different model"
    if kind == "key_exhausted":
        model = (p.get("model", "?")).split("/")[-1]
        nxt = p.get("next")
        tail = f" → next {nxt}" if nxt else " → no backup left"
        return f"⚠ {model} credential on {p.get('provider','?')} exhausted{tail}"
    if kind == "preflight_probe":
        model = (p.get("model", "?")).split("/")[-1]
        label = p.get("provider") or "?"
        status = p.get("status")
        if status == "ok":
            return f"✓ {model} via {label} ok ({p.get('latency_ms', 0)}ms)"
        if status == "empty":
            return f"⚠ {model} via {label} returned empty — trying next"
        if status == "inconclusive":
            return None  # budget-trap: not worth a line
        return f"⚠ {model} via {label} failed ({p.get('kind','?')}) — trying next"
    if kind == "preflight_summary":
        if p.get("healthy"):
            model = (p.get("working_model", "?")).split("/")[-1]
            label = p.get("working_provider") or "?"
            return f"● {p.get('role','?')}: using {model} via {label}"
        return f"✗ {p.get('role','?')}: NO working model among top picks"
    if kind == "silent_failure":
        model = (p.get("model", "?")).split("/")[-1]
        kd = p.get("kind", "?")
        labels = {"refusal": "refused / safety non-answer",
                  "degeneracy": "degenerate output (repetition)",
                  "tool_bypass": "claimed tool success but did nothing"}
        return f"⚠ {model} via {p.get('provider','?')}: {labels.get(kd, kd)} — cooling route"
    if kind == "credential_help":
        prov = p.get("provider", "?")
        cmd = f"syntra providers remove-key {prov} <key-suffix>"
        if p.get("kind") == "billing":
            return (f"💳 A credential for {prov} is OUT OF CREDITS — add credits, "
                    f"or remove it from private config:  {cmd}")
        return (f"🔑 A credential for {prov} was REJECTED (bad/expired key) — fix it, "
                f"or remove it from private config:  {cmd}")

    # ---- verbose-only telemetry (mirror _run_progress) ----
    if not verbose:
        return None
    if kind == "route":
        return f"[route] {p.get('role', '?')} -> {p.get('model', '?')} via {p.get('provider', '?')}"
    if kind == "analysis":
        return f"[analysis] {p.get('category', p.get('mode', '?'))}"
    if kind == "direct":
        return f"[direct] {p.get('model', '?')}"
    if kind == "usage":
        cache = ""
        if p.get("cache_read"):
            cache = f" cache✓{p.get('cache_read')}"
        return (f"[usage] {p.get('role', '?')} {p.get('model', '?')} "
                f"in={p.get('in', 0)} out={p.get('out', 0)}{cache} ${p.get('usd', 0.0):.4f}")
    if kind == "verification" and not p.get("passed", True):
        return f"⚠ step {p.get('step_id', '')} failed its verification check"
    if kind == "reasoning":
        return f"[think] {p.get('role', '?')} {p.get('model', '?')} effort={p.get('effort')}"
    if kind == "retry":
        return f"[retry] {p.get('role', '?')} -> {p.get('next_model', '?')}"
    if kind == "verify_command":
        return f"[verify] running: {p.get('command', '?')}"
    if kind == "escalation":
        return f"↑ retrying step {p.get('step_id', '?')} with more effort"
    if kind == "recovery":
        return f"↻ recovering step {p.get('step_id', '?')} (via {p.get('method', '?')})"
    if kind == "truncation_detected":
        return f"[truncated] {p.get('role', '?')} output cut off; recovering"
    return None
