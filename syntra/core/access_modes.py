"""Access modes — the file/command permission system, top to bottom.

Two MODES, each a dynamic preset of per-permission settings the user can tune live:
  - ``normal``  : work in the workspace (read auto; mutations ask; net off). The everyday mode.
  - ``locked``  : hardened — read-only sandbox, network off, mutations always ask. For untrusted
                  goals / when you want it to look but barely touch.

Each permission (read / edit / bash / webfetch / …) carries one of three settings:
  - ``auto`` : run without asking
  - ``ask``  : prompt the user (allow once / session / always / reject)
  - ``off``  : refuse outright

A MODE seeds defaults for every permission; the user overrides any of them in the popup, so
"normal" and "locked" are starting points, not fixed walls. The state maps onto the existing
exec-policy engine (``approval_policy`` × ``sandbox_mode``) for shell commands and onto the
per-tool ``permit`` gate for everything else.

THE FLOOR (never moves, every mode): touching a SENSITIVE path — ``.env``, ``.git/``, private
keys, credential stores — is forced to ASK even when a permission is set to ``auto``. You cannot
auto-approve a write to your own secrets; the most you can do is grant it interactively. This is
the one rule no mode or toggle can switch off (a perm may be made STRICTER — ``off`` — but never
``auto``). It mirrors what every serious agent runtime enforces.

PURE (no curses, no I/O at import): a model + a render function, unit-tested. The TUI paints
``access_box`` and forwards keys; persistence is a plain dict the CLI saves to access.json.
Mirrors key_entry.py / question_wizard.py / plan_approval.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ── the permissions, in display order ───────────────────────────────────────
# key -> (label, help). `network` is the sandbox net switch (not a tool); `sensitive_paths`
# is the FLOOR row (its value can only be ask/off, never auto).
PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("read",            "Read files",        "open / list / search workspace files"),
    ("edit",            "Write & edit",      "create, edit, apply patches, write files"),
    ("bash",            "Run commands",      "shell commands (sandboxed)"),
    ("exec_command",    "Long processes",    "start interactive / long-running processes"),
    ("git",             "Git",               "git status / diff / commit etc."),
    ("repo_clone",      "Clone repos",       "git clone into the workspace (network)"),
    ("webfetch",        "Fetch URLs",        "download an http(s) page (network)"),
    ("websearch",       "Web search",        "query a search backend (network)"),
    ("network",         "Network",           "allow sandboxed commands to reach the network"),
    ("sensitive_paths", "Secrets (floor)",   ".env / .git / keys — never auto, always at least ask"),
)
_PERM_KEYS = tuple(p[0] for p in PERMISSIONS)

# the three settings a permission can hold
AUTO, ASK, OFF = "auto", "ask", "off"
_SETTINGS = (AUTO, ASK, OFF)

# The four run modes, from least to most freedom. Shift+Tab cycles them in THIS order; the
# active one is shown on screen. Plain names anyone can read.
MODES: tuple[str, ...] = ("plan", "ask", "edit", "auto")
_MODE_LABELS = {
    "plan": "Plan · read-only",
    "ask":  "Ask · confirm each action",
    "edit": "Edit · files only, no commands",
    "auto": "Auto · do everything",
}
DEFAULT_MODE = "ask"        # Syntra opens in Ask (can act, but asks first).

# ── mode presets ─────────────────────────────────────────────────────────────
# plan: look only — read everything, change/run nothing.
# ask:  the careful default — can edit + run, but asks before each action.
# edit: edit files on its own, but NO shell commands (and no network).
# auto: do everything without asking.
# In EVERY mode the secret-path floor still forces a prompt (sensitive_paths is never `auto`),
# and the sandbox is always on underneath — neither is a mode setting.
MODE_PRESETS: dict[str, dict[str, str]] = {
    "plan": {
        "read": AUTO, "edit": OFF, "bash": OFF, "exec_command": OFF, "git": OFF,
        "repo_clone": OFF, "webfetch": OFF, "websearch": OFF, "network": OFF,
        "sensitive_paths": ASK,
    },
    "ask": {
        "read": AUTO, "edit": ASK, "bash": ASK, "exec_command": ASK, "git": ASK,
        "repo_clone": ASK, "webfetch": ASK, "websearch": ASK, "network": OFF,
        "sensitive_paths": ASK,
    },
    "edit": {
        "read": AUTO, "edit": AUTO, "bash": OFF, "exec_command": OFF, "git": OFF,
        "repo_clone": OFF, "webfetch": OFF, "websearch": OFF, "network": OFF,
        "sensitive_paths": ASK,
    },
    "auto": {
        "read": AUTO, "edit": AUTO, "bash": AUTO, "exec_command": AUTO, "git": AUTO,
        "repo_clone": AUTO, "webfetch": AUTO, "websearch": AUTO, "network": AUTO,
        "sensitive_paths": ASK,
    },
}

# which permission key gates each tool (tools not listed are ungated / internal). bash-family
# tools (bash/exec_command/run) ALSO go through the exec-policy matrix; the rest go through permit.
_TOOL_PERM: dict[str, str] = {
    "read": "read", "list": "read", "glob": "read", "grep": "read",
    "find_file": "read", "view_image": "read", "repo_overview": "read",
    "write": "edit", "edit": "edit", "apply_patch": "edit",
    "bash": "bash", "run": "bash",
    "exec_command": "exec_command", "write_stdin": "exec_command", "close_process": "exec_command",
    "git": "git", "repo_clone": "repo_clone",
    "webfetch": "webfetch", "websearch": "websearch", "preview": "edit",
}


# ── sensitive-path detection (THE FLOOR) ─────────────────────────────────────
# Secret FILES anywhere in the workspace. These are patterns, not product names — they describe
# the shape of a credential, so they live in source (and tests) freely.
_SENSITIVE_DIR_PARTS = (".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure")
_SENSITIVE_NAMES = (
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "secrets.json",
    "providers.json", ".pgpass", "application_default_credentials.json",
    "accesstokens.json", "refreshtokens.json",
    # writing these is an SSH/host backdoor even outside a .ssh dir (LOW-4):
    "authorized_keys", "known_hosts",
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore")


def is_sensitive_path(path: str) -> bool:
    """True if `path` names a secret file/dir we must never auto-touch.

    Matches: any `.git`/`.ssh`/… directory component, a known credential filename, a `.env`
    (but NOT `.env.example` / `.env.sample` — those are templates), or a key-like suffix. Pure;
    case-insensitive on the basename. Path need not exist (we gate intent, not existence)."""
    p = (path or "").strip()
    if not p:
        return False
    pl = p.replace("\\", "/")
    parts = [seg for seg in pl.split("/") if seg]
    name = (parts[-1] if parts else pl).lower()
    # secret directory anywhere in the path
    if any(seg.lower() in _SENSITIVE_DIR_PARTS for seg in parts):
        return True
    # Google Cloud stores user/application tokens under ~/.config/gcloud; avoid treating every
    # project-local `gcloud/` directory as sensitive by matching the config-store shape.
    low_parts = [seg.lower() for seg in parts]
    if any(a == ".config" and b == "gcloud" for a, b in zip(low_parts, low_parts[1:])):
        return True
    # .env and .env.<anything> EXCEPT obvious templates
    if name == ".env" or name.startswith(".env."):
        if name in (".env.example", ".env.sample", ".env.template", ".env.dist"):
            return False
        return True
    if name in _SENSITIVE_NAMES:
        return True
    if any(name.endswith(suf) for suf in _SENSITIVE_SUFFIXES):
        return True
    return False


def _paths_in_args(args: dict) -> list[str]:
    """Pull every filesystem path out of a tool's args — the direct path keys, an explicit
    `files` list, AND the targets buried inside an apply_patch `patch` blob (add/update/delete
    paths + rename targets). The patch case matters: apply_patch carries its paths in the patch
    TEXT, not a `path` arg, so without parsing it a `.env` write via apply_patch would slip past
    the sensitive-path floor entirely (HIGH-1)."""
    out: list[str] = []
    if not isinstance(args, dict):
        return out
    for k in ("path", "file", "filename", "dest", "target"):
        v = args.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    files = args.get("files")
    if isinstance(files, (list, tuple)):
        out.extend(str(x) for x in files if x)
    # apply_patch: parse the envelope and collect every op's path (+ rename target) via the
    # REAL parser, so detection can't drift from how the patch actually applies.
    patch = args.get("patch")
    if isinstance(patch, str) and patch:
        try:
            from .patch import parse_patch
            for op in parse_patch(patch):
                if getattr(op, "path", ""):
                    out.append(op.path)
                if getattr(op, "move_to", ""):
                    out.append(op.move_to)
        except Exception:  # noqa: BLE001 - a malformed patch fails later in apply; for gating,
            # fall back to scanning the raw text for any sensitive token so we never UNDER-detect.
            out.append(patch)
    return out


def call_is_sensitive(name: str, args: dict) -> bool:
    """Does this tool call touch a sensitive path? Only file-bearing tools can; bash/net tools
    are gated by the sandbox + exec-policy, not by this floor."""
    if name not in _TOOL_PERM:
        return False
    if _TOOL_PERM[name] not in ("read", "edit"):
        return False
    return any(is_sensitive_path(p) for p in _paths_in_args(args))


# ── access state ─────────────────────────────────────────────────────────────
@dataclass
class AccessState:
    """The live posture: a base mode + per-permission overrides. `effective(perm)` resolves the
    setting actually in force. Persisted as a small dict (mode + only the changed perms)."""

    mode: str = DEFAULT_MODE
    overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODE_PRESETS:
            self.mode = DEFAULT_MODE
        # keep only valid override values for valid perms
        self.overrides = {k: v for k, v in (self.overrides or {}).items()
                          if k in _PERM_KEYS and v in _SETTINGS}
        self._enforce_floor()

    def _enforce_floor(self) -> None:
        """sensitive_paths may be `ask` or `off`, NEVER `auto`."""
        if self.overrides.get("sensitive_paths") == AUTO:
            self.overrides.pop("sensitive_paths", None)

    # ---- resolution --------------------------------------------------------
    def effective(self, perm: str) -> str:
        """The setting in force for `perm`: an override if set, else the mode's preset value."""
        if perm in self.overrides:
            return self.overrides[perm]
        return MODE_PRESETS[self.mode].get(perm, ASK)

    def set_mode(self, mode: str) -> None:
        if mode in MODE_PRESETS:
            self.mode = mode
            # mode switch resets overrides to the new preset (a clean, predictable switch)
            self.overrides = {}

    def cycle_mode(self, direction: int = 1) -> None:
        """Shift+Tab: advance to the next/previous mode in MODES order, wrapping. Clears tweaks."""
        i = MODES.index(self.mode) if self.mode in MODES else 0
        self.set_mode(MODES[(i + direction) % len(MODES)])

    def mode_label(self) -> str:
        return _MODE_LABELS.get(self.mode, self.mode)

    def set_perm(self, perm: str, setting: str) -> None:
        if perm not in _PERM_KEYS or setting not in _SETTINGS:
            return
        if perm == "sensitive_paths" and setting == AUTO:
            return                                  # the floor: never auto
        # store as override only when it differs from the preset; else drop back to preset
        if MODE_PRESETS[self.mode].get(perm) == setting:
            self.overrides.pop(perm, None)
        else:
            self.overrides[perm] = setting

    def cycle_perm(self, perm: str, direction: int = 1) -> None:
        """Advance a permission's setting (Space / ←→). sensitive_paths cycles ask↔off only."""
        if perm == "sensitive_paths":
            order = (ASK, OFF)
        else:
            order = _SETTINGS
        cur = self.effective(perm)
        i = order.index(cur) if cur in order else 0
        self.set_perm(perm, order[(i + direction) % len(order)])

    # ---- mapping onto the engine ------------------------------------------
    def map_to_policy(self) -> tuple[str, str]:
        """(approval_policy, sandbox_mode) for the shell exec-policy gate, derived from the
        `bash` permission in force. bash=off → nothing runs (read-only sandbox, ask anyway);
        bash=auto → never prompt; bash=ask → prompt on each mutating command."""
        bash = self.effective("bash")
        if bash == OFF:
            return "untrusted", "read_only"     # commands can't auto-run; mutations always ask
        if bash == AUTO:
            return "never", "workspace_write"   # auto: run without prompting (floor still applies)
        return "on_request", "workspace_write"  # ask: prompt on each mutating command

    def is_auto_approve(self) -> bool:
        """True when the mode runs without per-action prompts (Auto, or bash flipped to auto).
        The TUI feeds this into the run as the auto_approve toggle."""
        return self.mode == "auto" or self.effective("bash") == AUTO

    def allow_network(self) -> bool:
        return self.effective("network") == AUTO

    def tool_setting(self, tool_name: str) -> str | None:
        """The setting (auto/ask/off) for the permission gating `tool_name`, or None if the tool
        isn't permission-mapped (internal/always-allowed)."""
        perm = _TOOL_PERM.get(tool_name)
        return self.effective(perm) if perm else None

    # ---- persistence -------------------------------------------------------
    def to_state_dict(self) -> dict:
        return {"mode": self.mode, "overrides": dict(self.overrides)}

    @classmethod
    def from_state_dict(cls, d: dict | None) -> "AccessState":
        d = d or {}
        return cls(mode=str(d.get("mode", DEFAULT_MODE) or DEFAULT_MODE),
                   overrides=dict(d.get("overrides", {}) or {}))

    def summary(self) -> str:
        """One-line posture for the status bar / a /mode echo."""
        n = len(self.overrides)
        return self.mode_label() + (f" · {n} tweak{'s' if n != 1 else ''}" if n else "")


