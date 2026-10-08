"""Pseudo-terminal sessions for the browser / CLI terminal (served by ``tsm-termd``).

A session is a login shell on a PTY, running as the user the daemon runs as (never root unless the
operator ran the unit as root, which setup never does). The manager enforces, on every call, what
the root-owned policy currently says: terminal off → no new sessions and running ones are closed;
at most ``terminal_max_sessions`` at once; an idle limit and an absolute time limit per session.

Output can be recorded to an asciicast v2 file (0600, size-capped, newest 50 kept) so there is a
trace of what was done on a machine nobody is sitting at. Only *output* is recorded — what is typed
(including a sudo password, which the terminal does not echo) never is.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import json
import os
import pwd
import re
import secrets
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any, Optional

from .policy import PolicySource, RemotePolicy

RECORD_CAP = 8 * 1024 ** 2
RECORD_KEEP = 50
QUEUE_MAX = 256
READ_CHUNK = 65536
MAX_INPUT = 1024 ** 2
_CAST_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}\.cast\Z")
_BAD_SHELLS = ("nologin", "false", "sync", "halt", "shutdown")


class TerminalError(RuntimeError):
    pass


def clamp_size(cols: Any, rows: Any) -> tuple[int, int]:
    def one(v: Any, lo: int, hi: int, default: int) -> int:
        return max(lo, min(hi, v)) if isinstance(v, int) and not isinstance(v, bool) else default
    return one(cols, 20, 500, 80), one(rows, 5, 200, 24)


def pick_shell(configured: Optional[str] = None) -> str:
    """The configured shell, else the user's login shell, else bash/sh — never nologin/false."""
    try:
        allowed = {ln.strip() for ln in Path("/etc/shells").read_text().splitlines()
                   if ln.startswith("/") and not ln.startswith("#")}
    except OSError:
        allowed = set()
    candidates = []
    if configured:
        candidates.append(configured)
    with contextlib.suppress(KeyError):
        candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
    candidates += ["/bin/bash", "/usr/bin/bash", "/bin/sh"]
    for c in candidates:
        if (c and os.path.isabs(c) and os.access(c, os.X_OK) and not c.endswith(_BAD_SHELLS)
                and (configured == c or not allowed or c in allowed or c in ("/bin/bash", "/bin/sh"))):
            return c
    raise TerminalError("no usable login shell on this machine")


class Recorder:
    """asciicast v2 writer (https://docs.asciinema.org/manual/asciicast/v2/) with a hard size cap."""

    def __init__(self, directory: Path, session_id: str, cols: int, rows: int, title: str):
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        self.started = time.time()
        self.path = directory / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.started)) + f"-{session_id}.cast")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._fh = os.fdopen(fd, "w", encoding="utf-8")
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.bytes = 0
        self.full = False
        self._write({"version": 2, "width": cols, "height": rows, "timestamp": int(self.started),
                     "title": title[:200], "env": {"TERM": "xterm-256color"}})
        self._prune(directory)

    def _write(self, obj: Any) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self.bytes += len(line.encode("utf-8"))
        self._fh.write(line)

    @staticmethod
    def _prune(directory: Path) -> None:
        files = sorted((p for p in directory.iterdir() if _CAST_RE.match(p.name)), key=lambda p: p.name)
        for p in files[:-RECORD_KEEP]:
            with contextlib.suppress(OSError):
                p.unlink()

    def output(self, data: bytes) -> None:
        if self.full or self._fh.closed:
            return
        text = self._dec.decode(data)
        if not text:
            return
        if self.bytes + len(text) * 4 > RECORD_CAP:
            self.full = True
            self._write([round(time.time() - self.started, 6), "m", "recording stopped: size limit"])
            return
        self._write([round(time.time() - self.started, 6), "o", text])

    def resize(self, cols: int, rows: int) -> None:
        if not self.full and not self._fh.closed:
            self._write([round(time.time() - self.started, 6), "r", f"{cols}x{rows}"])

    def close(self, reason: str) -> None:
        if self._fh.closed:
            return
        with contextlib.suppress(OSError, ValueError):
            self._write([round(time.time() - self.started, 6), "m", f"session closed: {reason}"])
            self._fh.flush()
            os.fsync(self._fh.fileno())
        self._fh.close()


