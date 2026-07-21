"""Tool registry for the agent executor loop (Step 2).

Each tool = {name, description, JSON-schema parameters, danger, run(args, ctx)}.
`tools_schema()` emits the OpenAI `tools` array; `dispatch()` runs one model
tool-call and returns a STRING result the loop feeds back as a tool message.

Safety:
- every path is confined to ctx.workspace_root (read/write/list/glob/grep);
- tools are tagged safe | write | exec; non-safe calls go through an injected
  `permit(name, danger, args) -> bool` gate (Step 3 supplies the real per-session
  store; default denies write/exec);
- bash reuses core/sandbox (classify + confined run); BLOCKED commands never run.

dispatch never raises into the loop — tool failures come back as readable error
strings so the model can adapt. Pure logic + filesystem -> unit-tested on a temp tree.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    workspace_root: str
    # permit(name, danger, args) -> bool ; default: allow safe, deny write/exec.
    permit: callable = None
    # #83: the live PermissionStore (when wired), so a denial can surface the user's typed
    # guidance (store.last_denial_reason) back to the agent in the tool result. None = no guidance.
    _perms: object = None
    # dynamic TODO the executor grows as it discovers work (Step 4).
    todos: list = None
    # spawn(description) -> str : delegate a scoped sub-task to a fresh agent (Step 7).
    spawn: callable = None
    # spawn_many(descriptions: list[str]) -> list[str] : delegate N sub-tasks to fresh
    # agents that run IN PARALLEL, results in input order. Set alongside `spawn`; the
    # `tasks` tool uses it so the model can fan out N real workers in one call.
    spawn_many: callable = None
    depth: int = 0                    # sub-agent recursion depth (0 = top executor)
    plan_notes: list = None           # structured plan the agent maintains (plan tool)
    ask_user: callable = None         # ask_user(question)->answer for the question tool
    web_search: callable = None       # web_search(query)->results for the websearch tool
    image_gen: callable = None        # image_gen(prompt, size)->bytes for the generate_image tool (provider-backed; injected by the loop)
    pending_images: list = None       # data: URLs the agent loaded via view_image, attached next turn
    procs: object = None              # ProcessManager for exec_command/write_stdin (lazy)
    hooks: object = None              # optional HookRegistry (pre/post tool-use lifecycle)
    rule_hooks: object = None         # optional HookEngine (pattern-based warn/block rules)
    # B1: when set, file-mutating tools (write/edit/apply_patch) route through this
    # EditApplier so every change is CHECKPOINTED (undoable via /undo) instead of a raw
    # write. on_edit(path, kind) fires an edit event for the live display. Both optional —
    # unset => direct write (unchanged behavior).
    edit_applier: object = None
    on_edit: callable = None
    # IMGFIX: on_image(path) signals the TUI to RENDER an image inline in the user's terminal
    # (distinct from view_image, which shows it to the MODEL). Fired by show_image + generate_image
    # + view_image so a produced/looked-at image auto-displays. Optional — unset => no inline render.
    on_image: callable = None
    # B2: approval × sandbox policy for shell commands. Empty approval_policy = gate INACTIVE
    # (unchanged behavior — every shell command still goes through `permit`). When set, the
    # execpolicy matrix decides AUTO/ASK/BLOCK and only ASK falls through to `permit`.
    approval_policy: str = ""         # "" | untrusted | on_request | on_failure | never
    sandbox_mode: str = ""            # "" | read_only | workspace_write | danger_full_access
    allow_prefixes: tuple = ()        # user-approved "always allow this command prefix" list
    # Gap 2: when a command ran WITHOUT an OS sandbox we inform the user instead of running
    # silently. on_command({command, sandboxed, exit}) surfaces it to the TUI; verbose_commands
    # announces EVERY command (not just unsandboxed ones). Both optional — unset => quiet.
    on_command: callable = None
    verbose_commands: bool = False
    # Live task state for provenance trailers on git commit (optional).
    state: object = None
    # How the agent's git commit messages are formatted (the app-user's choice): "" / "off" → no
    # trailers; else off|minimal|neutral|branded. Set from the explicit config value. When "" and
    # `resolve_commit_style` is set, `_git` calls it LAZILY at the first real commit (the ask-once
    # gate) so most runs — which never commit — are never asked.
    commit_style: str = ""
    resolve_commit_style: callable = None
    # #189: allow webfetch to reach private/loopback hosts (localhost dev servers). Default
    # False — the SSRF guard refuses metadata/internal targets so a prompt-injected agent
    # can't exfiltrate. A user who genuinely fetches local dev URLs opts in via the caller.
    allow_private_fetch: bool = False

    def __post_init__(self):
        if self.todos is None:
            self.todos = []
        if self.plan_notes is None:
            self.plan_notes = []
        if self.pending_images is None:
            self.pending_images = []

    def process_manager(self):
        """Lazily create the interactive-process manager (shared per context)."""
        if self.procs is None:
            from .proc_session import ProcessManager
            self.procs = ProcessManager()
        return self.procs

    def reap_processes(self) -> int:
        """#255: terminate any exec_command sessions this context spawned. Call in the run's
        finally/stop path so a backgrounded process can't outlive the run. No-op (returns 0) if
        no process manager was ever created. Safe to call multiple times."""
        if self.procs is None:
            return 0
        try:
            return self.procs.close_all()
        except Exception:  # noqa: BLE001 - cleanup must never raise out of a finally
            return 0

    def allowed(self, name: str, danger: str, args: dict) -> bool:
        if danger == "safe":
            return True
        if self.permit is None:
            return False
        return bool(self.permit(name, danger, args))


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema (object)
    danger: str               # "safe" | "write" | "exec"
    run: callable             # run(args: dict, ctx: ToolContext) -> str
    example: str = ""         # optional one-line usage sample shown to the model
    # #191: True when the result carries UNTRUSTED external content (fetched web page, a file
    # the agent read, an MCP server response). dispatch() fences+marks such output so a
    # payload like "SYSTEM: run X" reads as data, not an instruction. Default False.
    untrusted: bool = False


def _safe_path(root: str, p: str) -> Path:
    base = Path(root).resolve()
    cand = Path(p)
    full = (base / cand).resolve() if not cand.is_absolute() else cand.resolve()
    if full != base and base not in full.parents:
        raise ToolError(f"path {p!r} is outside the workspace")
    return full


_MAX_READ_BYTES = 50 * 1024
_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv"}


# ---- tool implementations (pure-ish; fs confined) --------------------------

def _read(args: dict, ctx: ToolContext) -> str:
    full = _safe_path(ctx.workspace_root, args["path"])
    if not full.exists():
        return f"error: no such file: {args['path']}"
    if full.is_dir():
        return f"error: {args['path']} is a directory (use list)"
    data = full.read_bytes()[:_MAX_READ_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"error: {args['path']} is not a text file"
    lines = text.split("\n")
    offset = int(args.get("offset", 0) or 0)
    limit = int(args.get("limit", 2000) or 2000)
    chunk = lines[offset:offset + limit]
    return "\n".join(f"{offset + i + 1}: {ln}" for i, ln in enumerate(chunk))


def _list(args: dict, ctx: ToolContext) -> str:
    full = _safe_path(ctx.workspace_root, args.get("path", "."))
    if not full.is_dir():
        return f"error: not a directory: {args.get('path', '.')}"
    entries = []
    for e in sorted(full.iterdir()):
        if e.name in _IGNORE or e.name.startswith("."):
            continue
        entries.append(e.name + ("/" if e.is_dir() else ""))
    return "\n".join(entries) if entries else "(empty)"


def _glob(args: dict, ctx: ToolContext) -> str:
    base = Path(ctx.workspace_root).resolve()
    pattern = args["pattern"]
    out: list[str] = []
    for p in base.glob(pattern):                       # pathlib handles ** recursion
        if not p.is_file():
            continue
        rel = p.relative_to(base)
        if any(part in _IGNORE or part.startswith(".") for part in rel.parts):
            continue
        out.append(rel.as_posix())
        if len(out) >= 500:
            break
    return "\n".join(sorted(out)) if out else "(no matches)"


def _fuzzy_filter_multi(query, candidates, limit=20):
    """Multi-word substring filter. Each whitespace-separated word must match.
    Single-word queries use difflib; multi-word sorts by SequenceMatcher ratio."""
    import difflib
    words = query.lower().split()
    if len(words) == 1:
        return difflib.get_close_matches(query, candidates, n=limit, cutoff=0.3)
    scored = []
    for c in candidates:
        low = c.lower()
        if all(w in low for w in words):
            ratio = difflib.SequenceMatcher(None, query.lower(), low).ratio()
            scored.append((ratio, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


def _find_file(args: dict, ctx: ToolContext) -> str:
    """Fuzzy-rank workspace files by relevance to a query (best matches first).
    """
    query = (args.get("query") or "").strip()
    if not query:
        return "error: 'query' required"
    from .files import list_workspace_files
    try:
        files = list_workspace_files(ctx.workspace_root)
    except Exception:  # noqa: BLE001
        files = []
    limit = int(args.get("limit", 20) or 20)
    matches = _fuzzy_filter_multi(query, files, limit=limit)
    return "\n".join(matches) if matches else "(no matches)"


def _grep_py(rx, base, root, scope_is_file) -> str:
    """Pure-Python grep fallback (no external binary)."""
    out: list[str] = []
    if scope_is_file:
        targets = [root]
    else:
        targets = []
        for dirpath, dirnames, filenames in os.walk(root if root.is_dir() else base):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
            targets.extend(Path(dirpath) / f for f in filenames)
    for f in targets:
        try:
            text = f.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                rel = os.path.relpath(f, base)
                out.append(f"{Path(rel).as_posix()}:{i}: {line.strip()[:200]}")
                if len(out) >= 200:
                    return "\n".join(out)
    return "\n".join(out) if out else "(no matches)"


def _grep_rg(pattern, base, scope) -> str:
    """Fast path: shell out to ripgrep (gitignore-aware, much faster on big repos)."""
    import shutil
    import subprocess
    rg = shutil.which("rg")
    if not rg:
        return ""          # signal "unavailable" -> caller falls back
    cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-count", "200", "-e", pattern]
    # IMPORTANT: always pass an explicit path. With no path and a non-tty stdin
    # (we run under subprocess), rg reads STDIN instead of searching the dir.
    cmd.append(scope or ".")
    try:
        proc = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return ""          # fall back on failure
    if proc.returncode not in (0, 1):   # 1 = no matches (fine); 2+ = real error
        return ""
    lines = (proc.stdout or "").splitlines()[:200]
    # rg emits "path:line:content" — normalize the content a touch.
    norm = []
    for ln in lines:
        if ln.startswith("./"):
            ln = ln[2:]
        parts = ln.split(":", 2)
        if len(parts) == 3:
            norm.append(f"{parts[0]}:{parts[1]}: {parts[2].strip()[:200]}")
        else:
            norm.append(ln[:240])
    return "\n".join(norm) if norm else "(no matches)"


def _grep(args: dict, ctx: ToolContext) -> str:
    base = Path(ctx.workspace_root).resolve()
    pattern = args["pattern"]
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"error: bad regex: {e}"
    scope = args.get("path", "")
    root = _safe_path(ctx.workspace_root, scope) if scope else base
    # Fast path: ripgrep when present (gitignore-aware, fast); else pure-Python.
    rg_out = _grep_rg(pattern, base, scope if scope else "")
    if rg_out:
        return rg_out
    return _grep_py(rx, base, root, root.is_file())


def _apply_edit(ctx: ToolContext, rel_path: str, new_content: str, *, delete: bool = False) -> None:
    """Write or delete a workspace file. B1: when ctx.edit_applier is set, route through it
    so the change is CHECKPOINTED (undoable via /undo); else write directly (unchanged).
    Either way, fire ctx.on_edit(path, kind) for the live edit display. Confinement is
    enforced by the applier (or _safe_path) — callers may also pre-check."""
    applier = getattr(ctx, "edit_applier", None)
    if applier is not None:
        from .edits import EditProposal
        applier.apply(EditProposal(path=rel_path, new_content=("" if delete else new_content),
                                   delete=delete))
    else:
        full = _safe_path(ctx.workspace_root, rel_path)
        if delete:
            if full.is_file():
                full.unlink()
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(new_content, "utf-8")
    on_edit = getattr(ctx, "on_edit", None)
    if on_edit is not None:
        try:
            on_edit(rel_path, "delete" if delete else "write")
        except Exception:  # noqa: BLE001 - an edit-event listener must never break a tool
            pass


def _write(args: dict, ctx: ToolContext) -> str:
    _safe_path(ctx.workspace_root, args["path"])     # confinement (raises ToolError on escape)
    content = args.get("content", "")
    _apply_edit(ctx, args["path"], content)
    return f"wrote {args['path']} ({len(content)} chars)"


def _edit(args: dict, ctx: ToolContext) -> str:
    full = _safe_path(ctx.workspace_root, args["path"])
    if not full.exists():
        return f"error: no such file: {args['path']}"
    text = full.read_text("utf-8")
    old, new = args.get("old_string", ""), args.get("new_string", "")
    if old == "":
        return "error: old_string must not be empty"
    n = text.count(old)
    if n == 0:
        return "error: old_string not found"
    if n > 1:
        return f"error: old_string appears {n} times; make it unique"
    _apply_edit(ctx, args["path"], text.replace(old, new, 1))
    return f"edited {args['path']}"


def _bash(args: dict, ctx: ToolContext) -> str:
    from .sandbox import classify_command, run_command, CommandClass, is_confinement_block
    cmd = args.get("command", "")
    plan = classify_command(cmd, workspace_root=ctx.workspace_root)
    full_access = (getattr(ctx, "sandbox_mode", "") == "danger_full_access"
                   and is_confinement_block(plan))
    if plan.classification is CommandClass.BLOCKED and not full_access:
        return f"error: command blocked by safety policy: {plan.reason}"
    result = run_command(cmd, workspace_root=ctx.workspace_root,
                         sandbox=("off" if full_access else "auto"),
                         allow_confinement_escape=full_access)
    # Gap 2: when the command ran WITHOUT an OS sandbox (none installed), the user isn't
    # protected by isolation — so we don't run it SILENTLY. Emit a clear "ran on host" notice
    # event so the TUI can show what was executed (the run is informed, not hidden). The verbose
    # toggle (ctx.verbose_commands) decides whether EVERY command is announced or only the
    # unsandboxed ones. Best-effort: a missing emit hook never breaks the run.
    _emit = getattr(ctx, "on_command", None)
    if _emit is not None:
        try:
            if not result.sandboxed:
                _emit({"command": cmd, "sandboxed": False, "exit": result.exit_code})
            elif getattr(ctx, "verbose_commands", False):
                _emit({"command": cmd, "sandboxed": True, "exit": result.exit_code})
        except Exception:  # noqa: BLE001
            pass
    out = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
    prefix = "" if result.sandboxed else "[ran on host — no OS sandbox] "
    return f"{prefix}exit={result.exit_code}\n{out.strip()}"   # bounded centrally in dispatch()


def _apply_patch(args: dict, ctx: ToolContext) -> str:
    """Apply a multi-file patch envelope atomically + confined to the workspace."""
    from .patch import parse_patch, apply_ops, PatchError
    text = args.get("patch", "") or ""
    try:
        ops = parse_patch(text)
    except PatchError as e:
        return f"error: {e}"

    # Confinement check for every path BEFORE doing anything.
    paths = set()
    for op in ops:
        paths.add(op.path)
        if getattr(op, "move_to", ""):
            paths.add(op.move_to)
    try:
        for p in paths:
            _safe_path(ctx.workspace_root, p)
    except ToolError as e:
        return f"error: {e}"

    read_fn = lambda p: _safe_path(ctx.workspace_root, p).read_text("utf-8")
    exists_fn = lambda p: _safe_path(ctx.workspace_root, p).is_file()
    try:
        new_files = apply_ops(ops, read_fn, exists_fn)
    except PatchError as e:
        return f"error: {e}"

    # All ops computed cleanly + all paths confined above -> apply (deletes + writes).
    # B1: route through _apply_edit so each change is checkpointed (undoable) when an
    # edit_applier is present, and fires an edit event.
    changed = []
    for p, content in new_files.items():
        if content is None:
            if _safe_path(ctx.workspace_root, p).exists():
                _apply_edit(ctx, p, "", delete=True)
                changed.append(f"deleted {p}")
        else:
            _apply_edit(ctx, p, content)
            changed.append(f"wrote {p}")
    return "applied patch:\n" + "\n".join(sorted(changed))


def _todo(args: dict, ctx: ToolContext) -> str:
    """Manage the running TODO so the executor can track discovered work (Step 4)."""
    action = args.get("action", "list")
    if action == "add":
        item = (args.get("item") or "").strip()
        if not item:
            return "error: 'item' required for add"
        ctx.todos.append({"item": item, "done": False})
        return f"added todo #{len(ctx.todos)}: {item}"
    if action == "done":
        i = int(args.get("index", 0) or 0) - 1
        if 0 <= i < len(ctx.todos):
            ctx.todos[i]["done"] = True
            return f"completed todo #{i + 1}"
        return "error: bad todo index"
    # list
    if not ctx.todos:
        return "(no todos)"
    return "\n".join(f"#{i+1} [{'x' if t['done'] else ' '}] {t['item']}"
                     for i, t in enumerate(ctx.todos))


MAX_SUBAGENT_DEPTH = 2


def _task(args: dict, ctx: ToolContext) -> str:
    """Delegate a scoped sub-task to a fresh sub-agent (Step 7)."""
    if ctx.spawn is None:
        return "error: sub-agent delegation is not available here"
    if ctx.depth >= MAX_SUBAGENT_DEPTH:
        return f"error: max sub-agent depth ({MAX_SUBAGENT_DEPTH}) reached; do this task yourself"
    desc = (args.get("description") or "").strip()
    if not desc:
        return "error: 'description' required"
    return ctx.spawn(desc)


def _coerce_task_list(raw) -> list[str]:
    """Accept the `tasks` arg as a JSON list, or newline/`;`-separated string, or a
    single string -> a clean list of non-empty descriptions."""
    items: list = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        s = raw.strip()
        items = s.split("\n") if "\n" in s else (s.split(";") if ";" in s else [s])
    return [str(d).strip() for d in items if str(d).strip()]


def _tasks(args: dict, ctx: ToolContext) -> str:
    """Delegate N sub-tasks to fresh sub-agents that run IN PARALLEL (real workers, not a
    simulated script). Returns each worker's result, labeled, in order."""
    if ctx.spawn is None and ctx.spawn_many is None:
        return "error: sub-agent delegation is not available here"
    if ctx.depth >= MAX_SUBAGENT_DEPTH:
        return f"error: max sub-agent depth ({MAX_SUBAGENT_DEPTH}) reached; do these yourself"
    descs = _coerce_task_list(args.get("descriptions") or args.get("tasks") or args.get("description"))
    if not descs:
        return "error: 'descriptions' (a list of sub-task descriptions) required"
    if ctx.spawn_many is not None:
        results = ctx.spawn_many(descs)
    else:                                  # fall back to serial spawn if no parallel hook
        results = [ctx.spawn(d) for d in descs]
    return "\n\n".join(f"### sub-agent {i + 1}: {descs[i][:60]}\n{results[i]}"
                       for i in range(len(descs)))


