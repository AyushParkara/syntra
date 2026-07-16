"""Expose Syntra itself AS an MCP server (B3).

The MCP client side (``mcp.py``) lets Syntra DRIVE other tools; this is the mirror —
other tools drive Syntra over the SAME JSON-RPC 2.0 protocol. We advertise a small,
generic tool surface (run a goal, continue a session) and run it against an injected
``runner``, so the message handling is a PURE function — fully unit-tested without any
process or socket.

Protocol-compatible with Syntra's own ``mcp.MCPClient``:
  - methods: ``initialize`` -> ``tools/list`` -> ``tools/call`` (+ the
    ``notifications/initialized`` handshake, ``ping``).
  - ``PROTOCOL_VERSION`` and the JSON-RPC envelope match the client exactly.
  - stdio framing is **Content-Length** (LSP-style), the SAME framing
    ``mcp.StdioTransport`` reads/writes — newline framing would NOT interoperate.
"""

from __future__ import annotations

import json

PROTOCOL_VERSION = "2024-11-05"          # must match mcp.MCPClient.PROTOCOL_VERSION
SERVER_INFO = {"name": "syntra", "version": "0"}

# The tool surface we expose. Generic, Syntra's-own names — no external product names.
TOOLS = [
    {
        "name": "run_goal",
        "description": ("Run a goal through Syntra's planner→executor→reviewer pipeline "
                        "and return the final result."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The task to accomplish."},
                "workspace": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "continue_session",
        "description": "Continue a previous Syntra session by id with a follow-up message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session/task id to continue."},
                "message": {"type": "string", "description": "The follow-up message."},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "review",
        "description": ("Skeptically review an OUTPUT against a GOAL with a routed reviewer "
                        "model (read-only). Returns a verdict (pass/fail) + concrete issues."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the output was supposed to achieve."},
                "output": {"type": "string", "description": "The work/answer to review."},
                "workspace": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["goal", "output"],
        },
    },
]


# ── JSON-RPC envelope helpers ───────────────────────────────────────────────
def _result(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text_content(text, *, is_error: bool = False) -> dict:
    """An MCP tool result the client's `_format_tool_result` understands."""
    out = {"content": [{"type": "text", "text": str(text)}]}
    if is_error:
        out["isError"] = True
    return out


class MCPServer:
    """Pure JSON-RPC message handler exposing Syntra as MCP tools.

    `runner` supplies the backing capability:
        runner.run_goal(goal: str, workspace: str) -> str
        runner.continue_session(session_id: str, message: str) -> str
    Inject a fake in tests; the real one wraps the Loop. `handle()` never raises —
    protocol problems become JSON-RPC errors, tool problems become isError results."""

    def __init__(self, runner, *, tools=None, server_info=None):
        self.runner = runner
        self.tools = tools if tools is not None else TOOLS
        self.server_info = server_info or SERVER_INFO
        self.initialized = False

    def handle(self, message: dict):
        """Handle ONE parsed JSON-RPC message. Returns a response dict, or None for a
        notification (which gets no reply)."""
        if not isinstance(message, dict):
            return _error(None, -32600, "invalid request: not an object")
        method = message.get("method", "")
        rid = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and get no response.
        if rid is None and str(method).startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            return None

        if method == "initialize":
            return _result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": self.server_info,
            })
        if method == "tools/list":
            return _result(rid, {"tools": self.tools})
        if method == "tools/call":
            return _result(rid, self._call_tool(params))
        if method == "ping":
            return _result(rid, {})
        return _error(rid, -32601, f"method not found: {method}")

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            if name == "run_goal":
                goal = str(args.get("goal", "")).strip()
                if not goal:
                    return _text_content("error: 'goal' is required", is_error=True)
                return _text_content(self.runner.run_goal(goal, str(args.get("workspace") or "")))
            if name == "continue_session":
                sid = str(args.get("session_id", "")).strip()
                msg = str(args.get("message", "")).strip()
                if not sid or not msg:
                    return _text_content("error: 'session_id' and 'message' are required",
                                         is_error=True)
                return _text_content(self.runner.continue_session(sid, msg))
            if name == "review":
                goal = str(args.get("goal", "")).strip()
                output = str(args.get("output", "")).strip()
                if not goal or not output:
                    return _text_content("error: 'goal' and 'output' are required", is_error=True)
                return _text_content(self.runner.review(goal, output, str(args.get("workspace") or "")))
            return _text_content(f"error: unknown tool {name!r}", is_error=True)
        except Exception as e:  # noqa: BLE001 - a tool failure is a result, not a crash
            return _text_content(f"error: {e}", is_error=True)


