"""Where an agent app runs, and how TwinSpark reaches its files and processes there.

An agent app's tools, files and terminal belong to the machine it runs on: when Hermes edits a file or runs
a shell command, it does so on *that* host, with that host's home directory, PATH and credentials. An
adapter therefore never mixes hosts: it reads and writes the app's configuration, starts its processes and
reads its logs through one ``HostOps`` object. ``LocalHost`` is the workstation running the TwinSpark client
(the default); ``SSHHost`` is another machine reached through an injected command runner.

All paths are plain strings in the host's own syntax (POSIX or Windows), because the client's Python may
run on a different operating system from the host.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import shutil
import signal
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, NamedTuple, Optional, Protocol, runtime_checkable

MAX_READ_BYTES = 1024 * 1024        # configuration files; anything larger is not a hand-written config
OUTPUT_LIMIT = 256 * 1024           # captured command output


class RunResult(NamedTuple):
    rc: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PathInfo:
    """``kind``: file · dir · other · broken-link (a symlink whose target is gone) · missing.
    ``target`` is the immediate symlink target, ``mode`` the permission bits of what the path resolves to."""

    kind: str
    is_symlink: bool = False
    target: Optional[str] = None
    executable: bool = False
    mode: Optional[int] = None
    mtime: Optional[float] = None

    @property
    def exists(self) -> bool:
        return self.kind not in ("missing", "broken-link")


MISSING = PathInfo("missing")


@runtime_checkable
class HostOps(Protocol):
    """Operations an adapter needs on the host where the app runs. Every method is async.

    The host is where the app's tools act: a file Hermes edits or a command it runs is on this machine, under
    this home directory. Configure, launch and read logs through the same host, never a mix.
    """

    label: str
    is_local: bool

    async def home(self) -> str: ...
    async def env(self, name: str) -> Optional[str]: ...
    async def platform(self) -> str: ...          # linux · darwin · windows · other
    async def stat(self, path: str) -> PathInfo: ...
    async def exists(self, path: str) -> bool: ...
    async def list_dir(self, path: str) -> list[str]: ...
    async def read_bytes(self, path: str, limit: int = MAX_READ_BYTES, *, head: bool = False) -> bytes: ...
    async def read_text(self, path: str, limit: int = MAX_READ_BYTES) -> str: ...
    async def read_tail(self, path: str, max_bytes: int) -> bytes: ...
    async def write_bytes_atomic(self, path: str, data: bytes, mode: Optional[int] = None) -> None: ...
    async def write_text_atomic(self, path: str, text: str, mode: Optional[int] = None) -> None: ...
    async def backup(self, path: str, dest: Optional[str] = None) -> Optional[str]: ...
    async def remove(self, path: str) -> bool: ...
    async def make_dirs(self, path: str, mode: int = 0o700) -> None: ...
    async def which(self, name: str) -> Optional[str]: ...
    async def run(self, argv: list[str], timeout: float,
                  env: Optional[Mapping[str, str]] = None) -> RunResult: ...
    async def pid_alive(self, pid: int) -> bool: ...
    async def process_args(self, pid: int) -> Optional[str]: ...
    async def start_detached(self, argv: list[str], env: Optional[Mapping[str, str]], log_path: str,
                             err_path: Optional[str] = None) -> int: ...
    async def terminate(self, pid: int, grace: float = 10.0) -> bool: ...


def _platform_name(value: str) -> str:
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    return "darwin" if value == "darwin" else value


def _decode(data: bytes) -> str:
    return data[:OUTPUT_LIMIT].decode("utf-8", "replace")


def _default_backup_name(path: str) -> str:
    return f"{path}.tsm-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bak"


def _stat_or_none(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except OSError:
        return None


def _keep_owner(path: str, like: Optional[os.stat_result]) -> None:
    """When running as root, give ``path`` the owner of ``like`` (no-op for a normal user)."""
    if like is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
        with contextlib.suppress(OSError):
            os.chown(path, like.st_uid, like.st_gid)


class LocalHost:
    """The workstation itself: pathlib/os for files, subprocess for commands.

    ``environ`` and ``home`` default to this process's environment and home directory; tests pass their own
    so nothing touches the real home. Commands inherit ``environ`` (plus per-call ``env``), the same
    environment a user's own launch would see.
    """

    is_local = True

    def __init__(self, *, environ: Optional[Mapping[str, str]] = None, home: Optional[str] = None,
                 platform: Optional[str] = None, label: str = "this computer"):
        self._environ = dict(os.environ if environ is None else environ)
        self._home = home
        self._platform = platform or _platform_name(sys.platform)
        self.label = label
        # Processes this object started: polled so they never linger as zombies, and so pid_alive is exact.
        self._children: dict[int, subprocess.Popen] = {}

    async def home(self) -> str:
        if self._home:
            return self._home
        name = "USERPROFILE" if self._platform == "windows" else "HOME"
        return self._environ.get(name) or os.path.expanduser("~")

    async def env(self, name: str) -> Optional[str]:
        return self._environ.get(name)

    async def platform(self) -> str:
        return self._platform

    async def stat(self, path: str) -> PathInfo:
        try:
            st = os.lstat(path)
        except OSError:
            return MISSING
        link = stat_mod.S_ISLNK(st.st_mode)
        target = None
        if link:
            with contextlib.suppress(OSError):
                target = os.readlink(path)
            try:
                st = os.stat(path)
            except OSError:
                return PathInfo("broken-link", True, target)
        kind = "dir" if stat_mod.S_ISDIR(st.st_mode) else "file" if stat_mod.S_ISREG(st.st_mode) else "other"
        return PathInfo(kind, link, target, os.access(path, os.X_OK) if kind == "file" else False,
                        stat_mod.S_IMODE(st.st_mode), st.st_mtime)

    async def exists(self, path: str) -> bool:
        return (await self.stat(path)).exists

    async def list_dir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    async def read_bytes(self, path: str, limit: int = MAX_READ_BYTES, *, head: bool = False) -> bytes:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            if head:
                return data[:limit]
            raise ValueError(f"{path} is larger than {limit // 1024} KiB; TwinSpark does not edit files that big")
        return data

    async def read_text(self, path: str, limit: int = MAX_READ_BYTES) -> str:
        # Decoding bytes (rather than text mode) keeps \r\n line endings as they are.
        return (await self.read_bytes(path, limit)).decode("utf-8")

    async def read_tail(self, path: str, max_bytes: int) -> bytes:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                return fh.read(max_bytes)
        except OSError:
            return b""

    async def write_bytes_atomic(self, path: str, data: bytes, mode: Optional[int] = None) -> None:
        # Write through a symlink (dotfile setups link config files), never replace the link itself.
        path = os.path.realpath(path) if os.path.islink(path) else path
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        try:
            current = os.stat(path)
        except OSError:
            current = None
        if mode is None:
            mode = stat_mod.S_IMODE(current.st_mode) if current else 0o600
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tsm-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            # Run as root (e.g. sudo) on someone's home: keep the file theirs (a new file goes to the directory's
            # owner), or the app running as that user could no longer read its own .env.
            _keep_owner(tmp, current or _stat_or_none(directory))
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        if self._platform != "windows":
            with contextlib.suppress(OSError):
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)

    async def write_text_atomic(self, path: str, text: str, mode: Optional[int] = None) -> None:
        await self.write_bytes_atomic(path, text.encode("utf-8"), mode)

    async def backup(self, path: str, dest: Optional[str] = None) -> Optional[str]:
        """Byte-exact copy, readable only by the owner (backups of .env hold secrets)."""
        if not os.path.isfile(path):
            return None
        dest = dest or _default_backup_name(path)
        os.makedirs(os.path.dirname(dest) or ".", mode=0o700, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with open(path, "rb") as src, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(dest)
            raise
        os.chmod(dest, 0o600)
        _keep_owner(dest, _stat_or_none(path))
        return dest

    async def remove(self, path: str) -> bool:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False

    async def make_dirs(self, path: str, mode: int = 0o700) -> None:
        """Create missing directories with exactly ``mode`` (not reduced by the umask), owned like their parent."""
        missing, current = [], os.path.abspath(path)
        while not os.path.isdir(current):
            missing.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        os.makedirs(path, mode=mode, exist_ok=True)
        for directory in reversed(missing):
            os.chmod(directory, mode)
            _keep_owner(directory, _stat_or_none(os.path.dirname(directory)))

    async def which(self, name: str) -> Optional[str]:
        return shutil.which(name, path=self._environ.get("PATH", ""))

    def _merged_env(self, env: Optional[Mapping[str, str]]) -> dict[str, str]:
        merged = dict(self._environ)
        merged.update(env or {})
        return merged

    async def run(self, argv: list[str], timeout: float, env: Optional[Mapping[str, str]] = None) -> RunResult:
        posix = self._platform != "windows"
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._merged_env(env), cwd=await self._cwd(), start_new_session=posix)
        except FileNotFoundError:
            return RunResult(127, "", f"{argv[0]}: not found")
        except PermissionError:
            return RunResult(126, "", f"{argv[0]}: permission denied")
        except OSError as exc:              # e.g. "exec format error" for a broken script
            return RunResult(126, "", f"{argv[0]}: {exc.strerror or exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            await self._kill_now(proc, posix)
            return RunResult(124, "", f"{os.path.basename(argv[0])} did not finish within {timeout:.0f} s")
        except asyncio.CancelledError:
            await self._kill_now(proc, posix)
            raise
        return RunResult(proc.returncode or 0, _decode(out), _decode(err))

    @staticmethod
    async def _kill_now(proc: asyncio.subprocess.Process, posix: bool) -> None:
        # The command ran in its own session, so this also ends anything it spawned.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)

    async def _cwd(self) -> Optional[str]:
        home = await self.home()
        return home if os.path.isdir(home) else None

    def _reap(self) -> None:
        for pid, proc in list(self._children.items()):
            if proc.poll() is not None:
                del self._children[pid]

    async def pid_alive(self, pid: int) -> bool:
        if pid in self._children:
            return self._children[pid].poll() is None
        self._reap()
        if pid <= 0:
            return False
        if self._platform == "windows":
            return _windows_pid_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                     # exists, owned by someone else
        except OSError:
            return False
        try:                                # a zombie answers kill(0) but is not running
            with open(f"/proc/{pid}/stat", "rb") as fh:
                state = fh.read().rsplit(b")", 1)[1].split()[0]
            return state not in (b"Z", b"X")
        except (OSError, IndexError):
            return True

    async def process_args(self, pid: int) -> Optional[str]:
        if self._platform == "windows":
            # no /proc and no ps: ask WMI for the command line (Stop and Restart check it before ending a pid)
            shell = await self.which("powershell") or await self.which("pwsh") or "powershell"
            query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
            result = await self.run([shell, "-NoProfile", "-NonInteractive", "-Command", query], 15)
            return (result.stdout.strip() or None) if result.rc == 0 else None
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read(8192)
            return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
        except OSError:
            pass
        result = await self.run(["ps", "-o", "args=", "-p", str(pid)], 5)
        return (result.stdout.strip() or None) if result.rc == 0 else None

    async def start_detached(self, argv: list[str], env: Optional[Mapping[str, str]], log_path: str,
                             err_path: Optional[str] = None) -> int:
        """Start a long-running process that outlives this request (and the client, if it exits), with its
        output appended to the given log files, its working directory the user's home and no terminal."""
        err_path = err_path or log_path
        for p in {log_path, err_path}:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        kwargs: dict = {}
        if self._platform == "windows":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "ab") as out, open(err_path, "ab") as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    env=self._merged_env(env), cwd=await self._cwd(), close_fds=True, **kwargs)
        self._children[proc.pid] = proc
        return proc.pid

    async def terminate(self, pid: int, grace: float = 10.0) -> bool:
        """SIGTERM (the whole process group when TwinSpark started it as a group leader), then SIGKILL."""
        if not await self.pid_alive(pid):
            return True
        if self._platform == "windows":
            await self.run(["taskkill", "/PID", str(pid), "/T", "/F"], 15)
            return not await self.pid_alive(pid)
        group = False
        with contextlib.suppress(OSError):
            group = os.getpgid(pid) == pid and pid != os.getpgid(0)
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, sig) if group else os.kill(pid, sig)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not await self.pid_alive(pid):
                    return True
                await asyncio.sleep(0.05)
        return not await self.pid_alive(pid)