def _plan(args: dict, ctx: ToolContext) -> str:
    """Maintain a structured plan the agent reasons over (action=set|show)."""
    action = args.get("action", "show")
    if action == "set":
        steps = args.get("steps")
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.split("\n") if s.strip()]
        if not isinstance(steps, list) or not steps:
            return "error: 'steps' (list) required for set"
        ctx.plan_notes[:] = [str(s) for s in steps]
        return "plan set:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(ctx.plan_notes))
    if not ctx.plan_notes:
        return "(no plan yet)"
    return "\n".join(f"{i+1}. {s}" for i, s in enumerate(ctx.plan_notes))


def _question(args: dict, ctx: ToolContext) -> str:
    """Ask the user a clarifying question and return their answer."""
    q = (args.get("question") or "").strip()
    if not q:
        return "error: 'question' required"
    if ctx.ask_user is None:
        return "error: no user available; proceed with your best assumption and state it"
    try:
        ans = ctx.ask_user(q)
    except Exception as e:  # noqa: BLE001
        return f"error: could not get an answer: {e}"
    return f"user answered: {ans}" if ans else "user gave no answer"


def _skill(args: dict, ctx: ToolContext) -> str:
    """Load a named skill/instruction set from .syntra/skills/<name>.md (confined)."""
    name = (args.get("name") or "").strip()
    if not name or "/" in name or ".." in name:
        return "error: invalid skill name"
    skill_path = _safe_path(ctx.workspace_root, f".syntra/skills/{name}.md")
    if not skill_path.is_file():
        sk_dir = Path(ctx.workspace_root) / ".syntra" / "skills"
        avail = sorted(p.stem for p in sk_dir.glob("*.md")) if sk_dir.is_dir() else []
        return f"error: no skill {name!r}. available: {', '.join(avail) or '(none)'}"
    return skill_path.read_text("utf-8")[:8000]


