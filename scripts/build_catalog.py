#!/usr/bin/env python3
"""Write catalog/index.json from twinspark/cookbook/recipes (see catalog/README.md).

    python scripts/build_catalog.py           # write the index
    python scripts/build_catalog.py --check   # exit 1 when the committed index is out of date (CI)

Every recipe is built in each of its modes on the way: a recipe that does not import is refused here,
not on a user's node. The output is deterministic (no timestamps), so the check is a plain comparison.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twinspark import cookbook  # noqa: E402
from twinspark.cookbook import catalog  # noqa: E402

INDEX = ROOT / "catalog" / "index.json"


def generate() -> tuple[str, int]:
    def validate(path: Path, mode):
        return cookbook._draft_from_file(path, mode=mode)[0]

    doc = catalog.build_index(cookbook.RECIPES_DIR, ROOT, validate=validate, modes_of=cookbook.recipe_modes)
    return catalog.dumps(doc), len(doc["recipes"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed index instead of writing it")
    args = ap.parse_args(argv)
    try:
        text, count = generate()
    except (ValueError, cookbook.RecipeImportError) as exc:
        print(f"recipe catalog: {exc}", file=sys.stderr)
        return 2
    if args.check:
        current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        if current != text:
            print("catalog/index.json is out of date — run `python scripts/build_catalog.py` and commit it",
                  file=sys.stderr)
            return 1
        print(f"catalog/index.json is current ({count} recipes)")
        return 0
    INDEX.parent.mkdir(exist_ok=True)
    INDEX.write_text(text, encoding="utf-8")
    print(f"wrote {INDEX.relative_to(ROOT)} ({count} recipes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
