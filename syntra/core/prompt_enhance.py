"""Prompt enhancer — auto-improve user prompts before sending to model.

Adds context, structure, and clarity to vague prompts. Doesn't modify
already-clear prompts. Inspired by legacy/syntra-v1/orchestrator/prompt_enhancer.py.
"""

from __future__ import annotations


def enhance(prompt: str, *, workspace: str = "", repo_summary: str = "") -> str:
    """Enhance a user prompt with context and structure.

    Only modifies the prompt if it's vague or could benefit from context.
    Already-clear prompts (long, specific, with code) pass through unchanged.
    """
    stripped = prompt.strip()
    if not stripped:
        return stripped

    # Don't enhance already-detailed prompts
    if len(stripped) > 200 or "```" in stripped or stripped.startswith("/"):
        return stripped

    # Don't enhance single-word greetings
    greetings = {"hi", "hey", "hello", "hii", "sup", "yo"}
    if stripped.lower() in greetings:
        return stripped

    parts = [stripped]

    # Add workspace context if available
    if workspace:
        parts.append(f"\n\nWorkspace: {workspace}")

    # Add repo context if available
    if repo_summary:
        parts.append(f"\n\nProject context:\n{repo_summary[:500]}")

    return "\n".join(parts)


def classify_intent(prompt: str) -> str:
    """Classify the user's intent from their prompt.

    Returns one of: 'code', 'debug', 'review', 'plan', 'question', 'chat'
    """
    lower = prompt.lower().strip()

    code_signals = ["write", "create", "add", "implement", "build", "make",
                    "function", "class", "api", "endpoint"]
    debug_signals = ["fix", "bug", "error", "crash", "broken", "failing",
                     "not working", "issue", "wrong"]
    review_signals = ["review", "check", "audit", "improve", "refactor",
                      "optimize", "simplify", "clean"]
    plan_signals = ["plan", "design", "architect", "structure", "approach",
                    "strategy", "how should"]
    question_signals = ["what", "why", "how", "where", "when", "explain",
                        "describe", "tell me"]

    for sig in debug_signals:
        if sig in lower:
            return "debug"
    for sig in review_signals:
        if sig in lower:
            return "review"
    for sig in plan_signals:
        if sig in lower:
            return "plan"
    for sig in code_signals:
        if sig in lower:
            return "code"
    for sig in question_signals:
        if lower.startswith(sig):
            return "question"

    return "chat"
