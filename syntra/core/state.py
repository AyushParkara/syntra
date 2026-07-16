"""Structured task state.

This is the core anti-compaction primitive. Active task knowledge lives
in typed files, NOT in the chat history. Chat history is disposable; this is not.

Files written per task:
    .syntra/tasks/<task-id>/task.json        # goal, status, created
    .syntra/tasks/<task-id>/plan.json        # planner output, steps
    .syntra/tasks/<task-id>/decisions.json   # durable architectural choices
    .syntra/tasks/<task-id>/failures.json    # attempts that didn't work + why
    .syntra/tasks/<task-id>/summary.json     # compressed running summary
    .syntra/tasks/<task-id>/cost.json        # token + $ accounting
    .syntra/tasks/<task-id>/events.jsonl     # append-only audit log
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PlanStep:
    id: str
    description: str
    role: str  # "executor" usually; could be "planner"/"reviewer" for sub-tasks
    status: str = "pending"  # pending | running | done | failed | skipped
    result: str = ""
    failure_reason: str = ""
    # T7: must|nice — the planner tags each step's importance. The FINAL whole-deliverable
    # gate blocks only on unmet MUST steps; unmet NICE steps become "known limitations" and
    # don't block shipping (matches "a project is never 100% complete, like humans").
    # Default "must" so untagged/legacy plans behave exactly as before (all-blocking).
    priority: str = "must"
    # T6: ids of the prior steps THIS step depends on. The executor prompt then inlines ONLY
    # those steps' results (not every prior step), so per-step context stays bounded as the
    # plan grows. Empty = the planner didn't declare deps → fall back to the immediately-
    # preceding step's result (a safe default that preserves the old "build on prior" behavior).
    deps: list = field(default_factory=list)
    # Per-step capability fingerprint (roadmap #10). Empty = fall back to whole-goal
    # routing (legacy). The planner stamps these; the router routes each step on its own.
    axis: str = ""          # primary CAPABILITY_AXIS: reasoning|code|tool_use|long_context|instruction
    difficulty: str = ""    # simple|medium|complex
    criticality: str = ""   # low|medium|high


@dataclass
class Decision:
    id: str
    description: str
    rationale: str
    timestamp: float


@dataclass
class Failure:
    step_id: str
    attempt: int
    reason: str
    timestamp: float
    artifact_path: str = ""
    # Reflexion: a short model-generated post-mortem (root cause + what to change),
    # fed into the NEXT attempt's prompt so the retry learns instead of repeating.
    reflection: str = ""


@dataclass
class CostEntry:
    role: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: float


@dataclass
class TaskState:
    """Mutable in-memory task state, mirrors files on disk."""

    task_id: str
    goal: str
    workspace_root: str
    state_root: str
    title: str = ""          # user-set session title (falls back to goal when empty)
    status: str = "pending"  # pending | running | done | failed | paused
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    plan: list[PlanStep] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    summary: str = ""
    costs: list[CostEntry] = field(default_factory=list)
    # Runtime-only (NOT serialized): path to the written plan markdown file, so the TUI can
    # surface the plan as a click-to-expand card naming a real file. Set by Loop._write_plan_file.
    plan_file: str = ""

    @property
    def task_dir(self) -> Path:
        return Path(self.state_root) / "tasks" / self.task_id

    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.costs), 6)

    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.input_tokens for c in self.costs),
            sum(c.output_tokens for c in self.costs),
        )


class TaskStore:
    """Read/write structured task state.

    Each writer is a single small JSON file with a single concern. Never
    blob everything into one document - that's how compaction kills you.
    """

    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root)
        (self.state_root / "tasks").mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_id: str) -> Path:
        tid = str(task_id or "")
        if not _TASK_ID_RE.fullmatch(tid):
            raise ValueError(f"invalid task id: {task_id!r}")
        base = (self.state_root / "tasks").resolve()
        d = (base / tid).resolve()
        if d != base and base not in d.parents:
            raise ValueError(f"task id escapes state root: {task_id!r}")
        return d

    def new_task(self, goal: str, workspace_root: Path | str) -> TaskState:
        task_id = _short_id()
        state = TaskState(
            task_id=task_id,
            goal=goal,
            workspace_root=str(Path(workspace_root).resolve()),
            state_root=str(self.state_root.resolve()),
        )
        state.task_dir.mkdir(parents=True, exist_ok=True)
        self.save(state)
        self.event(state, "task_created", {"goal": goal})
        return state

    def save(self, state: TaskState) -> None:
        state.updated = time.time()
        d = state.task_dir
        # Preserve flags written out-of-band (e.g. `archived`, `branched_from`,
        # and `title` set via set_title) — save() must not clobber them.
        _prior = _read(d / "task.json", default={}) or {}
        _doc = {
            "task_id": state.task_id,
            "goal": state.goal,
            "workspace_root": state.workspace_root,
            "state_root": state.state_root,
            "title": state.title or _prior.get("title", ""),
            "status": state.status,
            "created": state.created,
            "updated": state.updated,
        }
        # Preserve out-of-band flags + the #208 cached turn counter (append_rollout
        # writes it between save()s — save() must not wipe it).
        for _k in ("archived", "branched_from", "rollout_turns"):
            if _k in _prior and _k not in _doc:
                _doc[_k] = _prior[_k]
        _write(d / "task.json", _doc)
        _write(d / "plan.json", [asdict(s) for s in state.plan])
        _write(d / "decisions.json", [asdict(x) for x in state.decisions])
        _write(d / "failures.json", [asdict(x) for x in state.failures])
        _write(d / "summary.json", {"summary": state.summary})
        _write(d / "cost.json", {
            "total_usd": state.total_cost_usd(),
            "total_input_tokens": state.total_tokens()[0],
            "total_output_tokens": state.total_tokens()[1],
            "entries": [asdict(c) for c in state.costs],
        })

    def load(self, task_id: str) -> TaskState:
        d = self._task_dir(task_id)
        task = _read(d / "task.json")
        plan = [PlanStep(**row) for row in _read(d / "plan.json", default=[])]
        decisions = [Decision(**row) for row in _read(d / "decisions.json", default=[])]
        failures = [Failure(**row) for row in _read(d / "failures.json", default=[])]
        summary_doc = _read(d / "summary.json", default={"summary": ""})
        cost_doc = _read(d / "cost.json", default={"entries": []})
        return TaskState(
            task_id=task["task_id"],
            goal=task["goal"],
            workspace_root=task["workspace_root"],
            state_root=task["state_root"],
            title=task.get("title", ""),
            status=task.get("status", "pending"),
            created=task.get("created", time.time()),
            updated=task.get("updated", time.time()),
            plan=plan,
            decisions=decisions,
            failures=failures,
            summary=summary_doc.get("summary", ""),
            costs=[CostEntry(**c) for c in cost_doc.get("entries", [])],
        )

    def task_cost(self, task_id: str) -> float:
        """Total USD spent on one task — a cheap read of its cost.json `total_usd` (no full
        task load). 0.0 for an unknown task or a task with no recorded cost. Never raises.
        Restores the per-task cost column /history used to show."""
        try:
            p = self._task_dir(task_id) / "cost.json"
        except ValueError:
            return 0.0
        doc = _read(p, default={}) or {}
        try:
            return float(doc.get("total_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def list_tasks(self, include_archived: bool = False) -> list[dict]:
        tasks_dir = self.state_root / "tasks"
        if not tasks_dir.exists():
            return []
        rows = []
        for d in sorted(tasks_dir.iterdir()):
            f = d / "task.json"
            if f.exists():
                meta = _read(f) or {}
                if not include_archived and meta.get("archived"):
                    continue
                rows.append(meta)
        return rows

    def set_archived(self, task_id: str, archived: bool = True) -> bool:
        """Hide/unhide a session from the default listings WITHOUT deleting it (sets an
        ``archived`` flag in task.json). Returns True if the task existed."""
        try:
            meta_path = self._task_dir(task_id) / "task.json"
        except ValueError:
            return False
        if not meta_path.exists():
            return False
        meta = _read(meta_path, default={}) or {}
        meta["archived"] = bool(archived)
        _write(meta_path, meta)
        return True

    def set_title(self, task_id: str, title: str) -> bool:
        """Give a session a human-readable title (shown in the resume/fork pickers and
        the header) WITHOUT touching its goal. Writes ``title`` into task.json in place;
        an empty/blank title clears it (falls back to the goal). Returns True if the task
        existed. The title is truncated to a sane length so it never breaks the picker."""
        try:
            meta_path = self._task_dir(task_id) / "task.json"
        except ValueError:
            return False
        if not meta_path.exists():
            return False
        meta = _read(meta_path, default={}) or {}
        meta["title"] = str(title or "").strip()[:120]
        _write(meta_path, meta)
        return True

    def delete_task(self, task_id: str) -> bool:
        """Permanently delete a session's on-disk state (its whole task dir). Returns
        True if it existed."""
        import shutil
        try:
            d = self._task_dir(task_id)
        except ValueError:
            return False
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    def fork_task(self, task_id: str, *, at_event: int | None = None) -> TaskState:
        """Fork (branch) a new task from a prior task's rollout timeline.

        A thin wrapper over ``core.rollout.branch`` so callers don't need to know where the
        state root lives. ``at_event`` keeps events [0:N] (re-run from that point); None keeps
        the whole timeline."""
        from . import rollout
        self._task_dir(task_id)
        at = int(at_event) if at_event is not None else 10**9
        new_id = rollout.branch(self.state_root, str(task_id), at=at)
        return self.load(new_id)

    def save_loop_ledger(self, state: TaskState, ledger: dict[str, Any]) -> None:
        """Persist the LoopGuard ledger to ``loop.json`` (one concern, one file)."""
        _write(state.task_dir / "loop.json", ledger)

    def load_loop_ledger(self, task_id: str) -> dict[str, Any]:
        """Read ``loop.json`` for a task; empty dict if absent."""
        try:
            return _read(self._task_dir(task_id) / "loop.json", default={})
        except ValueError:
            return {}

    def append_verification(self, state: TaskState, report: dict[str, Any]) -> None:
        """Append one verification report to ``verification.json`` (a list)."""
        path = state.task_dir / "verification.json"
        existing = _read(path, default=[])
        if not isinstance(existing, list):
            existing = []
        existing.append(report)
        _write(path, existing)

    def load_verification(self, task_id: str) -> list[dict[str, Any]]:
        """Read all verification reports for a task; empty list if absent."""
        try:
            doc = _read(self._task_dir(task_id) / "verification.json", default=[])
        except ValueError:
            return []
        return doc if isinstance(doc, list) else []

    def write_handoff(self, state: TaskState, text: str) -> Path:
        """Write the continuity handoff (markdown) to ``handoff.md``."""
        path = state.task_dir / "handoff.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return path

    def read_handoff(self, task_id: str) -> str:
        """Read ``handoff.md`` for a task; empty string if absent."""
        try:
            path = self._task_dir(task_id) / "handoff.md"
        except ValueError:
            return ""
        return path.read_text() if path.exists() else ""

    def write_receipt(self, state: TaskState, text: str) -> Path:
        """M3: write the human/agent-readable per-run summary to ``receipt.md`` (atomic)."""
        path = state.task_dir / "receipt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return path

    def read_receipt(self, task_id: str) -> str:
        """M3: read ``receipt.md`` for a task; empty string if absent."""
        try:
            path = self._task_dir(task_id) / "receipt.md"
        except ValueError:
            return ""
        return path.read_text() if path.exists() else ""

    def write_context_pack(self, state: TaskState, step_id: str, pack: dict[str, Any]) -> Path:
        """Persist a step's context pack to ``context_packs/<step_id>.json``."""
        path = state.task_dir / "context_packs" / f"{step_id}.json"
        _write(path, pack)
        return path

    def load_context_pack(self, task_id: str, step_id: str) -> dict[str, Any]:
        """Read a step's context pack; empty dict if absent."""
        try:
            path = self._task_dir(task_id) / "context_packs" / f"{step_id}.json"
        except ValueError:
            return {}
        return _read(path, default={})

    def write_memory(self, state: TaskState, memory: dict[str, Any]) -> Path:
        """Persist durable long-term memory to ``memory.json``."""
        path = state.task_dir / "memory.json"
        _write(path, memory)
        return path

    def load_memory(self, task_id: str) -> dict[str, Any]:
        """Read ``memory.json`` for a task; empty dict if absent."""
        try:
            return _read(self._task_dir(task_id) / "memory.json", default={})
        except ValueError:
            return {}

    def append_execution_log(self, state: TaskState, entry: dict[str, Any]) -> None:
        """Append one execution entry (edit/command/test) to ``execution_log.json``."""
        path = state.task_dir / "execution_log.json"
        existing = _read(path, default=[])
        if not isinstance(existing, list):
            existing = []
        entry = {"ts": time.time(), **entry}
        existing.append(entry)
        _write(path, existing)

    def load_execution_log(self, task_id: str) -> list[dict[str, Any]]:
        """Read all execution-log entries; empty list if absent."""
        try:
            doc = _read(self._task_dir(task_id) / "execution_log.json", default=[])
        except ValueError:
            return []
        return doc if isinstance(doc, list) else []

    def write_completion_audit(self, state: TaskState, audit: dict[str, Any]) -> Path:
        """Persist the completion audit to ``completion.json``."""
        path = state.task_dir / "completion.json"
        _write(path, audit)
        return path

    def load_completion_audit(self, task_id: str) -> dict[str, Any]:
        """Read ``completion.json`` for a task; empty dict if absent."""
        try:
            return _read(self._task_dir(task_id) / "completion.json", default={})
        except ValueError:
            return {}

    def event(self, state: TaskState, kind: str, payload: dict[str, Any]) -> None:
        line = json.dumps({
            "ts": time.time(),
            "kind": kind,
            "payload": payload,
        }) + "\n"
        with (state.task_dir / "events.jsonl").open("a") as f:
            f.write(line)

    # ── rollout: the append-only, replayable turn stream ──────────────────────
    # ``events.jsonl`` is an audit log of {ts, kind, payload}. This is the ordered
    # CONVERSATION itself — one record per line — so a session can be replayed,
    # forked, or backtracked turn-by-turn without ever rewriting history in place.
    # A record is a plain dict; by convention it carries {ts, role, text} for a
    # turn, but any JSON-serialisable shape is accepted.

    def _rollout_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "rollout.jsonl"

    def append_rollout(self, task_id: str, record: dict[str, Any]) -> None:
        """Append ONE record as a line of JSON. Append-only: a past turn is never
        edited. A timestamp is stamped in when the caller didn't supply one.

        #208: keeps a cached user/assistant turn count in task.json (``rollout_turns``)
        so :meth:`recent_sessions` can list without reading each full rollout."""
        path = self._rollout_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = record if "ts" in record else {"ts": time.time(), **record}
        with path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if rec.get("role") in ("user", "assistant"):
            self._bump_rollout_turns(task_id, 1)

    @staticmethod
    def _count_turns(records: list[dict[str, Any]]) -> int:
        return sum(1 for r in records if r.get("role") in ("user", "assistant"))

    def _bump_rollout_turns(self, task_id: str, delta: int) -> None:
        """Adjust the cached ``rollout_turns`` counter in task.json by ``delta``.
        Best-effort: a listing-perf cache must never break an append."""
        try:
            meta_path = self._task_dir(task_id) / "task.json"
        except ValueError:
            return
        try:
            meta = _read(meta_path, default={}) or {}
            meta["rollout_turns"] = max(0, int(meta.get("rollout_turns", 0)) + int(delta))
            _write(meta_path, meta)
        except (OSError, ValueError):
            pass

    def _set_rollout_turns(self, task_id: str, value: int) -> None:
        """Set the cached ``rollout_turns`` to an exact value (for whole-rewrite paths)."""
        try:
            meta_path = self._task_dir(task_id) / "task.json"
        except ValueError:
            return
        try:
            meta = _read(meta_path, default={}) or {}
            meta["rollout_turns"] = max(0, int(value))
            _write(meta_path, meta)
        except (OSError, ValueError):
            pass

    def save_rollout(self, task_id: str, records: list[dict[str, Any]]) -> None:
        """Replace the whole rollout with an ordered list of records, atomically.
        Snapshots the full conversation in one shot — re-writing the same stream
        never duplicates lines (unlike repeated :meth:`append_rollout`)."""
        path = self._rollout_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(body)
        tmp.replace(path)
        # #208: keep the cached turn count exact after a whole-stream rewrite/fork.
        self._set_rollout_turns(task_id, self._count_turns(records))

    def load_rollout(self, task_id: str) -> list[dict[str, Any]]:
        """Read the rollout in order. A corrupt/half-written line is skipped rather
        than crashing replay — the file is append-only, so only the tail can tear."""
        path = self._rollout_path(task_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def backtrack_branch(self, task_id: str, turn_index: int,
                         edited_text: str | None = None) -> str:
        """Fork a NEW task from ``task_id`` at ``turn_index``, optionally rewriting
        that turn's text — the primitive behind "edit a past message and re-run from
        there". History is NEVER mutated in place: the source rollout is left
        byte-for-byte unchanged and a fresh task id owns the truncated (+edited)
        branch. Returns the new task id.

        The record at ``turn_index`` is kept (its text replaced when ``edited_text``
        is given) and everything after it is dropped. ``turn_index`` is clamped to
        the rollout's bounds.
        """
        src = self.load_rollout(task_id)
        n = len(src)
        if n == 0:
            kept: list[dict[str, Any]] = []
            idx = 0
        else:
            idx = max(0, min(int(turn_index), n - 1))
            kept = [dict(r) for r in src[: idx + 1]]
            if edited_text is not None:
                kept[-1] = {**kept[-1], "text": edited_text}
        try:
            src_meta = _read(self._task_dir(task_id) / "task.json", default={}) or {}
        except ValueError:
            src_meta = {}
        # the branch's goal = the last (edited) user turn we re-run from, else the source goal
        branch_goal = ""
        for r in reversed(kept):
            if r.get("role") == "user" and (r.get("text") or "").strip():
                branch_goal = r["text"]
                break
        if not branch_goal:
            branch_goal = str(src_meta.get("goal", ""))
        new_id = _short_id()
        new_dir = self.state_root / "tasks" / new_id
        new_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        _write(new_dir / "task.json", {
            "task_id": new_id,
            "goal": branch_goal,
            "workspace_root": str(src_meta.get("workspace_root", "")),
            "state_root": str(self.state_root.resolve()),
            "status": "branched",
            "created": now,
            "updated": now,
            "branched_from": task_id,
            "branched_at": idx,
        })
        self.save_rollout(new_id, kept)
        # audit trail lives on the NEW task — the source is never touched
        with (new_dir / "events.jsonl").open("a") as f:
            f.write(json.dumps({"ts": now, "kind": "branch_created",
                                "payload": {"from": task_id, "at": idx,
                                            "edited": edited_text is not None}}) + "\n")
        return new_id

    def recent_sessions(self, limit: int = 20, include_archived: bool = False) -> list[dict[str, Any]]:
        """Summaries of the most recently-touched sessions, newest first — the data
        behind the resume/fork pickers. Each row: {id, goal, turns, updated, status,
        branched_from}. ``turns`` counts user/assistant records in the rollout
        (falling back to plan length when a task predates rollouts). Archived sessions
        are hidden unless ``include_archived``."""
        tasks_dir = self.state_root / "tasks"
        if not tasks_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for d in tasks_dir.iterdir():
            meta_path = d / "task.json"
            if not meta_path.exists():
                continue
            meta = _read(meta_path, default={}) or {}
            if not include_archived and meta.get("archived"):
                continue
            # #208: read the cached turn count (stamped by append_rollout/save_rollout)
            # so listing never loads each full rollout. Only legacy tasks that predate
            # the counter fall back to a one-time count (and get healed below).
            if "rollout_turns" in meta:
                turns = int(meta.get("rollout_turns", 0) or 0)
            else:
                turns = self._count_turns(self.load_rollout(d.name))
                self._set_rollout_turns(d.name, turns)  # heal: cache it for next time
            rows.append({
                "id": d.name,
                "goal": str(meta.get("goal", "")),
                "title": str(meta.get("title", "")),
                "turns": turns,
                "updated": float(meta.get("updated", 0.0) or 0.0),
                "status": str(meta.get("status", "")),
                "branched_from": str(meta.get("branched_from", "")),
            })
        rows.sort(key=lambda r: r["updated"], reverse=True)
        return rows[: max(0, int(limit))]

    def branched_from(self, task_id: str) -> str:
        """The session id this one was forked/backtracked FROM, or '' for an original.
        Lightweight single-file read (no rollout scan) — used for the 'forked from'
        banner when a branched session is reopened (A2)."""
        try:
            meta = _read(self._task_dir(task_id) / "task.json", default={}) or {}
        except ValueError:
            return ""
        return str(meta.get("branched_from", "") or "")


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _read(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        if default is not None:
            return default
        raise
