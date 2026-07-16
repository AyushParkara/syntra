"""Rolling action feed — the inline 'what is the agent doing right now' play-by-play.

Pure model (no curses): the run loop calls ingest(kind, payload) with the SAME
structured events that already reach tui2._drain (agent_start/done, step_done,
phase, verify_result, and key tool_calls). The model keeps a list of FeedItems,
exposes a fade-tiered live window of the most recent ~5, folds the rest into
clickable group summaries, and render()s [(text, style)] lines. The curses layer
paints them just above the working line. Mirrors plan_approval.py's pure contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


# Tool names that are "key" enough to surface as their own feed line. Everything
# else (reads, lists, small lookups) is filtered out so the feed stays milestone-level.
_KEY_TOOLS = frozenset({
    "edit_file", "write_file", "apply_patch", "exec_command", "run_command",
    "shell", "search", "grep", "web_search", "web_fetch",
})

# Pretty Tool(arg) labels: map the engine's raw tool names to a short, capitalized verb-noun
# — Read(path) / Bash(cmd) / Edit(file) / … Anything not listed Title-Cases its own name, so
# a new engine tool still renders cleanly.
_TOOL_LABEL = {
    "read_file": "Read", "read": "Read", "open_file": "Read",
    "write_file": "Write", "create_file": "Write",
    "edit_file": "Edit", "apply_patch": "Edit", "patch": "Edit",
    "exec_command": "Bash", "run_command": "Bash", "shell": "Bash", "bash": "Bash",
    "search": "Search", "grep": "Grep", "find": "Find",
    "web_search": "Web", "web_fetch": "Fetch", "browse": "Browse",
    "plan": "Plan", "list_dir": "List", "ls": "List",
}

# Live window size: the N most-recent unfolded items stay loud; older ones fold.
LIVE_WINDOW = 5


def _tool_label(name: str) -> str:
    """Map a raw tool name → its short display verb (Read/Bash/Edit/…). Unknown names
    Title-Case their own identifier ('my_tool' → 'My Tool') so nothing renders raw."""
    n = (name or "").strip().lower()
    if n in _TOOL_LABEL:
        return _TOOL_LABEL[n]
    return " ".join(w.capitalize() for w in n.replace("-", "_").split("_") if w) or "Tool"


# Present-tense verb for a RUNNING tool + the settled past-tense for a DONE one (#135): the live
# line reads "Reading… <file>" / "Running… <cmd>" (animated), then settles to "Read <file>" /
# "Ran <cmd>". A verb not listed falls back to the label itself for both.
_VERB_LIVE = {"Read": "Reading", "Write": "Writing", "Edit": "Editing", "Bash": "Running",
              "Search": "Searching", "Grep": "Grepping", "Find": "Finding", "Web": "Searching",
              "Fetch": "Fetching", "Browse": "Browsing", "Plan": "Planning", "List": "Listing"}
_VERB_DONE = {"Read": "Read", "Write": "Wrote", "Edit": "Edited", "Bash": "Ran",
              "Search": "Searched", "Grep": "Grepped", "Find": "Found", "Web": "Searched",
              "Fetch": "Fetched", "Browse": "Browsed", "Plan": "Planned", "List": "Listed"}


@dataclass
class FeedItem:
    glyph: str                 # ✓ ● ▸ ⚠ ✗
    text: str
    style: str                 # a theme style name
    status: str                # "done" | "running" | "warn" | "error"
    group: str                 # fold-group key (e.g. phase/batch)
    seq: int                   # monotonic order
    verb: str = ""             # display verb label (Read/Bash/…) for present/past rendering (#135)
    arg: str = ""              # raw path/cmd — kept clickable (open file) + shown after the verb
    result: str = ""           # #84: one-line gist of what the tool GOT (rendered as a dim "└ …"
                               # second line once the tool settles to done); "" = no second line


class ActionFeed:
    def __init__(self) -> None:
        self.items: list[FeedItem] = []
        self._seq = 0
        self._cur_group = "run"
        self.expanded: set = set()       # group keys the user clicked to expand

    def _add(self, glyph: str, text: str, style: str, status: str,
             verb: str = "", arg: str = "") -> None:
        self.items.append(FeedItem(glyph, text, style, status, self._cur_group, self._seq,
                                   verb=verb, arg=arg))
        self._seq += 1

    def ingest(self, kind: str, payload: dict | None = None) -> None:
        p = payload or {}
        if kind == "phase":
            # A phase change only sets the current FOLD-GROUP (so tool lines group + fold
            # under "analyze"/"plan"/…). It does NOT add a visible "● ANALYZE" line — the
            # live phase is shown by the status line just above (plan_ribbon); emitting it
            # here too would double-render the phase right next to it (user-reported dup).
            self._cur_group = str(p.get("phase", p.get("mode", "run"))) or "run"
        elif kind == "agent_start":
            self._add("●", f"{p.get('role', 'agent')} started", "accent", "running")
        elif kind == "agent_done":
            tools = p.get("tools")
            txt = f"{p.get('role', 'agent')} done"
            if tools:
                txt += f" · {tools} tools"
            self._add("✓", txt, "diff_add", "done")
        elif kind == "step_done":
            self._add("✓", f"{p.get('step_id', 'step')} done", "diff_add", "done")
        elif kind == "verify_result":
            ok = bool(p.get("ok"))
            self._add("✓" if ok else "✗", "verify " + ("passed" if ok else "failed"),
                      "diff_add" if ok else "diff_del", "done" if ok else "error")
        elif kind == "tool_call":
            name = str(p.get("name", ""))
            if name in _KEY_TOOLS:
                self._add("▸", f"{name}", "default", "done")
        elif kind == "agent_activity":
            # #84: a RESULT update (carries `result`, no `activity`) attaches the tool's one-line
            # gist to the most recent tool item, so it renders a dim "└ <result>" second line once
            # settled. It belongs to the tool that just ran (the newest tool entry).
            res = str(p.get("result", "")).strip()
            if res and "activity" not in p:
                # Attach the result to the tool it's FOR. Prefer matching the tool label
                # (for_tool → _tool_label), else fall back to the most recent tool entry. This is
                # robust to whether the next tool has already settled this one or not.
                want = _tool_label(str(p.get("for_tool", "") or "")) if p.get("for_tool") else ""
                target = None
                for it in reversed(self.items):
                    if not it.verb:
                        continue
                    if want and it.verb == want and not it.result:
                        target = it
                        break
                    if target is None:                # remember the newest tool as a fallback
                        target = it
                if target is not None:
                    target.result = res
                return
            # C-hybrid live tool feed: the engine's activity string is "{name}: {arg}"
            # (built by loop._tool_activity). Render it as a Tool(arg) entry; the newest
            # tool shows ● live, and the moment the next one arrives the prior live entry
            # settles to ✓ done. Consecutive duplicates are ignored (the engine re-emits
            # the same activity on token-only updates).
            act = str(p.get("activity", "")).strip()
            if not act:
                return
            if ":" in act:
                raw_name, raw_arg = act.split(":", 1)
                label = _tool_label(raw_name)
                arg = raw_arg.strip()
            else:
                label, arg = _tool_label(act), ""
            entry = f"{label}({arg})" if arg else label
            # skip a consecutive duplicate of the current live entry
            if self.items and self.items[-1].text == entry and self.items[-1].status == "running":
                return
            # settle the previous live TOOL to done (leave phase markers like '● ANALYZE'
            # alone — those aren't tools; a tool entry is the Title(arg) form / not all-caps).
            # Settling flips its verb to past-tense (Reading→Read) so the live/done states differ.
            for it in self.items:
                if (it.status == "running" and it.glyph == "●"
                        and ("(" in it.text or not it.text.isupper())):
                    it.glyph, it.style, it.status = "✓", "diff_add", "done"
            # #135: carry the display verb + raw arg so render() can animate the RUNNING line
            # ("Reading… <file>") and keep the arg clickable (open the file).
            self._add("●", entry, "accent", "running", verb=label, arg=arg)
        # all other kinds (route, tick, …) are not feed lines

    # ---- fade / window / fold -------------------------------------------------
    def fade_tiers(self) -> list[int]:
        """Fade tier per item in self.items order: newest = 0 (brightest), older grows
        to a max of 3 by recency. Only the live-window tail is shown loud; items past
        the window are folded rather than just faded."""
        n = len(self.items)
        return [min(3, (n - 1) - i) for i in range(n)]

    # tier -> style override (None = keep the item's own style; the newest stays vivid)
    _STYLE_BY_TIER: ClassVar[dict[int, str | None]] = {0: None, 1: "default", 2: "dim", 3: "dim"}

    def _item_text(self, it: "FeedItem", tick: int, live: bool) -> str:
        """The display text for a feed item (#135). A tool with a verb renders as a natural,
        LIVE-vs-DONE phrase instead of a static "Read(path)":
          running → "⠹ Reading… <arg>"  (animated braille spinner + present-tense verb)
          done    → "Read <arg>"          (past-tense; the ✓ glyph is drawn separately)
        A non-tool line (phase marker, plain text) renders its text unchanged."""
        if not it.verb:
            return it.text
        if live and it.status == "running":
            from .tui_model import motion_enabled
            # freeze the spinner to a single steady glyph under reduced-motion (same policy the
            # shimmer band follows) so nothing on the line moves when the user opted out.
            spin = ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(tick) % 10] if (tick and motion_enabled()) else "⠿")
            live_verb = _VERB_LIVE.get(it.verb, it.verb)
            return f"{spin} {live_verb}…" + (f" {it.arg}" if it.arg else "")
        done_verb = _VERB_DONE.get(it.verb, it.verb)
        return f"{done_verb}" + (f" {it.arg}" if it.arg else "")

    # #135 shimmer: on a RUNNING tool, a bright band sweeps across the filename (Option 2, the
    # steady/smooth one the user picked). Implemented as a moving highlight SPAN (theme-color, so
    # it's portable + color-cheap), NOT per-char truecolor. Band width + cycle are fixed (calm,
    # constant speed). Frozen to no-shimmer under reduced motion.
    _SHIMMER_W = 5           # highlight band width (chars)
    _SHIMMER_STYLE = "user"  # bright theme style for the swept band

    def _shimmer_span(self, line: str, arg: str, tick: int):
        """Return [(start, end, style)] marking the swept band over `arg` within `line`, or []."""
        from .tui_model import motion_enabled
        if not arg or not tick or not motion_enabled():
            return []
        a0 = line.find(arg)
        if a0 < 0:
            return []
        n = len(arg)
        # steady sweep: band center moves one span-width per few frames, wraps past the end so
        # there's a brief gap between passes (reads as a smooth repeating glint, not a strobe).
        span = n + self._SHIMMER_W
        pos = (int(tick) * 1) % span          # 1 col/frame at the render tick rate → smooth
        s = a0 + pos - self._SHIMMER_W
        e = a0 + pos
        s = max(a0, s); e = min(a0 + n, e)
        return [(s, e, self._SHIMMER_STYLE)] if e > s else []

    def render(self, width: int, tick: int = 0) -> list:
        """Render the feed as [(text, style, spans)] — always a 3-tuple; spans is [] unless the
        row carries a shimmer band. Empty when idle. Older items beyond the live window fold into
        one ▸ summary line per group (unless expanded); the live window is faded by recency. A
        running tool line animates: spinner + a shimmer band sweeping the filename (#135, Opt 2)."""
        if not self.items:
            return []
        w = max(10, int(width))
        out: list = []
        window = self.items[-LIVE_WINDOW:]
        folded = self.items[: max(0, len(self.items) - LIVE_WINDOW)]

        def _row(text: str, style: str, spans=None):
            # pad to full width so the line self-clears its row (no stale tail bleeds
            # through when the layout shifts, e.g. a panel opens and narrows the chat).
            # Uniform 3-tuple (text, style, spans) for EVERY row — a caller never has to guess
            # the arity; spans is [] when the line has no shimmer band.
            padded = text.ljust(w)[:w]
            sp = [(s, min(e, w), st) for (s, e, st) in (spans or []) if s < w and e > s]
            return (padded, style, sp)

        # fold the overflow into one summary line per group (unless that group is expanded)
        if folded:
            counts: dict = {}
            order: list = []
            for it in folded:
                if it.group not in counts:
                    counts[it.group] = 0
                    order.append(it.group)
                counts[it.group] += 1
            for g in order:
                if g in self.expanded:
                    out.extend(_row(f"  {it.glyph} {self._item_text(it, tick, live=False)}", it.style)
                               for it in folded if it.group == g)
                else:
                    out.append(_row(f"▸ {g} · {counts[g]} more · ✓ (click)", "comment"))
        # the live window, faded by recency (newest brightest). A running tool line animates its
        # own spinner (in _item_text) + a shimmer band over the filename, so it needs no separate ●.
        for off, it in enumerate(window):
            tier = min(3, (len(window) - 1) - off)
            style = self._STYLE_BY_TIER.get(tier) or it.style
            live = (it.status == "running" and bool(it.verb))
            txt = self._item_text(it, tick, live=live)
            prefix = "  " if live else f"  {it.glyph} "
            line = f"{prefix}{txt}"
            spans = self._shimmer_span(line, it.arg, tick) if live else None
            out.append(_row(line, style, spans))
            # #84: a settled tool with a captured result gets a dim "└ <result>" second line
            # (user's chosen format). Only when done (result is unknown while running).
            if it.result and not live:
                out.append(_row(f"     └ {it.result}", "dim"))
        return out

    def fold_group_at(self, rendered_row: int):
        """Map a click on a rendered row index -> the group key of a ▸ fold line, or None."""
        rows = self.render(9999)
        if 0 <= rendered_row < len(rows):
            t = rows[rendered_row][0].lstrip()
            if t.startswith("▸ "):
                return t[2:].split(" · ", 1)[0]
        return None

    def toggle_group(self, group: str) -> None:
        if group in self.expanded:
            self.expanded.discard(group)
        else:
            self.expanded.add(group)

    def freeze_running(self) -> None:
        """BUG3: settle every in-flight (running) item to a static DONE row. Called when a
        run stops/interrupts (Esc-Esc) so no present-tense "Editing…" / spinner / shimmer
        line survives a "■ Stopped." — the history stays, just frozen. Idempotent; a no-op
        when nothing is running. Mirrors the auto-settle a following tool would have done."""
        for it in self.items:
            if it.status == "running":
                it.glyph, it.style, it.status = "✓", "diff_add", "done"

    def clear(self) -> None:
        self.items.clear()
        self._seq = 0
        self._cur_group = "run"
        self.expanded.clear()
