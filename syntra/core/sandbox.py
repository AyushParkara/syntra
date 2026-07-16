"""Bounded, classified command execution (Phase 3 safety foundation).

Two concerns, kept separate:

1. Classification (PURE, deterministic, exhaustively tested): given a command
   string + workspace root, decide SAFE / NEEDS_APPROVAL / BLOCKED. Dangerous
   commands (rm -rf /, writes outside the workspace, fork bombs, disk wipes,
   privilege escalation) are BLOCKED outright -- they never reach execution.

2. Execution (bounded): run an approved command with a hard timeout, captured +
   capped output, and working-dir confinement. Never run a BLOCKED command.

The classifier is the security boundary and is the part we test hardest
(PLAN Section 16 risks: filesystem/command chaos -> confinement, timeouts,
blocklist, exhaustive safety tests). Pattern reused from the legacy sandbox
concept; reimplemented clean (req A1).
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CommandClass(str, Enum):
    SAFE = "safe"                    # read-only / inspection -> may auto-run
    NEEDS_APPROVAL = "needs_approval"  # mutating but bounded -> human approves
    BLOCKED = "blocked"              # dangerous -> never run


# Commands that only read/inspect. Auto-runnable. Deliberately EXCLUDES anything
# that executes arbitrary code (python/node/go/cargo/pytest/make/...) -- those
# can do anything and must go through approval.
_SAFE_BINARIES = {
    "ls", "cat", "pwd", "echo", "head", "tail", "wc", "grep", "rg", "find",
    "stat", "file", "diff", "tree", "which", "env", "date", "whoami", "git",
}
# git subcommands that are read-only (anything else under git -> needs approval).
_SAFE_GIT_SUB = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files"}

# #254 — binaries that perform NETWORK EGRESS. A command starting with one of these is exfil-class
# and must ASK even under Auto mode (the inviolable rail above the auto layer). `git` is NOT here
# (its network subs like push/fetch are separately gated + identity-bound); these are the generic
# transfer/shell tools an exfil would reach for.
_NETWORK_BINARIES = {
    "curl", "wget", "scp", "sftp", "nc", "ncat", "netcat", "ssh", "telnet", "rsync",
    "ftp", "tftp", "socat", "aria2c", "httpie", "http", "https",
}

# Hard-blocked substrings/patterns: destructive or escaping the box. Matched on
# the normalized command string. Conservative -> prefer blocking over allowing.
_BLOCKED_SUBSTRINGS = (
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf .", ":(){:|:&};:",  # wipes / fork bomb
    "mkfs", "dd if=", "> /dev/sda", "of=/dev/sd", "wipefs",
    "chmod -r 777 /", "chown -r", "shutdown", "reboot", "halt",
    "/etc/passwd", "/etc/shadow",
)
# Tokens that indicate privilege escalation or remote code pulls.
_BLOCKED_TOKENS = {"sudo", "su", "doas"}
# Network-fetch-then-execute (curl|sh) is blocked.
_PIPE_TO_SHELL = ("| sh", "| bash", "|sh", "|bash", "| zsh")
_PIPE_TO_INTERPRETER_RE = re.compile(
    r"\b(?:curl|wget|aria2c|http|https)\b[^|\n;]*\|\s*(?:env\s+)?(?:/[\w./-]+/)?"
    r"(?:sh|bash|zsh|dash|ksh|python|python3|perl|ruby|node)\b"
)


@dataclass(frozen=True)
class CommandPlan:
    command: str
    classification: CommandClass
    reason: str
    # #202(a): True when the command references a credential path / the process env. This
    # is a HARD "always ask" signal — a full-auto policy (Auto mode → policy=never) must
    # NOT downgrade it to auto-run. Structured flag (not reason-string matching) so the
    # exec-policy gate can rely on it. Defaults False → the other 12 construction sites and
    # any external caller are unaffected.
    secret: bool = False
    # #254: True when the command performs NETWORK EGRESS (curl/wget/scp/nc/ssh/rsync/…). Like
    # `secret`, a HARD "always ask" signal — even Auto mode (policy=never) must not silently run
    # an exfil-class command (the inviolable rail above auto). Only an explicit prefix-allow opts
    # out. Defaults False → unaffected construction sites / external callers.
    network: bool = False

    @property
    def blocked(self) -> bool:
        return self.classification is CommandClass.BLOCKED


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    sandboxed: bool = True       # False when it ran on the bare host (no bwrap/Seatbelt) — Gap 2


def is_confinement_block(plan: CommandPlan) -> bool:
    """True only for workspace-confinement blocks that full-access may explicitly lift."""
    return bool(plan.blocked and "outside" in (plan.reason or "").lower()
                and "workspace" in (plan.reason or "").lower())


def _normalize(command: str) -> str:
    return " ".join((command or "").split()).lower()


# Redirect sinks that are NOT a filesystem write (discarding output, or to the
# terminal). Exempted so `cmd 2>/dev/null` isn't mistaken for a workspace escape.
_DEVNULL_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/tty"}


def _redirect_targets(command: str):
    """Yield the target token of each > / >> redirect in the command (best-effort).

    Skips fd-duplications (`2>&1`, `>&2`) and fd-prefixed redirects so a target like
    `1` from `2>&1` isn't mistaken for a file. Stops the scan at a shell separator
    (`| & ;`) so the token after a redirect is the real sink, not a later word."""
    norm = command or ""
    for op in (">>", ">"):
        idx = 0
        while True:
            idx = norm.find(op, idx)
            if idx < 0:
                break
            after = norm[idx + len(op):]
            if after[:1] == "&":                 # fd-dup like 2>&1 / >&2 -> not a file write
                idx += len(op)
                continue
            rest = after.strip()
            # stop at a shell operator so we only take the redirect target word
            for sep in ("|", "&", ";", "<"):
                cut = rest.find(sep)
                if cut >= 0:
                    rest = rest[:cut]
            parts = rest.split()
            if parts:
                yield parts[0].strip('"\'')
            idx += len(op)


def _has_outside_write_redirect(command: str, workspace_root: str | Path | None = None) -> bool:
    """True if the command redirects to a path that ESCAPES the workspace — either an
    ABSOLUTE path or a RELATIVE ``..`` traversal (e.g. ``> ../../etc/cron.d/x``).
    /dev/null-style sinks are exempt (they're not a filesystem write)."""
    for t in _redirect_targets(command):
        if t in _DEVNULL_TARGETS:
            continue
        if t.startswith("/"):
            if workspace_root is None:
                return True                      # no root to compare against: fail closed
            try:
                base = Path(workspace_root).resolve()
                target = Path(t).resolve()
            except OSError:
                return True
            if target == base or base in target.parents:
                continue
            return True
        # relative traversal that climbs out of the workspace (../, ..\, or a leading ~)
        norm = t.replace("\\", "/")
        if norm.startswith("~"):
            return True
        if norm == ".." or norm.startswith("../") or "/../" in norm or norm.endswith("/.."):
            return True
    return False


def _has_file_write_redirect(command: str) -> bool:
    """True if the command writes to a FILE via > / >> (excludes /dev/null-style
    sinks). Used to gate a 'read-only' tool that actually writes via redirect, so it
    still goes through the approval gate (e.g. `echo x > out.txt`)."""
    return any(t not in _DEVNULL_TARGETS for t in _redirect_targets(command))


_PROC_ENV_RE = re.compile(r"/proc/(?:self|\d+|[*?\[\]]+)/(?:environ|cmdline)")

# #240 — bash ANSI-C quoting `$'...'` decodes escapes (\xNN, \NNN octal, \uNNNN, \t\n\\ …) BEFORE
# running, so `cat $'\x2eenv'` runs `cat .env`. The classifier sees the literal escape and would
# miss the secret. Decode `$'...'` runs so the scan sees the real bytes the shell would.
_ANSI_C_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)