def load_access_state(path: str | Path) -> AccessState:
    """Read access.json (mode + overrides). Missing/unreadable → default normal. Never raises."""
    try:
        import json
        return AccessState.from_state_dict(json.loads(Path(path).read_text("utf-8")))
    except (FileNotFoundError, ValueError, OSError):
        return AccessState()


def save_access_state(path: str | Path, state: AccessState) -> bool:
    """Persist access.json. Best-effort; returns True on success."""
    try:
        import json
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state.to_state_dict(), indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


# ── popup form (mirrors KeyEntryForm) ────────────────────────────────────────
@dataclass
class AccessModeForm:
    """State for the access-modes popup. Row 0 = the mode switcher; rows 1..N = one per
    permission. ↑↓ move, ←/→ or Space cycle the focused row's value, Enter apply, Esc cancel.
    Edits a COPY of the state so Esc discards cleanly."""

    state: AccessState = field(default_factory=AccessState)
    focus: int = 0                                    # 0 = mode row, 1.. = permission rows
    mode_ui: str = "edit"                             # edit | done | cancelled
    result_value: AccessState | None = None
    _click_rows: dict = field(default_factory=dict)   # render-row -> form-row index
    _screen_y0: int = -1

    def _nrows(self) -> int:
        return 1 + len(PERMISSIONS)                   # mode row + one per perm

    # ---- navigation --------------------------------------------------------
    def move(self, delta: int) -> None:
        if self.mode_ui != "edit":
            return
        self.focus = (self.focus + delta) % self._nrows()

    def focus_row(self, i: int) -> None:
        if self.mode_ui == "edit" and 0 <= i < self._nrows():
            self.focus = i

    def click_row(self, content_row: int):
        i = self._click_rows.get(int(content_row))
        if i is not None:
            self.focus = i
        return None

    # ---- editing -----------------------------------------------------------
    def cycle(self, direction: int = 1) -> None:
        """Change the focused row. On the mode row → switch preset; on a perm row → cycle its value."""
        if self.mode_ui != "edit":
            return
        if self.focus == 0:
            i = MODES.index(self.state.mode) if self.state.mode in MODES else 0
            self.state.set_mode(MODES[(i + direction) % len(MODES)])
        else:
            perm = _PERM_KEYS[self.focus - 1]
            self.state.cycle_perm(perm, direction)

    # ---- submit / cancel ---------------------------------------------------
    def submit(self):
        if self.mode_ui != "edit":
            return None
        self.mode_ui = "done"
        self.result_value = self.state
        return ("apply", self.state)

    def cancel(self) -> None:
        self.mode_ui = "cancelled"

    def result(self):
        return self.result_value if self.mode_ui == "done" else None


