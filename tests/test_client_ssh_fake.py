"""A fake OpenSSH client (``ssh`` + ``ssh-keyscan``) for the workstation-client tests, plus its self-tests.

Nothing here talks to a real host. :class:`FakeSSH` writes two small Python programs into a tmp dir and
points ``TSM_SSH`` / ``TSM_SSH_KEYSCAN`` at them. They read ``config.json`` from their state dir on
every invocation (so a test can change behaviour while a tunnel runs) and append each argv to
``calls.jsonl``. The fake ssh:

* insists on ``StrictHostKeyChecking=yes`` and on ``--`` before the destination (exit 97 / 98);
* checks the client's ``UserKnownHostsFile`` against the configured host key like OpenSSH does
  ("Host key verification failed." / the REMOTE HOST IDENTIFICATION HAS CHANGED banner, exit 255);
* authenticates: ``auth`` = ``any`` | ``deny`` | ``keys`` (the ``-i`` key's public half must be in the
  configured authorized_keys file); a non-BatchMode password login calls ``$SSH_ASKPASS`` like OpenSSH
  (it requires ``SSH_ASKPASS_REQUIRE=force``) and compares the answer with ``password``;
* ``-N -L 127.0.0.1:L:127.0.0.1:R`` really forwards TCP from 127.0.0.1:L to 127.0.0.1:R, and exits
  with a keep-alive timeout after ``exit_after`` seconds when that is set;
* remote commands: the first ``canned`` entry whose ``match`` is a substring of the command wins
  (``rc``, ``stdout``, ``stderr``, optional ``sleep`` seconds first);
  otherwise, with ``shell`` configured, the command string runs under ``sh -c`` in a sandbox whose
  PATH holds only symlinked basic tools and fake systemctl/tsm/docker/nvidia-smi/tailscale, with
  /etc/twinspark, /opt/twinspark and /usr/local/bin/tsm rewritten into the sandbox.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

POSIX = os.name != "nt"
needs_posix = pytest.mark.skipif(not POSIX, reason="the fake ssh is a #! script; Windows runs it differently")


def _ed25519_line(seed: int) -> str:
    """A deterministic ``ssh-ed25519 <base64>`` public key (the blob format OpenSSH uses)."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(bytes((seed + i) % 256 for i in range(32)))
    return priv.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH).decode()


HOSTKEY = _ed25519_line(0)            # fingerprint verified against ssh-keygen: see test_client_ssh.py
HOSTKEY_FP = "SHA256:lbmsoA0yIEcEiVDRnMWuzm+nV+3ZEEpVIURqFoeSspg"
OTHER_HOSTKEY = _ed25519_line(100)


def blob_line(key_type: str, payload: bytes = b"x" * 32) -> str:
    t = key_type.encode()
    blob = struct.pack(">I", len(t)) + t + struct.pack(">I", len(payload)) + payload
    return f"{key_type} {base64.b64encode(blob).decode()}"


