"""The command-line side of the terminal: ``tsm remote terminal <node>``.

It speaks the controller's terminal WebSocket (binary frames = the terminal stream, JSON text frames
= control) and turns the local TTY into a raw pipe, so ``vim``, ``htop`` and ``sudo`` behave as over
SSH. Without a TTY (a pipe or a test) it still works: stdin EOF sends Ctrl-D so the remote shell
ends once it has run what it was given.

Local escape: ``Ctrl-]`` followed by ``.`` disconnects, the way telnet does. Nothing else is
intercepted, so Ctrl-C and friends reach the remote side.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import threading
from typing import Any, Callable, Optional

ESCAPE = b"\x1d"                 # Ctrl-]
EOF_CHAR = b"\x04"               # Ctrl-D


class TerminalClientError(RuntimeError):
    pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            select.select([], [fd], [], 1.0)
            continue
        view = view[n:]


def ws_url(api_base: str, path: str, ticket: str) -> str:
    """http(s)://host:port  ->  ws(s)://host:port/path?ticket=…"""
    base = api_base.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    else:
        raise TerminalClientError(f"unsupported API address {api_base!r}")
    return f"{base}{path}?ticket={ticket}"


def _start_reader(loop: asyncio.AbstractEventLoop, fd: int,
                  queue: "asyncio.Queue[Optional[bytes]]") -> Callable[[], None]:
    """Feed stdin into ``queue``; ``None`` marks EOF. Returns a function that stops the reader."""
    def on_ready() -> None:
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)
            queue.put_nowait(None)
        else:
            queue.put_nowait(data)

    try:
        loop.add_reader(fd, on_ready)
        return lambda: loop.remove_reader(fd)
    except (OSError, ValueError, PermissionError, NotImplementedError):
        stop = threading.Event()               # a regular file on stdin cannot be polled: read it in a thread

        def pump() -> None:
            while not stop.is_set():
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                loop.call_soon_threadsafe(queue.put_nowait, data or None)
                if not data:
                    return

        threading.Thread(target=pump, daemon=True).start()
        return stop.set


async def run_session(url: str, *, stdin_fd: int, stdout_fd: int, size: Callable[[], tuple[int, int]],
                      raw: bool = True, connect: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """Run one terminal session to the end. Returns ``{"session", "exit", "reason"}``."""
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    if connect is None:
        from websockets.asyncio.client import connect as ws_connect
        connect = ws_connect
    loop = asyncio.get_running_loop()
    result: dict[str, Any] = {"session": None, "exit": None, "reason": None}
    try:
        # proxy=None: this talks to the controller you named, never through an environment proxy
        ws = await connect(url, proxy=None, max_size=2 * 1024 ** 2, open_timeout=10,
                           ping_interval=20, ping_timeout=20)
    except InvalidStatus as exc:
        raise TerminalClientError(f"the controller refused the terminal (HTTP {exc.response.status_code}) — "
                                  f"the ticket may have expired; try again") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise TerminalClientError(f"could not open the terminal connection ({type(exc).__name__})") from exc

    saved = None
    is_tty = os.isatty(stdin_fd)
    if raw and is_tty:
        import termios
        import tty
        saved = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    stop_reader = _start_reader(loop, stdin_fd, queue)

    async def send_resize() -> None:
        cols, rows = size()
        with contextlib.suppress(ConnectionClosed):
            await ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    winch = False
    if raw and is_tty:
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(signal.SIGWINCH, lambda: asyncio.ensure_future(send_resize()))
            winch = True

    async def sender() -> None:
        pending = False
        while True:
            data = await queue.get()
            if data is None:
                if not is_tty:
                    await ws.send(EOF_CHAR)           # let the remote shell finish and exit
                return
            if pending:
                pending = False
                if data[:1] == b".":
                    result["reason"] = "disconnected (Ctrl-] .)"
                    return
                data = ESCAPE + data
            if data.endswith(ESCAPE):
                pending, data = True, data[:-1]
            cut = data.find(ESCAPE + b".")
            if cut >= 0:
                if cut:
                    await ws.send(data[:cut])
                result["reason"] = "disconnected (Ctrl-] .)"
                return
            if data:
                await ws.send(data)

    async def receiver() -> None:
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    write_all(stdout_fd, msg)
                    continue
                try:
                    frame = json.loads(msg)
                except ValueError:
                    continue
                kind = frame.get("type") if isinstance(frame, dict) else None
                if kind == "hello":
                    result["session"] = frame.get("session")
                elif kind == "notice":
                    write_all(stdout_fd, f"\r\n[tsm] {frame.get('text', '')}\r\n".encode("utf-8", "replace"))
                elif kind == "exit":
                    result["exit"] = frame
                elif kind == "error":
                    raise TerminalClientError(str(frame.get("error", "terminal error")))
        except ConnectionClosed:
            if result["exit"] is None:
                result["reason"] = result["reason"] or "the connection to the controller was lost"

    tasks = [asyncio.ensure_future(sender()), asyncio.ensure_future(receiver())]
    try:
        if raw and is_tty:
            await send_resize()
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if tasks[0] in done and tasks[1] not in done and result["reason"] is None:
            # stdin ended without a tty: give the remote shell a moment to finish what it was given
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(tasks[1]), 15)
        if tasks[1].done() and not tasks[1].cancelled() and tasks[1].exception():
            raise tasks[1].exception()                  # an error frame from the node
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_reader()
        if winch:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGWINCH)
        if saved is not None:
            import termios
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        with contextlib.suppress(Exception):
            await ws.close()
    if result["reason"] is None:
        ex = result["exit"] or {}
        result["reason"] = ex.get("reason") or "the remote shell ended"
    return result
