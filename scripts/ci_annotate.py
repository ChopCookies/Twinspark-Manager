#!/usr/bin/env python3
"""Turn pytest's JUnit XML into GitHub annotations (::error lines), so a failed CI run says which test
failed and why without opening the full log. Usage: python scripts/ci_annotate.py pytest.xml"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error title=pytest::no JUnit report ({exc}): the run stopped before tests finished")
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
        print("::notice title=pytest::no failing test in the report (see the log for a collection error)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pytest.xml"))
