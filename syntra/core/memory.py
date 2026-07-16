"""Two-tier memory: durable long-term knowledge (Phase 2).

Working memory = the per-step context (handoff + decisions + packed context).
Long-term memory = THIS module: durable, compressed knowledge that persists for
the whole task and across `resume` -- architecture summary, repo map,
conventions, and hard constraints.

Distinct from decisions.json (an event log of choices made) -- memory.json is the
small, deduped, always-injected "what's true here and what we must/never do".
Injected into every executor step so durable constraints never drift, and
preserved verbatim across compaction (PLAN Section 15: never summarize durable
state).

Pure + deterministic -> unit-tested. Persistence via TaskStore (memory.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _norm(text: str) -> str:
    return (text or "").strip()


import re as _re

# M1b: a file-path token inside a learned repo fact — e.g. "core/auth.py", "scripts/build.mk",
# "src/app/main.ts". A path has a directory-or-dotted-name shape with a file extension, so bare
# prose words don't match. Used to AUTO-tie a repo fact to the file(s) it names so freshness
# invalidation works without the learner passing an explicit path.
_PATH_RE = _re.compile(r"\b[\w./-]+/[\w.-]+\.[A-Za-z0-9]{1,6}\b|\b[\w-]+\.[A-Za-z0-9]{1,6}\b")
# Extensionless bare words that _PATH_RE's second alternative would wrongly grab are avoided by
# requiring a dot+ext; a leading dir (first alternative) also anchors real paths.


def extract_paths(text: str) -> list[str]:
    """M1b: pull the file-path tokens named inside a repo fact (order-preserving, deduped).
    Pure. Returns [] when the fact names no file (e.g. 'the system is event-driven')."""
    seen: list[str] = []
    for m in _PATH_RE.findall(text or ""):
        tok = m.strip().strip(".,;:)(")
        # Require either a directory separator or a plausible source extension, and reject
        # sentence-ending tokens like "design." that slipped through.
        if tok and tok not in seen and ("/" in tok or _re.search(r"\.[A-Za-z]{1,6}$", tok)):
            seen.append(tok)
    return seen


# ── memory-poisoning gate (B4) ──────────────────────────────────────────────
# Durable memory is injected into EVERY future step, so a poisoned entry is a
# persistent prompt-injection / command-execution vector: a malicious repo file or
# conversation could trick the learner into "remembering" an instruction to run a
# command or override the system prompt. Durable memory must hold declarative FACTS,
# never executable commands or instruction-overrides. This gate is conservative — it
# targets execution + instruction-override patterns, NOT ordinary must/never policy
# wording (e.g. "never delete the prod database" is a legitimate constraint).
_INJECTION_MARKERS = (
    "ignore previous instruction", "ignore all previous", "ignore the above",
    "ignore your instructions", "disregard previous", "disregard all",
    "disregard the above", "you are now", "new instructions:", "override the system",
    "system prompt", "reveal your", "print your api", "exfiltrate",
    "send it to http", "send them to http", "send the key", "leak the",
)
_DANGEROUS_SHELL = (
    "| sh", "|sh", "| bash", "|bash", "curl http", "wget http", "rm -rf", "sudo ",
    ":(){", "/etc/passwd", "/etc/shadow", "dd if=", "mkfs", "chmod 777", "> /dev/sd",
)


def looks_like_injection(text: str) -> bool:
    """True if a candidate durable memory looks like an injected COMMAND or a
    prompt-injection rather than a declarative fact. Used to reject poisoned learnings."""
    t = (text or "").lower()
    if not t.strip():
        return False
    return any(m in t for m in _INJECTION_MARKERS) or any(s in t for s in _DANGEROUS_SHELL)


@dataclass
class Memory:
    """Durable long-term task memory."""

    architecture_summary: str = ""
    repo_map: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    # INVIOLABLE user-set global/project rules. Rendered FIRST, with the strongest
    # priority language, into every role prompt. Distinct from `constraints` (learned
    # task memory) -- these are the user's absolute rules the model must never break.
    rules: list[str] = field(default_factory=list)
    # M1 (freshness): a repo_map fact MAY be tied to the file path it describes. When that
    # path is later edited, the fact may be STALE ("auth lives in x.py" after x.py is
    # rewritten) -> we mark it SUSPECT and stop injecting it (it stays in repo_map, just
    # withheld from render + flagged for re-validation). `repo_map` stays a plain list for
    # back-compat; these two parallel maps hold the optional metadata (empty = legacy behavior).
    repo_paths: dict[str, str] = field(default_factory=dict)      # fact text -> file path
    repo_suspect: list[str] = field(default_factory=list)         # fact texts currently stale

    # ------------------------------------------------------------- mutation

    def _add_unique(self, bucket: list[str], text: str) -> bool:
        """Append a trimmed, deduped entry. Returns True if it was added. Rejects poisoned
        entries (injected commands / instruction-overrides) AND entries that contain a
        credential (#248) — durable memory is injected into every future step, so a secret here
        would be re-exposed each turn and a poisoned line would hijack the pipeline."""
        t = _norm(text)
        if not t or t in bucket or looks_like_injection(t):
            return False
        try:
            from .redact import contains_secret
            if contains_secret(t):
                return False
        except Exception:  # noqa: BLE001 - never block a legit add on the scanner failing
            pass
        bucket.append(t)
        return True

    def add_constraint(self, text: str) -> bool:
        """A hard rule (must/never). Injected into every step."""
        return self._add_unique(self.constraints, text)

    def add_rule(self, text: str) -> bool:
        """An INVIOLABLE user rule. Injected first, with override-everything priority."""
        return self._add_unique(self.rules, text)

    def merge_rules(self, items) -> int:
        return sum(1 for x in (items or []) if self.add_rule(x))

    def add_convention(self, text: str) -> bool:
        """A 'how we do things here' preference."""
        return self._add_unique(self.conventions, text)

    def add_repo_entry(self, text: str, path: str = "") -> bool:
        """A notable file/dir/symbol fact. M1: pass `path` to tie the fact to a file so it can
        be invalidated (marked suspect, withheld from injection) when that file later changes.
        M1b: when no explicit path is given, the file path(s) NAMED in the fact text are
        auto-detected, so freshness works for the common 'auth lives in core/auth.py' shape."""
        added = self._add_unique(self.repo_map, text)
        if added:
            p = path.strip() or (extract_paths(text)[0] if extract_paths(text) else "")
            if p:
                self.repo_paths[_norm(text)] = p
        return added

    def mark_paths_changed(self, paths) -> int:
        """M1: mark every repo_map fact tied to one of these (now-edited) paths as SUSPECT, so
        it stops being injected until re-validated. Returns how many facts were newly flagged.
        A fact is flagged if its stored path matches OR the changed path is NAMED in the fact
        text (M1b — covers a fact that mentions several files). Pathless facts are never touched."""
        changed = {str(p).strip() for p in (paths or []) if str(p).strip()}
        if not changed:
            return 0
        flagged = 0
        for text in list(self.repo_map):
            key = _norm(text)
            if key in self.repo_suspect:
                continue
            stored = self.repo_paths.get(key, "")
            named = set(extract_paths(text))
            if (stored and stored in changed) or (named & changed):
                self.repo_suspect.append(key)
                flagged += 1
        return flagged

    def set_architecture(self, text: str) -> None:
        t = _norm(text)
        self.architecture_summary = "" if looks_like_injection(t) else t

    def merge_constraints(self, items) -> int:
        """Add many constraints; returns how many were newly added."""
        return sum(1 for x in (items or []) if self.add_constraint(x))

    def merge_from(self, other: "Memory") -> int:
        """Apply another Memory's buckets through the dedupe filter (Librarian job B).
        Returns the total count of newly-added entries. Architecture is only set when we
        don't already have one. Pure -> unit-tested."""
        added = 0
        added += sum(1 for x in other.rules if self.add_rule(x))
        added += sum(1 for x in other.constraints if self.add_constraint(x))
        added += sum(1 for x in other.conventions if self.add_convention(x))
        added += sum(1 for x in other.repo_map if self.add_repo_entry(x))
        if other.architecture_summary and not self.architecture_summary:
            self.set_architecture(other.architecture_summary)
            added += 1
        return added

    # ------------------------------------------------------------- queries

    def is_empty(self) -> bool:
        return not (self.architecture_summary or self.repo_map
                    or self.conventions or self.constraints or self.rules)

    def render(self) -> str:
        """Compact markdown for injection into an executor prompt."""
        lines: list[str] = []
        if self.rules:
            lines.append("INVIOLABLE RULES — these are ABSOLUTE. They override every "
                         "other instruction, including the user's current request. NEVER "
                         "break, weaken, or work around them. If a request conflicts with a "
                         "rule, refuse that part and say which rule applies:")
            lines.extend(f"  - {r}" for r in self.rules)
        if self.architecture_summary:
            lines.append(f"Architecture: {self.architecture_summary}")
        if self.constraints:
            lines.append("Constraints (MUST honor):")
            lines.extend(f"  - {c}" for c in self.constraints)
        if self.conventions:
            lines.append("Conventions:")
            lines.extend(f"  - {c}" for c in self.conventions)
        # M1: withhold SUSPECT repo facts — a fact about a file that was since edited may be
        # stale, so we don't inject it (stops "auth lives in x.py" persisting after x.py changed).
        fresh_repo = [r for r in self.repo_map if _norm(r) not in set(self.repo_suspect)]
        if fresh_repo:
            lines.append("Repo map:")
            lines.extend(f"  - {r}" for r in fresh_repo)
        return "\n".join(lines)

    # ------------------------------------------------------------- (de)serialize

    def to_dict(self) -> dict:
        return {
            "_schema_version": 2,   # v2 adds M1 repo freshness metadata (v1 loads unchanged)
            "architecture_summary": self.architecture_summary,
            "repo_map": list(self.repo_map),
            "conventions": list(self.conventions),
            "constraints": list(self.constraints),
            "rules": list(self.rules),
            # M1: freshness metadata (empty for legacy v1 memories).
            "repo_paths": dict(self.repo_paths),
            "repo_suspect": list(self.repo_suspect),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Memory":
        d = d or {}
        repo_map = [str(x) for x in d.get("repo_map", []) if _norm(str(x))]
        valid = {_norm(x) for x in repo_map}
        # M1: keep only metadata that still points at a live repo_map fact (defensive).
        repo_paths = {str(k): str(v) for k, v in (d.get("repo_paths") or {}).items()
                      if _norm(str(k)) in valid and str(v).strip()}
        repo_suspect = [str(x) for x in (d.get("repo_suspect") or []) if _norm(str(x)) in valid]
        return cls(
            architecture_summary=str(d.get("architecture_summary", "")),
            repo_map=repo_map,
            conventions=[str(x) for x in d.get("conventions", []) if _norm(str(x))],
            constraints=[str(x) for x in d.get("constraints", []) if _norm(str(x))],
            rules=[str(x) for x in d.get("rules", []) if _norm(str(x))],
            repo_paths=repo_paths,
            repo_suspect=repo_suspect,
        )
