"""MCP (Model Context Protocol) client + tool bridge.

MCP lets external servers expose tools to an agent. A server speaks JSON-RPC 2.0;
common methods: `initialize` (handshake), `tools/list` (advertise tools with a
JSON-schema `inputSchema`), `tools/call` (run one). We connect, list, and BRIDGE
each MCP tool into our own tool registry (their schema -> our Tool, their call ->
JSON-RPC), so the agent gains them with zero hardcoding.

Protocol logic is decoupled from I/O via an injected transport
(`request(method, params)->result`, `notify(method, params)`), so it is unit-
tested with an in-process fake. `StdioTransport` is the real one (spawns the
server, Content-Length framing). Independent implementation of an open protocol.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
import urllib.request

from .redact import redact as _redact


class MCPError(Exception):
    pass


class MCPClient:
    """Speaks the MCP JSON-RPC handshake + tool calls over an injected transport."""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, transport, *, name: str = "server"):
        self._t = transport
        self.name = name
        self._initialized = False

    def initialize(self) -> dict:
        res = self._t.request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "syntra", "version": "0"},
        })
        self._t.notify("notifications/initialized", {})
        self._initialized = True
        return res or {}

    def list_tools(self) -> list:
        res = self._t.request("tools/list", {}) or {}
        return res.get("tools", []) or []

    def call_tool(self, name: str, arguments: dict) -> str:
        res = self._t.request("tools/call", {"name": name, "arguments": arguments}) or {}
        return _format_tool_result(res)


def _format_tool_result(res: dict) -> str:
    """MCP tool result -> a plain string for the agent loop."""
    if res.get("isError"):
        prefix = "error: "
    else:
        prefix = ""
    parts = []
    for item in res.get("content", []) or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "resource":
                rsrc = item.get("resource", {}) or {}
                uri = str(rsrc.get("uri", ""))
                if rsrc.get("text") is not None:
                    parts.append((f"{uri}\n" if uri else "") + str(rsrc.get("text")))
                elif rsrc.get("blob") is not None:
                    mime = rsrc.get("mimeType", "application/octet-stream")
                    parts.append(f"{uri} ({mime}, {len(str(rsrc.get('blob')))} b64 bytes)")
                elif uri:
                    parts.append(uri)
            else:
                parts.append(json.dumps(item)[:500])
    body = "\n".join(p for p in parts if p)
    return (prefix + body) if body else (prefix + "(no content)")


def read_resource(client: MCPClient, uri: str, *, store=None, state=None) -> dict:
    """Fetch an MCP resource and return provenanced content with a content sha256."""
    import hashlib
    res = client._t.request("resources/read", {"uri": uri}) or {}
    out: dict = {"uri": uri, "mimeType": "text/plain", "text": "", "sha256": ""}
    for item in res.get("contents", []) or []:
        if not isinstance(item, dict):
            continue
        out["uri"] = str(item.get("uri", uri))
        out["mimeType"] = item.get("mimeType", out["mimeType"])
        if item.get("text") is not None:
            out["text"] = str(item.get("text"))
        elif item.get("blob") is not None:
            out["blob"] = str(item.get("blob"))
            out.pop("text", None)
        break
    payload = out.get("text") or out.get("blob") or ""
    out["sha256"] = hashlib.sha256(payload.encode() if isinstance(payload, str) else
                                    str(payload).encode()).hexdigest()
    if store is not None and state is not None and out.get("sha256"):
        try:
            import json
            from pathlib import Path
            task_dir = Path(getattr(store, "state_root", "")) / getattr(state, "task_id", "")
            res_dir = task_dir / "resources"
            res_dir.mkdir(parents=True, exist_ok=True)
            path = res_dir / f"{out['sha256'][:16]}.json"
            path.write_text(json.dumps(out), encoding="utf-8")
            out["persisted"] = str(path)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass
    return out


# #252(c): cap the length of a server-supplied tool description that enters the model prompt.
_MCP_DESC_CAP = 2048


def mcp_tools(client: MCPClient):
    """Bridge a connected MCP server's tools into our registry as Tool objects.

    Names are namespaced `mcp_<server>_<tool>` to avoid collisions. All MCP tools
    are tagged 'exec' (external side effects -> permission-gated).
    """
    from .tools import Tool
    out = {}
    for spec in client.list_tools():
        tname = spec.get("name", "")
        if not tname:
            continue
        full = f"mcp_{client.name}_{tname}"
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}

        def _run(args, ctx, _client=client, _tname=tname):
            try:
                return _client.call_tool(_tname, args)
            except Exception as e:  # noqa: BLE001 - surface as text
                return f"error: MCP call failed: {e}"

        # #252(c): the server-supplied DESCRIPTION reaches the model via tools_schema — a
        # separate injection surface from tool RESULTS (#191 handles those). A malicious server
        # can smuggle invisible instructions or a huge blob into it. Sanitize (strip
        # tag/bidi/zero-width/control) + cap the length before it enters the prompt.
        raw_desc = spec.get("description", "") or f"MCP tool {tname}"
        try:
            from .textguard import sanitize_model_text
            raw_desc = sanitize_model_text(raw_desc)
        except Exception:  # noqa: BLE001 - never fail the bridge on the sanitizer
            pass
        desc = raw_desc[:_MCP_DESC_CAP].rstrip() or f"MCP tool {tname}"
        if len(raw_desc) > _MCP_DESC_CAP:
            desc += " …[truncated]"
        # #191: an MCP server is a third party; its tool results are untrusted content →
        # fenced+marked at the dispatch seam so a malicious server can't inject instructions.
        out[full] = Tool(full, desc, schema, "exec", _run, untrusted=True)
    return out


class StdioTransport:
    """Real transport: spawn an MCP server subprocess, JSON-RPC over stdio with
    Content-Length framing (same framing LSP uses)."""

    def __init__(self, command: list, *, env: dict | None = None, timeout: float = 30.0):
        # #190: a stdio MCP server is an untrusted third-party subprocess. Never let it inherit
        # the parent's full environment (provider API keys via api_key_env, cloud creds). When
        # the caller gives no explicit env, hand it a SCRUBBED copy (secrets removed, PATH/etc.
        # kept). A server that legitimately needs a token gets it passed EXPLICITLY via `env`.
        if env is None:
            from .sandbox import scrubbed_child_env
            env = scrubbed_child_env()
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env)
        self._id = 0
        self._timeout = timeout
        self._lock = threading.Lock()

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + data)
        self._proc.stdin.flush()

    def _read_message(self) -> dict:
        def _read_chunk(deadline: float, max_bytes: int = 4096) -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise MCPError(f"MCP server timed out after {self._timeout:g}s")
            fd = self._proc.stdout.fileno()
            sel = selectors.DefaultSelector()
            try:
                sel.register(fd, selectors.EVENT_READ)
                if not sel.select(remaining):
                    self.close()
                    raise MCPError(f"MCP server timed out after {self._timeout:g}s")
            finally:
                sel.close()
            data = os.read(fd, max(1, max_bytes))
            if not data:
                raise MCPError("MCP server closed the connection")
            return data

        deadline = time.monotonic() + max(0.1, float(self._timeout or 30.0))
        raw = b""
        while b"\r\n\r\n" not in raw and b"\n\n" not in raw:
            raw += _read_chunk(deadline)
            if len(raw) > 64 * 1024:
                raise MCPError("MCP response headers too large")
        sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
        head, body = raw.split(sep, 1)
        headers = {}
        for line in head.decode("ascii", "replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError as e:
            raise MCPError("invalid MCP Content-Length") from e
        while len(body) < length:
            body += _read_chunk(deadline, length - len(body))
        return json.loads(body[:length].decode("utf-8"))

    def request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            while True:
                msg = self._read_message()
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise MCPError(str(msg["error"]))
                    return msg.get("result", {})
                # ignore notifications / other ids

    def notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        try:
            self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _extract_jsonrpc(content_type: str, text: str, rid: int) -> dict:
    """Parse a JSON-RPC response from a Streamable-HTTP body — either direct JSON or
    an SSE stream (find the `data:` event whose id matches, or any result/error)."""
    if "text/event-stream" in (content_type or ""):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict) and (obj.get("id") == rid or "result" in obj or "error" in obj):
                    return obj
        raise MCPError("no JSON-RPC response found in the SSE stream")
    try:
        obj = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise MCPError(f"invalid MCP HTTP response: {str(e)[:120]}") from e
    return obj if isinstance(obj, dict) else {"result": obj}


class HttpTransport:
    """Streamable-HTTP MCP transport (single endpoint, MCP spec 2025-03-26). POSTs
    JSON-RPC and accepts a JSON or SSE response. Supports a bearer token, custom
    headers, and the `Mcp-Session-Id` negotiated on initialize. Exposes the SAME
    request/notify/close interface as StdioTransport, so MCPClient uses it unchanged
    — this is how Syntra reaches hosted MCP servers (not just local stdio). No
    subprocess. `poster` is injectable for tests:
    poster(url, body_bytes, headers, timeout) -> (content_type, text, resp_headers)."""

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(self, url: str, *, headers: dict | None = None, token: str | None = None,
                 timeout: float = 30.0, poster=None):
        self._url = url
        self._timeout = timeout
        self._id = 0
        self._session_id: str | None = None
        self._headers = dict(headers or {})
        self._token = token or ""
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._poster = poster or self._default_post
        self._lock = threading.Lock()

    @staticmethod
    def _default_post(url, body, headers, timeout):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rheaders = {k.lower(): v for k, v in resp.headers.items()}
            return (resp.headers.get("Content-Type", ""),
                    resp.read().decode("utf-8", "replace"), rheaders)

    def _post(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": self.PROTOCOL_VERSION, **self._headers}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        ctype, text, rheaders = self._poster(self._url, body, h, self._timeout)
        sid = (rheaders or {}).get("mcp-session-id")
        if sid:
            self._session_id = sid   # stateful server: remember the session for later calls
        return ctype, text

    def request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            try:
                ctype, text = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                msg = _extract_jsonrpc(ctype, text, rid)
            except MCPError as e:
                # a hostile/verbose server can echo our bearer token in an error;
                # redact (literal + pattern) before it can reach the model's context.
                raise MCPError(_redact(str(e), [self._token])) from None
            if "error" in msg:
                raise MCPError(_redact(str(msg["error"]), [self._token]))
            return msg.get("result", {})

    def notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        pass  # stateless HTTP — nothing to tear down
