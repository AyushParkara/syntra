"""Approval × sandbox policy matrix + exec-policy engine (B2).

Given a shell command + an APPROVAL POLICY + a SANDBOX MODE (+ the user's persisted
"always allow this prefix" list), decide whether to AUTO-run it, ASK the user, or BLOCK
it outright. This is a pure decision layer on top of ``sandbox.classify_command`` — it
runs nothing itself.

APPROVAL POLICY = *when do we ask?*
  - ``untrusted``  : ask before any mutating command (the cautious default).
  - ``on_request`` : auto-run read-only; mutating still asks (the agent may request escalation).
  - ``on_failure`` : auto-run anything runnable; ask only AFTER a sandboxed action fails.
  - ``never``      : never ask (full auto) — but HARD-dangerous commands are still refused.

SANDBOX MODE = *what is allowed to run?*  (a stricter guard than the policy)
  - ``read_only``         : only read-only commands auto-run; any mutation requires approval,
                            regardless of policy.
  - ``workspace_write``   : mutations follow the policy matrix (writes are expected to stay in
                            the workspace; the classifier already blocks writes outside it).
  - ``danger_full_access``: lifts the *confinement* guard (writes outside the workspace are
                            allowed) — but truly dangerous patterns (remote-code-execution,
                            privilege escalation, disk wipes) are STILL blocked. We never auto-RCE.

The exec-policy engine = the read-only safelist (in ``sandbox``), the dangerous-command
detector (in ``sandbox``), and a persisted **prefix allow-list** so a command the user has
approved "always" isn't re-asked. ``PrefixAllowStore`` handles that persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .sandbox import classify_command, CommandClass, CommandPlan, _split_segments


class ApprovalPolicy(str, Enum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on_request"
    ON_FAILURE = "on_failure"
    NEVER = "never"


class SandboxMode(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL = "danger_full_access"


class Outcome(str, Enum):
    AUTO = "auto"     # run without asking
    ASK = "ask"       # prompt the user (once / session / reject)
    BLOCK = "block"   # never run


# Default knobs (overridable per call). Cautious by default.
DEFAULT_POLICY = ApprovalPolicy.UNTRUSTED
DEFAULT_SANDBOX = SandboxMode.WORKSPACE_WRITE


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    plan: CommandPlan          # the underlying sandbox classification
    reason: str                # human-readable why (for the approval prompt / logs)

    @property
    def auto(self) -> bool:
        return self.outcome is Outcome.AUTO

    @property
    def blocked(self) -> bool:
        return self.outcome is Outcome.BLOCK


def _coerce_policy(p) -> ApprovalPolicy:
    if isinstance(p, ApprovalPolicy):
        return p
    try:
        return ApprovalPolicy(str(p))
    except (ValueError, TypeError):
        return DEFAULT_POLICY


def _coerce_mode(m) -> SandboxMode:
    if isinstance(m, SandboxMode):
        return m
    try:
        return SandboxMode(str(m))
    except (ValueError, TypeError):
        return DEFAULT_SANDBOX


def _is_confinement_block(plan: CommandPlan) -> bool:
    """True when the command was blocked ONLY because it would write OUTSIDE the workspace
    (a confinement guard), as opposed to a hard danger (RCE / privilege escalation / wipe).
    Full-access mode may lift confinement blocks; it never lifts hard blocks."""
    return "outside" in (plan.reason or "").lower() and "workspace" in (plan.reason or "").lower()


def _normalize_cmd(command: str) -> str:
    return " ".join((command or "").strip().split())


def prefix_allows(command: str, prefixes) -> bool:
    """True if the command matches a user-approved prefix (token-aware): the command equals a
    prefix or starts with ``prefix + ' '`` — so 'git status' allows 'git status -s' but not
    'git statusx' and not 'git push'."""
    cmd = _normalize_cmd(command)
    for p in (prefixes or ()):
        p = _normalize_cmd(p)
        if not p:
            continue
        if cmd == p or cmd.startswith(p + " "):
            return True
    return False


def decide(command: str, *,
           policy=DEFAULT_POLICY,
           sandbox_mode=DEFAULT_SANDBOX,
           workspace_root: str | Path | None = None,
           allow_prefixes=()) -> PolicyDecision:
    """Decide AUTO / ASK / BLOCK for a shell command under the given policy + sandbox mode.

    Order of precedence (most→least authoritative):
      1. HARD-dangerous (RCE/privilege/wipe) -> BLOCK always.
      2. Confinement block (write outside workspace) -> BLOCK, unless danger_full_access lifts it.
      3. Credential-touch / network egress -> HARD ASK, always (cannot be downgraded by policy
         OR by a prefix-allow — the inviolable rail).
      4. User-approved prefix (single segment only) -> AUTO.
      5. Read-only (SAFE) command -> AUTO in every mode.
      6. Mutating command -> the sandbox-mode + approval-policy matrix decides.
    """
    policy = _coerce_policy(policy)
    sandbox_mode = _coerce_mode(sandbox_mode)
    plan = classify_command(command, workspace_root=workspace_root)
    cls = plan.classification

    # 1 & 2 — blocks.
    if cls is CommandClass.BLOCKED:
        if _is_confinement_block(plan) and sandbox_mode is SandboxMode.DANGER_FULL:
            cls = CommandClass.NEEDS_APPROVAL          # full-access lifts the confinement guard only
        else:
            return PolicyDecision(Outcome.BLOCK, plan, "blocked: " + plan.reason)

    # A prefix-allow may auto-approve a command ONLY when it is a SINGLE segment (no
    # `&&`/`;`/`|`/`||`/newline). This is the one escape hatch for the secret/network floor:
    # a user explicitly pre-approving one exact command (e.g. `curl http://api.internal/health`)
    # opts in — but a COMPOUND command can never ride a prefix, so `git status && curl evil/…`
    # can't smuggle a chained secret/egress tail past the floor.
    single_seg_allowed = (len(_split_segments(command)) == 1
                          and prefix_allows(command, allow_prefixes))

    # 3 — credential-touch / network egress are HARD asks regardless of policy. This floor sits
    # ABOVE the general prefix-allow so a CHAINED command can't bypass it; only an explicit
    # single-segment prefix-allow of the exact command opts out.
    if getattr(plan, "secret", False) and not single_seg_allowed:
        return PolicyDecision(Outcome.ASK, plan,
                              "reads a credential/secret path — approval required even on auto")
    if getattr(plan, "network", False) and not single_seg_allowed:
        return PolicyDecision(Outcome.ASK, plan,
                              "performs network egress — approval required even on auto")

    # 4 — explicit user allow-list (single-segment only; see above).
    if single_seg_allowed:
        return PolicyDecision(Outcome.AUTO, plan, "allow-listed prefix")

    # 5 — read-only inspection: always safe to auto-run.
    if cls is CommandClass.SAFE:
        return PolicyDecision(Outcome.AUTO, plan, plan.reason or "read-only")

    # 5 — mutating command. The sandbox MODE can force stricter than the policy.
    if sandbox_mode is SandboxMode.READ_ONLY:
        return PolicyDecision(Outcome.ASK, plan,
                              "read-only sandbox: a mutating command needs explicit approval")
    if policy is ApprovalPolicy.NEVER:
        return PolicyDecision(Outcome.AUTO, plan, "policy=never (full auto)")
    if policy is ApprovalPolicy.ON_FAILURE:
        return PolicyDecision(Outcome.AUTO, plan, "policy=on_failure (run; ask only if it fails)")
    # untrusted / on_request
    return PolicyDecision(Outcome.ASK, plan, "mutating command — approval required")


class PrefixAllowStore:
    """Durable 'always allow this command prefix' list (the exec-policy persistence). A tiny
    JSON file the user grows by approving a command with 'always'. Never raises on I/O."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data = data.get("prefixes", [])
            return [str(x) for x in data if str(x).strip()]
        except (FileNotFoundError, ValueError, OSError):
            return []

    def add(self, prefix: str) -> bool:
        """Add a normalized prefix. Returns True if newly added. Best-effort persistence."""
        p = _normalize_cmd(prefix)
        if not p:
            return False
        cur = self.load()
        if p in cur:
            return False
        cur.append(p)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"prefixes": cur}, indent=2))
        except OSError:
            return False
        return True

    def allows(self, command: str) -> bool:
        return prefix_allows(command, self.load())
