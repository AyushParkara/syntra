# Syntra Skills & Plugins

Syntra supports user-defined skills, agents, and plugins. Build your own, use
the community's, or contribute back.

---

## Quick Start

Create a skill from the TUI:
```
/skill-create my-skill
```
This creates `~/.config/syntra/plugins/user-skills/skills/my-skill/SKILL.md`.
Edit it, then `/skills` to verify it loaded.

Create an agent:
```
/agent-create my-agent
```
Creates a blank scaffold at `~/.config/syntra/plugins/user-skills/agents/my-agent.md`.

You can also have a model write the agent for you from a one-line description:
```
/agent-create sqlbot | an agent that audits SQL for injection
```
Syntra asks a model to fill in the agent's description, tools, and system prompt, then writes
the file. If it can't (offline, or no clear answer), it falls back to the blank scaffold.

---

## Skill Format

A skill is a markdown file with YAML frontmatter:

```markdown
---
name: my-skill
description: When this skill should trigger (used for routing)
model: inherit
---

You are performing the 'my-skill' task. Describe the method, rules,
and output format here. This becomes the system prompt.
```

**Fields:**
- `name` — unique identifier
- `description` — when it triggers (the router uses this)
- `model` — `inherit` (use the routed model) or a specific model ID

---

## Agent Format

An agent is a specialized sub-worker with its own tools and focus:

```markdown
---
name: my-agent
description: Use this agent when... (include 2-3 example scenarios)
model: inherit
color: blue
tools: ["Read", "Grep", "Edit"]
---

You are the 'my-agent' agent specializing in X.

**Your responsibilities:**
1. ...
2. ...

**Output format:**
...
```

**Fields:**
- `tools` — which tools the agent can use (principle of least privilege)
- `color` — blue (analysis), green (creation), red (security), yellow (exploration)

---

## Plugin Structure

A plugin bundles agents, commands, and skills:

```
my-plugin/
├── plugin.json          # {"name": "my-plugin", "description": "..."}
├── agents/
│   └── my-agent.md
├── commands/
│   └── my-command.md
└── skills/
    └── my-skill/
        └── SKILL.md
```

Drop it in `~/.config/syntra/plugins/` — auto-discovered on launch. Project-local
plugins can also be discovered, but in CLI-enforced contexts Syntra may skip
repo-local plugin prompt text until the folder is trusted and the plugin body passes
the prompt-injection scan.

---

## Built-in Skills

Syntra ships with these default skills (in `syntra/skills/`). The router matches a
request against each skill's `description`, so the "triggers on" column below is taken
straight from the manifests.

**Conversation & planning**

| Skill | Triggers on |
|-------|------------|
| `chat` | greetings, questions, casual conversation |
| `brainstorm` | brainstorm, explore an idea, help me think through, not sure how to |
| `write-plan` | plan this, write a plan, break this down, how should I approach |
| `execute-plan` | execute the plan, run the plan, do the steps, implement the plan |

**Building & changing code**

| Skill | Triggers on |
|-------|------------|
| `coding` | build, create, add, implement features |
| `refactor` | refactor, simplify, clean up, improve |
| `code-simplify` | simplify, clean up, make this clearer, reduce complexity, too complex |
| `frontend-design` | design, UI, frontend, component, styling, layout, make it look good |

**Debugging**

| Skill | Triggers on |
|-------|------------|
| `debug` | fix, bug, error, crash, broken, failing |
| `systematic-debug` | hard bug, can't figure out, intermittent, keeps failing, root cause |

**Review**

| Skill | Triggers on |
|-------|------------|
| `review` | review, check, audit, verify |
| `request-review` | get this reviewed, ready for review, hand off for review |
| `receive-review` | address the review, fix the feedback, apply review comments |

**Research**

| Skill | Triggers on |
|-------|------------|
| `research` | research, investigate, find out, compare |

**Multi-agent orchestration**

| Skill | Triggers on |
|-------|------------|
| `dispatch-agents` | do these in parallel, split this up, run agents, fan out |
| `subagent-driven` | fresh agent per step, isolate each task, clean-context execution |
| `tdd` | TDD, test first, write tests, test-driven |

(17 built-in skills; the agentic executor auto-picks the best match by `description`, or
force one from the TUI with `/skills`.)

---

## Discovery Locations

Plugins/skills are loaded from (in order):
1. `syntra/skills/` — built-in (ships with Syntra)
2. `~/.config/syntra/plugins/` — user global
3. `${SYNTRA_STATE_DIR:-.syntra}/plugins/` — state-dir plugins (often project-local)
4. `<cwd>/.syntra/plugins/` — current directory

Repo-local entries are treated as untrusted surfaces: they can be skipped by the
folder-trust gate, and any agent/command/skill body that looks prompt-injected is
dropped rather than injected into a role prompt.

---

## Contributing to the Community

Want to share your skill or plugin? Until an official Syntra community plugins
repository is created and linked here, open an issue in the main Syntra repository
to propose it or publish it from your own repository with clear installation notes.

1. Build and test your skill/plugin locally.
2. Verify it loads: `/skills` or `/plugins`.
3. Include:
   - Your plugin directory
   - A clear `plugin.json` with name + description
   - A README explaining what it does and when to use it
4. Others can install it by cloning or copying it into their
   `~/.config/syntra/plugins/` directory.

**Guidelines:**
- Keep skills focused — one clear job per skill.
- Write good `description` fields — that's how routing finds your skill.
- Don't include secrets or API keys.
- Test with multiple models (skills should be model-agnostic).