def _decode_ansi_c(s: str) -> str:
    """Expand every `$'...'` ANSI-C-quoted run in `s` to the string bash would produce, leaving
    the rest untouched. Best-effort + safe: an undecodable run is left as-is. Pure."""
    def _one(m):
        body = m.group(1)
        try:
            # Python's unicode_escape handles \xNN, \uNNNN, \t\n\r\\ etc.; translate bash octal
            # \NNN (no leading 0) into \0NNN which unicode_escape understands.
            oct_fixed = re.sub(r"\\([0-7]{1,3})", lambda o: "\\" + o.group(1), body)
            return oct_fixed.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except Exception:  # noqa: BLE001 - leave undecodable runs literal
            return m.group(0)
    return _ANSI_C_RE.sub(_one, s)


# #192/#193 — a read-only binary can WRITE or EXECUTE through its OWN flags, dodging the
# shell-redirect check. Per-binary denylist of flags that turn a "safe" tool dangerous.
# Matched against each token as an exact flag OR its `--flag=…` prefix.
_UNSAFE_FLAGS = {
    # git (any subcommand): --output=<path> writes an arbitrary file (diff/log/show/format-patch)
    "git":  ("--output",),
    # ripgrep: run an arbitrary preprocessor / search-zip binary on every file
    "rg":   ("--pre", "--pre-glob", "--hostname-bin"),
    # find: run/delete/WRITE via its own action primaries. The COMPLETE write/exec action set
    # (find's dangerous surface is small + stable, so a denylist is the right model here, vs the
    # huge version-varying test/filter surface an allowlist would have to enumerate).
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete",
             "-fprint", "-fprint0", "-fprintf", "-fprintf0", "-fls"),
    # date -s / --set MUTATES the system clock (everything else about date reads).
    "date": ("-s", "--set"),
}


# #241 — command wrappers that RUN another command. They must be transparent to classification:
# `timeout 5 sudo rm x` is still privilege-escalation, `nice cat f` is still a safe read. We strip
# the wrapper + its own flags + any leading `VAR=val` and re-classify the inner command. The
# `env` wrapper is handled separately (it also has a bare/dump form). Value-taking wrapper flags
# consume the next token so we don't mistake the flag's value for the inner command.
_WRAPPER_BINARIES = {
    # `pos` = number of leading POSITIONAL args the wrapper takes before the inner command
    # (e.g. `timeout DURATION cmd…`, `chrt PRIO cmd…`). `val_flags` = flags that consume a value.
    "timeout":  {"val_flags": {"-s", "--signal", "-k", "--kill-after"}, "pos": 1},
    "nice":     {"val_flags": {"-n", "--adjustment"}, "pos": 0},
    "ionice":   {"val_flags": {"-c", "--class", "-n", "--classdata", "-p", "--pid"}, "pos": 0},
    "stdbuf":   {"val_flags": {"-i", "-o", "-e", "--input", "--output", "--error"}, "pos": 0},
    "nohup":    {"val_flags": set(), "pos": 0},
    "setsid":   {"val_flags": set(), "pos": 0},
    "chrt":     {"val_flags": set(), "pos": 1},
}


def _strip_wrapper(tokens: list) -> list | None:
    """If tokens[0] is a wrapper binary, return the inner command tokens (wrapper + its flags +
    its leading positional args + leading VAR=val removed). None if not a wrapper or nothing runs
    after it. Pure — unit-tested."""
    if not tokens:
        return None
    spec = _WRAPPER_BINARIES.get(tokens[0].lower())
    if spec is None:
        return None
    val_flags = spec["val_flags"]
    pos_needed = spec.get("pos", 0)
    rest = tokens[1:]
    j = 0
    # First consume flags + VAR=val; then the required positional args (e.g. timeout's DURATION).
    while j < len(rest):
        t = rest[j]
        if "=" in t and not t.startswith("-"):        # leading VAR=val for the inner env
            j += 1
        elif t in val_flags:                          # flag that consumes its value
            j += 2
        elif t.startswith("-"):                       # bare wrapper flag (incl. --flag=val)
            j += 1
        elif pos_needed > 0:                          # a required positional (duration/priority)
            pos_needed -= 1
            j += 1
        else:
            break                                     # first remaining operand = the inner command
    inner = rest[j:]
    return inner or None


# #194 — well-known environment variables whose expansion is not a credential-path risk (a bare
# `$HOME`/`$PWD` operand is fine); any OTHER bare variable operand is unresolvable → gate it.
_KNOWN_SAFE_VARS = frozenset({
    "HOME", "PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP", "PATH", "USER", "LOGNAME",
    "HOSTNAME", "SHELL", "TERM", "LANG", "LC_ALL", "PAGER", "EDITOR", "CI",
})
_BARE_VAR_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def _has_bare_variable_operand(tokens: list) -> bool:
    """True if any non-flag operand is ENTIRELY a single unresolved variable (`$X`/`${X}`) whose
    name isn't a known-safe env var — i.e. a path we can't inspect because the shell resolves it
    at runtime. `$HOME/x` (a var WITH a literal suffix) is NOT bare and is fine. Pure."""
    for t in tokens:
        if not t or t.startswith("-"):
            continue
        m = _BARE_VAR_RE.match(t.strip("'\""))
        if m and m.group(1) not in _KNOWN_SAFE_VARS:
            return True
    return False


def _flag_hits(tokens: list, needles) -> str:
    """Return the first token that equals a needle or starts with `needle=` (so both
    `--output /tmp/x` and `--output=/tmp/x` match). '' if none."""
    for t in tokens:
        for n in needles:
            if t == n or (n.startswith("--") and t.startswith(n + "=")):
                return t
    return ""


