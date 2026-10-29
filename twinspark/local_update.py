"""`tsm update here`: update the TwinSpark install on this computer (the workstation client).

How TwinSpark got onto this computer decides what an update can do:

* ``pipx`` / a virtual environment — the release's wheel is installed into that environment with pip, using the
  release's constraints. Extras that are installed now (``client``, ``hf``) are kept.
* a development checkout (``pip install -e``) — never touched: ``git pull`` updates it.
* a Spark (``/opt/twinspark/venv`` from install.sh) — refused here: ``tsm update sparks`` updates both nodes
  together, through node A, with a rollback per node.
* the system Python — refused: install with pipx instead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from . import __version__, releases

MARKER = ".twinspark-install"


def install_kind(prefix: Optional[str] = None, base_prefix: Optional[str] = None) -> dict[str, Any]:
    prefix = prefix or sys.prefix
    base_prefix = base_prefix or sys.base_prefix
    p = Path(prefix)
    if p.name == "venv" and (p.parent / MARKER).is_file():
        return {"kind": "spark", "prefix": prefix, "ok": False,
                "why": "this is a Spark's install; `tsm update sparks` updates both nodes together"}
    try:
        dist = metadata.distribution(releases.PROJECT)
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        return {"kind": "source", "prefix": prefix, "ok": False,
                "why": "TwinSpark runs from a source tree here without being installed; use git pull"}
    if (direct.get("dir_info") or {}).get("editable"):
        return {"kind": "editable", "prefix": prefix, "ok": False, "path": direct.get("url"),
                "why": "this is a development checkout (pip install -e); update it with git pull"}
    norm = prefix.replace("\\", "/").lower()
    if "/pipx/venvs/" in norm:          # ~/.local/(share/)pipx/venvs/…, %USERPROFILE%\pipx\venvs\…
        return {"kind": "pipx", "prefix": prefix, "ok": True}
    if os.path.normcase(os.path.realpath(prefix)) != os.path.normcase(os.path.realpath(base_prefix)):
        return {"kind": "venv", "prefix": prefix, "ok": True}
    return {"kind": "system", "prefix": prefix, "ok": False,
            "why": "TwinSpark is installed into the system Python; reinstall it with pipx "
                   "(pipx install twinspark-manager) — `tsm update here` then works"}


_EXTRA = re.compile(r"""extra\s*==\s*["']([A-Za-z0-9._-]+)["']""")
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def installed_extras() -> list[str]:
    """The extras whose requirements are all installed now — the update installs them again."""
    try:
        reqs = metadata.distribution(releases.PROJECT).requires or []
    except metadata.PackageNotFoundError:
        return []
    by_extra: dict[str, list[str]] = {}
    for r in reqs:
        m, n = _EXTRA.search(r), _NAME.match(r)
        if m and n:
            by_extra.setdefault(m[1], []).append(n[1])
    out = []
    for extra, names in sorted(by_extra.items()):
        if extra == "dev":
            continue
        try:
            for name in names:
                metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        out.append(extra)
    return out


def pip_command(kind: str, python: str = sys.executable) -> list[str]:
    ok = subprocess.run([python, "-m", "pip", "--version"], capture_output=True, text=True, timeout=60)
    if ok.returncode == 0:
        return [python, "-m", "pip"]
    if kind == "pipx":
        return ["pipx", "runpip", releases.PROJECT]
    raise RuntimeError(f"pip is not available for {python}")


def download(client, plan: dict[str, Any], folder: Path) -> dict[str, Path]:
    out = {}
    for key in ("wheel", "constraints"):
        f = plan[key]
        r = client.get(f["url"])
        if r.status_code >= 400:
            raise releases.ReleaseError(f"downloading {f['name']} failed ({r.status_code})")
        releases.check_download(f["name"], r.content, f["sha256"])
        path = folder / f["name"]
        path.write_bytes(r.content)
        out[key] = path
    return out


def install_args(pip: list[str], files: dict[str, Path], extras: list[str]) -> list[str]:
    spec = str(files["wheel"]) + (f"[{','.join(extras)}]" if extras else "")
    return [*pip, "install", "--upgrade", "--disable-pip-version-check", "-c", str(files["constraints"]), spec]


def installed_version(python: str = sys.executable) -> str:
    p = subprocess.run([python, "-I", "-c", "import twinspark; print(twinspark.__version__)"],
                       capture_output=True, text=True, timeout=120)
    return p.stdout.strip().splitlines()[-1] if p.returncode == 0 and p.stdout.strip() else ""


def update_here(plan: dict[str, Any], *, client=None, run=subprocess.run, log=print) -> str:
    """Download, check, install; returns the version the environment reports afterwards."""
    kind = install_kind()
    if not kind["ok"]:
        raise RuntimeError(kind["why"])
    import httpx
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=120)
    try:
        with tempfile.TemporaryDirectory(prefix="tsm-update-") as tmp:
            files = download(client, plan, Path(tmp))
            log(f"downloaded {plan['wheel']['name']} (sha256 checked against the release's SHA256SUMS)")
            argv = install_args(pip_command(kind["kind"]), files, installed_extras())
            log("running: " + " ".join(argv[:3]) + " install --upgrade … " + Path(argv[-1]).name)
            p = run(argv, text=True)
            if p.returncode != 0:
                hint = ""
                if os.name == "nt":
                    hint = (" — on Windows, close every other TwinSpark window (tsm client) and try again; "
                            "a running tsm.exe cannot be replaced")
                raise RuntimeError(f"pip failed ({p.returncode}){hint}")
    finally:
        if own:
            client.close()
    got = installed_version()
    if got != plan["version"]:
        raise RuntimeError(f"after the install this environment reports {got or 'nothing'}, expected {plan['version']}")
    return got


def current() -> str:
    return __version__