FAKE_SSH = r'''
import json, os, socket, subprocess, sys, threading, time

STATE = os.environ["FAKE_SSH_DIR"]


def cfg():
    try:
        with open(os.path.join(STATE, "config.json")) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def err(*lines):
    for ln in lines:
        sys.stderr.write(ln + "\n")
    sys.stderr.flush()


C = cfg()
argv = sys.argv[1:]
with open(os.path.join(STATE, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"tool": "ssh", "argv": argv, "pid": os.getpid(),
                         "askpass": os.environ.get("SSH_ASKPASS"),
                         "askpass_require": os.environ.get("SSH_ASKPASS_REQUIRE")}) + "\n")

WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
opts, forwards, idents, flags = {}, [], [], set()
port, user, dest, rest = 22, None, None, []
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--":
        dest, rest = argv[i + 1], argv[i + 2:]
        break
    if not a.startswith("-") or len(a) < 2:
        err("fake-ssh: destination without a preceding --: " + a)
        sys.exit(98)
    ch = a[1]
    if ch in WITH_ARG:
        if len(a) > 2:
            val, i = a[2:], i + 1
        else:
            val, i = argv[i + 1], i + 2
        if ch == "o":
            k, _, v = val.partition("=") if "=" in val else val.partition(" ")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            opts.setdefault(k.strip().lower(), v.replace("%%", "%"))
        elif ch == "p":
            port = int(val)
        elif ch == "l":
            user = val
        elif ch == "L":
            forwards.append(val)
        elif ch == "i":
            idents.append(val)
        continue
    flags.update(a[1:])
    i += 1

if dest is None:
    err("fake-ssh: no destination")
    sys.exit(98)
if dest.startswith("-"):
    err("fake-ssh: destination looks like an option")
    sys.exit(96)
if opts.get("stricthostkeychecking") != "yes":
    err("fake-ssh: StrictHostKeyChecking must be yes, got %r" % opts.get("stricthostkeychecking"))
    sys.exit(97)

if C.get("mode") == "unreachable":
    err("ssh: connect to host %s port %d: Connection refused" % (dest, port))
    sys.exit(255)

hostkey = C.get("hostkey")
if hostkey:
    ktype, kb64 = hostkey.split()[:2]
    pattern = dest.lower() if port == 22 else "[%s]:%d" % (dest.lower(), port)
    same_type = []
    try:
        with open(opts.get("userknownhostsfile", "/nonexistent")) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and pattern in parts[0].split(",") and parts[1] == ktype:
                    same_type.append(parts[2])
    except FileNotFoundError:
        pass
    if kb64 not in same_type:
        if same_type:
            err("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
                "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @",
                "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
                "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!",
                "Host key for %s has changed and you have requested strict checking." % pattern,
                "Host key verification failed.")
        else:
            err("No ED25519 host key is known for %s and you have requested strict checking." % pattern,
                "Host key verification failed.")
        sys.exit(255)


def deny(methods="publickey"):
    err("%s@%s: Permission denied (%s)." % (user, dest, methods))
    sys.exit(255)


batch = opts.get("batchmode", "no") == "yes"
if not batch and "password" in opts.get("preferredauthentications", ""):
    askpass = os.environ.get("SSH_ASKPASS")
    if not askpass or os.environ.get("SSH_ASKPASS_REQUIRE") != "force":
        deny("publickey,password")
    r = subprocess.run([askpass, "%s@%s's password: " % (user, dest)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    answer = r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
    if r.returncode != 0 or answer != C.get("password"):
        err("Permission denied, please try again.")
        deny("publickey,password")
elif C.get("auth", "any") == "deny":
    deny()
elif C.get("auth") == "keys":
    try:
        with open(C["authorized_keys"]) as fh:
            authorized = fh.read()
    except (KeyError, FileNotFoundError):
        authorized = ""
    ok = False
    for ident in idents:
        try:
            with open(ident + ".pub") as fh:
                body = " ".join(fh.read().split()[:2])
        except FileNotFoundError:
            continue
        ok = ok or (bool(body) and body in authorized)
    if not ok:
        deny()


def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve(listener, rhost, rport):
    n = 2
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = socket.create_connection((rhost, rport), timeout=2)
        except OSError:
            err("channel %d: open failed: connect failed: Connection refused" % n)
            client.close()
            n += 1
            continue
        n += 1
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


if "N" in flags:
    listeners = []
    for spec in forwards:
        lhost, lport, rhost, rport = spec.rsplit(":", 3)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((lhost, int(lport)))
        except OSError:
            err("bind [%s]:%s: Address already in use" % (lhost, lport),
                "channel_setup_fwd_listener_tcpip: cannot listen to port: %s" % lport,
                "Could not request local forwarding.")
            sys.exit(255)
        s.listen(16)
        listeners.append((s, rhost.strip("[]"), int(rport)))
    for s, rh, rp in listeners:
        threading.Thread(target=serve, args=(s, rh, rp), daemon=True).start()
    start = time.time()
    exit_after = C.get("exit_after")
    while True:
        time.sleep(0.05)
        if exit_after is not None and time.time() - start >= exit_after:
            err("Timeout, server host not responding.")
            os._exit(255)

command = " ".join(rest)
for entry in C.get("canned", []):
    if entry["match"] in command:
        time.sleep(entry.get("sleep", 0))
        sys.stdout.write(entry.get("stdout", ""))
        sys.stderr.write(entry.get("stderr", ""))
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(entry.get("rc", 0))

shell = C.get("shell")
if shell:
    for prefix in ("/etc/twinspark", "/usr/local/bin/tsm", "/opt/twinspark"):
        command = command.replace(prefix, shell["root"] + prefix)
    env = {"HOME": shell["home"], "PATH": shell["path"], "LC_ALL": "C", "USER": user or ""}
    r = subprocess.run([shell["sh"], "-c", command], env=env, cwd=shell["home"], stdin=sys.stdin)
    sys.exit(r.returncode)

if command == "true":
    sys.exit(0)
err("fake-ssh: no canned output for: " + command[:200])
sys.exit(1)
'''

FAKE_KEYSCAN = r'''
import json, os, sys

STATE = os.environ["FAKE_SSH_DIR"]
try:
    with open(os.path.join(STATE, "config.json")) as fh:
        C = json.load(fh)
except FileNotFoundError:
    C = {}
argv = sys.argv[1:]
with open(os.path.join(STATE, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"tool": "ssh-keyscan", "argv": argv, "pid": os.getpid()}) + "\n")
port, i, host = 22, 0, None
while i < len(argv):
    a = argv[i]
    if a == "--":
        host = argv[i + 1]
        break
    if a in ("-p", "-T", "-t", "-f"):
        if a == "-p":
            port = int(argv[i + 1])
        i += 2
        continue
    sys.stderr.write("fake-ssh-keyscan: host without a preceding --\n")
    sys.exit(98)
if C.get("mode") == "unreachable":
    sys.stderr.write("connect (`%s:%d'): Connection refused\n" % (host, port))
    sys.exit(1)
sys.stderr.write("# %s:%d SSH-2.0-OpenSSH_9.6\n" % (host, port))
prefix = host if port == 22 else "[%s]:%d" % (host, port)
for line in C.get("hostkeys") or ([C["hostkey"]] if C.get("hostkey") else []):
    print(prefix + " " + line)
'''