# #239 — a positive per-binary safe-FLAG allowlist. `_UNSAFE_FLAGS` (above) is a denylist of the
# KNOWN dangerous flags; this is the stronger complement: for a binary listed here, ONLY the
# enumerated read-only flags may auto-run — any OTHER flag (a write/exec/network/exfil flag we
# didn't foresee, or a brand-new one) escalates to approval. Non-flag tokens (paths, patterns,
# glob args) are always fine. Long flags match by their `--name` (value after `=` ignored); short
# flags are unbundled (`-la` -> l, a). Binaries NOT listed here keep the prior behavior (denylist
# only) so this can be rolled out per-binary without over-blocking. Enumerated GENEROUSLY to avoid
# false-positive prompt slop — the goal is to catch the write/exec flags, not nag on `ls --color`.
_SAFE_FLAG_ALLOWLIST = {
    "ls":   {"a", "l", "h", "r", "R", "t", "S", "d", "1", "F", "i", "n", "p", "A", "G", "o", "g",
             "color", "group-directories-first", "almost-all", "human-readable", "reverse",
             "recursive", "sort", "time", "classify", "inode", "full-time", "block-size"},
    "tree": {"a", "d", "f", "i", "l", "s", "h", "p", "u", "g", "D", "C", "n", "L", "P", "I",
             "level", "dirsfirst", "noreport", "prune", "filelimit", "sort", "du"},
    # (cat/head/tail/wc/grep/rg/find/stat/file/diff/which/date have their WRITE/EXEC flags in the
    #  denylist; find/rg are further gated by _UNSAFE_FLAGS. Add more binaries here as verified.)
    "cat":  {"n", "b", "A", "E", "T", "v", "s", "u", "number", "number-nonblank",
             "show-all", "show-ends", "show-tabs", "show-nonprinting", "squeeze-blank"},
    "head": {"n", "c", "q", "v", "lines", "bytes", "quiet", "silent", "verbose", "z", "zero-terminated"},
    "tail": {"n", "c", "f", "F", "q", "v", "lines", "bytes", "follow", "quiet", "silent",
             "verbose", "z", "zero-terminated", "retry", "pid", "sleep-interval", "max-unchanged-stats"},
    "wc":   {"c", "m", "l", "w", "L", "bytes", "chars", "lines", "words", "max-line-length"},
}
# Flags that TAKE A VALUE as the next token (so `-o out.html` — the value isn't a flag). Only the
# ones we still want to REASON about; here they're mostly irrelevant because value-flags that write
# (tree -o) are simply NOT in the allowlist and thus escalate.
_ALLOWLISTED_BINARIES = frozenset(_SAFE_FLAG_ALLOWLIST)


def _unlisted_flag(binary: str, tokens: list) -> str:
    """For an allowlisted binary, return the first flag token that is NOT on its safe-flag
    allowlist (so the caller escalates it to approval). '' when every flag is allowed. Non-flag
    tokens (paths/patterns) are ignored. Honors `--` end-of-options. Pure — unit-tested."""
    allow = _SAFE_FLAG_ALLOWLIST.get(binary)
    if not allow:
        return ""
    seen_ddash = False
    for tok in tokens:
        if seen_ddash or not tok.startswith("-") or tok == "-":
            continue                                  # a path/pattern/operand, or after `--`
        if tok == "--":
            seen_ddash = True
            continue
        if tok.startswith("--"):                      # long flag: --name or --name=value
            name = tok[2:].split("=", 1)[0]
            if name not in allow:
                return tok
        else:                                         # short flag cluster: -la -> l, a
            for ch in tok[1:]:
                if ch.isdigit():                      # numeric args like -n20's leading part vary;
                    continue                          # digits are values, not flags
                if ch not in allow:
                    return tok
    return ""


def _dumps_environment(command: str) -> bool:
    """True if the command DUMPS the whole process environment (where `api_key_env` provider
    keys live). `env`, `printenv`, `set`, `export -p`, `declare -x/-p` all print every var —
    a silent key-exfil channel. `env` is in _SAFE_BINARIES for the common `env VAR=x cmd`
    wrapper, but bare `env` (or `env` with only flags, or `env | grep KEY`, or `env > f`) dumps.

    Checks the FIRST pipeline/chain segment so `env | grep KEY` and `printenv > f` are caught
    (the dump happens in that first stage regardless of what pipes/redirects follow). Distinguish
    a dump from the `env VAR=x CMD` wrapper by whether a non-flag, non-`VAR=VALUE` token — an
    executed command — follows (the wrapper is classified by its inner command elsewhere)."""
    # First segment only: cut at the first shell operator (| & ; < > newline).
    seg = re.split(r"[|&;<>\n]", (command or "").strip(), maxsplit=1)[0]
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return False
    head = tokens[0].lower()
    rest = tokens[1:]
    if head == "printenv":
        return True
    if head == "env":
        for t in rest:
            if t.startswith("-") or "=" in t:
                continue
            return False           # a command follows -> wrapper, handled by the exec-flag path
        return True
    if head == "set" and not rest:
        return True                # bare `set` prints all shell vars+functions
    if head == "export" and rest == ["-p"]:
        return True
    if head == "declare" and rest and rest[0] in ("-x", "-p"):
        return True
    return False


def _touches_secret_path(command: str) -> bool:
    """True if the command READS a credential file/dir (.env, ~/.ssh, keys, providers.json, …)
    or the process environment. Gates `cat .env`, `cat ~/.ssh/id_rsa` etc. — which would
    otherwise classify SAFE (cat is a read tool) — so reading secrets needs approval EVEN with
    no OS sandbox (the bwrap mask is host-dependent; this rule is not).

    Scans the RAW command string, not shlex tokens: shell metacharacters (`|`, `;`, `$(...)`,
    backticks, globs, redirects) glue a path onto adjacent text, so token-equality misses
    `echo $(cat .env)`, `cat .env|head`, `cat ./.env;x`, `cat *.env`. We instead split the raw
    string on shell separators + whitespace and test every `/`-style candidate. Reuses the one
    canonical secret definition (access_modes.is_sensitive_path); `~` is expanded."""
    from .access_modes import is_sensitive_path
    import os
    cmd = command or ""
    # #240: decode ANSI-C `$'...'` so `cat $'\x2eenv'` is scanned as `cat .env` (bash decodes it
    # before running). Scan the decoded form in ADDITION to the raw (decoding never hides a path).
    decoded = _decode_ansi_c(cmd)
    if decoded != cmd:
        cmd = decoded
    # process-environment dumps name no file but leak every secret — gate them directly.
    if _PROC_ENV_RE.search(cmd):
        return True
    home = os.path.expanduser("~")
    # Break on shell metacharacters AND whitespace so a glued token (`./.env;echo`, `.env|head`,
    # `$(cat`) yields the bare path candidate. Backticks/parens/redirects are separators.
    # #195(d): QUOTES are NOT separators — the shell REMOVES them (adjacent parts concatenate),
    # so `cat .en''v` runs `cat .env`. We split on everything EXCEPT quotes, then strip quote
    # characters from each candidate to reconstruct the real path the shell would see.
    for raw in re.split(r"""[\s|&;<>()`]+""", cmd):
        cand = raw.strip().replace("'", "").replace('"', "")   # shell quote-removal
        if not cand or cand.startswith("-"):
            continue
        cand = cand.lstrip("$")                 # $(...) / $VAR fragments -> drop the leading $
        if cand.startswith("~"):
            cand = home + cand[1:]
        if is_sensitive_path(cand):
            return True
        # A glob can target secrets without naming one literally (`*.env`, `.env*`, `id_*`,
        # `*.pem`). Expand against the workspace; if any real match is sensitive, gate it. Falls
        # back to a conservative pattern check when expansion finds nothing (the file may not
        # exist yet / cwd differs).
        if any(ch in cand for ch in "*?[") and (".env" in cand or cand.endswith((".pem", ".key"))
                                                or "id_" in cand or "secret" in cand.lower()
                                                or "cred" in cand.lower()):
            return True
    return False


