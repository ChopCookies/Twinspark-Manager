#!/usr/bin/env python3
"""Turn pytest's JUnit XML into GitHub annotations (::error lines), so a failed CI run says which test
failed and why without opening the full log. Usage: python scripts/ci_annotate.py pytest.xml"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def tail(log: str | None, lines: int = 60) -> str:
    if not log:
        return ""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])[-6000:]
    except OSError:
        return ""


def main(path: str, log: str | None = None) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        # the run ended before pytest wrote its report: the end of the log says why
        print(f"::error title=pytest::no JUnit report ({exc}): the run stopped before tests finished"
              + esc("\n" + tail(log)))
        return 0
    shown = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None or shown >= 15:
                continue
            shown += 1
            name = f"{case.get('classname')}.{case.get('name')}"
            detail = (node.get("message") or "") + "\n" + (node.text or "")[-1500:]
            print(f"::error title={kind}: {esc(name)}::{esc(detail.strip())}")
    if not shown:
        print("::error title=pytest::no failing test in the report; the end of the log:" + esc("\n" + tail(log)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 3:                 # the installed versions, to compare a failing set with a passing one
        print("::notice title=installed packages::" + esc(tail(sys.argv[3], 200)))
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pytest.xml", sys.argv[2] if len(sys.argv) > 2 else None))
