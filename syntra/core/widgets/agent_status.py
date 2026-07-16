"""Agent status widget — role agents + a focusable multi-agent view (P28).

Displays planner/executor/reviewer (and sub-agents) with status idle/running/
done. Click an agent to FOCUS it (a detail pane with its model, cost, and current
task); the focused row is bold; click it again, press 'b'/← or Esc to go back.
"""

from __future__ import annotations

from typing import ClassVar

from ..widget import Widget, RenderLine
from ..tui_model import BRAND_MARK, abbrev_count


class AgentStatusWidget(Widget):
    kind = "agent_status"
    focusable = True

    _ICONS: ClassVar[dict[str, str]] = {"idle": "○", "running": BRAND_MARK, "waiting": "◐", "blocked": "✗", "done": "✓"}
    _STYLES: ClassVar[dict[str, str]] = {"idle": "dim", "running": "accent", "waiting": "string",
               "blocked": "diff_del", "done": "diff_add"}

    # The analyzer→planner→executor→reviewer pipeline (and the librarian) are PHASES of one
    # flow, NOT agents — a solo message spawns no parallel agents, so the panel must stay empty
    # for it. These role names are ignored by the panel; only real FAN-OUT workers (sub·/scout·/
    # plan·/compare·/review·/agent·/job· — note the "·") ever register here. (The live phase is
    # shown in the chat working line, not the AGENTS panel.)
    _PIPELINE_ROLES = frozenset({"analyzer", "planner", "executor", "reviewer",
                                 "librarian", "main", "pipeline", "direct"})

    @classmethod
    def _is_pipeline_role(cls, role: str) -> bool:
        """A non-agent pipeline PHASE (kept out of the panel) — anything in the fixed set and
        WITHOUT a worker lane marker ('·'). Workers like 'sub·1' / 'plan·grok' always pass."""
        return role in cls._PIPELINE_ROLES and "·" not in role

    def __init__(self, *, title: str = "AGENTS", on_event=None):
        super().__init__(title=title, on_event=on_event)
        # Start EMPTY — no pre-seeded planner/executor/reviewer rows. The pipeline is phases,
        # not agents; rows appear only when a real fan-out worker registers.
        self.agents: dict[str, dict] = {}
        self.focused_agent: str | None = None   # set by a click -> detail view
        self._row_to_agent: dict = {}            # render-row -> role (for clicks)
        self._cursor: int = 0                    # ↑/↓ keyboard selection (list view)
        self._focus_scroll: int = 0              # ↑/↓ scroll of the focused agent's work

    def set_status(self, role: str, status: str, model: str = "", cost: float = 0.0,
                   task: str = "") -> None:
        # Pipeline phases are not agents — ignore them so the panel only ever shows real
        # fan-out workers (this guards BOTH the structured feed and the trace-line parser).
        if self._is_pipeline_role(role):
            return
        if role not in self.agents:
            self.agents[role] = {"status": "idle", "model": "", "cost": 0.0, "task": ""}
        a = self.agents[role]
        a["status"] = status
        # per-agent wall-clock: start on first 'running', freeze on 'done'
        if status == "running" and not a.get("_started"):
            a["_started"] = _now()
        elif status == "done" and a.get("_started") and not a.get("_ended"):
            a["_ended"] = _now()
        if model:
            a["model"] = model
        if cost > 0:
            a["cost"] = cost
        if task:
            a["task"] = task

    def running_count(self) -> int:
        """How many agents are currently running (for the live 'N agents' cue, #15)."""
        return sum(1 for a in self.agents.values() if a.get("status") == "running")

    def tool_total(self) -> int:
        """Total tool calls across all agents (live 'N tools' cue, #15)."""
        return sum(int(a.get("tools") or 0) for a in self.agents.values())

    def feed(self, kind: str, payload: dict | None = None) -> None:
        """Clean event API: agent_start / agent_done / phase."""
        p = payload or {}
        role = str(p.get("role") or p.get("agent") or "")
        if not role:
            return
        # Pipeline phases are not panel agents — drop them up front so neither agent_activity
        # nor agent_output can lazily create a phantom row (and so the panel stays empty for a
        # solo run). Real fan-out workers (with a '·' lane marker) always pass.
        if self._is_pipeline_role(role):
            return
        if kind in ("agent_start", "phase"):
            self.set_status(role, "running", model=str(p.get("model", "")),
                            task=str(p.get("task", p.get("description", ""))))
        elif kind == "agent_done":
            self.set_status(role, "done", cost=float(p.get("cost", 0.0) or 0.0))
        elif kind == "agent_activity":
            # per-agent tool-count + tokens + live activity ("· N tools · N tok · <activity>").
            # MERGE only the fields present -- token updates (from _record_cost) and tool/activity
            # updates (from tool_call) arrive on SEPARATE events; neither must clobber the other.
            if role not in self.agents:
                self.set_status(role, "running")
            a = self.agents[role]
            if not a.get("_started"):
                a["_started"] = _now()
            if a.get("status") != "done":
                a["status"] = "running"
            if "tools" in p:
                a["tools"] = int(p.get("tools") or 0)
            if "activity" in p:
                a["activity"] = str(p.get("activity", ""))
                # ordered action TRAIL (not just the latest blurt): append each new activity,
                # de-duping immediate repeats, so the drill-down shows the SEQUENCE of work.
                _act = str(p.get("activity", "")).strip()
                if _act:
                    _trail = a.setdefault("trail", [])
                    if not _trail or _trail[-1] != _act:
                        _trail.append(_act)
                        a["trail"] = _trail[-200:]      # bounded but generous
            if "tokens" in p:
                a["agent_tokens"] = int(p.get("tokens") or 0)
            # per-TOOL breakdown (3 Bash · 2 Edit) — only when the engine supplies a tool_name;
            # absent today, so this is a graceful no-op until the engine emits it (NEEDS-ENGINE).
            if "tool_name" in p:
                _tn = str(p.get("tool_name") or "")
                if _tn:
                    _byname = a.setdefault("tool_by_name", {})
                    _byname[_tn] = _byname.get(_tn, 0) + 1
            # REAL in/out tokens + live cost (vs the chars÷4 estimate). Guarded — fall back to
            # the estimate when the engine doesn't supply these yet (NEEDS-ENGINE).
            if "tokens_in" in p:
                a["tokens_in"] = int(p.get("tokens_in") or 0)
            if "tokens_out" in p:
                a["tokens_out"] = int(p.get("tokens_out") or 0)
            if "cost_usd" in p:
                a["cost_live"] = float(p.get("cost_usd") or 0.0)
        elif kind == "agent_output":
            # WHAT the agent produced — its work, shown in the drill-down (F20/F21).
            # Accumulate across steps (executor runs many) so you see the FULL picture —
            # no truncation, so long runs don't lose their early work (the drill-down scrolls).
            if role not in self.agents:
                self.set_status(role, "running")
            a = self.agents[role]
            prev = a.get("output", "")
            new = str(p.get("text", "")).strip()
            a["output"] = (prev + "\n\n" + new).strip() if prev else new

    def reset(self) -> None:
        # Fresh panel per run: clear ALL worker rows (the panel is empty until a real fan-out
        # registers). The pipeline never seeds rows, so there's nothing to keep idle.
        self.agents = {}
        self.focused_agent = None
        self._cursor = 0

    def finalize_running(self) -> None:
        """Flip every still-'running' agent to 'done' and FREEZE its clock. Called when a
        run finishes OR is interrupted — otherwise an agent whose agent_done was lost (e.g.
        the librarian when the run is hard-stopped) shows as 'running' with an elapsed timer
        that ticks forever. (Not actually working — just an unfinalised panel row.)"""
        for a in self.agents.values():
            if a.get("status") == "running":
                a["status"] = "done"
                if a.get("_started") and not a.get("_ended"):
                    a["_ended"] = _now()
                a["activity"] = ""

    def back(self) -> bool:
        if self.focused_agent is not None:
            self.focused_agent = None
            return True
        return False

    def handle_key(self, ch: int, meta: dict | None = None) -> bool:
        import curses
        roles = list(self.agents.keys())
        if self.focused_agent is not None:                       # detail view (an agent's work)
            if ch in (ord("b"), curses.KEY_LEFT):                # back to the list
                self.focused_agent = None
                self._focus_scroll = 0
                return True
            if ch in (ord("k"), ord("K")):                       # #152/#235: stop the run
                # per-agent cancel isn't an engine primitive (SteeringInbox is per-RUN, not
                # per-sub-agent), so 'k' stops the whole run via the same hard_stop the TUI
                # uses elsewhere. The TUI receiver decides what "kill" does.
                self.emit("kill", self.focused_agent or "")
                return True
            if ch == curses.KEY_UP:                              # scroll the work up/down
                self._focus_scroll = max(0, self._focus_scroll - 1)
                return True
            if ch in (curses.KEY_DOWN, curses.KEY_NPAGE):
                self._focus_scroll += 1
                return True
            if ch == curses.KEY_PPAGE:
                self._focus_scroll = max(0, self._focus_scroll - 8)
                return True
            return False
        if not roles:
            return False
        if ch == curses.KEY_UP:                                  # list view: ↑/↓ move selection
            self._cursor = (self._cursor - 1) % len(roles)
            return True
        if ch == curses.KEY_DOWN:
            self._cursor = (self._cursor + 1) % len(roles)
            return True
        if ch in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13):   # →/Enter = focus -> see its work
            self._cursor = max(0, min(self._cursor, len(roles) - 1))
            self.focused_agent = roles[self._cursor]
            self._focus_scroll = 0
            return True
        return False

    def handle_mouse(self, x: int, y: int, button: int) -> bool:
        import curses
        b1 = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_PRESSED", 0)
        if not (button & b1):
            return False
        role = self._row_to_agent.get(y)
        if role:
            self.focused_agent = None if self.focused_agent == role else role
            self._focus_scroll = 0           # fresh scroll when opening an agent's work
            return True
        if self.focused_agent is not None:   # click elsewhere = back to the list
            self.focused_agent = None
            self._focus_scroll = 0
            return True
        return False

    def render(self, width: int, height: int) -> list[RenderLine]:
        w = max(5, int(width))
        out: list[RenderLine] = []
        self._row_to_agent = {}

        if self.focused_agent and self.focused_agent in self.agents:
            role = self.focused_agent
            info = self.agents[role]
            icon = self._ICONS.get(info["status"], "○")
            # compact header: ‹ icon ROLE · status · ↓in ↑out · $cost · elapsed
            bits = [info["status"]]
            _ti, _to = info.get("tokens_in"), info.get("tokens_out")
            if _ti or _to:                       # REAL in/out tokens (preferred)
                bits.append(f"↓{abbrev_count(_ti or 0)} ↑{abbrev_count(_to or 0)}")
            elif info.get("agent_tokens"):        # fall back to the running estimate
                bits.append(f"{abbrev_count(info['agent_tokens'])} tok")
            if info.get("cost_live"):            # live cost (preferred over end-of-run cost)
                bits.append(f"${info['cost_live']:.4f}")
            _el = _elapsed_str(info)
            if _el:
                bits.append(_el)
            out.append(RenderLine(f" ‹ {icon} {role.upper()}  · " + " · ".join(bits)[:w], "accent"))
            if info.get("model"):
                _m = info["model"].split("/")[-1]
                _m += f"  ${info['cost']:.4f}" if info.get("cost", 0) > 0 else ""
                out.append(RenderLine(f"   {_m}"[:w], "dim"))
            # per-tool breakdown (3 Bash · 2 Edit · …) when the engine supplied tool names
            _byname = info.get("tool_by_name") or {}
            if _byname:
                _tb = " · ".join(f"{n}×{c}" for n, c in list(_byname.items())[:5])
                out.append(RenderLine(f"   {_tb}"[:w], "dim"))
            # ── action TRAIL: the ordered sequence of what this agent did (newest last) ──
            _trail = info.get("trail") or []
            if _trail:
                out.append(RenderLine("   ─ doing ─"[:w], "dim"))
                for _ti, _t in enumerate(_trail[-6:]):
                    _is_last = (_ti == len(_trail[-6:]) - 1)
                    _mark = "●" if (_is_last and info["status"] == "running") else "✓"
                    out.append(RenderLine(f"   {_mark} {_t}"[:w], "dim"))
            elif info.get("activity") and info["status"] == "running":
                out.append(RenderLine(f"   ⎿ {info['activity']}"[:w], "dim"))
            # ── what the agent DID (its work) — scrollable (F20/F21) ──
            work = info.get("output", "")
            if work:
                out.append(RenderLine("  ─ what it did ─ (↑/↓ scroll)"[:w], "dim"))
                wrapped: list[str] = []
                for para in work.split("\n"):
                    wrapped.extend(_wrap_all(para, w - 3))
                avail = max(1, height - len(out) - 1)
                self._focus_scroll = max(0, min(self._focus_scroll, max(0, len(wrapped) - avail)))
                out.extend(RenderLine(f"  {ln}"[:w], "default")
                           for ln in wrapped[self._focus_scroll:self._focus_scroll + avail])
            else:
                if info.get("task"):
                    out.append(RenderLine("  task:"[:w], "dim"))
                    out.extend(RenderLine(f"    {chunk}"[:w], "default") for chunk in _wrap(info["task"], w - 4))
                out.append(RenderLine("  (its output will appear here as it works)"[:w], "dim"))
            while len(out) < height - 1:
                out.append(RenderLine("", "default"))
            # footer: back + (when this agent is still running) a stop affordance (#152/#235)
            _foot = "  ‹ back: b / ← / click"
            if info.get("status") == "running":
                _foot += "   ·   k stop run"
            out.append(RenderLine(_foot[:w], "dim"))
            return out[:height]

        # Empty panel = a solo run (the common case): the pipeline is phases, shown in the
        # chat working line, not here. The panel populates only when a real fan-out spawns
        # workers (sub-agents / council / review panel / swarm / campaign).
        if not self.agents:
            out.append(RenderLine("  no agents running", "dim"))
            out.append(RenderLine("  (sub-agents appear here on fan-out)"[:w], "dim"))
            while len(out) < height:
                out.append(RenderLine("", "default"))
            return out[:height]

        # "Running N agents…" header — counts the live agents, which
        # now include council plan·X / panel review·X / subagent sub·N members.
        _running = sum(1 for a in self.agents.values() if a.get("status") == "running")
        if _running:
            out.append(RenderLine(f"  Running {_running} agent{'s' if _running != 1 else ''}…"[:w], "accent"))
        # list view: every agent is clickable / ↑↓-navigable; the running or
        # keyboard-selected row is accent, with a › cursor marking the selection.
        _items = list(self.agents.items())
        if self._cursor >= len(_items):
            self._cursor = max(0, len(_items) - 1)
        for idx, (role, info) in enumerate(_items):
            icon = self._ICONS.get(info["status"], "○")
            style = self._STYLES.get(info["status"], "dim")
            running = info["status"] == "running"
            selected = idx == self._cursor
            cur = "›" if selected else " "
            meta = []
            _tools = info.get("tools", 0)
            if _tools:
                meta.append(f"{_tools} tool{'s' if _tools != 1 else ''}")
            _tok = info.get("agent_tokens", 0)
            if _tok:
                meta.append(f"{abbrev_count(_tok)} tok")
            _el = _elapsed_str(info)
            if _el:
                meta.append(_el)
            line = f" {cur}{icon} {role.upper()}" + ("  · " + " · ".join(meta) if meta else "")
            self._row_to_agent[len(out)] = role
            out.append(RenderLine(line[:w], "accent" if (running or selected) else style))
            model = info["model"]
            if model:
                detail = f"     {model.split('/')[-1][:max(4, w - 16)]}"
                if info["cost"] > 0:
                    detail += f"  ${info['cost']:.4f}"
                out.append(RenderLine(detail[:w], "dim"))
            _act = info.get("activity", "")
            if _act and running:      # live current-activity
                out.append(RenderLine(f"     ⎿ {_act}"[:w], "dim"))
        out.append(RenderLine("  ↑/↓ select · → focus · click an agent", "dim"))
        while len(out) < height:
            out.append(RenderLine("", "default"))
        return out[:height]


def _now() -> float:
    import time
    return time.monotonic()


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _elapsed_str(info: dict) -> str:
    """Live wall-clock for an agent: ticks while running, frozen once done."""
    st = info.get("_started")
    if not st:
        return ""
    end = info.get("_ended")
    if end is None:
        end = _now()
    return _fmt_elapsed(max(0.0, end - st))


def _wrap(text: str, width: int) -> list[str]:
    width = max(4, width)
    words, line, out = (text or "").split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out[:6] or [""]


def _wrap_all(text: str, width: int) -> list[str]:
    """Word-wrap with NO line cap (for the focused agent's full work, which scrolls)."""
    width = max(4, width)
    if not (text or "").strip():
        return [""]
    out, line = [], ""
    for word in text.split():
        while len(word) > width:                 # hard-break a very long token
            if line:
                out.append(line); line = ""
            out.append(word[:width]); word = word[width:]
        if len(line) + len(word) + 1 > width:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]