def _split_segments(command: str) -> list[str]:
    """Split a compound command into its top-level segments on `;`, `&&`, `||`, `|` — but NOT
    inside single/double quotes or `$(...)`/backtick command-substitution (those are one unit).
    Returns [command] when there's no top-level separator. Pure — unit-tested."""
    segs, buf = [], []
    i, n = 0, len(command)
    sq = dq = False
    depth = 0                                        # $(...) / `...` nesting
    while i < n:
        c = command[i]
        two = command[i:i + 2]
        if sq:
            buf.append(c); sq = (c != "'"); i += 1; continue
        if dq:
            buf.append(c); dq = (c != '"'); i += 1; continue
        if c == "'":
            sq = True; buf.append(c); i += 1; continue
        if c == '"':
            dq = True; buf.append(c); i += 1; continue
        if c == "`":
            depth ^= 1; buf.append(c); i += 1; continue
        if two == "$(":
            depth += 1; buf.append(two); i += 2; continue
        if c == ")" and depth > 0:
            depth -= 1; buf.append(c); i += 1; continue
        if depth == 0:
            if two in ("&&", "||"):
                segs.append("".join(buf)); buf = []; i += 2; continue
            if c in ";|":
                segs.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    out = [s.strip() for s in segs if s.strip()]
    return out or [command]


def _matching_paren(s: str, open_idx: int) -> int:
    """Return the matching ')' for s[open_idx] == '('; -1 when unbalanced.

    Shell command/process substitutions can contain nested `$()`, quotes, and escaped chars.
    This is intentionally conservative: if a substitution is too weird to parse, the caller asks
    for approval rather than treating the outer `echo`/`cat` as safe."""
    depth = 1
    i = open_idx + 1
    sq = dq = False
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2; continue
        if sq:
            if c == "'":
                sq = False
            i += 1; continue
        if dq:
            if c == '"':
                dq = False
            elif s.startswith("$(", i):
                depth += 1; i += 2; continue
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1; continue
        if c == "'":
            sq = True; i += 1; continue
        if c == '"':
            dq = True; i += 1; continue
        if s.startswith("$(", i):
            depth += 1; i += 2; continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _extract_embedded_commands(command: str) -> list[str] | None:
    """Commands executed inside `$()`, backticks, or process substitution.

    Returns None when the shell syntax is unbalanced/ambiguous enough that we should require
    approval. Single quotes suppress substitution; double quotes do not."""
    out: list[str] = []
    s = command or ""
    i = 0
    sq = False
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2; continue
        if sq:
            if c == "'":
                sq = False
            i += 1; continue
        if c == "'":
            sq = True; i += 1; continue
        if s.startswith("$((", i):              # arithmetic expansion, not command substitution
            end = s.find("))", i + 3)
            if end < 0:
                return None
            i = end + 2; continue
        if s.startswith("$(", i):
            end = _matching_paren(s, i + 1)
            if end < 0:
                return None
            inner = s[i + 2:end].strip()
            if inner:
                out.append(inner)
            i = end + 1; continue
        if c in ("<", ">") and i + 1 < len(s) and s[i + 1] == "(":
            end = _matching_paren(s, i + 1)
            if end < 0:
                return None
            inner = s[i + 2:end].strip()
            if inner:
                out.append(inner)
            i = end + 1; continue
        if c == "`":
            j = i + 1
            buf = []
            while j < len(s):
                if s[j] == "\\" and j + 1 < len(s):
                    buf.append(s[j + 1]); j += 2; continue
                if s[j] == "`":
                    inner = "".join(buf).strip()
                    if inner:
                        out.append(inner)
                    i = j + 1
                    break
                buf.append(s[j]); j += 1
            else:
                return None
            continue
        i += 1
    return out


def _classify_embedded_commands(command: str, workspace_root: str | Path | None) -> CommandPlan | None:
    embedded = _extract_embedded_commands(command)
    if embedded is None:
        return CommandPlan(command, CommandClass.NEEDS_APPROVAL,
                           "unparseable command substitution -> require approval")
    worst: CommandPlan | None = None
    any_secret = False
    any_network = False
    for inner in embedded:
        plan = classify_command(inner, workspace_root=workspace_root)
        any_secret = any_secret or plan.secret
        any_network = any_network or plan.network
        if plan.classification is CommandClass.BLOCKED:
            return CommandPlan(command, CommandClass.BLOCKED,
                               f"command substitution blocked: {plan.reason}",
                               secret=any_secret, network=any_network)
        if plan.classification is CommandClass.NEEDS_APPROVAL and worst is None:
            worst = plan
    if worst is not None:
        return CommandPlan(command, CommandClass.NEEDS_APPROVAL,
                           f"command substitution needs approval: {worst.reason}",
                           secret=any_secret, network=any_network)
    return None


