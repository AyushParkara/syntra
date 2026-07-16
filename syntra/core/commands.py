"""Slash-command registry for the command palette (Track T1).

A single source of truth for the interactive commands, so the palette overlay
(SelectList) and /help can both enumerate them. Pure data + helpers -> testable.
The actual dispatch lives in cli/main.session_dispatch; this is just the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str        # e.g. "/help"
    desc: str        # one-line description
    takes_arg: bool = False


SLASH_COMMANDS: list[Command] = [
    Command("/help", "show available commands"),
    Command("/agent", "run a goal with the agentic tool-using executor", takes_arg=True),
    Command("/goal", "set a persistent objective for the session", takes_arg=True),
    Command("/models", "list the model catalog / routing"),
    Command("/agents", "show current model assignments per role"),
    Command("/layout", "switch/save/load panel layout", takes_arg=True),
    Command("/mode", "switch agent mode", takes_arg=True),
    Command("/context", "context relay: brief (flat cost) vs full conversation", takes_arg=True),
    Command("/key", "add an API key: /key <provider> <key> [base_url]", takes_arg=True),
    Command("/compact", "compact the conversation context now"),
    Command("/clear", "start a fresh conversation"),
    Command("/status", "session status: model, cost, tokens"),
    Command("/stats", "usage dashboard: activity heatmap, streaks, cost/day", takes_arg=True),
    Command("/copy", "copy the last answer — or code block #n — to the clipboard", takes_arg=True),
    Command("/attach", "attach an image to send next: /attach <path>, no arg = clipboard (Ctrl+V), /attach clear", takes_arg=True),
    Command("/plan", "reopen the current plan as a click-to-expand card"),
    Command("/blocks", "list the indexed code blocks (copy one with /copy <n>)"),
    Command("/diff", "diff this turn's changes (·  /diff git = working tree · /diff turns = browse past turns)", takes_arg=True),
    Command("/review", "run a skeptical review pass"),
    Command("/mcp", "list connected MCP servers + tools"),
    Command("/skill", "load a named skill", takes_arg=True),
    Command("/find", "fuzzy-find a workspace file"),
    Command("/tree", "show the workspace file tree"),
    Command("/memories", "show durable memory (constraints/conventions)"),
    Command("/memory-update", "add a durable memory constraint", takes_arg=True),
    Command("/memory-drop", "drop a durable memory constraint", takes_arg=True),
    Command("/spin", "demo the live multi-agent panel — watch agents work", takes_arg=True),
    Command("/sessions", "list past sessions/tasks"),
    Command("/msgs", "jump to one of your past messages (or hover the right-edge rail)"),
    Command("/title", "name the current session (e.g. /title Auth refactor)", takes_arg=True),
    Command("/fork", "branch a new session from a past one", takes_arg=True),
    Command("/themes", "list/switch TUI themes"),
    Command("/keymap", "show keybindings (·  /keymap bind <key> </cmd> · /keymap unbind <key>)", takes_arg=True),
    Command("/effort", "set reasoning effort: auto | low | medium | high | xhigh | max", takes_arg=True),
    Command("/trace", "fold/unfold the background activity trace in the chat"),
    Command("/wizard", "open an interactive multi-step question wizard"),
    Command("/resume-question", "reopen a question you paused to chat about"),
    Command("/permissions", "access modes + per-permission controls (popup; arg: normal|locked|ask|auto, or 'rules' for the grant board)", takes_arg=True),
    Command("/access", "access modes + per-permission controls (popup; or arg: normal|locked)", takes_arg=True),
    Command("/use-proxy", "set an HTTP(S) proxy for provider calls (VPN-style)", takes_arg=True),
    Command("/config", "show effective config + paths"),
    Command("/login", "browser login for a provider", takes_arg=True),
    Command("/logout", "remove a provider's stored token", takes_arg=True),
    Command("/verify", "run the no-API health check"),
    Command("/doctor", "deep foundation health check"),
    Command("/export", "export the session transcript", takes_arg=True),
    Command("/changelog", "show what changed"),
    Command("/debug", "toggle debug detail"),
    Command("/auto", "toggle autopilot (auto-approve + keep working until done)", takes_arg=True),
    Command("/resume", "resume a task by id", takes_arg=True),
    Command("/proof", "run a goal in proof-only mode", takes_arg=True),
    Command("/tasks", "list recent tasks"),
    Command("/verbose", "toggle telemetry detail"),
    Command("/commands", "toggle showing each shell command as it runs (off: only un-sandboxed ones flagged)"),
    Command("/web", "search the web for context", takes_arg=True),
    Command("/browse", "open a URL in a headless browser (renders JS)", takes_arg=True),
    Command("/vision", "analyze an image", takes_arg=True),
    Command("/image", "attach an image for the model to see", takes_arg=True),
    Command("/imagine", "generate an image from a prompt (saved + shown inline)", takes_arg=True),
    Command("/view", "show an image inline in the terminal (kitty/iterm2/sixel)", takes_arg=True),
    Command("/preview", "render a URL or HTML file IN the terminal (headless browser → inline)", takes_arg=True),
    Command("/open", "open an image full-size in your OS image viewer (any terminal)", takes_arg=True),
    Command("/undo", "undo the last file edit"),
    Command("/rollback", "rollback to a checkpoint", takes_arg=True),
    Command("/cost", "show cumulative cost breakdown"),
    Command("/providers", "list configured providers + status"),
    Command("/route", "show the last routing decision"),
    Command("/pin", "pin a model to a role (e.g. /pin planner claude-opus-4.7)", takes_arg=True),
    Command("/unpin", "remove a role pin", takes_arg=True),
    Command("/stop", "stop the current task"),
    Command("/raw", "show the raw model response for the last turn"),
    Command("/code-review", "review current diff for bugs (effort: low|medium|high)", takes_arg=True),
    Command("/simplify", "review the diff for cleanup/simplification (no bug hunt)", takes_arg=True),
    Command("/bg", "run a goal in background", takes_arg=True),
    Command("/jobs", "list running background tasks"),
    Command("/deep-research", "deep multi-source research on a topic", takes_arg=True),
    Command("/council", "run a goal with several agents in parallel (watch them in the panel)", takes_arg=True),
    Command("/compare", "ask N models the same question, compare side by side + synthesize the best", takes_arg=True),
    Command("/plan-review", "toggle plan approval before running (default off — tasks just run)"),
    Command("/commit-style", "how the agent formats git commits it makes: off|minimal|neutral|branded", takes_arg=True),
    Command("/spend", "spend summary over the last N days (default 7)", takes_arg=True),
    Command("/replay", "replay a task's event timeline: /replay [json] <task-id>", takes_arg=True),
    Command("/ab-handoff", "compare truncate vs brief handoff for a task (no tokens): /ab-handoff [task-id] [step-id]", takes_arg=True),
    Command("/todo", "track session todos: add/list/done", takes_arg=True),
    Command("/grep", "search workspace file contents", takes_arg=True),
    Command("/search", "workspace search overlay: pick a match → insert @path#Lnn", takes_arg=True),
    Command("/watch", "\"watch\" a YouTube video: read its transcript + explain it (flags visual-only content)", takes_arg=True),
    Command("/unwatch", "stop watching for changes"),
    Command("/feature", "build a feature with multi-agent workflow (explore→architect→review)", takes_arg=True),
    Command("/benchmark", "test model response speed across providers"),
    Command("/ask", "ask a question and get instant answer", takes_arg=True),
    Command("/git", "git operations: commit, push, pr, log, stash", takes_arg=True),
    Command("/rules", "set inviolable rules Syntra enforces (global or project)", takes_arg=True),
    Command("/hardware", "scan system hardware and recommend compatible models"),
    Command("/download-model", "download an Ollama model", takes_arg=True),
    Command("/btw", "quick side question without affecting main task", takes_arg=True),
    Command("/usage", "detailed cost breakdown by role"),
    Command("/skills", "list available skills (built-in + plugins)"),
    Command("/skill-create", "create a custom skill", takes_arg=True),
    Command("/agent-create", "create a custom agent (·  /agent-create <name> | <one sentence> = AI writes it)", takes_arg=True),
    Command("/plugins", "list installed plugins"),
    Command("/loop", "run a task on a recurring interval", takes_arg=True),
    Command("/batch", "run a task across git worktrees in parallel", takes_arg=True),
    Command("/rewind", "rollback conversation to N messages ago", takes_arg=True),
    Command("/hooks", "list active hooks/automation rules"),
    Command("/hook-add", "add a hook rule (name event pattern action message)", takes_arg=True),
    Command("/hook-remove", "remove a hook by name", takes_arg=True),
    Command("/map", "show workspace repo map with symbols"),
    Command("/exit", "quit syntra"),
]


# An OPTIONAL, out-of-tree integration may register extra slash commands at runtime
# (only when present in the local checkout — never part of the shipped command set).
# It supplies them via a `slash_commands()` function. Cached after first probe.
_OPTIONAL_COMMANDS: list[Command] | None = None


def _optional_commands() -> list[Command]:
    global _OPTIONAL_COMMANDS
    if _OPTIONAL_COMMANDS is not None:
        return _OPTIONAL_COMMANDS
    cmds: list[Command] = []
    try:
        from .tools import load_integration_module
        mod = load_integration_module()
        if mod is not None and hasattr(mod, "slash_commands"):
            for row in (mod.slash_commands() or []):
                # row: (name, desc) or (name, desc, takes_arg)
                takes_arg = bool(row[2]) if len(row) > 2 else False
                cmds.append(Command(row[0], row[1], takes_arg=takes_arg))
    except Exception:  # noqa: BLE001 - never let an optional probe break the command set
        cmds = []
    _OPTIONAL_COMMANDS = cmds
    return cmds


def _all_commands() -> list[Command]:
    return SLASH_COMMANDS + _optional_commands()


# P20: explicit "what to pass" usage hints for arg-taking commands. A command not
# listed here that still takes an arg falls back to a generic "<arg>"; commands that
# take no arg get nothing. One dict instead of churning every Command(...) line.
_USAGE = {
    "/fork": "/fork <session-id>",
    "/resume": "/resume [task-id]",
    "/login": "/login <provider>",
    "/logout": "/logout <provider>",
    "/skill": "/skill <name>",
    "/effort": "/effort <low|medium|high|xhigh|max|auto>",
    "/memory-update": "/memory-update <constraint>",
    "/memory-drop": "/memory-drop <text-match>",
    "/export": "/export [task-id]",
    "/mode": "/mode <name>",
    "/layout": "/layout <name|save|load>",
    "/goal": "/goal <objective>",
    "/agent": "/agent <goal>",
    "/council": "/council <goal>",
    "/auto": "/auto [on|off|N]",
    "/proof": "/proof <goal>",
    "/use-proxy": "/use-proxy <url>",
}


def command_usage(cmd: "Command") -> str:
    """P20 'what to pass to it': an explicit usage hint, else a generic '<arg>' for any
    arg-taking command, else '' for argument-less commands."""
    return _USAGE.get(cmd.name, "<arg>" if cmd.takes_arg else "")


def command_labels() -> list[str]:
    """Display rows for the palette: '/name  — what it does · what to pass'.

    The arg/usage hint goes in the description half so name_from_label still recovers
    the bare command name (it splits on the '  —' separator)."""
    rows = []
    for c in _all_commands():
        u = command_usage(c)
        rows.append(f"{c.name}  — {c.desc}" + (f"  · {u}" if u else ""))
    return rows


def command_names() -> list[str]:
    return [c.name for c in _all_commands()]


def name_from_label(label: str) -> str:
    """Extract the bare command name from a palette label (inverse of label)."""
    return (label or "").split("  —", 1)[0].strip()


def command_for(name: str) -> Command | None:
    n = (name or "").strip()
    for c in _all_commands():
        if c.name == n:
            return c
    return None
