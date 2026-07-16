"""Per-session tool permission store (Step 3).

When a tool wants to do something risky (write/edit/run a command), the agent
asks ONCE with three choices:
  - allow once    : permit this single call
  - allow session : permit this kind for the rest of the session (don't ask again)
  - reject        : deny this call

The "allow session" decision is remembered so the agent never nags. Safe tools
(read/list/glob/grep) never ask. The actual prompting is INJECTED (an `ask`
callback returning one of the three choices), so this is pure + unit-tested; the
CLI/TUI supplies a real prompt. The "key" we remember a session-grant against is
the tool name + danger class (not the exact args), so granting a tool once covers
the rest of the session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ALLOW_ONCE = "once"
ALLOW_SESSION = "session"
ALLOW_ALWAYS = "always"     # durable — remember across sessions (persisted to disk)
DENY_ONCE = "deny_once"     # refuse THIS call; ask again next time
DENY_ALL = "deny_all"       # refuse + remember for this session — stop asking for this action
REJECT = DENY_ONCE          # back-compat alias: the old single "reject" == deny once
_CHOICES = (ALLOW_ONCE, ALLOW_SESSION, ALLOW_ALWAYS, DENY_ONCE, DENY_ALL)

# #83 deny-with-guidance: the user denies the call BUT hands the agent a reason/suggestion so it
# adapts instead of being silently blocked. Carried as a "deny_guide:<reason>" answer string
# (a deny that also stashes the reason on the store → dispatch surfaces it in the tool result).
DENY_GUIDE_PREFIX = "deny_guide:"


def deny_guide(reason: str) -> str:
    """Build the deny-with-guidance answer: denies this call + carries `reason` back to the agent."""
    return DENY_GUIDE_PREFIX + (reason or "").strip()


@dataclass
class PermissionStore:
    # ask(name, danger, args) -> one of _CHOICES. Default: reject (deny by default).
    ask: callable = None
    auto_approve: bool = False                       # --auto-approve: never prompt, always allow
    store_path: "Path | str | None" = None           # B2: where durable "always" grants persist
    # THE FLOOR: a predicate(name, args) -> bool that flags a call touching a SENSITIVE path
    # (.env / .git / keys / credential stores). A sensitive call is NEVER auto-approved and never
    # covered by a session/durable grant — it always asks fresh, so you can't blanket-allow a
    # write to your own secrets. Injected so this module stays decoupled from the path logic
    # (access_modes.call_is_sensitive supplies it). None => no floor (legacy behavior).
    sensitive_check: callable = None
    # ACCESS MODE: a callable(name) -> "auto" | "ask" | "off" | None giving the per-tool setting
    # from the active access mode (access_modes.AccessState.tool_setting). Most-restrictive wins:
    # `off` refuses the tool outright, `auto` runs it without a prompt (unless it's a secret —
    # the floor still wins), `ask`/None fall back to the prompt + grant flow. None => no gate.
    tool_gate: callable = None
    _granted: set = field(default_factory=set)       # action keys granted for THIS session
    _always: set = field(default_factory=set)        # action keys granted DURABLY (loaded from disk)
    _denied: set = field(default_factory=set)        # action keys DENIED for this session (deny-all)
    last_denial_reason: str = ""                     # #83: the reason from the most recent
                                                     # deny-with-guidance (dispatch surfaces it)

    def __post_init__(self) -> None:
        self._always = self._load_always()

    def child_store(self) -> "PermissionStore":
        """#244/#253: a fresh permission store for a SUB-AGENT that does NOT inherit the parent's
        one-off/session grants or denies — so a parent's "allow session" can't silently authorize
        a child (approval-leak). The child keeps the same interactive `ask`, the sensitive-path
        FLOOR, the access-mode `tool_gate`, and the durable "always" policy (a user's persisted
        allow-list is intended to apply everywhere); only the SESSION grant/deny sets start empty.
        `auto_approve` is preserved (an explicit --auto-approve run stays auto for children)."""
        child = PermissionStore(
            ask=self.ask,
            auto_approve=self.auto_approve,
            store_path=self.store_path,
            sensitive_check=self.sensitive_check,
            tool_gate=self.tool_gate,
        )
        return child

    def _is_sensitive(self, name: str, args: dict) -> bool:
        if self.sensitive_check is None:
            return False
        try:
            return bool(self.sensitive_check(name, args or {}))
        except Exception:  # noqa: BLE001 - a misbehaving predicate must not crash the gate
            return False

    # Binaries that dispatch on a SUBCOMMAND with very different risk per sub (e.g. `git status`
    # vs `git push --force`). For these the grant key includes the subcommand so allowing a safe
    # sub never silently authorizes a dangerous one (MED-3).
    _SUBCOMMAND_BINARIES = frozenset({
        "git", "npm", "npx", "pnpm", "yarn", "docker", "kubectl", "pip", "pip3",
        "cargo", "go", "gh", "aws", "gcloud", "systemctl", "make", "terraform",
    })
    # #202(d): interpreters are dispatchers of ARBITRARY code, not fixed subcommands.
    # Keying only on the `python` head means one "always" grant on `python build.py`
    # silently auto-approves `python -c '<any code>'`. So: when an inline-eval flag is
    # present (`-c`/`-e`/`-E`/`--eval`/`--exec`), the grant key is the FULL command (each
    # distinct payload asks fresh — the first payload word is NOT enough, it collides);
    # otherwise key on the script path (so re-running the same script reuses the grant).
    _INTERPRETERS = frozenset({
        "python", "python2", "python3", "node", "deno", "bun", "sh", "bash",
        "zsh", "dash", "ksh", "ruby", "perl", "php", "Rscript", "osascript", "pwsh",
    })
    _EVAL_FLAGS = frozenset({"-c", "-e", "-E", "--eval", "--exec", "-p", "-P"})

    @staticmethod
    def _bin_base(tok: str) -> str:
        """Basename of the invoked binary so `/usr/bin/python3` and `./python` classify like
        `python`. Version suffixes (python3.11) are normalized to the family for grouping."""
        import os
        base = os.path.basename(tok)
        for fam in ("python", "node", "ruby", "perl", "php"):
            if base.startswith(fam):
                return fam if base == fam or base[len(fam):].replace(".", "").isdigit() else base
        return base

    def _key(self, name: str, args: dict | None = None) -> str:
        """A grant/deny key scoped to the KIND OF ACTION, not just the tool name — so 'allow
        session' means "this kind of thing", not "this tool forever". For a shell command it's
        the tool + command HEAD, plus the SUBCOMMAND for dispatcher binaries (e.g. `bash:git:push`
        — so a `git status` grant never covers `git push`); interpreters running inline code key on
        the FULL command (so a `python build.py` grant never covers `python -c '<code>'`); for a
        file tool / apply_patch it's the tool + the target PATH(s); everything else falls back to
        the bare tool name. A genuinely different command/path → a different key → asked again."""
        args = args or {}
        if name in ("bash", "run", "exec_command"):
            cmd = str(args.get("command", "") or args.get("cmd", "")).strip()
            parts = cmd.split()
            if not parts:
                return name
            head = parts[0]
            base = self._bin_base(head)
            if base in self._INTERPRETERS and len(parts) > 1:
                rest = parts[1:]
                # Inline eval (`python -c …`, `node -e …`, `perl -e …`) runs arbitrary code:
                # every distinct payload must be its own key — grant one, not all.
                if any(p in self._EVAL_FLAGS for p in rest):
                    return f"{name}:{base}:code:{cmd}"
                # Otherwise it's `<interpreter> <script> [args]` — key on the script path so
                # the SAME script reused across flags is one grant, a DIFFERENT script asks.
                script = next((p for p in rest if not p.startswith("-")), "")
                return f"{name}:{base}:{script}" if script else f"{name}:{base}:{cmd}"
            if head in self._SUBCOMMAND_BINARIES and len(parts) > 1:
                # first non-flag token after the binary = the subcommand
                sub = next((p for p in parts[1:] if not p.startswith("-")), "")
                return f"{name}:{head}:{sub}" if sub else f"{name}:{head}"
            return f"{name}:{head}"
        # apply_patch carries paths in the patch blob — key on them so one grant ≠ all patches.
        if name == "apply_patch":
            try:
                from .access_modes import _paths_in_args
                paths = sorted(set(_paths_in_args(args)))
                if paths:
                    return f"{name}:{','.join(paths)}"
            except Exception:  # noqa: BLE001
                pass
            return name
        path = args.get("path") or args.get("file") or args.get("dest")
        if isinstance(path, str) and path:
            return f"{name}:{path}"
        return name

    def _load_always(self) -> set:
        if not self.store_path:
            return set()
        try:
            data = json.loads(Path(self.store_path).read_text(encoding="utf-8"))
            return {str(x) for x in (data.get("tools", []) if isinstance(data, dict) else [])}
        except (OSError, ValueError):
            return set()

    def _save_always(self) -> None:
        if not self.store_path:
            return
        try:
            p = Path(self.store_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"tools": sorted(self._always)}, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _tool_setting(self, name: str) -> str | None:
        if self.tool_gate is None:
            return None
        try:
            return self.tool_gate(name)
        except Exception:  # noqa: BLE001 - a misbehaving gate must not crash the permit path
            return None

    def tool_is_off(self, name: str) -> bool:
        """#251: True if this tool is turned OFF (never available). A NON-prompting query so the
        model's advertised tool schema can drop off tools — an unavailable tool in the schema is
        attack surface + an injection lure + wasted tokens. Never asks the user."""
        return self._tool_setting(name) == "off"

    def permit(self, name: str, danger: str, args: dict) -> bool:
        """Decide whether a non-safe tool call may run (safe tools bypass this)."""
        if danger == "safe":
            return True
        # THE FLOOR — runs BEFORE every shortcut. A call touching a sensitive path is never
        # auto-approved and never covered by a session/durable grant; it asks fresh each time.
        # The user can still ALLOW it interactively (once/session/always all permit THIS call),
        # but a blanket auto-approve cannot silently write/read your secrets.
        sensitive = self._is_sensitive(name, args)
        key = self._key(name, args)
        # ACCESS MODE per-tool setting (most-restrictive-wins): `off` refuses outright; `auto`
        # runs without a prompt UNLESS the call is sensitive (the floor still forces the ask).
        setting = self._tool_setting(name)
        if setting == "off":
            return False
        # Session-level deny (the user chose "deny all" for this action) short-circuits to NO
        # without re-asking — the deny counterpart of an "allow session" grant. Secrets re-ask
        # every time, so a remembered deny on a secret is irrelevant (it would only ever deny).
        if not sensitive and key in self._denied:
            return False
        if setting == "auto" and not sensitive:
            return True
        if not sensitive:
            if self.auto_approve:
                return True
            if key in self._always or key in self._granted:   # durable OR this-session grant
                return True
        if self.ask is None:
            # no prompt available: deny a sensitive call even under auto_approve (fail closed on
            # secrets), otherwise fall back to auto_approve's earlier allow path being unreachable.
            return False
        choice = self.ask(name, danger, args)
        # Clear any stale guidance up front; set it only when THIS answer carries a reason.
        self.last_denial_reason = ""
        # #83 deny-with-guidance: "deny_guide:<reason>" denies the call AND stashes the reason so
        # dispatch can hand it back to the agent ("denied — <why/what to do instead>").
        if isinstance(choice, str) and choice.startswith(DENY_GUIDE_PREFIX):
            self.last_denial_reason = choice[len(DENY_GUIDE_PREFIX):].strip()
            return False
        if sensitive:
            # a sensitive call: any positive choice permits THIS call only — never remembered,
            # so the next secret touch asks again. A deny denies.
            return choice in (ALLOW_ONCE, ALLOW_SESSION, ALLOW_ALWAYS)
        if choice == ALLOW_ALWAYS:
            self._always.add(key)
            self._save_always()                       # persist so we never re-ask, even next session
            return True
        if choice == ALLOW_SESSION:
            self._granted.add(key)
            return True
        if choice == ALLOW_ONCE:
            return True
        if choice == DENY_ALL:
            self._denied.add(key)                     # remember the NO for this session
            return False
        return False                                 # DENY_ONCE (or anything unexpected): just this time

    def granted_for_session(self) -> set:
        return set(self._granted)

    def grant_session(self, name: str) -> None:
        self._granted.add(self._key(name))

    def always_allowed(self) -> set:
        """Tool names the user chose to allow DURABLY (across sessions)."""
        return set(self._always)