def write_program(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable} -I\n" + textwrap.dedent(source))
    path.chmod(0o755)
    return path


class FakeSSH:
    """Installs the fake ssh / ssh-keyscan for one test (``monkeypatch`` restores the environment)."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.dir = tmp_path / "fake-ssh"
        self.dir.mkdir()
        self.ssh = write_program(self.dir / "ssh", FAKE_SSH)
        self.keyscan = write_program(self.dir / "ssh-keyscan", FAKE_KEYSCAN)
        monkeypatch.setenv("TSM_SSH", str(self.ssh))
        monkeypatch.setenv("TSM_SSH_KEYSCAN", str(self.keyscan))
        monkeypatch.setenv("FAKE_SSH_DIR", str(self.dir))
        self.config: dict[str, Any] = {}
        self.configure(hostkey=HOSTKEY)

    def configure(self, **changes: Any) -> None:
        self.config.update(changes)
        tmp = self.dir / "config.json.tmp"
        tmp.write_text(json.dumps(self.config))
        os.replace(tmp, self.dir / "config.json")

    def calls(self, tool: str = "ssh") -> list[dict[str, Any]]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in rows if r["tool"] == tool]


# ---- a sandbox "remote" for running the real remote scripts ---------------------------------------
BASIC_TOOLS = ("sh", "head", "tail", "sed", "grep", "uname", "id", "hostname", "cat", "mkdir", "wc", "tr",
               "timeout", "touch", "chmod", "ls", "od", "env", "sleep", "printf", "test", "[")


def _sh_script(path: Path, body: str, sh: str) -> None:
    path.write_text(f"#!{sh}\n{body}")
    path.chmod(0o755)


class Sandbox:
    """A pretend node for ``shell`` mode: its own HOME, /etc/twinspark and PATH of fake tools.

    PATH contains only symlinks to harmless basic tools plus fakes, so no real systemctl, docker,
    nvidia-smi, tailscale or sudo can ever run from a test.
    """

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home" / "spark"
        self.bin = root / "bin"
        self.etc = root / "etc" / "twinspark"
        for d in (self.home, self.bin, self.etc):
            d.mkdir(parents=True, exist_ok=True)
        sh = shutil.which("sh")
        if not sh:
            pytest.skip("no POSIX sh on this machine")
        self.sh = sh
        for tool in BASIC_TOOLS:
            found = shutil.which(tool)
            if found and not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(found)

    def shell_config(self) -> dict[str, str]:
        return {"root": str(self.root), "home": str(self.home), "path": str(self.bin), "sh": self.sh}

    def tool(self, name: str, body: str) -> None:
        _sh_script(self.bin / name, body, self.sh)

    def install_tsm(self, version: str = "0.5.1", show: str = "", show_rc: int = 0, show_err: str = "") -> None:
        """A fake ``tsm`` in /usr/local/bin (rewritten into the sandbox) answering --version and init --show."""
        target = self.root / "usr" / "local" / "bin"
        target.mkdir(parents=True, exist_ok=True)
        (self.root / "tsm-show.out").write_text(show)
        (self.root / "tsm-show.err").write_text(show_err)
        _sh_script(target / "tsm", textwrap.dedent(f"""\
            case "$1" in
              --version) echo "tsm {version}" ;;
              init) cat "{self.root}/tsm-show.out"; cat "{self.root}/tsm-show.err" >&2; exit {show_rc} ;;
            esac
            """), self.sh)

    def systemctl(self, states: dict[str, tuple[str, str]]) -> None:
        cases = "".join(f'  "is-active {u}") echo {a}; exit {0 if a == "active" else 3} ;;\n'
                        f'  "is-enabled {u}") echo {e} ;;\n' for u, (a, e) in states.items())
        self.tool("systemctl", f'case "$1 $2" in\n{cases}  *) echo inactive; exit 3 ;;\nesac\n')


# ---- self-tests of the fake --------------------------------------------------------------------
@needs_posix
def test_fake_ssh_refuses_lax_host_key_checking_and_missing_double_dash(tmp_path, monkeypatch):
    import subprocess

    fake = FakeSSH(tmp_path, monkeypatch)
    lax = subprocess.run([str(fake.ssh), "-o", "StrictHostKeyChecking=accept-new", "--", "h", "true"],
                         capture_output=True, text=True)
    assert lax.returncode == 97
    nodash = subprocess.run([str(fake.ssh), "-o", "StrictHostKeyChecking=yes", "h", "true"],
                            capture_output=True, text=True)
    assert nodash.returncode == 98
    assert len(fake.calls()) == 2


@needs_posix
def test_fake_keyscan_prints_the_configured_key(tmp_path, monkeypatch):
    import subprocess

    fake = FakeSSH(tmp_path, monkeypatch)
    out = subprocess.run([str(fake.keyscan), "-p", "2222", "--", "spark-a"], capture_output=True, text=True)
    assert out.stdout.strip() == f"[spark-a]:2222 {HOSTKEY}"