def _windows_pid_alive(pid: int) -> bool:   # pragma: no cover - exercised on Windows only
    # os.kill(pid, 0) would *terminate* the process on Windows, so ask the kernel instead.
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                              # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


SSHRun = Callable[[list[str], float, Optional[bytes]], Awaitable[tuple[int, bytes, bytes]]]

# Every remote operation is a fixed POSIX sh script; paths and values travel as positional arguments ($1, $2…),
# never inside the script text, so a path with spaces, quotes or $(…) cannot change what runs.
_SH_STAT = r'''p=$1
if [ -L "$p" ]; then l=1; t=$(readlink "$p"); else l=0; t=; fi
if [ -d "$p" ]; then k=dir; elif [ -f "$p" ]; then k=file; elif [ -e "$p" ]; then k=other
elif [ "$l" = 1 ]; then k=broken-link; else k=missing; fi
if [ -f "$p" ] && [ -x "$p" ]; then x=1; else x=0; fi
m=; s=
if [ -e "$p" ]; then
  m=$(stat -L -c %a "$p" 2>/dev/null || stat -L -f %Lp "$p" 2>/dev/null)
  s=$(stat -L -c %Y "$p" 2>/dev/null || stat -L -f %m "$p" 2>/dev/null)
fi
printf '%s\n%s\n%s\n%s\n%s\n%s' "$k" "$l" "$x" "$m" "$s" "$t"'''
_SH_READ = r'''[ -f "$1" ] || exit 3
head -c "$2" "$1" | base64'''
_SH_TAIL = r'''[ -f "$1" ] || exit 0
tail -c "$2" "$1" | base64'''
_SH_WRITE = r'''set -e
p=$1; mode=$2; n=0
while [ -L "$p" ] && [ "$n" -lt 20 ]; do
  t=$(readlink "$p")
  case $t in /*) p=$t ;; *) p=$(dirname "$p")/$t ;; esac
  n=$((n + 1))
done
d=$(dirname "$p")
mkdir -p "$d"
if [ -z "$mode" ]; then mode=$(stat -c %a "$p" 2>/dev/null || stat -f %Lp "$p" 2>/dev/null || echo 600); fi
umask 077
t=$(mktemp "$d/.tsm-XXXXXX")
trap 'rm -f "$t"' EXIT
if printf '' | base64 -d >/dev/null 2>&1; then base64 -d > "$t"; else base64 -D > "$t"; fi
chmod "$mode" "$t"
mv -f "$t" "$p"
trap - EXIT'''
_SH_BACKUP = r'''[ -f "$1" ] || exit 3
umask 077
mkdir -p "$(dirname "$2")"
cp "$1" "$2"
chmod 600 "$2"'''
_SH_START = r'''out=$1; err=$2; shift 2
mkdir -p "$(dirname "$out")" "$(dirname "$err")"
cd "$HOME" 2>/dev/null || true
nohup "$@" >>"$out" 2>>"$err" </dev/null &
echo $!'''
# A zombie still answers kill -0; it counts as gone (an init that does not reap must not make a stop "fail").
_SH_ALIVE_FN = r'''alive() {
  if kill -0 "$1" 2>/dev/null; then
    case $(ps -o stat= -p "$1" 2>/dev/null) in Z*) return 1 ;; esac
    return 0
  fi
  ps -p "$1" >/dev/null 2>&1 && ! ps -o stat= -p "$1" 2>/dev/null | grep -q '^Z'
}
'''
_SH_ALIVE = _SH_ALIVE_FN + r'''if alive "$1"; then echo alive; else echo dead; fi'''
_SH_TERM = _SH_ALIVE_FN + r'''pid=$1; grace=$2; i=0
alive "$pid" || exit 0
kill -TERM "$pid" 2>/dev/null
while alive "$pid" && [ "$i" -lt "$grace" ]; do sleep 1; i=$((i + 1)); done
if alive "$pid"; then kill -KILL "$pid" 2>/dev/null; sleep 1; fi
if alive "$pid"; then exit 1; fi'''