def classify_command(command: str, *, workspace_root: str | Path | None = None) -> CommandPlan:
    """Classify a command. Never executes. The security boundary.

    BLOCKED        : destructive, privilege-escalating, or escaping the box.
    SAFE           : read-only inspection (auto-runnable).
    NEEDS_APPROVAL : everything else (mutating but bounded).

    A COMPOUND command (`a && b`, `a ; b`, `a | b`) is classified per-segment and merged
    BLOCK > NEEDS_APPROVAL > SAFE (#242) — so a safe first segment can't hide a dangerous
    second (`cat f ; sudo rm x`). Whole-string hard-blocks (pipe-to-shell, dangerous
    substrings, outside-write-redirect) run first and win regardless of segmentation.
    """
    raw = (command or "").strip()
    if not raw:
        return CommandPlan(raw, CommandClass.BLOCKED, "empty command")

    norm = _normalize(raw)

    # WHOLE-STRING hard blocks run first (must see the full command, not a segment): `curl|sh`,
    # dangerous substrings, and outside-write redirects win over any per-segment verdict.
    for bad in _BLOCKED_SUBSTRINGS:
        if bad in norm:
            return CommandPlan(raw, CommandClass.BLOCKED, f"dangerous pattern: {bad!r}")
    for pat in _PIPE_TO_SHELL:
        if pat in norm:
            return CommandPlan(raw, CommandClass.BLOCKED, "pipe-to-shell (remote code execution)")
    if _PIPE_TO_INTERPRETER_RE.search(norm):
        return CommandPlan(raw, CommandClass.BLOCKED, "pipe-to-interpreter (remote code execution)")
    if _has_outside_write_redirect(raw, workspace_root):
        return CommandPlan(raw, CommandClass.BLOCKED, "write redirect to an absolute path outside workspace")

    # #242: classify each top-level segment; merge with BLOCK > NEEDS_APPROVAL > SAFE.
    segments = _split_segments(raw)
    if len(segments) > 1:
        worst = CommandClass.SAFE
        worst_reason = "all segments read-only"
        any_secret = False
        any_network = False
        for seg in segments:
            plan = _classify_segment(seg, workspace_root=workspace_root)
            any_secret = any_secret or plan.secret
            any_network = any_network or plan.network
            if plan.classification is CommandClass.BLOCKED:
                return CommandPlan(raw, CommandClass.BLOCKED,
                                   f"segment blocked: {plan.reason}",
                                   secret=any_secret, network=any_network)
            if plan.classification is CommandClass.NEEDS_APPROVAL and worst is CommandClass.SAFE:
                worst = CommandClass.NEEDS_APPROVAL
                worst_reason = f"segment needs approval: {plan.reason}"
        return CommandPlan(raw, worst, worst_reason, secret=any_secret, network=any_network)

    return _classify_segment(raw, workspace_root=workspace_root)


def _classify_segment(command: str, *, workspace_root: str | Path | None = None) -> CommandPlan:
    """Classify a SINGLE command segment (no top-level `;`/`&&`/`||`/`|`). The per-command
    security logic; `classify_command` splits compounds and calls this per segment."""
    raw = (command or "").strip()
    if not raw:
        return CommandPlan(raw, CommandClass.BLOCKED, "empty command")

    norm = _normalize(raw)

    # 1) Hard blocks first.
    for bad in _BLOCKED_SUBSTRINGS:
        if bad in norm:
            return CommandPlan(raw, CommandClass.BLOCKED, f"dangerous pattern: {bad!r}")
    for pat in _PIPE_TO_SHELL:
        if pat in norm:
            return CommandPlan(raw, CommandClass.BLOCKED, "pipe-to-shell (remote code execution)")
    if _PIPE_TO_INTERPRETER_RE.search(norm):
        return CommandPlan(raw, CommandClass.BLOCKED, "pipe-to-interpreter (remote code execution)")
    if _has_outside_write_redirect(raw, workspace_root):
        return CommandPlan(raw, CommandClass.BLOCKED, "write redirect to an absolute path outside workspace")

    sub_plan = _classify_embedded_commands(raw, workspace_root)
    if sub_plan is not None:
        return sub_plan

    # Tokenize for binary/escalation checks (best-effort; shlex may fail on odd input).
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return CommandPlan(raw, CommandClass.NEEDS_APPROVAL, "unparseable command -> require approval")
    if not tokens:
        return CommandPlan(raw, CommandClass.BLOCKED, "empty after tokenize")

    first = tokens[0].lower()

    # #241: a command wrapper (`timeout`/`nice`/`stdbuf`/`ionice`/…) runs another command — strip
    # it and classify the INNER, so `timeout 5 sudo rm x` is still BLOCKED (escalation) and
    # `nice cat f` is still SAFE (not a needless prompt). Recurse so the inner runs every check.
    # (Only when the raw wasn't already a hard-block substring above, which still wins.)
    inner = _strip_wrapper(tokens)
    if inner is not None:
        return classify_command(" ".join(inner), workspace_root=workspace_root)

    if first in _BLOCKED_TOKENS:
        return CommandPlan(raw, CommandClass.BLOCKED, f"privilege escalation: {first}")

    # 1b) Reading a credential file (.env / ~/.ssh / keys / providers.json) or the process env
    # ALWAYS needs approval — even via a 'safe' read tool like cat/head, even through shell
    # obfuscation ($(...), pipes, globs, chains), and even when no OS sandbox is present to mask
    # it. Secret exfil shouldn't be a silent auto-run. Scans the RAW command. (Gap 3 / HIGH-2)
    if _touches_secret_path(raw):
        return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                           "command references a credential path — approval required",
                           secret=True)
    # Dumping the whole process environment leaks every key the same way — gate it too, so
    # `env` (in the safe list for the wrapper form) and `printenv` are consistent with the
    # already-gated /proc/self/environ.
    if _dumps_environment(raw):
        return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                           "command dumps the process environment — approval required",
                           secret=True)

    # 1c) `env [VAR=val ...] CMD` is a WRAPPER — it executes CMD, so classify by the inner
    # command, not by `env` (which is in the safe list for the bare/dump form). Strip env +
    # its own flags + VAR=VALUE assignments; whatever remains is the real command. (`env -i`,
    # `env -u X` etc. are env's flags.) The bare/dump form was already caught above.
    if first == "env":
        rest = tokens[1:]
        # env's own value-taking options consume the following token.
        _env_val_flags = ("-u", "--unset", "-C", "--chdir", "-S", "--split-string")
        i = 0
        while i < len(rest):
            t = rest[i]
            if "=" in t and not t.startswith("-"):      # VAR=VALUE assignment
                i += 1
            elif t in _env_val_flags:                    # flag + its value
                i += 2
            elif t.startswith("-"):                      # bare flag (-i, --null, …)
                i += 1
            else:
                break                                    # first real token = the wrapped command
        inner = rest[i:]
        if inner:
            # Reclassify the wrapped command on its own (recurse on the reconstructed string).
            return classify_command(" ".join(inner), workspace_root=workspace_root)
        # nothing to run after env's args -> falls through to the safe/dump handling below

    # 2) Safe read-only inspection. BUT a write redirect (`echo x > f`) makes even a
    # 'safe' tool mutate the filesystem -> it must go through the approval gate. A binary
    # can ALSO write/exec through its own flags (git --output, rg --pre, find -exec) — those
    # dodge the redirect check, so screen them explicitly (#192/#193).
    if first in _SAFE_BINARIES:
        writes = _has_file_write_redirect(raw)
        bad_flag = _flag_hits(tokens[1:], _UNSAFE_FLAGS.get(first, ()))
        if bad_flag:
            return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                               f"{first} {bad_flag} can write/execute — approval required")
        # #239: positive safe-flag allowlist. For an allowlisted binary, any flag NOT enumerated
        # as read-only (a write/exec/network flag we didn't foresee) escalates to approval.
        unlisted = _unlisted_flag(first, tokens[1:])
        if unlisted:
            return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                               f"{first} {unlisted} is not a known read-only flag — approval required")
        if first == "git":
            sub = tokens[1].lower() if len(tokens) > 1 else ""
            if sub in _SAFE_GIT_SUB and not writes:
                return CommandPlan(raw, CommandClass.SAFE, f"read-only git {sub}")
            return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                               f"git {sub} writes to a file" if writes else f"git {sub} may mutate")
        if writes:
            return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                               f"{first} redirects output to a file (a write)")
        # #194: a path operand that is ENTIRELY an unresolved variable (`cat $SECRETFILE`) can't
        # be proven safe — its value (unknown here) could point at a secret. Fail to the human
        # gate. (A var WITH a visible non-secret suffix like `$HOME/README.md` stays SAFE.)
        if _has_bare_variable_operand(tokens[1:]):
            return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                               f"{first} reads a variable-resolved path — approval required")
        # rm/mv/cp are NOT in safe list, so reaching here means an inspection tool.
        return CommandPlan(raw, CommandClass.SAFE, f"read-only tool: {first}")

    # 3) Everything else: bounded but mutating -> approval. Flag network-egress binaries so the
    # exec-policy floor (#254) keeps them at ASK even under Auto mode.
    if first in _NETWORK_BINARIES:
        return CommandPlan(raw, CommandClass.NEEDS_APPROVAL,
                           f"{first} performs network egress — approval required", network=True)
    return CommandPlan(raw, CommandClass.NEEDS_APPROVAL, "non-inspection command -> require approval")


