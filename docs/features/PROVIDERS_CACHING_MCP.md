# Providers, Prompt Caching, and MCP

A plain-English guide to three things in Syntra:

1. **Providers** — the AI services Syntra is allowed to use, and the keys it logs in with.
2. **Prompt caching** — a money-saver that makes the model reuse the same big instructions cheaply.
3. **MCP** — a way to plug in extra tools from outside servers.

There's a short **catalog** section too (the list of models Syntra knows about), and a **Quick how-to** cheat sheet at the very end.

No prior coding knowledge needed. Terms are defined as we go.

---

## A few words first

**What is a "provider"?**
A provider is a company (or a program on your own computer) that runs AI models for you. Examples: OpenRouter, DeepSeek, xAI (Grok), or a local Ollama server. You talk to a provider over the internet, and it charges you for what you use.

**What is an "API key"?**
An API key is a long secret password (it usually looks like `sk-or-abc123…`). It proves the request is *yours* so the provider knows who to bill. Treat it like a password: don't share it, don't post it online.

**What is a "model"?**
A model is one specific AI brain, like `claude-opus-4.5` or `deepseek-chat`. One provider can serve many models. Syntra ranks the models it can reach for each job (planning, doing the work, reviewing it), and you can inspect or override the choice.

**What is a "role"?**
Syntra splits a task into three roles — the **planner** (decides the steps), the **executor** (does the work), and the **reviewer** (checks the result). Each role can use a different model.

---

## 1. Providers

### What it does

Syntra needs to know **which AI services you have, where they live on the internet, and the secret key to use them.** You list these once in a small settings file. After that, Syntra ranks eligible models for each job and authenticates with the configured provider.

You can give Syntra **several services at once.** It ranks eligible routes and can fall back to another when one fails or becomes unavailable.

### Where the settings live

Syntra looks for your provider settings in this order and uses the first one it finds:

