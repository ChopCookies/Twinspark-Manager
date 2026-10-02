"""Support bundle: one tar.gz with everything needed to diagnose a node from afar.

* Read-only, fixed commands with timeouts; nothing is changed on the machine.
* Secrets never go in: the vault is listed by slot name only, known secret values are replaced
  everywhere, and anything that looks like ``token=…`` / ``Authorization: Bearer …`` is masked.
* Bounded: each item is cut to the last 256 KiB, the whole bundle to 8 MiB; best effort — a command
  that is missing or fails is noted in ``_errors.txt`` instead of failing the bundle.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional

from . import logs, privops

ITEM_LIMIT = 256 * 1024
BUNDLE_LIMIT = 8 * 1024 * 1024
DEADLINE_S = 75.0

_KEYED = re.compile(r"(?im)\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential)[\w.-]*"
                    r"\s*[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])([^\s\"',;]+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKENS = re.compile(r"\b(hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|nvapi-[A-Za-z0-9_-]{20,}"
                     r"|tsm1\.[A-Za-z0-9_=-]{20,})")


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()):
        self.values = sorted({v for v in secret_values if v and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self.values:
            text = text.replace(v, "[REDACTED]")
        text = _BEARER.sub(r"\1 [REDACTED]", text)
        text = _TOKENS.sub("[REDACTED]", text)
        return _KEYED.sub(r"\1[REDACTED]", text)


# name in the archive -> command (list) or file (str path); everything is best effort
COMMANDS: list[tuple[str, Any, float]] = [
    ("system/date.txt", ["date", "-u", "+%FT%TZ"], 3),
    ("system/uptime.txt", ["uptime"], 3),
    ("system/last-boots.txt", ["last", "-x", "-n", "15", "reboot", "shutdown"], 5),
    ("system/boots.txt", ["journalctl", "--list-boots", "--no-pager"], 8),
    ("system/uname.txt", ["uname", "-a"], 3),
    ("system/os-release.txt", "/etc/os-release", 0),
    ("system/cmdline.txt", "/proc/cmdline", 0),
    ("system/meminfo.txt", "/proc/meminfo", 0),
    ("system/loadavg.txt", "/proc/loadavg", 0),
    ("system/pressure-memory.txt", "/proc/pressure/memory", 0),
    ("system/free.txt", ["free", "-m"], 3),
    ("system/df.txt", ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x", "overlay"], 8),
    ("system/lsblk.txt", ["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT"], 8),
    ("system/timedatectl.txt", ["timedatectl", "status"], 5),
    ("system/failed-units.txt", ["systemctl", "--failed", "--no-pager"], 8),
    ("system/services.txt", ["systemctl", "status", "twinspark-agent", "twinspark-privd", "twinspark-controller",
                             "twinspark-terminal", "docker", "--no-pager", "-l"], 8),
    ("system/remote-timers.txt", ["systemctl", "list-timers", "--no-pager", "twinspark-remote-*"], 5),
    ("network/addr.txt", ["ip", "-br", "addr"], 5),
    ("network/link.txt", ["ip", "-br", "link"], 5),
    ("network/route.txt", ["ip", "route"], 5),
    ("network/listening.txt", ["ss", "-ltn"], 5),
    ("network/rdma-link.txt", ["rdma", "link", "show"], 5),
    ("gpu/nvidia-smi.txt", ["nvidia-smi"], 15),
    ("gpu/query.csv", ["nvidia-smi", "--query-gpu=name,driver_version,temperature.gpu,power.draw,"
                                     "clocks.sm,clocks.mem,memory.used,memory.total,utilization.gpu",
                       "--format=csv"], 15),
    ("docker/ps.txt", ["docker", "ps", "-a", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
     15),
    ("docker/system-df.txt", ["docker", "system", "df"], 20),
    ("config/remote-policy.json", "/etc/twinspark/remote-policy.json", 0),
    ("config/maintenance-policy.json", "/etc/twinspark/maintenance-policy.json", 0),
    ("config/agent.yaml", "/etc/twinspark/agent.yaml", 0),
    ("config/controller.yaml", "/etc/twinspark/controller.yaml", 0),
]
LOGS = [("agent", 500), ("privd", 200), ("controller", 300), ("terminal", 200), ("docker", 300), ("kernel", 800),
        ("previous-boot", 300), ("ssh", 100)]


def build_bundle(node: str, *, version: str, facts: Optional[dict[str, Any]] = None,
                 config_dump: Optional[dict[str, Any]] = None, vault_slots: Iterable[str] = (),
                 secret_values: Iterable[str] = (), priv: Optional[logs.PrivCall] = None,
                 extra: Optional[dict[str, str]] = None,
                 now: Optional[float] = None) -> tuple[str, bytes, list[str]]:
    """Returns (file name, tar.gz bytes, problems)."""
    now = now if now is not None else time.time()
    redact = Redactor(secret_values)
    files: dict[str, str] = {}
    problems: list[str] = []
    deadline = time.monotonic() + DEADLINE_S

    def command(item: tuple[str, Any, float]) -> tuple[str, Optional[str], Optional[str]]:
        name, spec, timeout = item
        if time.monotonic() > deadline:
            return name, None, "skipped: bundle time budget used up"
        if isinstance(spec, str):
            try:
                return name, Path(spec).read_text(errors="replace"), None
            except OSError as exc:
                return name, None, f"{spec}: {exc.strerror or exc}"
        rc, out, err = privops.RUN(spec, timeout)
        if rc == 127:
            return name, None, f"{spec[0]}: not installed"
        text = out if out.strip() else err
        if rc not in (0, 3) and not text.strip():
            return name, None, f"{' '.join(spec)}: exit {rc}"
        return name, text, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, text, problem in pool.map(command, COMMANDS):
            if text is not None:
                files[name] = text
            if problem:
                problems.append(problem)

    for source, lines in LOGS:
        try:
            r = logs.read_logs(source, lines, priv=priv)
            files[f"logs/{source}.log"] = "\n".join(r["lines"]) + ("\n" if r["lines"] else "")
            if r.get("note"):
                problems.append(f"logs/{source}: {r['note']}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"logs/{source}: {exc}")

    if priv is not None:
        for op, name in (("remote_boot_status", "boot/efi-entries.json"), ("remote_wol_status", "network/wol.json")):
            try:
                files[name] = json.dumps(priv(op, {}), indent=2)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name}: {exc}")
    if facts is not None:
        files["tsm/hardware-facts.json"] = json.dumps(facts, indent=2, default=str)
    if config_dump is not None:
        files["tsm/effective-config.json"] = json.dumps(config_dump, indent=2, default=str)
    files["tsm/vault-slots.txt"] = "\n".join(sorted(vault_slots)) + "\n"      # names only, never values
    files["tsm/version.txt"] = f"{version}\n"
    for name, text in (extra or {}).items():
        files[name] = text

    files["README.txt"] = (
        f"TwinSpark support bundle for node {node}\ncreated (UTC): {time.strftime('%FT%TZ', time.gmtime(now))}\n"
        f"TwinSpark {version}\n\nRead-only snapshot. Secrets are masked; the vault is listed by slot name only.\n"
        "Each file is cut to its last 256 KiB. See _errors.txt for anything that could not be collected.\n")
    files["_errors.txt"] = "\n".join(problems) + ("\n" if problems else "nothing went wrong\n")

    buf = io.BytesIO()
    total = 0
    skipped = []
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for name in sorted(files, key=lambda n: (n.startswith("_"), n)):
            raw = redact(logs.clean(files[name])).encode("utf-8", "replace")
            if len(raw) > ITEM_LIMIT:
                raw = b"[... cut to the last 256 KiB ...]\n" + raw[-ITEM_LIMIT:]
            if total + len(raw) > BUNDLE_LIMIT:
                skipped.append(name)
                continue
            total += len(raw)
            info = tarfile.TarInfo(f"tsm-bundle-{node}/{name}")
            info.size, info.mtime, info.mode = len(raw), int(now), 0o600
            tar.addfile(info, io.BytesIO(raw))
        if skipped:
            note = ("left out to stay under 8 MiB:\n" + "\n".join(skipped) + "\n").encode()
            info = tarfile.TarInfo(f"tsm-bundle-{node}/_skipped.txt")
            info.size, info.mtime, info.mode = len(note), int(now), 0o600
            tar.addfile(info, io.BytesIO(note))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"tsm-bundle-{node}-{stamp}.tar.gz", buf.getvalue(), problems