def bwrap_available() -> bool:
    """True if bubblewrap (bwrap) is installed for OS-level command isolation."""
    import shutil
    return shutil.which("bwrap") is not None


# #243 — git config keys that let a repo's own .git/config execute a shell command on a plain
# read (`git status`/`diff`): fsmonitor hook, hooks dir, pager, external diff driver, ssh command,
# and the `ext::` transport. Injected as GIT_CONFIG_* env so they override the repo config for
# EVERY git call in a sandboxed command (verified: env-injected config wins over repo config,
# and commit identity / normal ops are preserved).
_GIT_HARDEN_KV = (
    ("core.fsmonitor", ""),
    ("core.hooksPath", "/dev/null"),
    ("core.pager", "cat"),
    ("diff.external", ""),
    ("core.sshCommand", ""),
    ("protocol.ext.allow", "never"),
)


def _git_hardening_env() -> dict:
    """Return the GIT_CONFIG_COUNT/KEY_n/VALUE_n env that neutralizes a hostile repo's own
    config for every git invocation. Pure — unit-tested."""
    env = {"GIT_CONFIG_COUNT": str(len(_GIT_HARDEN_KV))}
    for i, (k, v) in enumerate(_GIT_HARDEN_KV):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def _with_git_hardening(base: dict | None) -> dict:
    """Merge the git-config hardening env onto `base` (defaulting to the parent env) — for the
    seatbelt + no-bwrap paths where the child inherits `env` directly (bwrap uses --setenv). If
    the command's own env already set GIT_CONFIG_COUNT, we prepend ours (ours win via lower
    indices is not guaranteed, so we simply take precedence by overwriting the count+our slots —
    an app that needs custom GIT_CONFIG must pass it explicitly, which is not our command path)."""
    out = dict(os.environ if base is None else base)
    out.update(_git_hardening_env())
    return out


def build_bwrap_argv(command: str, workspace_root: str | Path, *,
                     allow_network: bool = False, unshare_user: bool = True,
                     mask_paths: tuple = ()) -> list:
    """Build a bubblewrap argv that runs `command` with kernel-enforced isolation.

    Read-only bind of the whole filesystem, a WRITABLE bind of only the workspace,
    a private /tmp, no network namespace (default), plus PID/user-namespace and
    session isolation. Even a malicious command cannot write outside the
    workspace, reach the net, signal host processes, or escalate. Pure ->
    unit-tested; the caller runs the returned argv.

    `mask_paths` are directories overlaid with an empty tmpfs so a sandboxed
    command cannot READ them (API keys, SSH/cloud creds) — the read-only bind of
    `/` would otherwise expose them. The caller resolves the existing secret dirs
    (`default_secret_masks`) and passes them, keeping this builder pure.

    `unshare_user=False` drops `--unshare-user` for environments that block
    nested user namespaces (the caller falls back to that on failure).
    """
    root = str(Path(workspace_root).resolve())
    bwrap = shutil.which("bwrap") or "bwrap"
    argv = [
        bwrap,
        "--new-session",                  # detach from the controlling terminal (TIOCSTI safety)
        "--die-with-parent",              # killed if syntra dies
        "--unshare-pid",                  # can't see/signal host processes
        "--ro-bind", "/", "/",            # whole FS readable
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    # Secret hygiene: SCRUB the environment so API keys passed via env vars
    # (e.g. providers.json `api_key_env`, OPENAI_API_KEY, AWS_*) are NOT visible
    # inside the sandbox — masking secret *files* is not enough if the key lives in
    # the environment. Re-inject only a minimal, non-sensitive set the command needs.
    argv += ["--clearenv"]
    for var in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "USER", "LOGNAME"):
        val = os.environ.get(var)
        if val:
            argv += ["--setenv", var, val]
    if "PATH" not in os.environ:          # ensure a usable PATH even if the parent lacks one
        argv += ["--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin"]
    # #243: neutralize a hostile repo's own .git/config for EVERY git invocation in the command
    # (bash `git status` runs raw git → a planted core.fsmonitor/hooksPath/pager/protocol.ext
    # would run code, confined but real). GIT_CONFIG_COUNT/KEY/VALUE injects overriding config
    # via env — no command parsing, applies to `x && git y` too. Same posture as the #198 git tool.
    for k, v in _git_hardening_env().items():
        argv += ["--setenv", k, v]
    # Hide secret directories: empty tmpfs over each, BEFORE the workspace bind so a
    # workspace nested under one is still re-exposed by the writable bind below.
    for p in mask_paths:
        argv += ["--tmpfs", str(p)]
    argv += [
        "--tmpfs", "/tmp",                # private tmp (after masks, BEFORE the
        "--bind", root, root,             # workspace bind so nesting resolves right)
        "--chdir", root,
    ]
    if unshare_user:
        argv.append("--unshare-user")     # private user namespace (no host uid mapping)
    if not allow_network:
        argv.append("--unshare-net")      # cut off network egress
    argv += ["/bin/sh", "-c", command]
    return argv


