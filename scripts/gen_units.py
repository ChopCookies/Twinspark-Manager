#!/usr/bin/env python3
"""Regenerate deploy/systemd/*.service from the generator `tsm setup` uses.

The files in deploy/systemd are the reference units for a default install (dedicated
`twinspark` user, /opt/twinspark venv). tests/test_setup_flow.py fails when they drift
from provision.render_units(); run this script to refresh them.
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twinspark.provision import Answers, Layout, render_units  # noqa: E402


class ReferenceLayout(Layout):
    """Render Linux installation paths independently of the generator's host OS."""

    def p(self, *parts: str) -> PurePosixPath:
        return PurePosixPath("/", *[x.lstrip("/") for x in parts])

    @property
    def sandbox(self) -> bool:
        return False


def reference_units() -> dict[str, str]:
    return render_units(Answers(role="controller", service_user="twinspark", group="twinspark",
                                remote={"terminal": True}), ReferenceLayout())


if __name__ == "__main__":
    out = ROOT / "deploy" / "systemd"
    for name, text in reference_units().items():
        (out / name).write_text(text)
        print("wrote", out / name)