def _setting_chip(setting: str, focused: bool) -> str:
    """A compact ‹auto|ask|off› chip with the current value bracketed."""
    cells = [f"[{s}]" if s == setting else f" {s} " for s in _SETTINGS]
    return "".join(cells)


def access_box(form: AccessModeForm, width: int) -> list:
    """Render the popup -> [(text, style)]. Pure; caller centers + borders. Records _click_rows."""
    w = max(46, int(width))
    out: list = []
    form._click_rows = {}
    st = form.state

    out.append(("  Access & permissions", "accent"))
    out.append(("  the sandbox is always on; pick how much it can do — then tune any line", "dim"))
    out.append(("", "default"))

    # mode row (form-row 0)
    mode_focused = (form.focus == 0 and form.mode_ui == "edit")
    cur = "❯ " if mode_focused else "  "
    chips = "".join(f"[{_MODE_LABELS[m]}]" if m == st.mode else f" {_MODE_LABELS[m]} " for m in MODES)
    form._click_rows[len(out)] = 0
    out.append((f"  {cur}Mode: {chips}"[:w], "user" if mode_focused else "default"))
    out.append(("", "default"))

    # permission rows (form-rows 1..N)
    label_w = max(len(p[1]) for p in PERMISSIONS)
    for i, (key, label, help_text) in enumerate(PERMISSIONS):
        focused = (form.focus == i + 1 and form.mode_ui == "edit")
        cur = "❯ " if focused else "  "
        setting = st.effective(key)
        chip = _setting_chip(setting, focused)
        floor = "  · floor" if key == "sensitive_paths" else ""
        # mark a value that differs from the mode preset (a user tweak)
        tweak = " *" if (key in st.overrides) else ""
        line = f"  {cur}{label.ljust(label_w)}  {chip}{tweak}{floor}"
        form._click_rows[len(out)] = i + 1
        style = "user" if focused else ("dim" if setting == OFF else "default")
        out.append((line[:w], style))

    out.append(("", "default"))
    out.append(("  ↑↓ move · ←→/Space change · Enter apply · Esc cancel", "dim"))
    out.append(("  secrets (.env/.git/keys) always ask — even on auto", "dim"))
    return out
