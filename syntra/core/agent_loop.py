"""Agent executor loop (Step 4).

The heart of the agentic executor: drive a tool-capable model turn-by-turn —
model responds (optionally with tool calls) -> run the calls -> feed results
back -> repeat — until the model stops calling tools (it's done) or a hard turn
limit is hit. Discovered work goes into the running TODO via the `todo` tool.

The model call is INJECTED as `call_model(messages, tools_schema) -> ChatResult`
so the loop is unit-tested with a scripted fake (no network, no provider). The
caller wires the real provider + chosen model + cost tracking into call_model,
and bounds `max_turns` from LoopGuard. Tool execution + permissions + workspace
confinement are handled by core/tools.dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tools import Tool, ToolContext, dispatch, tools_schema
from ..providers.openai_compat import ChatMessage


@dataclass
class AgentResult:
    answer: str
    turns: int
    tool_calls: int
    stopped: str                      # "done" | "max_turns" | "interrupted"
    messages: list = field(default_factory=list)
    todos: list = field(default_factory=list)


def _result_summary(out: str) -> str:
    """A ONE-LINE gist of a tool's return string, for the feed's dim "└ <result>" line (#84).

    Multi-line output (a file read, a directory listing) → "N lines" (the shape the user cares
    about — how much came back). A single-line result (status like "wrote x (12 chars)", an
    "error: …", "(no matches)") passes through, capped so the feed line stays tidy. Empty →
    "" (the caller renders no second line). Pure + deterministic."""
    if not out or not out.strip():
        return ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) > 1:
        return f"{len(lines)} lines"
    return lines[0].strip()[:60]


# #159/#203: tools whose consecutive calls are safe to run CONCURRENTLY within one
# turn. Deliberately narrow: only the pure filesystem READERS, which touch no shared
# ctx state and fire no callbacks (verified). NOT "every danger==safe" — view_image /
# show_image / skill / task / todo / plan mutate ctx (pending_images, todos, on_image)
# and must stay serial. Write/exec are never here (ordering + side effects matter).
_PARALLEL_SAFE_TOOLS = frozenset({"read", "list", "glob", "grep", "find_file"})


def normalize_tool_result(out) -> str:
    """#206: never hand the model an EMPTY tool result.

    A tool message with empty/whitespace-only content — especially as the last
    message before the model samples again — is read by some models as a stop cue:
    the turn ends with no output ("it just stopped"). Normalize to an explicit
    "(no output)" so the model sees the call completed and keeps going. Non-empty
    results (including intentional leading/trailing whitespace) pass through
    unchanged. Pure."""
    if out is None:
        return "(no output)"
    if not isinstance(out, str):
        out = str(out)
    return out if out.strip() else "(no output)"


def _batch_tool_calls(calls):
    """Group a turn's tool calls into ordered runs: a run of ≥2 CONSECUTIVE
    parallel-safe reads (to run concurrently), or a single call (run serially).
    Preserves original order — the runs, flattened, are exactly `calls`."""
    batches: list[list] = []
    cur: list = []
    for tc in calls:
        if tc.name in _PARALLEL_SAFE_TOOLS:
            cur.append(tc)
        else:
            if cur:
                batches.append(cur); cur = []
            batches.append([tc])           # non-parallel call: its own singleton batch
    if cur:
        batches.append(cur)
    return batches


def _run_tool_calls(calls, tools, ctx, emit, stop):
    """Execute a turn's tool calls, yielding (output, tool_call) in ORIGINAL order.

    Consecutive pure-read calls run concurrently in a thread pool; everything else runs
    serially. Events fire in order. `stop()` is checked BEFORE each batch, so an Esc
    between batches halts further dispatch (a batch already begun runs to completion —
    the model still gets consistent results for whatever it launched)."""
    for batch in _batch_tool_calls(calls):
        if stop and stop():
            return                          # interrupt: dispatch no further batches
        if len(batch) == 1:
            tc = batch[0]
            emit("tool_call", {"name": tc.name, "arguments": tc.arguments})
            out = dispatch(tc, tools, ctx)
            emit("tool_result", {"name": tc.name, "ok": not out.startswith("error:"),
                                 "summary": _result_summary(out)})
            yield out, tc
            continue
        # a batch of parallel-safe reads: emit calls in order, run concurrently, then
        # emit results + yield in the batch's original order.
        for tc in batch:
            emit("tool_call", {"name": tc.name, "arguments": tc.arguments})
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(batch), 8)) as ex:
            outs = list(ex.map(lambda tc: dispatch(tc, tools, ctx), batch))
        for tc, out in zip(batch, outs):
            emit("tool_result", {"name": tc.name, "ok": not out.startswith("error:"),
                                 "summary": _result_summary(out)})
            yield out, tc


def run_agent(call_model, tools: dict[str, Tool], ctx: ToolContext,
              messages: list, *, max_turns: int = 20, on_event=None,
              max_context_tokens: int = 0, should_stop=None, drain_steer=None) -> AgentResult:
    """Run the tool loop. `messages` is the seed transcript (system + user).

    call_model(messages, schema) -> ChatResult. Returns when the model stops
    requesting tools (final answer) or max_turns is exhausted (then asks for a
    no-tools summary so the user always gets a closing message). When
    max_context_tokens > 0, the transcript is auto-compacted between turns once
    it grows past that budget (inline auto-compaction).

    should_stop() -> bool (optional): a cooperative interrupt hook checked at every turn
    boundary — before sampling AND after a turn's tool calls run — so a user's Esc/Ctrl+K
    actually HALTS the tool loop (returns stopped="interrupted") instead of the loop grinding
    on to max_turns while the UI has already detached. None = never stop (unchanged behavior).
    The in-flight model/socket call itself can't be aborted, so worst case is ≤1 more call.

    drain_steer() -> list[str] (optional, #124): live user steering. Checked at every turn
    boundary; any returned messages are appended to the transcript as a user turn ("New
    instructions from the user…") so the model honors them at its NEXT step. This is what makes
    "type while it works, Esc to send" actually reach the model on the tool-using tiers (not just
    the plan-step loop) — the previous silent-drop was the felt lag. None = no steering (unchanged).
    """
    emit = on_event or (lambda kind, payload: None)
    _stop = should_stop or (lambda: False)
    _drain = drain_steer or (lambda: [])

    def _apply_steers(turn: int) -> None:
        """Fold any pending live-steer messages into the transcript before the next model call."""
        try:
            steers = _drain() or []
        except Exception:  # noqa: BLE001 - a steering-read failure must never break the loop
            steers = []
        if steers:
            joined = "\n".join(s for s in steers if s)
            if joined.strip():
                msgs.append(ChatMessage("user",
                    "New instructions from the user — honor these now, while finishing the "
                    "current step:\n" + joined))
                emit("agent_steered", {"turn": turn, "count": len(steers)})

    # #251: drop OFF tools from the advertised schema (attack surface / injection lure / tokens).
    # Uses the live PermissionStore's non-prompting tool_is_off when wired; else advertise all.
    _perms = getattr(ctx, "_perms", None)
    _is_off = getattr(_perms, "tool_is_off", None) if _perms is not None else None
    schema = tools_schema(tools, is_off=_is_off)
    msgs = list(messages)
    total_calls = 0

    for turn in range(1, max_turns + 1):
        if _stop():
            emit("agent_interrupted", {"turn": turn})
            return AgentResult(msgs[-1].content if msgs and getattr(msgs[-1], "content", "") else "",
                               turn - 1, total_calls, "interrupted", msgs, list(ctx.todos))
        _apply_steers(turn)   # #124: inject live user steering before sampling this turn
        # Auto-compaction on context pressure (before sampling this turn).
        if max_context_tokens:
            from .compaction import auto_compact_messages
            # Reserve headroom for the model's reply so we compact BEFORE the
            # window is too full to respond. 16k or 1/8 of window.
            reserve = min(16_000, max(2_000, max_context_tokens // 8))
            msgs, did = auto_compact_messages(
                msgs, max_tokens=max_context_tokens, reserve_tokens=reserve)
            if did:
                emit("compaction", {"turn": turn, "reason": "context pressure"})
        result = call_model(msgs, schema)
        msgs.append(ChatMessage("assistant", result.text or "", tool_calls=result.tool_calls))

        if not result.tool_calls:
            emit("agent_done", {"turn": turn})
            return AgentResult(result.text or "", turn, total_calls, "done", msgs, list(ctx.todos))

        # #159/#203: run the turn's tool calls, batching CONSECUTIVE pure-read calls to
        # run concurrently (real latency win) while write/exec stay serial+ordered. The
        # model always sees tool results in the ORIGINAL call order; events are emitted in
        # order too. Interrupt is honored between batches (a batch is atomic once started).
        for out, tc in _run_tool_calls(result.tool_calls, tools, ctx, emit, _stop):
            total_calls += 1              # count calls that actually RAN (interrupt-aware)
            msgs.append(ChatMessage("tool", normalize_tool_result(out), tool_call_id=tc.id))

        # If the agent loaded image(s) via view_image, attach them as a vision
        # message so the model actually SEES them on the next turn, then clear.
        if getattr(ctx, "pending_images", None):
            msgs.append(ChatMessage("user", "Here are the image(s) you loaded:",
                                    images=tuple(ctx.pending_images)))
            ctx.pending_images.clear()

        # Interrupt requested DURING this turn's tool calls → stop before the next (costly)
        # model call, so Esc halts within one turn instead of sampling again.
        if _stop():
            emit("agent_interrupted", {"turn": turn})
            return AgentResult("", turn, total_calls, "interrupted", msgs, list(ctx.todos))

    # Turn limit reached: get a final, tool-free summary so there's always an answer.
    emit("agent_max_turns", {"turns": max_turns})
    closing = msgs + [ChatMessage("user",
        "Turn limit reached. Stop using tools and summarize what you did and what remains.")]
    final = call_model(closing, [])
    return AgentResult(final.text or "", max_turns, total_calls, "max_turns", msgs, list(ctx.todos))
