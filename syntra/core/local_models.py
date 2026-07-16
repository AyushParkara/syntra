"""On-demand LOCAL model server orchestration (L4/L5).

Syntra talks to already-running OpenAI-compatible endpoints; it does NOT spawn/manage its own
local model servers. This module adds that — the "all-local" capability the references (llama-swap)
demonstrate — by EXTENDING the same patterns Syntra already uses in `proc_session.py`
(injectable spawn, `alive()`, `last_activity`), not by building a new process layer from zero.

L4 — `LocalModelManager`:
  - config-driven `cmd` template with `${MODEL_ID}` / `${PORT}` macros (backend-agnostic:
    llama.cpp / vllm / Ollama — anything OpenAI-compatible);
  - spawn on demand, then READINESS-POLL the server before it's routable (a cold 70B can take
    minutes to load — pairs with the L1 `warming` health state);
  - TTL idle-unload (reusing a `last_activity` timestamp, like ProcSession) to free VRAM;
  - graceful SIGTERM→SIGKILL PROCESS-GROUP stop (`start_new_session=True`) — fixes the bare
    `proc.terminate()` in ProcessManager that can orphan GPU-pinning children.

L5 — `as_provider_endpoint(model_id)` returns a `ProviderEndpoint` for the running server so the
existing router / route_health / registry treat it as "just another provider" — zero routing-code
changes (it's a local, no-auth endpoint whose base_url is on localhost).

Spawn + probe are INJECTABLE so the whole lifecycle is unit-tested with a fake process and NO
network. `signal`/`os` group-kill is only used on the real-Popen path.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass


DEFAULT_START_PORT = 5800          # llama-swap's default; each new local model takes the next free port
DEFAULT_MAX_WAIT_S = 300.0         # a cold multi-GB model can take minutes to load (see L1 warming)
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_HEALTH_PATH = "/health"    # llama.cpp/vllm expose /health; overridable, "none" = ready-on-spawn
_GRACEFUL_STOP_S = 5.0             # wait after SIGTERM before SIGKILL


class LocalModelReadyTimeout(RuntimeError):
    """A spawned local model server never became ready within max_wait_s."""


@dataclass
class LocalModelSpec:
    """L6: a user-declared local model (from providers.json `local_models`). Declarative — no
    process is spawned until it's actually needed (lazy ensure())."""
    model_id: str
    cmd: str
    port: int = 0            # 0 = auto-allocate a free port
    ttl_s: float = 0.0       # idle-unload TTL; 0 = never auto-unload
    cwd: str = ""