def _repo_overview(args: dict, ctx: ToolContext) -> str:
    """Summarize the repo: top-level layout, file counts by extension, entry points."""
    base = Path(ctx.workspace_root).resolve()
    ext_counts: dict = {}
    entry_markers = ("package.json", "pyproject.toml", "setup.py", "Cargo.toml",
                     "go.mod", "pom.xml", "Makefile", "README.md")
    found_entries = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE and not d.startswith(".")]
        for fn in filenames:
            total += 1
            ext = Path(fn).suffix or "(none)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            rel = os.path.relpath(os.path.join(dirpath, fn), base)
            if fn in entry_markers and rel.count(os.sep) <= 1:
                found_entries.append(rel)
        if total > 20000:
            break
    top = sorted(e.name + ("/" if e.is_dir() else "")
                 for e in base.iterdir()
                 if e.name not in _IGNORE and not e.name.startswith("."))[:40]
    top_exts = sorted(ext_counts.items(), key=lambda kv: -kv[1])[:12]
    lines = [f"workspace: {base.name}  ({total} files)",
             "top-level: " + " ".join(top),
             "by type: " + ", ".join(f"{e}:{n}" for e, n in top_exts)]
    if found_entries:
        lines.append("entry points: " + ", ".join(sorted(set(found_entries))))
    return "\n".join(lines)


