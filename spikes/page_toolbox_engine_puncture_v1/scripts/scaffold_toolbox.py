from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from toolbox_cadence.scaffold import scaffold_toolbox


def main() -> int:
    parser = argparse.ArgumentParser(description="Create exactly one leaf toolbox package")
    parser.add_argument("--leaf-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = scaffold_toolbox(ROOT, args.leaf_key, dry_run=args.dry_run)
    print(json.dumps({"toolbox_key": result.toolbox_key, "package_root": result.package_root, "dry_run": result.dry_run, "planned_files": result.planned_files}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