class SSHHost:
    """Another machine, reached through an injected runner (later: the client's SSH connection).

    ``run(argv, timeout, input)`` must execute ``argv`` on the remote host with every element passed as one
    argument (for OpenSSH: ``ssh host -- <shlex.join(argv)>``), feed ``input`` to its stdin and return
    ``(rc, stdout, stderr)``. Files move as base64 through ``sh``; writes go to a temp file in the target's
    directory and are renamed over it, so a dropped connection never leaves a half-written config.

    The remote session is non-interactive: its PATH and environment may lack what the user's terminal has
    (``~/.local/bin``, variables exported in ``.bashrc``). Adapters therefore probe documented install paths
    instead of trusting ``which``.
    """

    is_local = False

    def __init__(self, run: SSHRun, *, label: str = "the remote host", timeout: float = 20.0):
        self._run = run
        self.label = label
        self._timeout = timeout
        self._cache: dict[str, Optional[str]] = {}

    async def _sh(self, script: str, *args: object, timeout: Optional[float] = None,
                  input: Optional[bytes] = None) -> tuple[int, bytes, bytes]:
        argv = ["sh", "-c", script, "tsm", *[str(a) for a in args]]
        return await self._run(argv, timeout or self._timeout, input)

    async def _cached(self, key: str, script: str, *args: object) -> Optional[str]:
        if key not in self._cache:
            rc, out, _ = await self._sh(script, *args)
            self._cache[key] = out.decode("utf-8", "replace") if rc == 0 else None
        return self._cache[key]

    async def home(self) -> str:
        value = await self._cached("home", 'printf %s "$HOME"')
        if not value:
            raise OSError(f"could not read the home directory on {self.label}")
        return value

    async def env(self, name: str) -> Optional[str]:
        value = await self._cached(f"env:{name}", 'printenv "$1"', name)
        return value[:-1] if value and value.endswith("\n") else value

    async def platform(self) -> str:
        value = (await self._cached("uname", "uname -s") or "").strip().lower()
        if value.startswith(("mingw", "msys", "cygwin")):
            return "windows"
        return value or "linux"

    async def stat(self, path: str) -> PathInfo:
        rc, out, _ = await self._sh(_SH_STAT, path)
        if rc != 0:
            return MISSING
        kind, link, exe, mode, mtime, *target = out.decode("utf-8", "replace").split("\n")
        return PathInfo(kind, link == "1", "\n".join(target) or None, exe == "1",
                        int(mode, 8) if mode.isdigit() else None, float(mtime) if mtime.isdigit() else None)

    async def exists(self, path: str) -> bool:
        return (await self.stat(path)).exists

    async def list_dir(self, path: str) -> list[str]:
        rc, out, _ = await self._sh('[ -d "$1" ] || exit 0\nls -1A "$1"', path)
        return sorted(n for n in out.decode("utf-8", "replace").split("\n") if n) if rc == 0 else []

    async def read_bytes(self, path: str, limit: int = MAX_READ_BYTES, *, head: bool = False) -> bytes:
        rc, out, err = await self._sh(_SH_READ, path, limit + 1)
        if rc == 3:
            raise FileNotFoundError(path)
        if rc != 0:
            raise OSError(f"could not read {path} on {self.label}: {_decode(err).strip()[:200]}")
        data = base64.b64decode(b"".join(out.split()))
        if len(data) > limit:
            if head:
                return data[:limit]
            raise ValueError(f"{path} is larger than {limit // 1024} KiB; TwinSpark does not edit files that big")
        return data

    async def read_text(self, path: str, limit: int = MAX_READ_BYTES) -> str:
        return (await self.read_bytes(path, limit)).decode("utf-8")

    async def read_tail(self, path: str, max_bytes: int) -> bytes:
        rc, out, _ = await self._sh(_SH_TAIL, path, max_bytes)
        return base64.b64decode(b"".join(out.split())) if rc == 0 else b""

    async def write_bytes_atomic(self, path: str, data: bytes, mode: Optional[int] = None) -> None:
        rc, _, err = await self._sh(_SH_WRITE, path, "" if mode is None else format(mode, "o"),
                                    input=base64.encodebytes(data), timeout=max(self._timeout, 30.0))
        if rc != 0:
            raise OSError(f"could not write {path} on {self.label}: {_decode(err).strip()[:200]}")

    async def write_text_atomic(self, path: str, text: str, mode: Optional[int] = None) -> None:
        await self.write_bytes_atomic(path, text.encode("utf-8"), mode)

    async def backup(self, path: str, dest: Optional[str] = None) -> Optional[str]:
        dest = dest or _default_backup_name(path)
        rc, _, err = await self._sh(_SH_BACKUP, path, dest)
        if rc == 3:
            return None
        if rc != 0:
            raise OSError(f"could not back up {path} on {self.label}: {_decode(err).strip()[:200]}")
        return dest

    async def remove(self, path: str) -> bool:
        rc, out, _ = await self._sh('[ -e "$1" ] || [ -L "$1" ] || { echo absent; exit 0; }\nrm -f -- "$1"', path)
        return rc == 0 and b"absent" not in out

    async def make_dirs(self, path: str, mode: int = 0o700) -> None:
        rc, _, err = await self._sh('mkdir -p -m "$2" "$1"', path, format(mode, "o"))
        if rc != 0:
            raise OSError(f"could not create {path} on {self.label}: {_decode(err).strip()[:200]}")

    async def which(self, name: str) -> Optional[str]:
        rc, out, _ = await self._sh('command -v "$1"', name)
        found = out.decode("utf-8", "replace").strip()
        return found if rc == 0 and found.startswith("/") else None

    async def run(self, argv: list[str], timeout: float, env: Optional[Mapping[str, str]] = None) -> RunResult:
        # Values in env become part of the remote command line (visible in ps): callers never pass secrets.
        full = ["env", *[f"{k}={v}" for k, v in env.items()], *argv] if env else list(argv)
        rc, out, err = await self._run(full, timeout, None)
        return RunResult(rc, _decode(out), _decode(err))

    async def pid_alive(self, pid: int) -> bool:
        rc, out, _ = await self._sh(_SH_ALIVE, int(pid))
        return rc == 0 and out.strip() == b"alive"

    async def process_args(self, pid: int) -> Optional[str]:
        rc, out, _ = await self._sh('ps -o args= -p "$1"', int(pid))
        return (_decode(out).strip() or None) if rc == 0 else None

    async def start_detached(self, argv: list[str], env: Optional[Mapping[str, str]], log_path: str,
                             err_path: Optional[str] = None) -> int:
        full = ["env", *[f"{k}={v}" for k, v in env.items()], *argv] if env else list(argv)
        rc, out, err = await self._sh(_SH_START, log_path, err_path or log_path, *full)
        text = out.decode("utf-8", "replace").strip()
        if rc != 0 or not text.isdigit():
            raise OSError(f"could not start {os.path.basename(argv[0])} on {self.label}: {_decode(err).strip()[:200]}")
        return int(text)

    async def terminate(self, pid: int, grace: float = 10.0) -> bool:
        seconds = max(1, int(grace))
        rc, _, _ = await self._sh(_SH_TERM, int(pid), seconds, timeout=seconds + 15)
        return rc == 0
