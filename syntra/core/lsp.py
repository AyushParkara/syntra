"""LSP (Language Server Protocol) client + diagnostics.

Talks to a real language server (pyright/tsserver/gopls/...) over JSON-RPC to get
semantic ground truth: diagnostics (compiler errors/warnings), go-to-definition,
and find-references. The big win for the agent: after an edit, ask the server
"did I break anything?" instead of guessing.

Wrinkle: `textDocument/publishDiagnostics` is a server-PUSHED notification, not a
response — so the transport separates request/response (matched by id) from
queued server notifications. The protocol logic is decoupled from I/O via an
injected transport, so lifecycle + diagnostics handling are unit-tested with a
fake. `StdioTransport` is the real one. Independent implementation of an open
protocol.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(uri).path)
    return uri


def format_diagnostics(diags_by_uri: dict) -> str:
    """Render collected diagnostics into a compact, readable block."""
    lines = []
    for uri, diags in sorted(diags_by_uri.items()):
        rel = uri_to_path(uri)
        for d in diags:
            sev = _SEVERITY.get(d.get("severity", 1), "error")
            line = (d.get("range", {}).get("start", {}).get("line", 0)) + 1
            col = (d.get("range", {}).get("start", {}).get("character", 0)) + 1
            msg = (d.get("message", "") or "").replace("\n", " ")[:300]
            lines.append(f"{Path(rel).name}:{line}:{col}: {sev}: {msg}")
    return "\n".join(lines) if lines else "(no diagnostics)"


class LSPClient:
    """Minimal LSP client: lifecycle + diagnostics + definition/references."""

    def __init__(self, transport, *, root_uri: str = ""):
        self._t = transport
        self.root_uri = root_uri
        self._diagnostics: dict = {}     # uri -> list[diagnostic]
        self._initialized = False

    def _drain(self) -> None:
        """Process queued server notifications (collect publishDiagnostics)."""
        for method, params in self._t.notifications():
            if method == "textDocument/publishDiagnostics":
                uri = params.get("uri", "")
                self._diagnostics[uri] = params.get("diagnostics", []) or []

    def initialize(self) -> dict:
        res = self._t.request("initialize", {
            "processId": None, "rootUri": self.root_uri or None,
            "capabilities": {"textDocument": {"publishDiagnostics": {},
                                              "definition": {}, "references": {}}},
            "clientInfo": {"name": "syntra"},
        })
        self._t.notify("initialized", {})
        self._initialized = True
        self._drain()
        return res or {}

    def did_open(self, path: str, text: str, language_id: str = "python") -> None:
        self._t.notify("textDocument/didOpen", {"textDocument": {
            "uri": path_to_uri(path), "languageId": language_id, "version": 1, "text": text}})
        self._drain()

    def did_change(self, path: str, text: str, version: int = 2) -> None:
        self._t.notify("textDocument/didChange", {
            "textDocument": {"uri": path_to_uri(path), "version": version},
            "contentChanges": [{"text": text}]})
        self._drain()

    def diagnostics(self, path: str | None = None):
        """Current diagnostics: for one file (list) or all (dict)."""
        self._drain()
        if path is not None:
            return self._diagnostics.get(path_to_uri(path), [])
        return dict(self._diagnostics)

    def definition(self, path: str, line: int, character: int):
        self._drain()
        return self._t.request("textDocument/definition", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character}})

    def references(self, path: str, line: int, character: int):
        self._drain()
        return self._t.request("textDocument/references", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True}})


class StdioTransport:
    """Real LSP transport: spawn the server, JSON-RPC over stdio (Content-Length
    framing). A reader thread separates responses (matched by id) from pushed
    server notifications (queued for the client to drain)."""

    def __init__(self, command: list, *, env: dict | None = None, timeout: float = 30.0):
        # #190: a language server is a third-party subprocess; don't leak the parent's provider
        # keys / cloud creds into it. No explicit env -> a scrubbed copy (secrets stripped,
        # PATH/PYTHONPATH/etc. kept so the server still starts).
        if env is None:
            from .sandbox import scrubbed_child_env
            env = scrubbed_child_env()
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env)
        self._id = 0
        self._timeout = timeout
        self._lock = threading.Lock()
        self._responses: dict = {}
        self._resp_events: dict = {}
        self._notifications: "queue.Queue" = queue.Queue()
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while self._alive:
                headers = {}
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        return
                    s = line.decode("ascii", "replace").strip()
                    if s == "":
                        break
                    if ":" in s:
                        k, v = s.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length", "0"))
                body = self._proc.stdout.read(n)
                msg = json.loads(body.decode("utf-8"))
                if "id" in msg and ("result" in msg or "error" in msg):
                    rid = msg["id"]
                    self._responses[rid] = msg
                    ev = self._resp_events.get(rid)
                    if ev:
                        ev.set()
                elif "method" in msg:
                    self._notifications.put((msg["method"], msg.get("params", {}) or {}))
        except Exception:  # noqa: BLE001
            return

    def _send(self, msg: dict):
        data = json.dumps(msg).encode("utf-8")
        self._proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        self._proc.stdin.flush()

    def request(self, method: str, params: dict):
        with self._lock:
            self._id += 1
            rid = self._id
        ev = threading.Event()
        self._resp_events[rid] = ev
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            if not ev.wait(self._timeout):
                raise TimeoutError(f"LSP request {method} timed out")
            msg = self._responses.pop(rid, {})
            if "error" in msg:
                raise RuntimeError(str(msg["error"]))
            return msg.get("result")
        finally:
            # F21: always clean up both maps, incl. the timeout path — otherwise each timed-out
            # request leaks an entry (and a late reply) for the life of the transport.
            self._resp_events.pop(rid, None)
            self._responses.pop(rid, None)

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def notifications(self):
        out = []
        while True:
            try:
                out.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self):
        self._alive = False
        try:
            self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
