"""Analyzer-selected domain packs — prompts, reviewer lenses, and routing bias."""
from __future__ import annotations

from dataclasses import dataclass, field

from .prompts import load as _load_prompt

_PLANNER = _load_prompt("planner")
_EXECUTOR = _load_prompt("executor")
_REVIEWER_AGENT = _load_prompt("reviewer_agent")
_RESEARCHER = _load_prompt("researcher")
_REVIEWER_RESEARCH = _load_prompt("reviewer_research")

_CODING_EXECUTOR_ADDON = (
    "\n\nYou can use tools (read, list, glob, grep, write, edit, bash, todo) to do the "
    "work directly in the workspace. Keep working until the goal is fully done. As you "
    "discover new work, add it with the `todo` tool. When everything is complete, stop "
    "calling tools and give a short summary of what you changed."
)

_RESEARCH_EXECUTOR_ADDON = (
    "\n\nTools: `websearch` (find sources), `webfetch` (read a source), `task` "
    "(delegate ONE research angle to a fresh scout sub-agent), `todo` (track angles). "
    "Decompose into orthogonal angles + one DISCONFIRMING angle, delegate them, then "
    "SYNTHESIZE one report where every non-obvious claim ends with [n] keyed to a "
    "numbered ## Sources list of URLs you actually fetched. Then stop."
)


@dataclass(frozen=True)
class Pack:
    name: str
    planner_prompt: str
    executor_prompt: str
    reviewer_prompt: str
    executor_addon: str = ""
    reviewer_lenses: tuple[str, ...] = ("dev", "qa", "pm")
    routing_bias: dict = field(default_factory=dict)
    research_tools: bool = False


PACKS: dict[str, Pack] = {
    "coding": Pack(
        name="coding",
        planner_prompt=_PLANNER,
        executor_prompt=_EXECUTOR,
        reviewer_prompt=_REVIEWER_AGENT,
        executor_addon=_CODING_EXECUTOR_ADDON,
    ),
    "research": Pack(
        name="research",
        planner_prompt=_PLANNER,
        executor_prompt=_RESEARCHER,
        reviewer_prompt=_REVIEWER_RESEARCH,
        executor_addon=_RESEARCH_EXECUTOR_ADDON,
        routing_bias={"reasoning": 0.1, "tool_use": 0.1},
        research_tools=True,
    ),
}

_RESEARCH_CATEGORIES = frozenset({"factual", "analysis", "reasoning"})


def pack_for(analysis, *, force_research: bool = False) -> Pack:
    """Map task analysis to a domain pack. ``force_research`` mirrors ``config.research``."""
    if force_research:
        return PACKS["research"]
    cat = getattr(analysis, "category", "general") if analysis is not None else "general"
    if cat in _RESEARCH_CATEGORIES:
        return PACKS["research"]
    return PACKS["coding"]
