"""Per-role permissions (Phase 3 safety foundation).

Before Syntra can edit files or run commands, it must answer: *is this role even
allowed to do this, and does it need approval?* This module is that gate. It is
pure + deterministic (no I/O) so the permission logic is fully unit-tested and
cannot be accidentally bypassed by execution code.

Defaults (PLAN Section 16, req G6 / Section 6 #4 -- approval-gated by default):
- planner  : read-only. Plans; never edits, never runs commands.
- reviewer : read-only. Judges; never mutates.
- executor : may PROPOSE edits + commands, but nothing auto-applies. Approval is
             required unless the caller explicitly opts into auto-approve.

Nothing here performs the action; it only decides allow / needs-approval / deny.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    READ = "read"              # read files / inspect
    PROPOSE_EDIT = "propose_edit"  # produce a file-edit proposal (not apply)
    APPLY_EDIT = "apply_edit"      # write a file edit to disk
    RUN_COMMAND = "run_command"    # execute a shell command


class Decision(str, Enum):
    ALLOW = "allow"                  # permitted outright (no approval)
    NEEDS_APPROVAL = "needs_approval"  # permitted only with human approval
    DENY = "deny"                    # not permitted for this role, ever


# Which capabilities each role may even attempt. Read is universal.
_ROLE_CAPABILITIES: dict[str, set[Capability]] = {
    "planner": {Capability.READ},
    "reviewer": {Capability.READ},
    "executor": {Capability.READ, Capability.PROPOSE_EDIT,
                 Capability.APPLY_EDIT, Capability.RUN_COMMAND},
    "analyzer": {Capability.READ},
}

# Capabilities that mutate state -> approval-gated by default.
_MUTATING = {Capability.APPLY_EDIT, Capability.RUN_COMMAND}


@dataclass(frozen=True)
class RolePolicy:
    """Permission policy for one run.

    auto_approve: when True, mutating capabilities the role is allowed to perform
    return ALLOW instead of NEEDS_APPROVAL. Dangerous; opt-in only (CLI
    --auto-approve). Default False == approval-gated (safe).
    """

    auto_approve: bool = False

    def can(self, role: str, capability: Capability) -> Decision:
        """Decide whether ``role`` may perform ``capability`` right now."""
        allowed = _ROLE_CAPABILITIES.get((role or "").lower())
        if not allowed or capability not in allowed:
            return Decision.DENY
        if capability in _MUTATING and not self.auto_approve:
            return Decision.NEEDS_APPROVAL
        return Decision.ALLOW

    def is_read_only(self, role: str) -> bool:
        """True if the role can only read (planner/reviewer/analyzer)."""
        allowed = _ROLE_CAPABILITIES.get((role or "").lower(), set())
        return allowed == {Capability.READ}

    def requires_approval(self, role: str, capability: Capability) -> bool:
        return self.can(role, capability) is Decision.NEEDS_APPROVAL
