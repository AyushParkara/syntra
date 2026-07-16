"""Plugin loader — auto-discover agents, commands, skills from directories.

Plugins are directories under ~/.config/syntra/plugins/ or .syntra/plugins/
containing markdown files that define custom agents, commands, and skills.

Structure:
  my-plugin/
  ├── plugin.json          # {"name": "my-plugin", "description": "..."}
  ├── agents/              # *.md files = agent definitions
  │   ├── my-agent.md
  │   └── helper.md
  ├── commands/            # *.md files = slash commands
  │   └── my-command.md
  └── skills/              # */SKILL.md = skill definitions
      └── my-skill/
          └── SKILL.md

Agent markdown format (YAML frontmatter):
  ---
  name: my-agent
  description: Use when...
  model: inherit
  color: blue
  tools: ["Read", "Grep"]
  ---
  System prompt for the agent...

Command markdown format:
  ---
  name: /my-command
  description: What it does
  ---
  Instructions for executing the command...

Syntra's plugin system: auto-discovered agents, commands, and skills from markdown.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PluginAgent:
    name: str
    description: str
    system_prompt: str
    model: str = "inherit"
    color: str = "blue"
    tools: list[str] = field(default_factory=list)
    plugin: str = ""


@dataclass
class PluginCommand:
    name: str
    description: str
    instructions: str
    plugin: str = ""


@dataclass
class PluginSkill:
    name: str
    description: str
    content: str
    plugin: str = ""
    references: list[str] = field(default_factory=list)
    when_to_use: str = ""   # frontmatter "when-to-use:" — drives implicit matching


@dataclass
class Plugin:
    name: str
    description: str
    path: str
    agents: list[PluginAgent] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    skills: list[PluginSkill] = field(default_factory=list)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter from a markdown file."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm = {}
    for line in parts[1].strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip('"\'') for v in val[1:-1].split(",") if v.strip()]
            elif val.lower() in ("true", "false"):
                val = val.lower() == "true"
            fm[key] = val
    return fm, parts[2].strip()


def load_plugin(plugin_dir: str | Path) -> Plugin | None:
    """Load a single plugin from a directory."""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return None

    # Read plugin.json for metadata
    meta_file = plugin_dir / "plugin.json"
    name = plugin_dir.name
    description = ""
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            name = meta.get("name", name)
            description = meta.get("description", "")
        except (json.JSONDecodeError, OSError):
            pass

    plugin = Plugin(name=name, description=description, path=str(plugin_dir))

    # Load agents
    agents_dir = plugin_dir / "agents"
    if agents_dir.is_dir():
        for md in sorted(agents_dir.glob("*.md")):
            try:
                text = md.read_text()
                fm, body = _parse_frontmatter(text)
                plugin.agents.append(PluginAgent(
                    name=fm.get("name", md.stem),
                    description=fm.get("description", ""),
                    system_prompt=body,
                    model=fm.get("model", "inherit"),
                    color=fm.get("color", "blue"),
                    tools=fm.get("tools", []) if isinstance(fm.get("tools"), list) else [],
                    plugin=name,
                ))
            except OSError:
                pass

    # Load commands
    commands_dir = plugin_dir / "commands"
    if commands_dir.is_dir():
        for md in sorted(commands_dir.glob("*.md")):
            try:
                text = md.read_text()
                fm, body = _parse_frontmatter(text)
                cmd_name = fm.get("name", f"/{md.stem}")
                if not cmd_name.startswith("/"):
                    cmd_name = f"/{cmd_name}"
                plugin.commands.append(PluginCommand(
                    name=cmd_name,
                    description=fm.get("description", ""),
                    instructions=body,
                    plugin=name,
                ))
            except OSError:
                pass

    # Load skills
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md" if skill_dir.is_dir() else None
            if skill_md and skill_md.exists():
                try:
                    text = skill_md.read_text()
                    fm, body = _parse_frontmatter(text)
                    refs = []
                    refs_dir = skill_dir / "references"
                    if refs_dir.is_dir():
                        refs = [str(r) for r in refs_dir.glob("*.md")]
                    plugin.skills.append(PluginSkill(
                        name=fm.get("name", skill_dir.name),
                        description=fm.get("description", ""),
                        content=body,
                        plugin=name,
                        references=refs,
                        when_to_use=fm.get("when-to-use", fm.get("when_to_use", "")),
                    ))
                except OSError:
                    pass

    return plugin


def discover_plugins() -> list[Plugin]:
    """Auto-discover all plugins from standard locations.

    #201(c)/#200: a plugin's agent/command/skill bodies are injected as prompt text into the
    pipeline. Global (~/.config/syntra) plugins are user-installed → trusted. REPO-LOCAL plugins
    (a cloned repo's ./.syntra/plugins) are only loaded when the folder is trusted for exec
    (folder_trust enforcement, off by default so tests/library are unaffected); and any plugin
    whose body carries an injection marker is dropped even from a trusted folder (defense in depth).
    """
    from . import folder_trust as _ft
    plugins = []
    search_dirs = [
        Path.home() / ".config" / "syntra" / "plugins",
        Path(os.environ.get("SYNTRA_STATE_DIR", ".syntra")) / "plugins",
        Path.cwd() / ".syntra" / "plugins",
    ]
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for entry in sorted(search_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                # Repo-local, untrusted folder → skip the whole plugin (its prompt text is untrusted).
                if not _ft.repo_local_exec_allowed(str(entry)):
                    continue
                plugin = load_plugin(entry)
                if plugin and not _plugin_has_injected_body(plugin, _ft):
                    plugins.append(plugin)
    return plugins


def _plugin_has_injected_body(plugin, ft) -> bool:
    """True if any of the plugin's agent/command/skill bodies looks like a prompt injection —
    such a plugin is dropped so a repo-committed persona can't hijack a pipeline role."""
    bodies = ([a.system_prompt for a in plugin.agents]
              + [c.instructions for c in plugin.commands]
              + [s.content for s in plugin.skills])
    return any(ft.plugin_body_is_injected(b) for b in bodies if b)


def bundled_skills() -> list[PluginSkill]:
    """Load Syntra's built-in skills shipped in syntra/skills/."""
    skills = []
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    if not skills_root.is_dir():
        return skills
    for skill_dir in sorted(skills_root.iterdir()):
        skill_md = skill_dir / "SKILL.md" if skill_dir.is_dir() else None
        if skill_md and skill_md.exists():
            try:
                text = skill_md.read_text()
                fm, body = _parse_frontmatter(text)
                skills.append(PluginSkill(
                    name=fm.get("name", skill_dir.name),
                    description=fm.get("description", ""),
                    content=body,
                    plugin="builtin",
                    when_to_use=fm.get("when-to-use", fm.get("when_to_use", "")),
                ))
            except OSError:
                pass
    return skills


def get_skill(name: str) -> PluginSkill | None:
    """Find a skill by name from bundled + plugin skills."""
    for s in bundled_skills():
        if s.name == name:
            return s
    for p in discover_plugins():
        for s in p.skills:
            if s.name == name:
                return s
    return None


_STOPWORDS = frozenset((
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "are", "use",
    "using", "used", "when", "what", "how", "all", "any", "can", "will", "should",
    "into", "out", "via", "per", "its", "it's", "a", "an", "to", "of", "in", "on",
    "is", "be", "do", "or", "if", "as", "at", "by",
))


def _significant_tokens(s: str) -> set[str]:
    """Lowercased word tokens worth matching on (>2 chars, not a stopword)."""
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def match_skills(query: str, skills: "list[PluginSkill] | None" = None,
                 *, limit: int = 3) -> "list[PluginSkill]":
    """Rank skills by how well their name/description/when-to-use match a free-text
    goal — the "implicit invoke by description match" primitive. Pure keyword-overlap
    scoring (no API call): the executor calls this to surface the right skill for a
    task. Returns the top ``limit`` skills with a non-zero overlap, best first; ties
    break by name for determinism. An empty/blank query yields no matches."""
    if skills is None:
        skills = bundled_skills() + [s for p in discover_plugins() for s in p.skills]
    q = _significant_tokens(query)
    if not q:
        return []
    scored: list[tuple[int, PluginSkill]] = []
    for s in skills:
        hay = _significant_tokens(f"{s.name} {s.description} {getattr(s, 'when_to_use', '')}")
        overlap = len(q & hay)
        if overlap > 0:
            scored.append((overlap, s))
    scored.sort(key=lambda t: (-t[0], t[1].name))
    return [s for _, s in scored[:limit]]


def plugin_summary(plugins: list[Plugin]) -> str:
    """Generate a human-readable summary of loaded plugins."""
    if not plugins:
        return "no plugins installed"
    lines = []
    for p in plugins:
        parts = []
        if p.agents:
            parts.append(f"{len(p.agents)} agents")
        if p.commands:
            parts.append(f"{len(p.commands)} commands")
        if p.skills:
            parts.append(f"{len(p.skills)} skills")
        detail = ", ".join(parts) or "empty"
        lines.append(f"  {p.name:20s}  {detail}")
    return "plugins:\n" + "\n".join(lines)
