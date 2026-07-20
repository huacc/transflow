import argparse
import json
from pathlib import Path
from typing import Any


KNOWN_BINDINGS = [
    ("extract_pdf_structure.py", "tools/probes/extract_source_structure.py", "implemented"),
    ("render_pdf.py", "tools/generators/generate_candidate.py", "partial"),
    ("collect_visual_region_metrics.py", "tools/judges/compare_source_candidate.py", "partial"),
    ("generate_semantic_backfill.py", "tools/generators/generate_candidate.py", "implemented"),
    ("build_role_plan.py", "tools/planners/plan_roles.py", "implemented"),
    ("classify_pages.py", "tools/planners/classify_pages.py", "implemented"),
    ("build_layout_plan.py", "tools/planners/plan_layout.py", "implemented"),
    ("apply_column_flow_elastic.py", "tools/planners/apply_column_flow_elastic.py", "implemented"),
    ("validate_semantic_translations.py", "embedded:run_round28_contract_case.py", "partial"),
    ("validate_process_artifacts.py", "tools/validators/validate_process.py", "implemented"),
    ("evaluate_pdf_quality.py", "tools/validators/validate_quality.py", "implemented"),
    ("build_repair_patch.py", "tools/repairs/build_repair_patch.py", "implemented"),
    ("apply_repair_patch.py", "tools/repairs/apply_repair_patch.py", "implemented"),
    ("materialize_decision_artifacts.py", "tools/validators/materialize_round28_artifacts.py", "implemented"),
    ("validate_decision_graph.py", "tools/validators/validate_decision_graph.py", "implemented"),
    ("obstacle_aware_reflow.py", "tools/repairs/obstacle_aware_reflow.py", "implemented"),
]


def binding_status(root: Path, target: str, declared: str) -> str:
    if target.startswith("embedded:"):
        return declared
    if (root / target).exists():
        return declared
    if declared == "missing_or_to_be_created":
        return "missing"
    return "missing"


def run(root: Path) -> dict[str, Any]:
    bindings = []
    for contract_name, target, declared in KNOWN_BINDINGS:
        status = binding_status(root, target, declared)
        bindings.append(
            {
                "contract_tool_name": contract_name,
                "round28_target": target,
                "binding_status": status,
                "notes": "Round28 adds page classification and column-flow postprocessing inside the lab package; global core is read-only.",
            }
        )
    return {
        "artifact": "tool_binding_map",
        "binding_count": len(bindings),
        "bindings": bindings,
        "boundary": {
            "round28_tools_only": True,
            "global_core_read_only": True,
            "round25_dispatch_is_seed_not_authority": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.round_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(run(root), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
