"""Lifecycle hooks: pluggable callbacks at defined points in a run.

Syntra exposes lifecycle hooks (session_start, pre/post tool use, pre/post
compact) that user/plugin scripts subscribe to; a pre_tool_use hook may even
block a call. Stdlib-only:

Events:
  session_start / session_end
  pre_tool_use  (may BLOCK or rewrite-args)  / post_tool_use
  pre_compact   / post_compact

Two kinds of handler:
  - in-process Python callable(event, payload) -> HookResult | None
  - a shell COMMAND: the payload is passed as JSON on stdin; the command's exit
    code / stdout decides (exit 2 or stdout '{"decision":"block"}' blocks).

`pre_tool_use` is the only blocking event: a handler returning decision="block"
(or "deny") stops the call and feeds the reason back to the model. Everything is
pure dispatch over injected handlers, so it is fully unit-tested without spawning
anything; the command runner is only used when a command hook is registered.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

SESSION_START = "session_start"
SESSION_END = "session_end"
PRE_TOOL_USE = "pre_tool_use"
POST_TOOL_USE = "post_tool_use"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"

_BLOCKING = {PRE_TOOL_USE}
_BLOCK_DECISIONS = {"block", "deny"}
_EVENTS = {SESSION_START, SESSION_END, PRE_TOOL_USE, POST_TOOL_USE,
           PRE_COMPACT, POST_COMPACT}


@dataclass
class HookResult:
    decision: str = "allow"           # "allow" | "block" | "deny" | "ask"
    reason: str = ""                  # why (shown to the model when blocking)
    new_args: dict | None = None      # pre_tool_use may rewrite tool args

    @property
    def blocks(self) -> bool:
        return self.decision in _BLOCK_DECISIONS


@dataclass
class HookRegistry:
    # event -> list of handlers (python callables and/or {"command": "..."} dicts)
    handlers: dict = field(default_factory=dict)

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def _run_one(self, handler, event: str, payload: dict) -> HookResult | None:
        if callable(handler):
            return handler(event, payload)
        if isinstance(handler, dict) and handler.get("command"):
            return _run_command_hook(handler["command"], event, payload,
                                     timeout=float(handler.get("timeout", 10.0)))
        return None

    def fire(self, event: str, payload: dict | None = None) -> HookResult:
        """Run all handlers for `event`. For blocking events, the FIRST block/deny
        wins (and arg-rewrites accumulate). Non-blocking events ignore decisions.
        Returns an aggregate HookResult (allow if nothing blocked)."""
        payload = dict(payload or {})
        agg = HookResult(decision="allow")
        for h in self.handlers.get(event, []):
            try:
                res = self._run_one(h, event, payload)
            except Exception:  # noqa: BLE001 - a broken hook never kills the run
                res = None
            if res is None:
                continue
            if res.new_args is not None and event in _BLOCKING:
                agg.new_args = res.new_args
                payload = {**payload, "arguments": res.new_args}
            if event in _BLOCKING and res.blocks:
                return HookResult(decision=res.decision,
                                  reason=res.reason or "blocked by a pre_tool_use hook",
                                  new_args=agg.new_args)
        return agg


def load_hooks(path) -> "HookRegistry | None":
    """Build a HookRegistry from a JSON hooks config; None if no file / no hooks.

    File shape (lives under a ``hooks`` key so it can share hooks.json with the
    pattern-rule engine, which uses ``rules``)::

        {"hooks": {
            "pre_tool_use":  [{"command": "guard.sh", "timeout": 10}],
            "post_tool_use": ["log-tool.sh"]
        }}

    A bare string handler is treated as a shell command. Unknown event names are
    ignored. Only command hooks are loadable from JSON (Python callables are
    registered in-process via ``HookRegistry.on``).
    """
    from pathlib import Path as _Path
    try:
        data = json.loads(_Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    spec = data.get("hooks")
    if not isinstance(spec, dict):
        return None
    reg = HookRegistry()
    count = 0
    for event, handlers in spec.items():
        if event not in _EVENTS:
            continue
        if not isinstance(handlers, list):
            handlers = [handlers]
        for h in handlers:
            if isinstance(h, str) and h.strip():
                reg.on(event, {"command": h.strip()})
                count += 1
            elif isinstance(h, dict) and h.get("command"):
                reg.on(event, {"command": str(h["command"]),
                               "timeout": float(h.get("timeout", 10.0))})
                count += 1
    return reg if count else None


def _run_command_hook(command: str, event: str, payload: dict, *, timeout: float) -> HookResult | None:
    """Run a shell-command hook: JSON payload on stdin; exit 2 or a JSON stdout
    {"decision": "block", "reason": "..."} blocks. Non-zero exit (not 2) = allow
    but noted. Used only when a command hook is registered."""
    data = json.dumps({"event": event, **payload})
    try:
        proc = subprocess.run(command, shell=True, input=data, capture_output=True,
                              text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (proc.stdout or "").strip()
    if out.startswith("{"):
        try:
            obj = json.loads(out)
            return HookResult(decision=obj.get("decision", "allow"),
                              reason=obj.get("reason", ""), new_args=obj.get("new_args"))
        except json.JSONDecodeError:
            pass
    if proc.returncode == 2:                  # convention: exit 2 == block
        return HookResult(decision="block", reason=out[:200] or "hook exit 2")
    return HookResult(decision="allow")