# ── Content-Length framing (mirrors mcp.StdioTransport so the two interoperate) ──
def write_message(writable, msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    writable.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    writable.write(data)
    writable.flush()


def read_message(readable):
    """Read one Content-Length-framed JSON-RPC message from a binary stream. Returns the
    parsed dict, {} on a malformed body, or None at EOF."""
    headers: dict = {}
    while True:
        line = readable.readline()
        if not line:
            return None                                  # EOF
        line = line.decode("ascii", "replace").strip()
        if line == "":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        length = 0
    body = readable.read(length) if length else b""
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


# ── a real runner backed by the Loop ────────────────────────────────────────
def result_text(result) -> str:
    """Extract the human-facing answer from a LoopResult — the done steps' results, mirroring
    the CLI renderer (`_print_run_result`). Pure + best-effort (never raises)."""
    state = getattr(result, "state", None)
    plan = getattr(state, "plan", None) or []
    done = [s for s in plan
            if getattr(s, "status", "") == "done" and (getattr(s, "result", "") or "").strip()]
    if not done:
        return "(no output produced)"
    if len(done) == 1:
        return done[0].result.strip()
    return "\n\n".join(f"--- {getattr(s, 'id', '')}: {getattr(s, 'description', '')}\n"
                       f"{(s.result or '').strip()}" for s in done)


class LoopRunner:
    """Backs the MCP server with a real Loop. `run_goal` runs the pipeline and returns the
    answer text; `continue_session` re-runs with the prior session's history when the CLI
    supplies a `history_for(session_id) -> [(role, text), ...]` loader, else resumes the task.
    Constructed by the CLI `mcp-server` entry with a ready Loop + LoopConfig."""

    def __init__(self, loop, config, *, workspace_root=".", history_for=None):
        self.loop = loop
        self.config = config
        self.workspace_root = workspace_root
        self.history_for = history_for

    def run_goal(self, goal: str, workspace: str = "") -> str:
        res = self.loop.run(goal, workspace_root=(workspace or self.workspace_root),
                            config=self.config)
        return result_text(res)

    def continue_session(self, session_id: str, message: str) -> str:
        hist = self.history_for(session_id) if self.history_for else None
        if hist is not None:
            res = self.loop.run(message, workspace_root=self.workspace_root,
                                config=self.config, history=hist)
        else:
            res = self.loop.resume(session_id, config=self.config)
        return result_text(res)

    def review(self, goal: str, output: str, workspace: str = "") -> str:
        """Expose Loop.review over MCP: returns "verdict: pass/fail" + the issue list."""
        r = self.loop.review(goal, output, workspace_root=(workspace or self.workspace_root),
                             config=self.config)
        lines = [f"verdict: {r.get('verdict', '?')}"]
        lines.extend(f"  - {issue}" for issue in (r.get("issues") or []))
        return "\n".join(lines)


def serve_stdio(runner, *, readable=None, writable=None) -> None:
    """Thin stdio serve loop: read Content-Length-framed requests, dispatch through the
    pure MCPServer, write framed responses. The logic lives in MCPServer.handle (tested);
    this is only the I/O frame. Loops until EOF."""
    import sys
    readable = readable or getattr(sys.stdin, "buffer", sys.stdin)
    writable = writable or getattr(sys.stdout, "buffer", sys.stdout)
    server = MCPServer(runner)
    while True:
        msg = read_message(readable)
        if msg is None:
            break                                        # EOF -> done
        if not msg:
            continue                                     # malformed frame -> skip
        resp = server.handle(msg)
        if resp is not None:
            write_message(writable, resp)
