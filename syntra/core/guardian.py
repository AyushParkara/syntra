"""Guardian: auto-approve clearly-safe tool calls instead of always prompting.

When a risky tool call needs permission, the guardian can run a fast review to
decide whether it's obviously safe (auto-allow) or should still be shown to the
user. This cuts nagging without lowering safety: the guardian only AUTO-ALLOWS;
it never auto-denies past the normal flow, and it FAILS CLOSED — any timeout,
error, or unclear verdict falls through to the normal permission prompt.

Syntra's guardian: a stdlib-only safety reviewer. The review call is INJECTED
(`review_fn(name, danger, args, context) -> "safe"|"unsafe"|"unsure"`), so the
policy is fully unit-tested without a model; the CLI wires a real fast model in.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tool names that are NEVER auto-allowed regardless of the review (always ask):
# shells + NETWORK EGRESS. Egress (webfetch/websearch) is the channel a hijacked
# agent would use to exfiltrate; file edits stay guardian-approvable so the agent's
# normal work isn't gated. This is "always ask", not "block".
_NEVER_AUTO = {"bash", "exec_command", "write_stdin", "repo_clone", "webfetch", "websearch"}


@dataclass
class Guardian:
    # review_fn(name, danger, args, context) -> "safe" | "unsafe" | "unsure"
    review_fn: object = None
    enabled: bool = False

    def auto_allows(self, name: str, danger: str, args: dict, context: str = "") -> bool:
        """True only when the guardian is confident the call is safe to run
        without asking. Fails closed (returns False) on anything else."""
        if not self.enabled or self.review_fn is None:
            return False
        if danger == "safe":
            return True                       # safe tools never needed asking anyway
        if name in _NEVER_AUTO:
            return False                      # high-impact tools always prompt
        try:
            verdict = (self.review_fn(name, danger, args, context) or "").strip().lower()
        except Exception:  # noqa: BLE001 - fail closed -> normal prompt
            return False
        return verdict == "safe"


def make_permit(store, guardian: "Guardian"):
    """Compose a guardian in FRONT of a PermissionStore: guardian auto-allows the
    obvious-safe calls, everything else goes through the store's ask-once flow.
    Returns permit(name, danger, args) -> bool for ToolContext."""
    def permit(name: str, danger: str, args: dict) -> bool:
        if guardian.auto_allows(name, danger, args):
            return True
        return store.permit(name, danger, args)
    return permit