# #190 — MCP/LSP servers are spawned as long-lived subprocesses and are NOT bwrap-confined
# (they need real stdio + often network). A child spawned with env=None inherits the parent's
# FULL environment, including provider API keys reached via `api_key_env` (OPENROUTER_API_KEY,
# GROQ_API_KEY, …) and cloud creds. A malicious/compromised MCP package could read them. We
# hand these children a SCRUBBED copy: strip secret-shaped variables, keep everything else so
# the server still runs (unlike the bwrap allowlist, a subprocess needs PYTHONPATH/NODE_ENV/…).
_SECRET_ENV_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
                      "PRIVATE", "SESSION", "AUTH", "APIKEY", "PASS")
_SECRET_ENV_PREFIX = ("AWS_", "AZURE_", "GCP_", "GOOGLE_", "OPENAI_", "ANTHROPIC_",
                      "OPENROUTER_", "GROQ_", "GITHUB_", "GH_", "GITLAB_", "NPM_",
                      "HF_", "HUGGINGFACE_", "SLACK_", "STRIPE_", "TWILIO_", "SENTRY_")
# Non-secret vars whose NAME contains a trigger substring — keep them (avoid false strips).
_SECRET_ENV_KEEP = frozenset({
    "SSH_AUTH_SOCK",          # a socket path, not a credential
    "GPG_TTY", "SSH_TTY",
    "XAUTHORITY",             # X cookie file path (not the cookie); keep for GUI child
})


def _is_secret_env_name(name: str, extra: frozenset = frozenset()) -> bool:
    up = name.upper()
    if up in _SECRET_ENV_KEEP:
        return False
    if name in extra:
        return True
    if any(up.startswith(p) for p in _SECRET_ENV_PREFIX):
        return True
    return any(s in up for s in _SECRET_ENV_SUBSTR)


def scrubbed_child_env(base: dict | None = None, *, extra_secret_names=()) -> dict:
    """Return a copy of `base` (default os.environ) with secret-shaped variables removed, so a
    spawned MCP/LSP subprocess can't read provider API keys / cloud creds from the inherited
    environment. Keeps non-secret vars (PATH, HOME, PYTHONPATH, NODE_ENV, …) so the child runs.
    `extra_secret_names` are exact names to also strip (e.g. a provider's configured
    `api_key_env` that doesn't match the name heuristics). Pure — unit-tested."""
    import os
    src = os.environ if base is None else base
    extra = frozenset(extra_secret_names or ())
    return {k: v for k, v in src.items() if not _is_secret_env_name(k, extra)}


def seatbelt_available() -> bool:
    """True on macOS where `sandbox-exec` (Seatbelt) is present — the OS-level command
    isolation equivalent to bubblewrap on Linux. (Cross-platform sandbox parity.)"""
    import sys
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def build_seatbelt_profile(workspace_root: str | Path, *, allow_network: bool = False,
                           mask_paths: tuple = ()) -> str:
    """A macOS Seatbelt (.sb) profile string: deny-by-default, allow reads everywhere,
    allow WRITES only inside the workspace (+ /tmp + /dev), and deny network unless
    allowed. `mask_paths` (secret dirs) are denied for READ too. Pure -> unit-tested;
    the runtime equivalent of build_bwrap_argv on Linux."""
    root = str(Path(workspace_root).resolve())
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read*)",                                 # read-all (like bwrap's ro-bind /)
        f'(allow file-write* (subpath "{root}"))',            # writable ONLY in the workspace
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/dev"))',
    ]
    # Hide secret dirs: deny reads (the read-all above would otherwise expose them).
    lines.extend(f'(deny file-read* (subpath "{p}"))' for p in mask_paths)
    lines.append("(allow network*)" if allow_network else "(deny network*)")
    return "\n".join(lines)


def build_seatbelt_argv(command: str, workspace_root: str | Path, *,
                        allow_network: bool = False, mask_paths: tuple = ()) -> list:
    """`sandbox-exec` argv running `command` under the deny-default Seatbelt profile —
    the macOS counterpart to build_bwrap_argv. Pure -> unit-tested; caller runs it."""
    exe = shutil.which("sandbox-exec") or "sandbox-exec"
    profile = build_seatbelt_profile(workspace_root, allow_network=allow_network,
                                     mask_paths=mask_paths)
    return [exe, "-p", profile, "/bin/sh", "-c", command]


_NO_SANDBOX_WARNED = False