1. A custom path you set in the `SYNTRA_PROVIDERS_FILE` environment variable.
2. `~/.config/syntra/providers.json`  ← **this is the normal home for it.**
3. `./.syntra/providers.json` (a copy living inside your current project folder).
4. A built-in example file with no real keys (so brand-new installs don't crash).

> `~` means your home folder. So `~/.config/syntra/providers.json` is usually `/home/yourname/.config/syntra/providers.json`.

The file is saved **chmod 600**, which means *only you* can read it — your secret keys are not visible to other accounts on the machine.

### What one provider entry looks like

The file is JSON (a simple text format of names and values). It holds a list called `providers`, and each item describes one service:

```json
{
  "providers": [
    {
      "name": "openrouter",
      "display_name": "OpenRouter",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY"
    }
  ]
}
```

What each field means:

- **`name`** — a short nickname you choose (e.g. `openrouter`). Used in commands and messages.
- **`display_name`** — a friendlier label shown in lists (optional; defaults to `name`).
- **`base_url`** — the web address of the service. For most providers this ends in `/v1`.
- **The key** — three ways to give it (pick one, or mix; see below):
  - **`api_key`** — paste the secret key directly into the file.
  - **`api_key_env`** — *don't* store the key in the file; instead give the **name** of an environment variable that holds it (e.g. `OPENROUTER_API_KEY`). Safer, because the key never touches the file.
  - **`api_keys`** / **`api_key_envs`** — lists, for **multiple keys** (see "Multiple keys" below).
- **`allowed_models`** *(optional)* — a list restricting which models this provider may serve. **Leave it out** to let the provider serve *any* model (a "wildcard" gateway like OpenRouter). Include it for single-vendor services (e.g. DeepSeek only serves `deepseek-chat` and `deepseek-reasoner`).
- **`extra_headers`** *(optional)* — extra labels some providers want (e.g. OpenRouter likes an `HTTP-Referer` and `X-Title`).
- **`kind`** *(optional)* — which "language" the provider speaks (see "The 4 adapter kinds" below). Defaults to `openai`, which covers most services.

> **Order matters.** The first provider in the list that can serve a model is preferred. Put your favorite first.

#### A local, no-key example

If you run models on your own computer (e.g. Ollama), there's no key to pay with. Use `"api_key": "no-auth"`:

```json
{
  "name": "ollama-local",
  "display_name": "Local Ollama",
  "base_url": "http://localhost:11434/v1",
  "api_key": "no-auth"
}
```

### Multiple keys per provider (automatic failover)

**Failover** means: if one key stops working, Syntra quietly tries the next one instead of failing.

This is handy when a single key has a spending cap. Give a provider **several keys** and Syntra uses them in order — when the first runs out of credit (or gets rejected), it switches to the next, and tells you which one died.

Two ways to list several keys:

```json
{
  "name": "openrouter",
  "display_name": "OpenRouter",
  "base_url": "https://openrouter.ai/api/v1",
  "api_keys": ["YOUR_PRIMARY_KEY", "YOUR_BACKUP_KEY"]
}
```

…or keep them out of the file and point at environment variables:

```json
{
  "name": "openrouter",
  "base_url": "https://openrouter.ai/api/v1",
  "api_key_envs": ["OPENROUTER_KEY_A", "OPENROUTER_KEY_B"]
}
```

Notes:
- Keys are tried **in the order you list them.** First = primary, the rest = backups.
- You can even mix: a single `api_key` *plus* an `api_key_env` are both kept and both available for failover.
- When a key is exhausted or rejected, Syntra prints a one-line hint telling you exactly how to remove it (see `providers remove-key` below).

### The 4 adapter kinds

An **adapter** is the translator Syntra uses to talk to a provider, because different companies expect requests in slightly different shapes. You usually don't set this — `openai` is the default and works for the large majority of services. Set `kind` only when a provider needs its own native shape.

| `kind` value | Use it for | Plain meaning |
|---|---|---|
| `openai` *(default)* | OpenRouter, DeepSeek, xAI, Moonshot, NVIDIA, local servers, most gateways | The common "OpenAI-style chat" format almost everyone supports. |
| `anthropic` | Claude models hit through Anthropic's own API | Claude's native message format. |
| `gemini` | Google Gemini through Google's own API | Gemini's native format. |
| `responses` | OpenAI's newer "Responses" API endpoint | OpenAI's newer request style. |

Example (Claude through Anthropic's native API):

```json
{
  "name": "anthropic",
  "display_name": "Anthropic (native)",
  "base_url": "https://api.anthropic.com",
  "api_key_env": "ANTHROPIC_API_KEY",
  "kind": "anthropic",
  "allowed_models": ["claude-opus-4-5", "claude-sonnet-4-5"]
}
```

### Commands for managing providers

**`syntra init`** — a friendly setup wizard.
It asks you, one provider at a time: the name, the `base_url`, whether to read the key from an environment variable or type it in (typing is hidden, never shown on screen), and which models to allow. It then writes a fresh `providers.json` with safe `chmod 600` permissions. Your keys are **never printed back.**

```bash
syntra init
# follow the prompts; press Enter on a blank provider name to finish
```

Add `--force` to overwrite an existing config, or `--path` to write somewhere other than the default.

**`syntra providers`** — list what's configured.
Shows every provider, its address, whether a key is present, and how many models it's allowed to serve (`*` means "any").

```bash
syntra providers
```

```
Provider config: /home/you/.config/syntra/providers.json
NAME                   DISPLAY                      BASE_URL                                      KEY    ALLOWED
openrouter             OpenRouter                   https://openrouter.ai/api/v1                  yes    *
deepseek               DeepSeek (native)            https://api.deepseek.com                      yes    2
ollama-local           Local Ollama                 http://localhost:11434/v1                     noauth *
```

Tip: `syntra providers --free` lists ready-made templates for free / low-cost providers you can copy in.

**`syntra login <provider>`** — log in with your browser instead of a key.
Some providers let you sign in through a web page rather than pasting a key. Syntra shows you a web address and a short code; you open the page, type the code, approve, and Syntra stores the resulting token securely (again `chmod 600`, kept in a `secrets.json` next to your config). This only works for a provider that has an `oauth` block in its config entry.

```bash
syntra login openrouter
# Syntra prints: open <url> and enter code: WXYZ-1234
# approve in your browser; Syntra stores the token
```

Use `syntra login <provider> --refresh` to renew an existing login, and `syntra logout <provider>` to delete the stored token.

**`syntra providers remove-key <provider> <last-6-chars>`** — remove a dead key.
When a key runs out of credit or is rejected, Syntra tells you the **last 6 characters** of that key. Use them to drop just that key (your other keys stay). It's a **dry run by default** (shows what *would* change) — add `--yes` to actually do it. It backs up your config first and never prints the full key.

```bash
# See what would be removed (safe, changes nothing):
syntra providers remove-key openrouter abc123

# Actually remove it (makes a backup first):
syntra providers remove-key openrouter abc123 --yes
```

---

## 2. Prompt caching

### What it does (plain terms)

Every time Syntra asks a model to plan, work, or review, it sends a **big block of standing instructions** along with your actual request. That instruction block is the **same every time** for a given role.

**Prompt caching** lets the model **remember that repeated block** instead of re-reading it from scratch on each call. The provider keeps a warm copy and charges you a fraction of the price for the repeated part.

In Syntra's pricing math, a **cache read costs about one-tenth (0.1x)** of the normal input price. So the repeated instructions become **roughly 10x cheaper.** (Writing something into the cache the first time can cost a little extra on some providers — e.g. Anthropic charges about 1.25x — but you make that back quickly across a multi-step task.)

> **Why does this matter?** Syntra runs many model calls per task (plan, then several work steps, then review). The standing instructions get re-sent every time. Caching turns that repeated cost from "full price, every call" into "a sliver, every call."

### It's automatic and safe

You don't switch it on. Syntra turns caching on **only where it's known to be safe**, and leaves every other request **exactly as it was before.**

Specifically, caching is applied only when **both** are true:

- The model is a **Claude / Anthropic** model (the model id contains `claude` or `anthropic`), **and**
- It's reached through a provider that's known to support this caching style — that means the **native `anthropic`** adapter, **or** a gateway whose name contains `openrouter`, `anthropic`, `dashscope`, or `alibaba`.

For **every other** provider and model, Syntra sends the request **unchanged** — byte-for-byte identical to having no caching feature at all. (Providers like OpenAI and DeepSeek do their *own* automatic caching behind the scenes; Syntra simply doesn't get in the way.)

### How to turn it off

If you ever want to disable Syntra's caching behavior entirely, set this environment variable before running:

```bash
SYNTRA_NO_PROMPT_CACHE=1 syntra run "your goal here"
```

With that set, Syntra adds no caching markers anywhere, for any provider.

### How to tell it's working

When caching kicks in, the usage line for a step shows a small badge: **`cache✓<N>`**, where `<N>` is how many input tokens were served cheaply from the warm cache.

```
[usage] executor  anthropic/claude-opus-4.5  in=8421 out=512  cache✓7980  $0.0123
```

That `cache✓7980` means ~7,980 tokens were reused at the cheap rate instead of being charged at full price. (You only see this line when running with details on — `--verbose`, or `/verbose` inside the app.)

---

## 3. MCP (Model Context Protocol)

### What it does (plain terms)

**MCP** is a standard way for outside programs — called **MCP servers** — to hand a set of **tools** to your AI. A "tool" is an action the model can take on its own, like "search GitHub issues," "query a database," or "look something up on a company wiki."

Out of the box, Syntra's executor already has built-in tools (read files, search, edit, run commands). **MCP lets you bolt on more tools from other people's servers** — without changing any of Syntra's code. When you attach an MCP server, Syntra connects to it, asks "what can you do?", and adds each of its tools to the executor's toolbox automatically.

> The model can then *choose* to use those tools while working — and because they can have real-world effects, every MCP tool is treated as "needs permission" (Syntra asks before running risky actions in agent mode).

You attach MCP servers **per run** with the `--mcp` flag (you can repeat it to attach several). There are two kinds:

### Kind 1: stdio (a program on your computer)

**stdio** ("standard in/out") means Syntra **starts a small program on your own machine** and talks to it directly. This is the common case — many MCP servers are little Node.js packages you run with `npx`.

```bash
syntra run "list my open GitHub issues" --agent \
  --mcp 'npx -y @modelcontextprotocol/server-github'
```

Here Syntra launches the GitHub MCP server locally, discovers its tools, and lets the model use them. (`--agent` turns on the tool-using executor.)

### Kind 2: HTTP (a hosted server on the internet)

**HTTP** means the MCP server is **already running somewhere online**, and Syntra reaches it over the web — no local program to start. Give the server's web address. If it needs a login token (a **bearer token** — another kind of secret password), add it after the address:

```bash
# With the token written inline:
syntra run "summarize today's incidents" --agent \
  --mcp 'https://mcp.example.com/mcp my-secret-token'
```

…or keep the token out of your command history by putting it in an environment variable instead:

```bash
export SYNTRA_MCP_TOKEN="my-secret-token"
syntra run "summarize today's incidents" --agent \
  --mcp 'https://mcp.example.com/mcp'
```

Syntra uses `SYNTRA_MCP_TOKEN` automatically when you don't write a token after the URL.

### Good to know

- **Repeatable:** use `--mcp` more than once to attach multiple servers in the same run.
- **Safe to fail:** if a server won't start or can't be reached, Syntra prints a one-line warning and **keeps going** — a broken MCP server never crashes your task.
- **Timeout-bounded:** stdio MCP servers must answer the handshake/request in time. A silent server is terminated instead of hanging the run forever.
- **Scrubbed + untrusted:** stdio servers get a scrubbed environment by default; MCP tool descriptions/results are sanitized/fenced before they reach the model.
- **Repo-local caution:** saved MCP configs from a project-local state directory can auto-spawn subprocesses, so CLI enforcement may skip them until the folder/config is trusted.
- **You'll see a confirmation** like `[mcp] connected: github (12 tools)` listing how many tools it picked up.

---

## Catalog (the list of models Syntra knows)

### What it is

The **catalog** is Syntra's built-in list of AI models and their routing signals —
intelligence-style scores, speed, price, role hints, and specialties. Syntra uses
these values to rank models for each role. It comes pre-filled with an **approximate
seed snapshot**; use it as a starting point, then inspect routes, refresh supported
data, or override/pin models for your workload.

### See the catalog

```bash
syntra catalog
```

This prints every known model with its intelligence score, speed, input/output price, suggested roles, and specialties.

### Refresh the numbers (optional)

The bundled catalog is not a live feed. Where the refresh integration has data
available, you can use **Artificial Analysis** to update supported numbers. Its
published Data API includes a free, rate-limited tier for primary metrics and still
requires an API key in an environment variable called `ARTIFICIALANALYSIS_API_KEY`.

```bash
export ARTIFICIALANALYSIS_API_KEY="YOUR_AA_KEY"
syntra catalog refresh
```

To obtain a key, create or copy one from the Artificial Analysis account/API area,
then export it in your shell. Check its current API documentation for the published
free-tier request limit and any paid-tier options. Add `--dry-run` to preview without
saving. If you skip this, the seeded catalog still works as a starting point; it
simply is not refreshed from the upstream source.

---

## Quick how-to cheat sheet

**Set up your providers**
```bash
syntra init                 # friendly wizard, writes ~/.config/syntra/providers.json (chmod 600)
syntra providers            # list what's configured
syntra providers --free     # see free/cheap provider templates to copy in
```

**Settings file lives at:** `~/.config/syntra/providers.json`
(or set `SYNTRA_PROVIDERS_FILE` to point elsewhere)

**One key, simplest entry**
```json
{ "name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY" }
```

**Several keys for one provider (auto failover)**
```json
{ "name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
  "api_keys": ["sk-or-PRIMARY", "sk-or-BACKUP"] }
```

**Adapter kinds:** `openai` (default, most providers) · `anthropic` (Claude native) · `gemini` (Google native) · `responses` (OpenAI's newer API)

**Browser login (no key) / manage keys**
```bash
syntra login <provider>                         # sign in via browser
syntra logout <provider>                        # delete the stored token
syntra providers remove-key <provider> <last6>  # dry-run remove a dead key
syntra providers remove-key <provider> <last6> --yes   # actually remove it
```

**Prompt caching** — automatic; ~10x cheaper on the repeated instructions; only for Claude models via anthropic/openrouter/dashscope.
```bash
SYNTRA_NO_PROMPT_CACHE=1 syntra run "..."   # turn caching off
# look for "cache✓<N>" in the usage line to confirm it's working
```

**MCP — add outside tools (use --agent so the model can call them)**
```bash
# local program (stdio):
syntra run "..." --agent --mcp 'npx -y @modelcontextprotocol/server-github'

# hosted server (HTTP) with a token:
syntra run "..." --agent --mcp 'https://host/mcp my-token'
# or:
export SYNTRA_MCP_TOKEN="my-token"
syntra run "..." --agent --mcp 'https://host/mcp'
```

**Catalog**
```bash
syntra catalog                                   # show all known models + stats
export ARTIFICIALANALYSIS_API_KEY="aa-..."       # needed only for refresh
syntra catalog refresh                           # pull fresh numbers
```