class Session:
    def __init__(self, sid: str, actor: str, proc: subprocess.Popen, master: int, recorder: Optional[Recorder],
                 cols: int, rows: int):
        self.id, self.actor, self.proc, self.master, self.recorder = sid, actor, proc, master, recorder
        self.cols, self.rows = cols, rows
        self.created = self.last_input = time.monotonic()
        self.opened_at = time.time()
        self.bytes_in = self.bytes_out = 0
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.closed = False
        self.reason = ""
        self.paused = False
        self.warned_idle = False
        self.exit_code: Optional[int] = None

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "actor": self.actor, "opened_at": self.opened_at,
                "idle_s": int(time.monotonic() - self.last_input), "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out, "recorded": self.recorder is not None}


def session_members(sid: int, proc: str = "/proc") -> list[int]:
    """PIDs whose session id is ``sid`` (the shell the PTY started is its session leader)."""
    out = []
    try:
        names = os.listdir(proc)
    except OSError:
        return out
    for name in names:
        if not name.isdigit() or int(name) == sid:
            continue
        try:
            with open(f"{proc}/{name}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 3 and fields[3] == str(sid) and fields[0] != "Z":
            out.append(int(name))
    return out


class TerminalManager:
    def __init__(self, policy: PolicySource, record_dir: str | Path, shell: Optional[str] = None,
                 node_id: str = "?"):
        self.policy = policy
        self.record_dir = Path(record_dir)
        self.shell = shell
        self.node_id = node_id
        self.sessions: dict[str, Session] = {}
        self._reaper: Optional[asyncio.Task] = None

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_forever())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for s in list(self.sessions.values()):
            await self.close(s, "terminal service stopping")

    def status(self) -> dict[str, Any]:
        pol = self.policy.current()
        return {"policy": pol.as_dict(), "sessions": [s.describe() for s in self.sessions.values()],
                "user": _whoami(), "shell": _safe_shell(self.shell)}

    # ---- sessions -------------------------------------------------------------------------
    async def open(self, cols: int, rows: int, actor: str = "user") -> Session:
        pol = self.policy.current()
        if not pol.terminal:
            why = f" ({pol.error})" if pol.error else ""
            raise TerminalError(f"the terminal is not enabled on node {self.node_id}{why} — "
                                f"on that node run: sudo tsm remote enable terminal")
        if len(self.sessions) >= pol.terminal_max_sessions:
            raise TerminalError(f"{len(self.sessions)} terminal session(s) already open on this node "
                                f"(limit {pol.terminal_max_sessions}); close one first")
        cols, rows = clamp_size(cols, rows)
        shell = pick_shell(self.shell)
        try:
            info = pwd.getpwuid(os.getuid())
            home, user = info.pw_dir if os.path.isdir(info.pw_dir) else "/", info.pw_name
        except KeyError:
            home, user = "/", str(os.getuid())
        env = {"TERM": "xterm-256color", "HOME": home, "USER": user, "LOGNAME": user, "SHELL": shell,
               "LANG": os.environ.get("LANG", "C.UTF-8"), "TSM_TERMINAL": "1",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        sid = secrets.token_hex(6)
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            proc = subprocess.Popen([shell, "-l"], stdin=slave, stdout=slave, stderr=slave, cwd=home, env=env,
                                    close_fds=True, start_new_session=True,
                                    preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise TerminalError(f"could not start {shell}: {exc.strerror or exc}") from exc
        os.close(slave)
        os.set_blocking(master, False)
        recorder = None
        if pol.record_terminal:
            try:
                recorder = Recorder(self.record_dir, sid, cols, rows, f"node {self.node_id} terminal ({actor})")
            except OSError:
                recorder = None                          # recording trouble must not lock the operator out
        sess = Session(sid, actor, proc, master, recorder, cols, rows)
        self.sessions[sid] = sess
        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._readable, sess)
        loop.create_task(self._watch_exit(sess))
        self.start()
        return sess

    def _readable(self, sess: Session) -> None:
        try:
            data = os.read(sess.master, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:                                    # EIO: the shell closed its side
            data = b""
        loop = asyncio.get_running_loop()
        if not data:
            with contextlib.suppress(Exception):
                loop.remove_reader(sess.master)
            return
        sess.bytes_out += len(data)
        if sess.recorder:
            sess.recorder.output(data)
        sess.queue.put_nowait(("out", data))
        if sess.queue.qsize() >= QUEUE_MAX and not sess.paused:
            sess.paused = True
            loop.remove_reader(sess.master)

    async def _watch_exit(self, sess: Session) -> None:
        loop = asyncio.get_running_loop()
        code = await loop.run_in_executor(None, sess.proc.wait)
        sess.exit_code = code
        await asyncio.sleep(0.15)                          # let the last output drain
        with contextlib.suppress(Exception):
            self._readable(sess)
        await self.close(sess, f"shell exited ({code})")

    async def next(self, sess: Session) -> tuple[str, Any]:
        item = await sess.queue.get()
        if sess.paused and sess.queue.qsize() < QUEUE_MAX // 2 and not sess.closed:
            sess.paused = False
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().add_reader(sess.master, self._readable, sess)
        return item

    async def write(self, sess: Session, data: bytes) -> None:
        if sess.closed:
            raise TerminalError("session is closed")
        if len(data) > MAX_INPUT:
            raise TerminalError("input too large")
        sess.last_input = time.monotonic()
        sess.warned_idle = False
        sess.bytes_in += len(data)
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view:
            try:
                n = os.write(sess.master, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TerminalError("the shell is not reading its input") from None
                await asyncio.sleep(0.01)
            except OSError as exc:
                raise TerminalError(f"write failed: {exc.strerror or exc}") from exc

    def resize(self, sess: Session, cols: Any, rows: Any) -> None:
        cols, rows = clamp_size(cols, rows)
        if sess.closed or (cols, rows) == (sess.cols, sess.rows):
            return
        sess.cols, sess.rows = cols, rows
        with contextlib.suppress(OSError):
            fcntl.ioctl(sess.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if sess.recorder:
            sess.recorder.resize(cols, rows)

    async def close(self, sess: Session, reason: str) -> None:
        if sess.closed:
            return
        sess.closed = True
        sess.reason = reason
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            loop.remove_reader(sess.master)
        if sess.proc.poll() is None:
            for sig, wait in ((signal.SIGHUP, 1.5), (signal.SIGKILL, 2.0)):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(sess.proc.pid, sig)
                end = time.monotonic() + wait
                while sess.proc.poll() is None and time.monotonic() < end:
                    await asyncio.sleep(0.05)
                if sess.proc.poll() is not None:
                    break
        # Background jobs of a job-control shell (or of a shell that does not pass SIGHUP on, like dash) run
        # in their own process groups but in the session the PTY created: end those as well.
        for pid in session_members(sess.proc.pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        sess.exit_code = sess.proc.poll()
        with contextlib.suppress(OSError):
            os.close(sess.master)
        if sess.recorder:
            sess.recorder.close(reason)
        self.sessions.pop(sess.id, None)
        sess.queue.put_nowait(("exit", {"code": sess.exit_code, "reason": reason}))

    # ---- limits ---------------------------------------------------------------------------
    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await self.reap()

    async def reap(self, now: Optional[float] = None) -> None:
        pol: RemotePolicy = self.policy.current()
        now = time.monotonic() if now is None else now
        for s in list(self.sessions.values()):
            if not pol.terminal:
                await self.close(s, "terminal disabled on this node")
            elif now - s.created > pol.terminal_max_s:
                await self.close(s, f"session time limit ({pol.terminal_max_s // 60} min) reached")
            elif now - s.last_input > pol.terminal_idle_s:
                await self.close(s, f"idle for {pol.terminal_idle_s // 60} min")
            elif not s.warned_idle and now - s.last_input > pol.terminal_idle_s - 60:
                s.warned_idle = True
                s.queue.put_nowait(("notice", "closing in about a minute unless you type something"))

    # ---- recordings -----------------------------------------------------------------------
    def recordings(self) -> list[dict[str, Any]]:
        try:
            files = sorted((p for p in self.record_dir.iterdir() if _CAST_RE.match(p.name)), reverse=True)
        except OSError:
            return []
        return [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime} for p in files]

    def read_recording(self, name: str) -> str:
        if not _CAST_RE.match(name):
            raise TerminalError("invalid recording name")
        path = self.record_dir / name
        if path.is_symlink() or not path.is_file():
            raise TerminalError("no such recording")
        return path.read_text(encoding="utf-8", errors="replace")[:RECORD_CAP]


def _whoami() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _safe_shell(configured: Optional[str]) -> Optional[str]:
    try:
        return pick_shell(configured)
    except TerminalError:
        return None
