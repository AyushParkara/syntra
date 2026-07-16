"""Hook engine — event-driven automation rules.

Rules trigger on events (tool calls, file edits, commands, prompts) and can
warn the user or block the action. Loaded from hooks.json.

Syntra's event-rule automation engine.

hooks.json format:
{
  "hooks": [
    {
      "name": "block-rm-rf",
      "enabled": true,
      "event": "bash",
      "pattern": "rm\\s+-rf",
      "action": "block",
      "message": "Dangerous rm -rf detected! Please verify the path."
    },
    {
      "name": "warn-env-file",
      "enabled": true,
      "event": "file",
      "pattern": "\\.env",
      "action": "warn",
      "message": "Editing .env file — make sure no secrets are committed."
    }
  ]
}
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Hook:
    name: str
    event: str       # "bash" | "file" | "prompt" | "tool" | "all"
    pattern: str     # regex pattern to match
    action: str      # "warn" | "block"
    message: str     # message to show
    enabled: bool = True
    _compiled: re.Pattern | None = field(default=None, repr=False)

    def matches(self, text: str) -> bool:
        if not self.enabled:
            return False
        if self._compiled is None:
            try:
                self._compiled = re.compile(self.pattern, re.IGNORECASE)
            except re.error:
                return False
        return bool(self._compiled.search(text))


@dataclass
class HookResult:
    hook: Hook
    matched_text: str
    blocked: bool


class HookEngine:
    """Evaluates hooks against events. Thread-safe for read operations."""

    def __init__(self):
        self._hooks: list[Hook] = []

    def load(self, path: str | Path | None = None) -> int:
        """Load hooks from a JSON file. Returns count loaded."""
        paths = []
        if path:
            paths.append(Path(path))
        paths.extend([
            Path.home() / ".config" / "syntra" / "hooks.json",
            Path(os.environ.get("SYNTRA_STATE_DIR", ".syntra")) / "hooks.json",
        ])
        # F32: load + merge EVERY existing candidate path (arg, ~/.config, .syntra), not just the
        # first — otherwise a project-local hooks.json is silently ignored when a global one exists.
        total = 0
        for p in paths:
            if p.exists():
                try:
                    data = json.loads(p.read_text())
                    raw_hooks = data.get("hooks", [])
                    loaded = [
                        Hook(
                            name=h.get("name", f"hook-{i}"),
                            event=h.get("event", "all"),
                            pattern=h.get("pattern", ""),
                            action=h.get("action", "warn"),
                            message=h.get("message", ""),
                            enabled=h.get("enabled", True),
                        )
                        for i, h in enumerate(raw_hooks)
                        if h.get("pattern")
                    ]
                    # Append loaded rules (preserve any already-added defaults + earlier files),
                    # de-duping by name so a later rule overrides an earlier one.
                    for h in loaded:
                        self._hooks = [x for x in self._hooks if x.name != h.name]
                        self._hooks.append(h)
                    total += len(loaded)
                except (json.JSONDecodeError, OSError):
                    pass
        return total

    def add(self, hook: Hook) -> None:
        self._hooks.append(hook)

    def remove(self, name: str) -> bool:
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        return len(self._hooks) < before

    def check(self, event: str, text: str) -> list[HookResult]:
        """Check text against all hooks for the given event type.

        Returns list of triggered hook results (may be empty).
        """
        results = []
        for hook in self._hooks:
            if not hook.enabled:
                continue
            if hook.event not in (event, "all"):
                continue
            if hook.matches(text):
                results.append(HookResult(
                    hook=hook,
                    matched_text=text[:200],
                    blocked=(hook.action == "block"),
                ))
        return results

    def check_blocked(self, event: str, text: str) -> HookResult | None:
        """Check if any blocking hook triggers. Returns the first block or None."""
        for r in self.check(event, text):
            if r.blocked:
                return r
        return None

    def list_hooks(self) -> list[Hook]:
        return list(self._hooks)

    def save(self, path: str | Path | None = None) -> None:
        """Save current hooks to JSON."""
        p = Path(path) if path else Path.home() / ".config" / "syntra" / "hooks.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "hooks": [
                {
                    "name": h.name,
                    "event": h.event,
                    "pattern": h.pattern,
                    "action": h.action,
                    "message": h.message,
                    "enabled": h.enabled,
                }
                for h in self._hooks
            ]
        }
        p.write_text(json.dumps(data, indent=2))


# Default safety hooks (always active, not loaded from file)
DEFAULT_HOOKS = [
    # F23: catch dangerous recursive rm forms, not just an absolute "/" path — short flags in
    # any order (-rf/-fr/-Rf), the long --recursive flag, and the high-risk targets / ~ . *
    # (advisory layer; the real guard is the sandbox). Case-insensitive is applied at match time.
    Hook("block-rm-rf", "bash",
         r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\s+(?:-\S+\s+)*(?:/|~|\.|\*)\S*(?:\s|$)",
         "block",
         "BLOCKED: recursive rm targeting / ~ . or * — this could destroy your files/system."),
    Hook("warn-env-edit", "file", r"\.env$", "warn",
         "Editing .env file — ensure no secrets are committed."),
    Hook("warn-force-push", "bash", r"git\s+push\s+.*--force", "warn",
         "Force push detected — this rewrites remote history."),
    Hook("block-drop-table", "bash", r"DROP\s+TABLE|DROP\s+DATABASE", "block",
         "BLOCKED: destructive SQL detected."),
]