def parse_local_model_specs(raw: dict) -> list["LocalModelSpec"]:
    """Parse the `local_models` array of a providers config into typed specs. Tolerant: a row
    with no `model_id` or no `cmd` is dropped (never crashes load); a non-list is []. Pure."""
    rows = (raw or {}).get("local_models")
    if not isinstance(rows, list):
        return []
    out: list[LocalModelSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("model_id", "") or "").strip()
        cmd = str(row.get("cmd", "") or "").strip()
        if not mid or not cmd:
            continue
        try:
            port = int(row.get("port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            ttl = float(row.get("ttl_s", 0) or 0)
        except (TypeError, ValueError):
            ttl = 0.0
        out.append(LocalModelSpec(model_id=mid, cmd=cmd, port=port, ttl_s=ttl,
                                  cwd=str(row.get("cwd", "") or "")))
    return out


@dataclass
class LocalServer:
    model_id: str
    port: int
    proc: object                   # subprocess.Popen | fake
    base_url: str
    ready: bool = False
    last_activity: float = 0.0

    def alive(self) -> bool:
        poll = getattr(self.proc, "poll", lambda: 0)
        try:
            return poll() is None
        except Exception:  # noqa: BLE001
            return False


class LocalModelManager:
    """Spawn / health-check / TTL-unload local OpenAI-compatible model servers, and expose each
    as a ProviderEndpoint. Pure bookkeeping + real (or injected) spawn/probe."""

    def __init__(self, *, spawn=None, probe=None, host: str = "127.0.0.1",
                 start_port: int = DEFAULT_START_PORT, health_path: str = DEFAULT_HEALTH_PATH):
        # spawn(cmd, cwd) -> process-like; probe(base_url) -> bool ready. Both injectable for tests.
        self._spawn = spawn or self._default_spawn
        self._probe = probe or self._default_probe
        self._host = host
        self._next_port = int(start_port)
        self._health_path = health_path
        self._servers: dict[str, LocalServer] = {}

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def render_cmd(template: str, *, model_id: str, port: int) -> str:
        """Substitute ${MODEL_ID} / ${PORT} in a backend-agnostic launch template. Pure."""
        return (template or "").replace("${MODEL_ID}", str(model_id)).replace("${PORT}", str(port))

    def _default_spawn(self, cmd: str, cwd: str | None = None):
        # Real launch: own process GROUP (start_new_session) so we can signal the WHOLE tree on
        # stop — a llama.cpp/vllm server forks children that pin the GPU; a bare terminate() on
        # the parent can orphan them. stdout/stderr are dropped to DEVNULL (the model server's
        # logs aren't Syntra's transcript). Shell form mirrors ProcessManager's fallback path.
        #
        # F49 — TRUST BOUNDARY: `cmd` is the operator's OWN launch template from providers.json
        # (`local_models[].cmd`, with ${MODEL_ID}/${PORT} substituted), so shell=True runs only
        # what the machine owner already put in their config — not model/agent output. This is
        # NOT a path for untrusted input. If you ever wire a less-trusted source into a local
        # launch command, switch to shlex.split + shell=False (and validate model_id) first.
        return subprocess.Popen(
            cmd, shell=True, cwd=cwd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _default_probe(self, base_url: str) -> bool:
        # Real readiness check: GET {base_url}{health_path}, 200 == ready. Short timeout; any
        # error (connection refused while still spawning) == not ready yet. No external deps.
        if self._health_path == "none":
            return True
        import urllib.request
        url = base_url.rstrip("/") + self._health_path
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:  # noqa: S310 - localhost only
                return 200 <= getattr(r, "status", r.getcode()) < 300
        except Exception:  # noqa: BLE001 - not ready / not up yet
            return False

    def _base_url(self, port: int) -> str:
        return f"http://{self._host}:{port}/v1"

    # ---------------------------------------------------------------- lifecycle

    def start(self, model_id: str, *, cmd: str, port: int | None = None,
              cwd: str | None = None, max_wait_s: float = DEFAULT_MAX_WAIT_S,
              poll_interval_s: float = DEFAULT_POLL_INTERVAL_S, now: float | None = None):
        """Spawn a local server for `model_id` and BLOCK until it's ready (health probe) or the
        readiness deadline passes (-> LocalModelReadyTimeout, after killing the dud process).
        Returns the ProviderEndpoint for the ready server. Reuses an already-ready server."""
        existing = self._servers.get(model_id)
        if existing is not None and existing.ready and existing.alive():
            self.touch(model_id, now=now)
            return self.as_provider_endpoint(model_id)
        # F46: a stale/half-started entry (present but not ready&alive, e.g. an interrupted prior
        # start) must be terminated BEFORE we overwrite _servers[model_id] — otherwise the old
        # still-alive process is dropped from the dict and never reaped (orphaned server/GPU).
        if existing is not None:
            self.stop(model_id)

        chosen_port = int(port if port is not None else self._alloc_port())
        base_url = self._base_url(chosen_port)
        launch = self.render_cmd(cmd, model_id=model_id, port=chosen_port)
        proc = self._spawn(launch, cwd)
        srv = LocalServer(model_id=model_id, port=chosen_port, proc=proc, base_url=base_url,
                          last_activity=(time.time() if now is None else now))
        self._servers[model_id] = srv

        # Readiness poll: don't route to a model that's still cold-loading. Driven by REAL
        # elapsed wall-clock (a cold load is a real duration, not a probe count).
        deadline = time.monotonic() + float(max_wait_s)
        while True:
            if self._probe(base_url):
                srv.ready = True
                return self.as_provider_endpoint(model_id)
            if time.monotonic() >= deadline:
                # Never came up -> kill the dud and surface an explicit timeout (never silently
                # leave a half-spawned server around or route to it).
                self.stop(model_id)
                raise LocalModelReadyTimeout(
                    f"local model {model_id!r} not ready on {base_url} within {max_wait_s}s")
            time.sleep(max(0.0, poll_interval_s))

    def ensure(self, spec: "LocalModelSpec", registry=None):
        """L6: start `spec` if it isn't already running+ready, and (if a registry is given)
        register its endpoint so the router can reach it — idempotent (a second call reuses the
        running server and does NOT double-register). Returns the ProviderEndpoint. This is the
        lazy, on-demand entry point: a declared local model costs nothing until first needed."""
        ep = self.start(spec.model_id, cmd=spec.cmd, port=(spec.port or None), cwd=(spec.cwd or None))
        if registry is not None and ep is not None:
            existing = [e for e in registry.endpoints if e.name == ep.name]
            if not existing:
                registry.endpoints.append(ep)
        return ep

    @staticmethod
    def _port_free(port: int) -> bool:
        """F48: is the port actually bindable on the host? (_alloc only tracked our own servers,
        so a port held by an UNTRACKED process would bind-fail on spawn → a 300s ready-timeout
        instead of a fast, clear error.)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _alloc_port(self) -> int:
        used = {s.port for s in self._servers.values()}
        p = self._next_port
        # skip ports we already track AND ports the OS reports as busy (untracked processes).
        while p in used or not self._port_free(p):
            p += 1
        self._next_port = p + 1
        return p

    def is_ready(self, model_id: str) -> bool:
        s = self._servers.get(model_id)
        return bool(s and s.ready and s.alive())

    def touch(self, model_id: str, *, now: float | None = None) -> None:
        """Mark a server recently-used so TTL idle-unload won't reap it (call on each route)."""
        s = self._servers.get(model_id)
        if s is not None:
            s.last_activity = time.time() if now is None else now

    def unload_idle(self, *, ttl_s: float, now: float | None = None) -> list[str]:
        """Stop every server idle longer than `ttl_s` (frees VRAM between uses). Returns the ids
        stopped, sorted. ttl_s <= 0 disables (never unloads)."""
        if ttl_s <= 0:
            return []
        ref = time.time() if now is None else now
        stale = [mid for mid, s in self._servers.items()
                 if (ref - float(s.last_activity or 0.0)) > ttl_s]
        for mid in stale:
            self.stop(mid)
        return sorted(stale)

    def as_provider_endpoint(self, model_id: str):
        """L5: a ProviderEndpoint for the running server so the router/registry treat it as just
        another (local, no-auth) provider. None if the model isn't started."""
        s = self._servers.get(model_id)
        if s is None:
            return None
        from ..providers.openai_compat import ProviderEndpoint
        return ProviderEndpoint(
            name=f"local:{model_id}",
            display_name=f"local {model_id}",
            base_url=s.base_url,
            api_key="no-auth",
            credential_state="no-auth",
            allowed_models=(model_id,),
            kind="openai",
        )

    def stop(self, model_id: str) -> str:
        """Graceful SIGTERM→SIGKILL PROCESS-GROUP stop of one server (frees GPU + children)."""
        s = self._servers.pop(model_id, None)
        if s is None:
            return f"error: no such local model {model_id!r}"
        s.ready = False
        self._terminate(s.proc)
        return f"stopped {model_id}"

    def _terminate(self, proc) -> None:
        """SIGTERM the process GROUP, wait briefly, then SIGKILL if still alive. Falls back to
        proc.terminate()/kill() for a fake/simple process. Best-effort — never raises."""
        # Real Popen with our own session: signal the whole group via -pgid.
        pid = getattr(proc, "pid", None)
        used_group = False
        if pid is not None and hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                used_group = True
            except (ProcessLookupError, PermissionError, OSError):
                used_group = False
        if not used_group:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # Wait for graceful exit, then force-kill.
        # F47: proc.wait() returns when the PARENT exits, NOT when the process GROUP is empty. A
        # parent that exits fast but leaves SIGTERM-ignoring children would otherwise skip the
        # group SIGKILL below and orphan them (the exact leak this module exists to prevent). So
        # after a group SIGTERM we do NOT early-return on wait — we still SIGKILL the whole group
        # to sweep any survivors. (Non-group / fake-proc paths still return after wait.)
        graceful_exited = False
        try:
            waiter = getattr(proc, "wait", None)
            if waiter is not None:
                waiter(timeout=_GRACEFUL_STOP_S)
                graceful_exited = True
                if not used_group:
                    return   # single process, confirmed exited — nothing more to sweep
        except Exception:  # noqa: BLE001 - didn't exit in time / fake proc
            pass
        if pid is not None and hasattr(os, "killpg") and used_group:
            try:
                # sweep the group even after a graceful parent exit (SIGKILL is a no-op if empty).
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                if graceful_exited:
                    return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def stop_all(self) -> int:
        """Stop EVERY running local server (call on run/session exit so a model server can't
        outlive Syntra). Returns how many were reaped. Best-effort per server."""
        n = 0
        for mid in list(self._servers.keys()):
            self.stop(mid)
            n += 1
        return n

    def running(self) -> list[str]:
        return sorted(self._servers.keys())
