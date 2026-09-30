#!/usr/bin/env python3
"""Build the web GUI: copy src/ -> dist/ (no bundler needed — vanilla assets).

The FastAPI controller mounts ``twinspark/web/dist`` at ``/``. This just stages
the static assets there so the served tree stays clean and versionable.
"""
from __future__ import annotations

import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "twinspark" / "web" / "src"
DST = SRC.parent / "dist"

if DST.exists():
    shutil.rmtree(DST)
shutil.copytree(SRC, DST)

total = sum(len(list(p.rglob("*"))) for p in (DST / "css", DST / "js") if p.exists())
print(f"built web GUI -> {DST} ({total} css/js files)")
