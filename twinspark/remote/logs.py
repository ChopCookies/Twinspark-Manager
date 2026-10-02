"""Read-only log access for a node: journald units, the kernel ring, the previous boot.

The agent first tries ``journalctl`` as its own (unprivileged) user. If the user cannot see system
units, journald says so ("not seeing messages from other users") and the read goes through
``tsm-privd``, which only accepts the allow-listed sources in :mod:`twinspark.remote.privops`.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from . import privops

SOURCES = ["agent", "controller", "privd", "terminal", "docker", "kernel", "previous-boot", "ssh", "network",
           "containerd", "networkd", "nvidia"]
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLIND = "not seeing messages from other users"
PrivCall = Callable[[str, dict[str, Any]], Any]


def clean(text: str) -> str:
    """Drop terminal escape sequences and control characters so logs are safe to display anywhere."""
    return _CTRL.sub("", _ANSI.sub("", text))


def read_logs(source: str, lines: int = 200, since_s: Optional[int] = None, grep: Optional[str] = None,
              priv: Optional[PrivCall] = None) -> dict[str, Any]:
    if source not in SOURCES:
        raise ValueError(f"unknown log source '{source}' (known: {', '.join(SOURCES)})")
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 2000:
        raise ValueError("lines must be between 1 and 2000")
    if grep is not None and (not isinstance(grep, str) or len(grep) > 100):
        raise ValueError("filter text must be at most 100 characters")
    fetch = min(2000, max(lines * 5, 500)) if grep else lines
    params: dict[str, Any] = {"source": source, "lines": fetch}
    if since_s is not None:
        params["since_s"] = since_s
    via, text, note = "direct", "", None
    argv = privops.journal_argv(params)               # validates source / numbers
    rc, out, err = privops.RUN(argv, 20)
    blind = _BLIND in (out + err) or (rc != 0 and not out.strip())
    if not blind and rc == 0:
        text = out
    elif priv is not None:
        try:
            text = priv("remote_journal", params)["text"]
            via = "privd"
        except Exception as exc:  # noqa: BLE001 - tell the operator, do not hide the reason
            note = f"journal not readable by the agent user and the privileged helper failed: {exc}"
    else:
        note = "journal not readable by this user (add the user to the 'adm' group or run with sudo)"
    out_lines = clean(text).splitlines()
    if grep:
        needle = grep.lower()
        out_lines = [ln for ln in out_lines if needle in ln.lower()]
    out_lines = out_lines[-lines:]
    return {"source": source, "lines": out_lines, "count": len(out_lines), "via": via, "note": note}
