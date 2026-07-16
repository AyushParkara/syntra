"""Long-running / interactive process sessions (exec_command + write_stdin).

Most tools run one command and finish. Some work needs a PERSISTENT process you
feed input to over several turns (a REPL, a dev server, an interactive prompt).
A unified interactive-exec model (create / reuse / buffer-with-caps + write stdin),
but lean: pipe-based (not PTY), output buffered with a hard cap, idle/total
timeouts, registry keyed by a session id.

The process spawning is real, but the manager is structured so its bookkeeping
(buffering, caps, lifecycle) is unit-tested with a fake process. Output is read
on a background thread into a thread-safe buffer so reads never block the loop.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field

MAX_BUFFER_CHARS = 64 * 1024     # per-session captured output cap
DEFAULT_IDLE_WAIT = 0.4          # seconds to collect output after a write/start
# #256: how long a session may be ALIVE with no new output before we tell the AI it
# looks stuck (likely blocked on an interactive prompt like "continue? (y/n)").
STUCK_IDLE_SECONDS = 45.0


@dataclass
class ProcSession:
    sid: str
    proc: object                  # subprocess.Popen | fake
    _buf: list = field(default_factory=list)
    _lock: object = field(default_factory=threading.Lock)
    _reader: object = None
    closed: bool = False
    # #256: wall-clock of the last output (or spawn). A process that stays alive with no
    # new output past STUCK_IDLE_SECONDS is probably waiting on stdin -> flag it.
    last_activity: float = 0.0

    def _append(self, chunk: str, *, now: float | None = None) -> None:
        with self._lock:
            self._buf.append(chunk)
            self.last_activity = time.time() if now is None else now
            # cap total buffered output
            total = sum(len(c) for c in self._buf)
            while total > MAX_BUFFER_CHARS and len(self._buf) > 1:
                total -= len(self._buf.pop(0))

    def drain(self) -> str:
        with self._lock:
            out = "".join(self._buf)
            self._buf = []
            return out

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._buf)

    def alive(self) -> bool:
        poll = getattr(self.proc, "poll", lambda: 0)
        return (not self.closed) and poll() is None


class ProcessManager:
    """Registry of interactive process sessions. Pure bookkeeping + real spawn."""

    def __init__(self, *, spawn=None):
        # spawn(command, cwd) -> process-like (Popen by default; injectable for tests)
        self._spawn = spawn or self._default_spawn
        self._sessions: dict = {}
        self._counter = 0

    @staticmethod
    def _default_spawn(command: str, cwd: str, *, sandbox: str = "auto"):
        # SECURITY: confine the interactive process exactly like run_command
        # confines one-shot bash — bubblewrap (no network egress, writes only
        # inside the workspace, no host-process/secret access). Without this an
        # exec_command session would run unconfined and defeat the whole sandbox.
        # Falls back to an unconfined shell ONLY when bwrap is absent (the same
        # posture run_command takes in "auto" mode on a host without bwrap).
        from .sandbox import sandboxed_popen_argv, _with_git_hardening
        argv = None if sandbox == "off" else sandboxed_popen_argv(command, cwd)
        if argv is not None:
            proc = subprocess.Popen(argv, cwd=cwd,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            time.sleep(0.05)
            if proc.poll() is None:
                return proc
            try:
                setup_err = (proc.stdout.read() if proc.stdout else "") or ""
            except Exception:  # noqa: BLE001
                setup_err = ""
            try:
                if proc.stdin:
                    proc.stdin.close()
                if proc.stdout:
                    proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            from .sandbox import _bwrap_setup_failed, _warn_no_sandbox_once
            if _bwrap_setup_failed(setup_err):
                _warn_no_sandbox_once()
            else:
                return proc
        # F16: no-bwrap fallback — apply the SAME git-config hardening run_command uses on its
        # non-bwrap path, so an interactive git command in a hostile repo can't execute planted
        # .git/config code just because bwrap is absent.
        return subprocess.Popen(command, shell=True, cwd=cwd,
                                env=_with_git_hardening(None),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)

    def _start_reader(self, sess: ProcSession) -> None:
        out = getattr(sess.proc, "stdout", None)
        if out is None:
            return

        def _read():
            try:
                for line in out:
                    sess._append(line)
            except Exception:  # noqa: BLE001
                pass
        t = threading.Thread(target=_read, name=f"proc-{sess.sid}", daemon=True)
        sess._reader = t
        t.start()

    def open(self, command: str, cwd: str, *, idle_wait: float = DEFAULT_IDLE_WAIT,
             sandbox: str = "auto") -> tuple[str, str]:
        """Start a process; return (session_id, initial_output)."""
        self._counter += 1
        sid = f"proc{self._counter}"
        try:
            proc = self._spawn(command, cwd, sandbox=sandbox)
        except TypeError:
            proc = self._spawn(command, cwd)
        sess = ProcSession(sid=sid, proc=proc, last_activity=time.time())
        self._sessions[sid] = sess
        self._start_reader(sess)
        time.sleep(max(0.0, idle_wait))
        return sid, self._with_hint(sess, sess.drain())

    def stuck_hint(self, sid: str, *, now: float | None = None,
                   idle_threshold: float = STUCK_IDLE_SECONDS) -> str:
        """#256: a note when a session looks STUCK — alive, no buffered output, and idle
        (no new output) past `idle_threshold`. That pattern almost always means the process
        is blocked on an interactive stdin prompt ("continue? (y/n)"), which makes the AI
        look frozen. Returns "" when the session is healthy / has output / has exited /
        doesn't exist — so it's silent unless there's a real stall to act on."""
        sess = self._sessions.get(sid)
        if sess is None or not sess.alive() or sess.has_pending():
            return ""
        now = time.time() if now is None else now
        idle = now - float(sess.last_activity or 0.0)
        if idle < idle_threshold:
            return ""
        return (f"[stuck? no output for {int(idle)}s — {sid} is still running but likely "
                f"waiting on interactive input (e.g. a 'continue? (y/n)' prompt). If you didn't "
                f"expect a prompt, kill it with close_process(session={sid!r}) and retry the "
                f"command non-interactively (add a -y/--yes/--non-interactive flag or pipe input).]")

    def _with_hint(self, sess: "ProcSession", out: str, *, now: float | None = None) -> str:
        """Append the stuck-hint (if any) to a drained output string."""
        hint = self.stuck_hint(sess.sid, now=now)
        if not hint:
            return out
        return (out + ("\n" if out and not out.endswith("\n") else "") + hint) if out else hint

    def write_stdin(self, sid: str, data: str, *, idle_wait: float = DEFAULT_IDLE_WAIT) -> str:
        """Send `data` to a session's stdin; return output produced after."""
        sess = self._sessions.get(sid)
        if sess is None:
            return f"error: no such process session {sid!r}"
        if not sess.alive():
            return f"error: process {sid} has exited\n{sess.drain()}"
        stdin = getattr(sess.proc, "stdin", None)
        if stdin is None:
            return "error: process has no stdin"
        try:
            stdin.write(data if data.endswith("\n") else data + "\n")
            stdin.flush()
        except Exception as e:  # noqa: BLE001
            return f"error: write failed: {e}"
        time.sleep(max(0.0, idle_wait))
        return self._with_hint(sess, sess.drain())

    def read(self, sid: str, *, idle_wait: float = 0.0, now: float | None = None) -> str:
        sess = self._sessions.get(sid)
        if sess is None:
            return f"error: no such process session {sid!r}"
        if idle_wait:
            time.sleep(idle_wait)
        return self._with_hint(sess, sess.drain(), now=now)

    def close(self, sid: str) -> str:
        sess = self._sessions.pop(sid, None)
        if sess is None:
            return f"error: no such process session {sid!r}"
        sess.closed = True
        try:
            sess.proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return f"closed {sid}"

    def sessions(self) -> list:
        return sorted(self._sessions.keys())

    def close_all(self) -> int:
        """#255: terminate EVERY tracked session and empty the registry. Called on run/agent
        exit so an `exec_command` process can't outlive the run that spawned it (an orphaned
        background command otherwise lingers indefinitely). Returns how many were reaped.
        Best-effort per session — a failure to kill one never blocks reaping the rest."""
        n = 0
        for sid in list(self._sessions.keys()):
            sess = self._sessions.pop(sid, None)
            if sess is None:
                continue
            sess.closed = True
            proc = getattr(sess, "proc", None)
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            n += 1
        return n
