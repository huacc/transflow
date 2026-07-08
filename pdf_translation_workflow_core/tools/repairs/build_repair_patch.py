"""Build a repair patch from failed visual/product quality gates.

tool_name: build_repair_patch
category: repairs
input_contract: layout policy JSON, visual repair plan JSON, optional product quality gate JSON, optional selected gate/repair atom
output_contract: repair_patch_<n>.json with selected failure, generic repair atom, and policy patch operations
failure_signals: no executable repair atom, selected repair not found, invalid JSON
fallback: caller records not_executed_unrepairable and product-quality failure
anti_overfit_statement: patch selection and operations are driven by current-run gate ids, repair atoms, language metadata, and region roles; no filename, page number, literal text, document identity, or fixed sample coordinate is used
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import now_local, read_json, rel, write_json  # noqa: E402
from repairs.repair_policy_patch import build_policy_patch_operations, select_executable_repair_plan  # noqa: E402


def build_patch(
    *,
    layout_policy_path: Path,
    visual_repair_plan_path: Path,
    product_quality_path: Path | None,
    out_path: Path,
    case_id: str,
    loop_index: int,
    gate_id: str | None,
    repair_atom: str | None,
) -> dict[str, Any]:
    policy = read_json(layout_policy_path)
    repair_plan = read_json(visual_repair_plan_path)
    product_quality: dict[str, Any] | None = read_json(product_quality_path) if product_quality_path else None
    selected, failed_plans = select_executable_repair_plan(repair_plan, gate_id=gate_id, repair_atom=repair_atom)
    if selected is None:
        available = [
            {
                "gate_id": item.get("gate_id"),
                "repair_atom": item.get("repair_atom"),
                "target_state": item.get("target_state"),
            }
            for item in failed_plans
        ]
        raise ValueError(f"no executable repair atom selected; gate_id={gate_id!r}, repair_atom={repair_atom!r}, available={available}")

    operations = build_policy_patch_operations(policy, selected)
    failed_gate_ids = [
        gate.get("gate_id")
        for gate in (product_quality or {}).get("gates", [])
        if gate.get("blocking") and gate.get("status") == "fail"
    ]
    patch: dict[str, Any] = {
        "tool_name": "build_repair_patch",
        "patch_schema_version": "repair_patch.v1",
        "case_id": case_id,
        "loop_iteration": loop_index,
        "patch_id": f"{case_id}_repair_patch_{loop_index:04d}",
        "status": "patch_built" if operations else "patch_built_no_policy_delta_needed",
        "selected_failure": {
            "gate_id": selected.get("gate_id"),
            "failure_class": selected.get("failure_class") or selected.get("gate_id"),
            "repair_atom": selected.get("repair_atom"),
            "target_state": selected.get("target_state"),
            "target_scope": selected.get("sample_regions", [])[:5],
            "expected_effect": selected.get("description"),
        },
        "failed_gate_ids": failed_gate_ids or [item.get("gate_id") for item in failed_plans],
        "deferred_failures": [
            {
                "gate_id": item.get("gate_id"),
                "repair_atom": item.get("repair_atom"),
                "target_state": item.get("target_state"),
            }
            for item in failed_plans
            if item is not selected
        ],
        "source_artifacts": {
            "layout_policy": rel(layout_policy_path),
            "visual_repair_plan": rel(visual_repair_plan_path),
            "product_quality_gates": rel(product_quality_path) if product_quality_path else None,
        },
        "policy_context": {
            "target_language": policy.get("target_language"),
            "source_language": policy.get("source_language"),
            "language_pair_profile": policy.get("language_pair_profile"),
            "layout_policy_version": policy.get("layout_policy_version"),
        },
        "operation_count": len(operations),
        "operations": operations,
        "anti_overfit_statement": (
            "RepairPatch is derived from current-run failed gate id, repair atom, target language, role policy, "
            "and source-relative profile settings. It does not branch on document names, page numbers, literal text, "
            "known reference PDFs, or fixed sample coordinates."
        ),
        "timestamp_local": now_local(),
    }
    write_json(out_path, patch)
    return patch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-policy", required=True)
    parser.add_argument("--visual-repair-plan", required=True)
    parser.add_argument("--product-quality")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--loop-index", type=int, default=1)
    parser.add_argument("--gate-id")
    parser.add_argument("--repair-atom")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    build_patch(
        layout_policy_path=Path(args.layout_policy),
        visual_repair_plan_path=Path(args.visual_repair_plan),
        product_quality_path=Path(args.product_quality) if args.product_quality else None,
        out_path=Path(args.out),
        case_id=args.case_id,
        loop_index=args.loop_index,
        gate_id=args.gate_id,
        repair_atom=args.repair_atom,
    )


if __name__ == "__main__":
    main()
