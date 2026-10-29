"""The workstation side (``tsm``, the client app) also runs on Windows.

There the default text encoding is the ANSI code page and ``write_text`` turns ``\\n`` into ``\\r\\n``.
A recipe with an umlaut then fails to load, and a mod script written there fails on the Spark with
``bash\\r: No such file or directory``. CI runs on Linux, where neither shows, so these tests pin the
explicit choices in the source and check the line-ending repair directly.
"""

from __future__ import annotations

import ast
import base64
import io
import zipfile
from pathlib import Path

from twinspark import cli

ROOT = Path(__file__).resolve().parents[1] / "twinspark"
# Modules a workstation imports when it runs `tsm …` or the client app.
WORKSTATION = [ROOT / "cli.py", ROOT / "cli_remote.py", ROOT / "schemas" / "config.py",
               *sorted((ROOT / "client").rglob("*.py"))]


def _text_io_without_encoding(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name not in ("read_text", "write_text", "NamedTemporaryFile", "open"):
            continue
        if name == "open" and isinstance(f, ast.Attribute):
            continue  # os.open, z.open, … take no encoding
        if name in ("read_text", "write_text") and isinstance(f.value, ast.Attribute) and f.value.attr == "host":
            continue  # the Hermes host helpers decode UTF-8 themselves
        mode = next((a.value for a in node.args[1:2] if isinstance(a, ast.Constant)), None)
        mode = next((k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)),
                    mode)
        if name == "NamedTemporaryFile" and (mode is None or "b" in mode):
            continue  # binary by default
        if name == "open" and mode is not None and "b" in mode:
            continue
        if not any(k.arg == "encoding" for k in node.keywords):
            out.append(f"{path.relative_to(ROOT.parent)}:{node.lineno} {name}")
    return out


def test_workstation_modules_never_rely_on_the_platform_text_encoding():
    missing = [hit for p in WORKSTATION for hit in _text_io_without_encoding(p)]
    assert not missing, "pass encoding='utf-8' (and newline='\\n' for scripts):\n" + "\n".join(missing)


def test_a_mod_with_windows_line_endings_is_sent_with_lf(tmp_path, capsys):
    mod = tmp_path / "fix-crlf"
    mod.mkdir()
    (mod / "run.sh").write_bytes(b"#!/usr/bin/env bash\r\nset -e\r\necho ok\r\n")
    (mod / "data.bin").write_bytes(b"keep\r\nthis\r\n")
    z = zipfile.ZipFile(io.BytesIO(base64.b64decode(cli._pack_dir(mod))))
    assert z.read("run.sh") == b"#!/usr/bin/env bash\nset -e\necho ok\n"
    assert z.read("data.bin") == b"keep\r\nthis\r\n"  # only the script is the mod's own; payloads stay as they are
    assert "Windows line endings" in capsys.readouterr().err


def test_file_patch_writes_its_script_with_lf(tmp_path):
    src = tmp_path / "f.py"
    src.write_bytes(b"x = 1\r\n")
    d = cli.file_patch_mod("fix-1", src, "/opt/app/f.py", tmp_path / "out")
    assert b"\r" not in (d / "run.sh").read_bytes()
    assert (d / "f.py").read_bytes() == b"x = 1\r\n"


def test_config_with_non_ascii_text_loads(tmp_path):
    from pydantic import BaseModel

    from twinspark.schemas.config import load_config

    class Doc(BaseModel):
        db_path: str

    p = tmp_path / "workstation.yaml"
    p.write_bytes("db_path: /srv/küche/state.db\n".encode("utf-8"))
    assert load_config(p, Doc).db_path == "/srv/küche/state.db"