def _repo_clone(args: dict, ctx: ToolContext) -> str:
    """git clone a repo into the workspace (shallow, confined, bounded)."""
    import subprocess
    from urllib.parse import urlparse
    url = (args.get("url") or "").strip()
    dest = (args.get("dest") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "error: url must be an http(s) git URL"
    target = _safe_path(ctx.workspace_root, dest or (Path(parsed.path).stem or "repo"))
    if target.exists():
        return f"error: dest {target.name!r} already exists"
    try:
        # #198: harden the clone too — kill the ext:: transport and any config-driven
        # program (`--` ends option parsing so a `--upload-pack=`-style URL can't inject
        # a flag). Scheme is already restricted to http(s) above; this is defense in depth.
        proc = subprocess.run(["git", *_GIT_HARDEN_CONFIG, "clone", "--depth", "1",
                               "--", url, str(target)],
                              cwd=ctx.workspace_root, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"error: clone failed: {e}"
    if proc.returncode != 0:
        return f"error: git clone exit {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
    return f"cloned {url} -> {target.name}"


# Read-only git subcommands the agent may run freely; anything else is gated.
_GIT_SAFE = {"status", "diff", "log", "show", "branch", "blame", "remote",
             "describe", "rev-parse", "ls-files", "shortlog"}

# #198 — a repo's OWN .git/config can turn a read-only-looking `git status`/`diff`
# into arbitrary host code: core.fsmonitor, core.pager, diff.external, core.hooksPath,
# core.sshCommand, and per-file `diff.<name>.textconv` drivers all run a shell command.
# `_git`/`_repo_clone` run raw on the host (no bwrap), so opening or cloning a hostile
# repo would execute that config. We neutralize the fixed-name vectors with `-c`
# overrides that WIN over repo-local config (verified) while leaving the user's global
# identity/signing intact, and disable protocol.ext (the `ext::sh -c` transport). Named
# textconv/external-diff drivers can't be killed by fixed-name `-c`, so diff-family
# subcommands additionally get --no-textconv/--no-ext-diff.
_GIT_HARDEN_CONFIG = [
    "-c", "core.fsmonitor=",          # no filesystem-monitor hook program
    "-c", "core.hooksPath=/dev/null",  # no hooks fire (pre-commit/post-checkout/…)
    "-c", "core.pager=cat",           # no pager program (core.pager=<shell>)
    "-c", "diff.external=",           # no external diff driver
    "-c", "core.sshCommand=",         # no attacker ssh command on fetch/push
    "-c", "protocol.ext.allow=never",  # kill the ext:: transport (ext::sh -c '…')
]
# Subcommands that render blobs through diff drivers → strip the driver entirely.
_GIT_DIFFY = {"diff", "show", "log", "blame"}


def _git_argv(sub: str, parts: list) -> list:
    """Build a hardened `git` argv: safety `-c` overrides before the subcommand, plus
    driver-disabling flags for diff-family reads. Pure — unit-tested."""
    argv = ["git", *_GIT_HARDEN_CONFIG, "--no-pager", sub]
    if sub in _GIT_DIFFY:
        # --no-ext-diff/--no-textconv defeat arbitrarily-NAMED drivers that -c can't.
        argv += ["--no-ext-diff", "--no-textconv"]
    return [*argv, *parts]


def _git(args: dict, ctx: ToolContext) -> str:
    """Run a git subcommand in the workspace. Read-only subcommands run freely;
    mutating ones require the args be gated via the 'git' tool's danger class."""
    import shutil
    import subprocess
    import shlex
    if shutil.which("git") is None:
        return "error: git is not installed"
    sub = (args.get("subcommand") or "").strip()
    if not sub:
        return "error: 'subcommand' required (e.g. status, diff, log)"
    extra = args.get("args", "")
    parts = shlex.split(extra) if isinstance(extra, str) else [str(a) for a in (extra or [])]
    # Sensible read defaults to keep output bounded.
    if sub == "log" and not any(p.startswith("-n") or p == "--oneline" for p in parts):
        parts = ["--oneline", "-n", "20", *parts]
    if sub == "diff" and "--stat" not in parts and not parts:
        parts = ["--stat"]
    # Provenance: append typed-state trailers to agent commits in the app-user's chosen style
    # (best-effort). Style "" / "off" → no trailers (a plain, intent-only commit message). The
    # style is resolved LAZILY, HERE — only when a commit actually happens — so the one-time
    # "how should I format commits?" question fires at the first real commit, never at run start
    # (most runs never commit and must never be asked).
    task_state = getattr(ctx, "state", None)
    if task_state is not None and sub == "commit" and "-m" in parts:
        style = getattr(ctx, "commit_style", "") or ""
        if not style:
            resolver = getattr(ctx, "resolve_commit_style", None)
            if callable(resolver):
                try:
                    style = resolver() or "off"
                except Exception:  # noqa: BLE001
                    style = "off"
            else:
                style = "off"
        if style != "off":
            try:
                from .provenance import commit_trailers
                trailers = commit_trailers(task_state, style=style)
                i = parts.index("-m")
                if trailers and i + 1 < len(parts):
                    parts[i + 1] = parts[i + 1].rstrip() + "\n\n" + trailers
            except Exception:  # noqa: BLE001 — never block a commit on provenance
                pass
    cmd = _git_argv(sub, parts)
    try:
        proc = subprocess.run(cmd, cwd=ctx.workspace_root, capture_output=True,
                              text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"error: git failed: {e}"
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return f"exit={proc.returncode}\n{out.strip()}"   # bounded centrally in dispatch()


def _webfetch(args: dict, ctx: ToolContext) -> str:
    from .web import fetch_url
    return fetch_url((args.get("url") or "").strip(),
                     args.get("format", "markdown") or "markdown",
                     args.get("timeout", 30) or 30,
                     allow_private=bool(getattr(ctx, "allow_private_fetch", False)))


def _websearch(args: dict, ctx: ToolContext) -> str:
    from .web import run_web_search
    q = (args.get("query") or "").strip()
    if not q:
        return "error: 'query' required"
    return run_web_search(q, ctx.web_search)


# ponytail: _youtube_transcript deleted. YouTube transcript extraction
# (core/youtube.py + core/video_understand.py) was speculative at v0.1.0:
# requires a manual API key, handles PoToken walls, and the agent can't
# see the video anyway. Restore when a user requests /watch.


def _exec_command(args: dict, ctx: ToolContext) -> str:
    """Start a long-running / interactive process; returns (session id, initial output)."""
    from .sandbox import classify_command, CommandClass, is_confinement_block
    cmd = args.get("command", "")
    plan = classify_command(cmd, workspace_root=ctx.workspace_root)
    full_access = (getattr(ctx, "sandbox_mode", "") == "danger_full_access"
                   and is_confinement_block(plan))
    if plan.classification is CommandClass.BLOCKED and not full_access:
        return f"error: command blocked by safety policy: {plan.reason}"
    sid, out = ctx.process_manager().open(cmd, ctx.workspace_root,
                                          sandbox=("off" if full_access else "auto"))
    return f"session={sid}\n{out.strip()}"   # bounded centrally in dispatch()


def _write_stdin(args: dict, ctx: ToolContext) -> str:
    """Send input to a running interactive process and return new output."""
    sid = (args.get("session") or "").strip()
    if not sid:
        return "error: 'session' required (from exec_command)"
    out = ctx.process_manager().write_stdin(sid, args.get("data", ""))
    return out.strip() or "(no output)"   # bounded centrally in dispatch()


def _close_process(args: dict, ctx: ToolContext) -> str:
    sid = (args.get("session") or "").strip()
    return ctx.process_manager().close(sid)


def _inline_data_url(path):
    """Read a local file and return a base64 data: URL with sniffed MIME."""
    import base64
    data = path.read_bytes()
    mime = sniff_mime(data) or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def sniff_mime(data: bytes) -> str | None:
    """Detect image type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _view_image(args: dict, ctx: ToolContext) -> str:
    """Load a workspace image so the model can SEE it on the next turn (vision). Also asks the
    TUI to render it inline for the USER, so 'view' shows it to both sides."""
    # ponytail: was from .multimodal import data_url_from_file (deleted, YAGNI).
    # Inlined the 5 essential lines here.
    rel = args.get("path", "")
    full = _safe_path(ctx.workspace_root, rel)
    if not full.is_file():
        return f"error: no such image: {rel}"
    try:
        ctx.pending_images.append(_inline_data_url(full))
    except (OSError, ValueError) as e:
        return f"error: {e}"
    if ctx.on_image:
        try: ctx.on_image(str(full))
        except Exception: pass  # noqa: BLE001
    return f"loaded image {rel} — shown to you (and rendered inline for the user)"


def _show_image(args: dict, ctx: ToolContext) -> str:
    """DISPLAY an existing workspace image inline in the USER'S terminal. Use this when the user
    asks to see/show/display an image they referred to or you just made. Confined to the workspace."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "error: 'path' required"
    try:
        full = _safe_path(ctx.workspace_root, rel)
    except ToolError as e:
        return f"error: {e}"
    if not full.is_file():
        return f"error: no such image: {rel}"
    if ctx.on_image is None:
        return ("note: no inline-display surface here (non-TUI run). The image is at "
                f"{rel} — open it in an image viewer.")
    try:
        ctx.on_image(str(full))
    except Exception as e:  # noqa: BLE001
        return f"error: could not display image: {e}"
    return f"displayed {rel} inline in the terminal"


def _preview(args: dict, ctx: ToolContext) -> str:
    """Render a URL / local HTML file / raw HTML string IN the terminal: a headless desktop browser
    screenshots it to a PNG, then that PNG is shown inline via the same seam show_image uses.
    """
    # ponytail: stubbed. browser_preview.py was deleted (YAGNI at v0.1.0).
    # Requires a Chromium binary on PATH + terminal_image for rendering.
    # Restore when browser automation is requested.
    return "error: preview is not available in this build (was over-engineered for v0.1.0)"


def _generate_image(args: dict, ctx: ToolContext) -> str:
    """Generate an image from a text prompt and WRITE it into the workspace (gated as a write).

    The provider call is injected as ctx.image_gen(prompt, size)->bytes (the loop builds it with
    the registry, routing to an image-capable model); None => no image backend configured. The
    bytes are saved to the given path (or a derived one) via the same confinement as other writes,
    so the TUI can then render it inline."""
    # ponytail: was from .multimodal import sniff_mime (deleted, YAGNI).
    # Inlined the 4-line sniff_mime here.
    def _sniff(data):
        if data[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
        if data[:3] == b"\xff\xd8\xff": return "image/jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
        return None
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "error: 'prompt' required"
    if ctx.image_gen is None:
        return "error: no image-generation backend configured (set a provider/model that supports image output)"
    size = (args.get("size") or "1024x1024").strip()
    try:
        data = ctx.image_gen(prompt, size)
    except Exception as e:  # noqa: BLE001
        return f"error: image generation failed: {e}"
    if not data:
        return "error: image generation returned no data"
    mime = _sniff(data) or "image/png"
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(mime, "png")
    rel = (args.get("path") or "").strip() or f"generated_image.{ext}"
    try:
        full = _safe_path(ctx.workspace_root, rel)   # confinement: escape → ToolError
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
    except ToolError as e:
        return f"error: {e}"
    except OSError as e:
        return f"error: could not save image: {e}"
    # log it as an edit (for the activity feed) AND auto-render it inline for the user.
    if ctx.on_edit:
        try: ctx.on_edit(rel, "image")
        except Exception: pass  # noqa: BLE001
    if ctx.on_image:
        try: ctx.on_image(str(full))
        except Exception: pass  # noqa: BLE001
    return f"generated image → {rel} ({len(data)} bytes, {mime}) — rendered inline"


def default_tools() -> dict[str, Tool]:
    """The v1 tool set. read/list/glob/grep/todo are safe; write/edit/bash are gated."""
    _str = {"type": "string"}
    tools = {t.name: t for t in [
        Tool("read", "Read a text file (line-numbered).",
             {"type": "object", "properties": {"path": _str,
              "offset": {"type": "integer"}, "limit": {"type": "integer"}},
              "required": ["path"]}, "safe", _read),
        Tool("list", "List a directory's entries.",
             {"type": "object", "properties": {"path": _str}}, "safe", _list),
        Tool("glob", "Find files by glob pattern (e.g. '**/*.py').",
             {"type": "object", "properties": {"pattern": _str}, "required": ["pattern"]},
             "safe", _glob),
        Tool("find_file", "Fuzzy-rank workspace files by a query (best matches first); "
             "scores partial matches, e.g. 'edmd' -> 'edit_md.py'.",
             {"type": "object", "properties": {"query": _str, "limit": {"type": "integer"}},
              "required": ["query"]}, "safe", _find_file,
             example='{"query":"editmd","limit":10}'),
        Tool("grep", "Search file contents by regex.",
             {"type": "object", "properties": {"pattern": _str, "path": _str},
              "required": ["pattern"]}, "safe", _grep),
        Tool("write", "Create or overwrite a file.",
             {"type": "object", "properties": {"path": _str, "content": _str},
              "required": ["path", "content"]}, "write", _write),
        Tool("edit", "Replace one UNIQUE occurrence of old_string with new_string in a file. "
             "old_string must match the current file BYTE-FOR-BYTE (indentation included) and occur "
             "exactly once — include enough surrounding context that it matches one place only.",
             {"type": "object", "properties": {"path": _str, "old_string": _str, "new_string": _str},
              "required": ["path", "old_string", "new_string"]}, "write", _edit,
             example='{"path":"app.py","old_string":"return 1","new_string":"return 2"}'),
        Tool("apply_patch", "Apply a multi-file edit bundle (add/update/delete/rename; atomic, confined) — "
             "prefer this over rewriting whole files. Format: a '=== SYNTRA EDIT BUNDLE ===' header, then "
             "per-file sections 'file + <path>' (add; +lines are the contents), 'file - <path>' (delete), "
             "'file ~ <path>' (update); an update may add '> rename <newpath>' and one or more '@@' hunks "
             "whose lines use ' ' context, '-' remove, '+' add; end with '=== END BUNDLE ==='. The context "
             "+ '-' lines MUST match the current file BYTE-FOR-BYTE (indentation included); NO line numbers. "
             "Prefer replacing a whole function/block over scattered one-line edits (anchors more reliably); "
             "never write placeholders like '... unchanged'. If it fails to apply, re-read the file, fix the "
             "context, and resend — don't guess.",
             {"type": "object", "properties": {"patch": _str}, "required": ["patch"]}, "write", _apply_patch,
             example='{"patch":"=== SYNTRA EDIT BUNDLE ===\\nfile ~ a.py\\n@@\\n-old\\n+new\\n=== END BUNDLE ==="}'),
        Tool("bash", "Run a shell command (safety-classified; blocked commands refused).",
             {"type": "object", "properties": {"command": _str}, "required": ["command"]},
             "exec", _bash),
        Tool("todo", "Track work: action=add|done|list (grow this as you discover work).",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["add", "done", "list"]},
                 "item": _str, "index": {"type": "integer"}}},
             "safe", _todo),
        Tool("task", "Delegate ONE focused sub-task to a fresh sub-agent; returns its result. "
             "Each call is a real, separate model run — never simulate sub-agents with a script.",
             {"type": "object", "properties": {"description": _str}, "required": ["description"]},
             "safe", _task),
        Tool("tasks", "Spawn N REAL sub-agents in PARALLEL — one per description — and return all "
             "their results. Use this when asked to 'run/spawn/use N agents' on different slices; "
             "NEVER fake agents with a print-script. You merge their results yourself afterward.",
             {"type": "object", "properties": {
                 "descriptions": {"type": "array", "items": _str}}, "required": ["descriptions"]},
             "safe", _tasks,
             example='{"descriptions":["summarize file a.py","summarize file b.py"]}'),
        Tool("plan", "Maintain a live plan: action=set (the full ordered step list) | show. Keep "
             "exactly ONE step in progress; mark a step done only AFTER its check passes; ADD a step "
             "when you discover new work rather than doing it silently. The plan is the resume anchor — "
             "a fresh context window should be able to read it and continue.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["set", "show"]},
                 "steps": {"type": "array", "items": _str}}},
             "safe", _plan),
        Tool("question", "Ask the user a clarifying question and get their answer.",
             {"type": "object", "properties": {"question": _str}, "required": ["question"]},
             "safe", _question),
        Tool("skill", "Load a named skill/instruction set from .syntra/skills/<name>.md.",
             {"type": "object", "properties": {"name": _str}, "required": ["name"]},
             "safe", _skill),
        Tool("repo_overview", "Summarize the workspace: layout, file types, entry points.",
             {"type": "object", "properties": {}}, "safe", _repo_overview),
        Tool("repo_clone", "git clone an http(s) repo into the workspace (shallow).",
             {"type": "object", "properties": {"url": _str, "dest": _str}, "required": ["url"]},
             "exec", _repo_clone),
        Tool("git", "Run git in the workspace: subcommand=status|diff|log|show|branch|... + optional args.",
             {"type": "object", "properties": {"subcommand": _str, "args": _str},
              "required": ["subcommand"]}, "exec", _git,
             example='{"subcommand":"diff","args":"--stat"}'),
        Tool("webfetch", "Fetch an http(s) URL; format=text|markdown|html (bounded).",
             {"type": "object", "properties": {
                 "url": _str, "format": {"type": "string", "enum": ["text", "markdown", "html"]},
                 "timeout": {"type": "integer"}}, "required": ["url"]},
             "exec", _webfetch, untrusted=True),
        Tool("websearch", "Search the web (requires a configured search backend).",
             {"type": "object", "properties": {"query": _str}, "required": ["query"]},
             "exec", _websearch, untrusted=True),
        Tool("youtube_transcript", "Get a YouTube video's title/description + full transcript from "
             "its URL (spoken words only — cannot see on-screen visuals; flags when the core is visual).",
             {"type": "object", "properties": {
                 "url": _str, "lang": {"type": "string"}}, "required": ["url"]},
             "exec", _youtube_transcript, untrusted=True),
        Tool("view_image", "Load a workspace image (png/jpeg/gif/webp) so you can SEE it next turn.",
             {"type": "object", "properties": {"path": _str}, "required": ["path"]},
             "safe", _view_image),
        Tool("show_image", "Display an existing workspace image INLINE in the user's terminal "
             "(use when the user asks to see/show an image).",
             {"type": "object", "properties": {"path": _str}, "required": ["path"]},
             "safe", _show_image,
             example='{"path":"panda.png"}'),
        Tool("generate_image", "Generate an image from a text prompt and save it into the workspace "
             "(needs an image-capable provider/model configured).",
             {"type": "object", "properties": {
                 "prompt": _str, "path": _str,
                 "size": {"type": "string", "description": "e.g. 1024x1024 (optional)"}},
              "required": ["prompt"]},
             "write", _generate_image,
             example='{"prompt":"a red fox in snow, flat vector","path":"fox.png"}'),
        Tool("preview", "Render a web page IN the terminal: give a url, a local HTML file path, or "
             "an html string; a headless browser screenshots it and it shows inline (works on "
             "terminals without a graphics protocol). Use when the user wants to SEE a page/report.",
             {"type": "object", "properties": {"url": _str, "path": _str, "html": _str}},
             "write", _preview,
             example='{"url":"https://example.com"}'),
        Tool("exec_command", "Start a long-running/interactive process; returns a session id + output.",
             {"type": "object", "properties": {"command": _str}, "required": ["command"]},
             "exec", _exec_command,
             example='{"command":"python3 -i"}'),
        Tool("write_stdin", "Send input to a running process (from exec_command) and read new output.",
             {"type": "object", "properties": {"session": _str, "data": _str}, "required": ["session", "data"]},
             "exec", _write_stdin),
        Tool("close_process", "Terminate a running interactive process session.",
             {"type": "object", "properties": {"session": _str}, "required": ["session"]},
             "exec", _close_process),
    ]}
    # ponytail: browser_tools removed (core/browser.py deleted, YAGNI at v0.1.0).
    # An OPTIONAL, private integration may live OUTSIDE this package and is loaded
    # only if present in the local checkout. Shipped Syntra contains no such code;
    # this is a no-op for an installed wheel.
    tools.update(_load_optional_integrations())
    return tools


