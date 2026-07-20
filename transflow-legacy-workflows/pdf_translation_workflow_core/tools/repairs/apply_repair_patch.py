"""Apply a generic repair patch to a layout policy.

tool_name: apply_repair_patch
category: repairs
input_contract: layout policy JSON and repair patch JSON from build_repair_patch.py
output_contract: repaired layout policy JSON with repair_overrides and applied change records
failure_signals: missing patch, unsupported operation, invalid layout policy JSON
fallback: caller records S_FAIL_QUALITY or S_FAIL_TOOLING without accepting candidate quality
anti_overfit_statement: applies declarative patch operations only; no filename, page number, literal text, document identity, reference PDF, or fixed sample coordinate is inspected
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import now_local, read_json, rel, write_json  # noqa: E402
from repairs.repair_policy_patch import apply_operations  # noqa: E402


def apply_patch(layout_policy_path: Path, repair_patch_path: Path, out_path: Path) -> dict:
    policy = read_json(layout_policy_path)
    patch = read_json(repair_patch_path)
    operations = patch.get("operations") or []
    if not isinstance(operations, list):
        raise ValueError("repair patch operations must be a list")
    changes = apply_operations(policy, operations)
    selected = patch.get("selected_failure") or {}
    policy.setdefault("repair_overrides", []).append(
        {
            "repair_atom": selected.get("repair_atom"),
            "gate_id": selected.get("gate_id"),
            "repair_patch": rel(repair_patch_path),
            "source": "apply_repair_patch.py",
            "patch_schema_version": patch.get("patch_schema_version"),
            "operation_count": len(operations),
            "applied_change_count": len(changes),
            "anti_overfit": (
                "applied declarative operations generated from current-run failure class, region role, "
                "target language, and source-relative policy; no sample identity or fixed coordinates used"
            ),
            "changes": changes,
            "timestamp_local": now_local(),
        }
    )
    write_json(out_path, policy)
    return {
        "tool_name": "apply_repair_patch",
        "layout_policy": rel(layout_policy_path),
        "repair_patch": rel(repair_patch_path),
        "out": rel(out_path),
        "operation_count": len(operations),
        "applied_change_count": len(changes),
        "changes": changes,
        "timestamp_local": now_local(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-policy", required=True)
    parser.add_argument("--repair-patch", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    apply_patch(Path(args.layout_policy), Path(args.repair_patch), Path(args.out))


if __name__ == "__main__":
    main()
