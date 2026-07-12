from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from toolbox_cadence.lifecycle import validate_acceptance_package


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate one toolbox acceptance package")
    parser.add_argument("--leaf-key", required=True)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--without-promotion", action="store_true")
    args = parser.parse_args()
    result = validate_acceptance_package(Path(args.package_root), args.leaf_key, require_promotion=not args.without_promotion)
    print(json.dumps({"passed": result.passed, "missing_paths": result.missing_paths, "failed_reports": result.failed_reports, "reasons": result.reasons}, ensure_ascii=False, indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

