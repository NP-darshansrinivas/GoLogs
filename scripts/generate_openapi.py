#!/usr/bin/env python3
"""Generate (or, with --check, verify) `docs/openapi.json` from the real
FastAPI app's schema (PRD §14: "this is the authoritative machine-readable
contract and must stay in sync (enforced by a CI schema-diff check, §18)").

Usage:
    python scripts/generate_openapi.py             # write docs/openapi.json
    python scripts/generate_openapi.py --check      # exit 1 if it would change
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
OUTPUT_PATH = REPO_ROOT / "docs" / "openapi.json"

sys.path.insert(0, str(BACKEND_ROOT))


def generate_schema() -> dict:
    from app.main import create_app

    app = create_app()
    return app.openapi()


def main() -> int:
    check_only = "--check" in sys.argv
    schema = generate_schema()
    new_content = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if check_only:
        if not OUTPUT_PATH.exists():
            print(f"FAIL: {OUTPUT_PATH} does not exist. Run without --check to generate it.")
            return 1
        current_content = OUTPUT_PATH.read_text()
        if current_content != new_content:
            print(
                f"FAIL: {OUTPUT_PATH} is out of date with the actual API routes.\n"
                "Run `python scripts/generate_openapi.py` and commit the result."
            )
            return 1
        print(f"OK: {OUTPUT_PATH} matches the current API.")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(new_content)
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