def _load_optional_integrations() -> dict:
    """Load extra tools from an OPTIONAL, out-of-tree integration if present.

    Kept entirely external (never part of the `syntra` package, so it never ships).
    Discovered via $SYNTRA_INTEGRATION_DIR, else a `syntra_integration/` folder near
    the checkout. The module may expose `tools() -> dict`. Skipped silently when
    absent or on any error.
    """
    out: dict = {}
    mod = load_integration_module()
    if mod is not None and hasattr(mod, "tools"):
        try:
            out.update(mod.tools())
        except Exception:  # noqa: BLE001 - an optional integration must never break the tool set
            pass
    return out


def integration_dir():
    """The out-of-tree integration folder, or None. From $SYNTRA_INTEGRATION_DIR,
    else a `syntra_integration/` folder in an ancestor of the package. Always None
    for an installed wheel (nothing ships there)."""
    import os
    from pathlib import Path
    env = os.environ.get("SYNTRA_INTEGRATION_DIR")
    if env:
        d = Path(env).expanduser()
        return d if d.is_dir() else None
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        cand = parent / "syntra_integration"
        if cand.is_dir():
            return cand
    return None


_INTEGRATION_MODULE = None  # cache: None=not tried, False=absent, else the module


def load_integration_module():
    """Import the optional integration module (`integration.py` in the integration
    dir), cached. The module may expose tools()/slash_commands()/dispatch_slash().
    Returns the module or None. Never raises."""
    global _INTEGRATION_MODULE
    if _INTEGRATION_MODULE is not None:
        return _INTEGRATION_MODULE or None
    import importlib.util
    d = integration_dir()
    f = (d / "integration.py") if d is not None else None
    if f is None or not f.is_file():
        _INTEGRATION_MODULE = False
        return None
    try:
        spec = importlib.util.spec_from_file_location("_syntra_integration", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _INTEGRATION_MODULE = mod
        return mod
    except Exception:  # noqa: BLE001
        _INTEGRATION_MODULE = False
        return None


def readonly_tools() -> dict[str, Tool]:
    """Safe, read-only tools (read/list/glob/grep) for the reviewer to verify work
    without being able to change anything."""
    return {n: t for n, t in default_tools().items() if t.danger == "safe" and n not in ("todo", "task", "tasks")}


def tools_schema(tools: dict[str, Tool], *, is_off=None) -> list[dict]:
    """OpenAI `tools` array for the chat() call. A tool's `example`, when set, is
    appended to its description so the model sees a concrete usage sample (helps
    it call the tool with correct arguments).

    #251: when `is_off(name)` is provided, tools the user has turned OFF are omitted — an
    unavailable tool in the advertised schema is attack surface + a prompt-injection lure +
    wasted tokens. Default None → advertise everything (unchanged behavior)."""
    out = []
    for t in tools.values():
        if is_off is not None:
            try:
                if is_off(t.name):
                    continue
            except Exception:  # noqa: BLE001 - a misbehaving predicate must not drop all tools
                pass
        desc = t.description
        if t.example:
            desc = f"{desc}\nExample: {t.example}"
        out.append({"type": "function", "function": {
            "name": t.name, "description": desc, "parameters": t.parameters}})
    return out


def _rule_event_and_text(tool_name: str, args: dict) -> tuple[str, str]:
    """Map a tool call to a (hook_event, text_to_check) pair for rule matching.

    Returns ("", "") for tools that don't need rule-checking.
    """
    if tool_name in ("bash", "exec_command", "run"):
        return ("bash", str(args.get("command", "") or args.get("cmd", "")))
    if tool_name in ("write", "edit", "create"):
        return ("file", str(args.get("path", "") or args.get("file", "")))
    return ("", "")


def dispatch(call, tools: dict[str, Tool], ctx: ToolContext) -> str:
    """Run one model tool-call; return a result string (never raises into loop)."""
    tool = tools.get(call.name)
    if tool is None:
        return f"error: unknown tool {call.name!r}"
    try:
        args = json.loads(call.arguments) if call.arguments.strip() else {}
    except json.JSONDecodeError as e:
        return f"error: invalid tool arguments JSON: {e}"
    if not isinstance(args, dict):
        return "error: tool arguments must be a JSON object"
    # Pattern-based rule hooks (HookEngine): check command/path against warn/block rules.
    if ctx.rule_hooks is not None:
        event, check_text = _rule_event_and_text(tool.name, args)
        if check_text:
            blocked = ctx.rule_hooks.check_blocked(event, check_text)
            if blocked is not None:
                return f"error: blocked by rule '{blocked.hook.name}': {blocked.hook.message}"
    # pre_tool_use lifecycle hook: may BLOCK the call or REWRITE its args.
    if ctx.hooks is not None:
        from .hooks import PRE_TOOL_USE, POST_TOOL_USE
        pre = ctx.hooks.fire(PRE_TOOL_USE, {"name": tool.name, "danger": tool.danger, "arguments": args})
        if pre.blocks:
            return f"error: blocked by hook: {pre.reason}"
        if pre.new_args is not None:
            args = pre.new_args
    # B2: policy-aware gate for shell commands. Active ONLY when ctx.approval_policy is set
    # (default off -> behavior unchanged: every exec/write still goes through `permit`). When
    # set, the approval policy × sandbox mode × user prefix allow-list decide AUTO / ASK /
    # BLOCK; only ASK falls through to the `permit` gate below.
    _policy_auto = False
    _policy = getattr(ctx, "approval_policy", "") or ""
    if _policy and tool.name in ("bash", "exec_command", "run"):
        from .execpolicy import decide, Outcome
        _pd = decide(str(args.get("command", "") or args.get("cmd", "")),
                     policy=_policy,
                     sandbox_mode=(getattr(ctx, "sandbox_mode", "") or "workspace_write"),
                     workspace_root=ctx.workspace_root,
                     allow_prefixes=(getattr(ctx, "allow_prefixes", ()) or ()))
        if _pd.outcome is Outcome.BLOCK:
            return f"error: command blocked by safety policy: {_pd.reason}"
        _policy_auto = _pd.outcome is Outcome.AUTO
    if not _policy_auto and not ctx.allowed(tool.name, tool.danger, args):
        # #83 deny-with-guidance: if the user denied WITH a reason/suggestion, hand it back to the
        # agent so it adapts (edit instead of rm, use a different path, …) rather than just seeing
        # a blank refusal. The reason lives on the PermissionStore (ctx._perms) when wired.
        _reason = getattr(getattr(ctx, "_perms", None), "last_denial_reason", "") or ""
        if _reason:
            return (f"error: permission denied for {tool.name} ({tool.danger}) — "
                    f"user guidance: {_reason}")
        return f"error: permission denied for {tool.name} ({tool.danger})"
    try:
        result = tool.run(args, ctx)
    except ToolError as e:
        result = f"error: {e}"
    except KeyError as e:
        result = f"error: missing required argument {e}"
    except Exception as e:  # noqa: BLE001 - surface as text, never crash the loop
        result = f"error: {tool.name} failed: {e}"
    # #191: sanitize + (for untrusted-content tools) fence every result BEFORE it reaches the
    # model. Sanitize strips invisible/bidi/tag smuggling from ALL output; fencing wraps
    # web/file/MCP content in a "treat as data, not instructions" boundary. Our own `error:`
    # strings are trusted framing → sanitized but not fenced. Fence BEFORE truncation so the
    # closing fence can't be cut off mid-content.
    if isinstance(result, str):
        from .textguard import sanitize_model_text, fence_untrusted
        result = sanitize_model_text(result)
        if getattr(tool, "untrusted", False) and not result.startswith("error:"):
            result = fence_untrusted(result, source=tool.name)
    # Bound large output centrally so one tool call can't blow up the context
    # window. Middle-truncation keeps the head
    # (what ran) + the tail (how it ended) and reports how much was elided.
    if isinstance(result, str):
        from .output_truncation import truncate_output
        result = truncate_output(result)
    if ctx.hooks is not None:
        from .hooks import POST_TOOL_USE
        ctx.hooks.fire(POST_TOOL_USE, {"name": tool.name, "result": result[:1000]})
    return result