def _warn_no_sandbox_once() -> None:
    """Warn ONCE per process (to stderr) that commands run WITHOUT an OS sandbox.
    Loud-but-not-fatal: the user can install bwrap/run on macOS, or pass sandbox='require'
    to fail closed. Best-effort — never raises."""
    global _NO_SANDBOX_WARNED
    if _NO_SANDBOX_WARNED:
        return
    _NO_SANDBOX_WARNED = True
    try:
        import sys
        sys.stderr.write(
            "\n⚠ SECURITY: no OS sandbox available (bubblewrap/Seatbelt not installed) — "
            "model-run commands execute with FULL host access (can write outside the "
            "workspace, read secrets, reach the network). Install `bwrap` (Linux) or run on "
            "macOS, or use sandbox='require' to refuse instead.\n\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _bwrap_setup_failed(stderr: str) -> bool:
    """True when bubblewrap itself failed before the command could run."""
    s = (stderr or "").lower()
    return s.startswith("bwrap:") and any(x in s for x in (
        "operation not permitted", "failed to create", "permission denied", "not permitted",
        "creating new namespace", "user namespace",
    ))


def os_sandbox_available() -> bool:
    """True if ANY OS-level command sandbox is usable on this host (bwrap on Linux,
    Seatbelt on macOS). Lets run_command pick a backend without hardcoding the OS."""
    return bwrap_available() or seatbelt_available()


def default_secret_masks() -> tuple:
    """Existing secret directories to hide from sandboxed commands.

    Syntra's own provider keys + common credential stores. Only dirs that exist
    are returned (so bwrap never errors on a missing mount point). I/O lives here,
    keeping `build_bwrap_argv` pure. Workspace-local config (`./.syntra`) is NOT
    masked — the writable workspace bind re-exposes anything nested under it.
    """
    home = Path.home()
    cands = [
        home / ".config" / "syntra",      # providers.json + secrets.json (the crown jewels)
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".docker",
        home / ".config" / "gcloud",
        home / ".config" / "gh",
        home / ".kube",
    ]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        cands.append(Path(xdg).expanduser() / "syntra")
    pf = os.environ.get("SYNTRA_PROVIDERS_FILE")
    if pf:
        cands.append(Path(pf).expanduser().parent)
    seen, out = set(), []
    for c in cands:
        try:
            if not c.is_dir():
                continue
            r = str(c.resolve())
        except OSError:
            continue
        if r in seen or r in ("/", str(home)):   # never mask all of $HOME or /
            continue
        seen.add(r)
        out.append(r)
    return tuple(out)


@functools.lru_cache(maxsize=1)
def bwrap_userns_ok() -> bool:
    """Probe ONCE whether bwrap can set up a user namespace on this host.

    Cached: some kernels/containers block nested user namespaces. There we still
    sandbox but drop ``--unshare-user`` (every other isolation still applies),
    instead of failing the whole session. Mirrors run_command's no-userns retry,
    but resolved up front because a long-lived Popen can't be retried in place.
    """
    if not bwrap_available():
        return False
    bwrap = shutil.which("bwrap") or "bwrap"
    try:
        proc = subprocess.run(
            [bwrap, "--unshare-user", "--ro-bind", "/", "/", "/bin/true"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - probe is best-effort; fall back to no-userns
        return False


def sandboxed_popen_argv(command: str, workspace_root: str | Path, *,
                         allow_network: bool = False) -> list | None:
    """bwrap argv for a LONG-LIVED / interactive process (exec_command), or None
    when bwrap is unavailable.

    Same kernel confinement as run_command's auto mode (no network, writes only
    inside the workspace, no host-process/secret access), but suitable for Popen:
    the caller keeps the process alive and writes its stdin. ``--unshare-user`` is
    chosen from a cached one-time host probe so a no-userns host still gets every
    other isolation rather than a dead session.
    """
    if not bwrap_available():
        return None
    return build_bwrap_argv(command, workspace_root, allow_network=allow_network,
                            unshare_user=bwrap_userns_ok(),
                            mask_paths=default_secret_masks())


def run_command(
    command: str,
    *,
    workspace_root: str | Path,
    timeout: float = 30.0,
    max_output_chars: int = 20_000,
    env: dict | None = None,
    sandbox: str = "auto",
    allow_network: bool = False,
    allow_confinement_escape: bool = False,
) -> CommandResult:
    """Execute an ALREADY-APPROVED command, bounded + confined.

    Refuses to run a BLOCKED command (defense in depth -- callers must classify
    and approve first, but we re-check here). Confines cwd to workspace_root,
    enforces a hard timeout, and caps captured output.

    `sandbox`: "auto" = use bubblewrap OS-level isolation when available (falls
    back to plain confinement otherwise); "off" = never sandbox; "require" =
    error if bwrap is unavailable. `allow_network` opens the net namespace.
    """
    plan = classify_command(command, workspace_root=workspace_root)
    if plan.blocked and not (allow_confinement_escape and is_confinement_block(plan)):
        raise ValueError(f"refusing to run blocked command: {plan.reason}")

    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace_root is not a directory: {root}")

    use_bwrap = sandbox != "off" and bwrap_available()
    use_seatbelt = sandbox != "off" and not use_bwrap and seatbelt_available()
    if sandbox == "require" and not (bwrap_available() or seatbelt_available()):
        raise ValueError("sandbox=require but no OS sandbox (bubblewrap/Seatbelt) is available")
    # HIGH-2: 'auto' silently falls back to an UNSANDBOXED shell when no OS sandbox is
    # installed — the user thinks they're protected but a model-run command then has full
    # host access (writes anywhere, reads secrets, network). Make that LOUD (once) so it's
    # an informed choice, not a silent downgrade. `sandbox="require"` fails closed instead.
    if sandbox == "auto" and not use_bwrap and not use_seatbelt:
        _warn_no_sandbox_once()

    timed_out = False
    try:
        if use_bwrap:
            masks = default_secret_masks()
            argv = build_bwrap_argv(command, root, allow_network=allow_network,
                                    mask_paths=masks)
            # F39: NOTE — on the bwrap path the child's environment is rebuilt from scratch
            # inside build_bwrap_argv (--clearenv + explicit --setenv), so this `env=` does NOT
            # propagate caller-supplied custom vars into the sandbox (they DO propagate on the
            # seatbelt/no-bwrap paths). This is intentional isolation: an arbitrary caller env
            # must not leak into the confined process. Callers needing a var inside the sandbox
            # should extend build_bwrap_argv's --setenv allowlist, not rely on `env=` here.
            proc = subprocess.run(argv, cwd=str(root), capture_output=True,
                                  text=True, timeout=timeout, env=env)
            # Some kernels/containers block nested user namespaces -> bwrap fails
            # to set up. Detect that and retry once without --unshare-user (the
            # other isolations still apply). Heuristic: bwrap's own setup error.
            if proc.returncode != 0 and "user namespace" in (proc.stderr or "").lower():
                argv = build_bwrap_argv(command, root, allow_network=allow_network,
                                        unshare_user=False, mask_paths=masks)
                proc = subprocess.run(argv, cwd=str(root), capture_output=True,
                                      text=True, timeout=timeout, env=env)
            if proc.returncode != 0 and sandbox == "auto" and _bwrap_setup_failed(proc.stderr or ""):
                _warn_no_sandbox_once()
                proc = subprocess.run(command, shell=True, cwd=str(root), capture_output=True,
                                      text=True, timeout=timeout, env=_with_git_hardening(env))
        elif use_seatbelt:
            # macOS: Seatbelt (sandbox-exec) — same confinement contract as bwrap.
            argv = build_seatbelt_argv(command, root, allow_network=allow_network,
                                       mask_paths=default_secret_masks())
            proc = subprocess.run(argv, cwd=str(root), capture_output=True,
                                  text=True, timeout=timeout, env=_with_git_hardening(env))
        else:
            proc = subprocess.run(
                command, shell=True, cwd=str(root), capture_output=True,
                text=True, timeout=timeout, env=_with_git_hardening(env),
            )
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        code = -1

    truncated = False
    if len(out) > max_output_chars:
        out = out[:max_output_chars] + "\n[...output truncated]"
        truncated = True
    if len(err) > max_output_chars:
        err = err[:max_output_chars] + "\n[...output truncated]"
        truncated = True

    return CommandResult(
        command=command, exit_code=code, stdout=out, stderr=err,
        timed_out=timed_out, truncated=truncated,
        sandboxed=bool(use_bwrap or use_seatbelt),   # False = ran on the bare host (Gap 2)
    )
