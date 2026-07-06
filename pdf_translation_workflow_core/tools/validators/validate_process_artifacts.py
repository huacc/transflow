"""Validate process artifacts for a workflow run.

tool_name: validate_process_artifacts
category: validators
input_contract: run directory containing state_trace.json, decision_log.jsonl, operation_log.jsonl
output_contract: JSON validation report
failure_signals: missing required artifacts, missing fields, missing decisions, invalid terminal state
fallback: S_FAIL_PROCESS_CONTRACT
anti_overfit_statement: validates schemas only and does not inspect sample identity
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import write_json  # noqa: E402


REQUIRED_DECISIONS = {
    "D1_role_classification",
    "D2_translation",
    "D3_visual_only_text",
    "D4_layout_plan",
    "D5_initial_verification",
    "D6_user_feedback_adjudication",
    "D7_similarity_gate",
    "D8_minimal_repair_selection",
    "D9_final_acceptance",
}

REQUIRED_DECISION_FIELDS = {
    "decision_id",
    "state",
    "purpose",
    "input_artifacts",
    "prompt_contract",
    "required_output_dimensions",
    "model_output",
    "next_state",
}

REQUIRED_TRACE_FIELDS = {
    "transition_id",
    "from",
    "to",
    "entry_condition",
    "run_mode",
    "tools",
    "input_artifacts",
    "output_artifacts",
    "decision_record_ids",
    "gates",
    "next_state_rule",
    "timestamp_local",
}


MANDATORY_VISUAL_ARTIFACTS = {
    "candidate_render_manifest.json",
    "visual_region_metrics.json",
    "visual_repair_plan.json",
    "visual_adjudication.json",
}


def resolve_artifact(run_dir: Path, path_text: str) -> Path | None:
    path = Path(path_text)
    candidates = [path] if path.is_absolute() else [run_dir / path, Path.cwd() / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def validate_boundary_check(
    run_dir: Path,
    holder: dict[str, Any],
    label: str,
    errors: list[str],
) -> None:
    output_artifacts = holder.get("output_artifacts") or []
    if not output_artifacts:
        return
    inline = holder.get("workspace_boundary_check")
    if isinstance(inline, dict):
        if inline.get("workspace_boundary_verdict") != "PASS":
            errors.append(f"{label} workspace_boundary_check is not PASS")
        return
    ref = holder.get("workspace_boundary_check_ref")
    if not ref:
        errors.append(f"{label} writes output_artifacts without workspace_boundary_check_ref")
        return
    report_path = resolve_artifact(run_dir, str(ref))
    if report_path is None:
        errors.append(f"{label} workspace_boundary_check_ref missing: {ref}")
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label} workspace boundary report unreadable: {ref}: {exc}")
        return
    if report.get("workspace_boundary_verdict") != "PASS":
        errors.append(f"{label} workspace boundary report is not PASS: {ref}")


def operation_outputs(operations: list[dict[str, Any]]) -> list[str]:
    outputs: list[str] = []
    for item in operations:
        value = item.get("output_artifacts") or []
        if isinstance(value, list):
            outputs.extend(str(path) for path in value)
    return outputs


def find_candidate_case_dirs(run_dir: Path, operations: list[dict[str, Any]]) -> list[Path]:
    dirs: dict[str, Path] = {}
    for path_text in operation_outputs(operations):
        if Path(path_text).name == "candidate_generation_evidence.json":
            path = resolve_artifact(run_dir, path_text)
            if path is not None:
                dirs[str(path.parent.resolve())] = path.parent.resolve()
    for candidate_path in run_dir.rglob("candidate_generation_evidence.json"):
        if "pdf_translation_workflow_core" in candidate_path.parts:
            continue
        dirs[str(candidate_path.parent.resolve())] = candidate_path.parent.resolve()
    return sorted(dirs.values(), key=lambda item: str(item))


def decision_by_id(decisions: list[dict[str, Any]], decision_id: str) -> dict[str, Any] | None:
    for item in decisions:
        if item.get("decision_id") == decision_id:
            return item
    return None


def has_unrepairable_reason(decision: dict[str, Any]) -> bool:
    model_output = decision.get("model_output")
    if not isinstance(model_output, dict):
        return False
    repair_plan = model_output.get("repair_plan")
    return any(
        key in model_output and model_output.get(key)
        for key in ["unrepairable_reason", "no_valid_repair_reason", "failure_class", "repair_atom"]
    ) or isinstance(repair_plan, dict)


def validate_product_quality_visual_closure(
    run_dir: Path,
    state_trace: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    errors: list[str],
) -> None:
    is_product_quality = any(item.get("run_mode") == "product_quality" for item in state_trace)
    if not is_product_quality:
        return

    outputs = operation_outputs(operations)
    candidate_pdf_written = any(str(path).lower().endswith(".pdf") and "candidate" in str(path).lower() for path in outputs)
    candidate_evidence_written = any(Path(str(path)).name == "candidate_generation_evidence.json" for path in outputs)
    if not (candidate_pdf_written or candidate_evidence_written):
        return

    case_dirs = find_candidate_case_dirs(run_dir, operations)
    if not case_dirs:
        errors.append("product_quality candidate generation reached but no candidate_generation_evidence.json case directory was found")
    for case_dir in case_dirs:
        for artifact_name in MANDATORY_VISUAL_ARTIFACTS:
            if not (case_dir / artifact_name).exists():
                errors.append(f"product_quality visual closure missing {artifact_name} in {case_dir}")

    tool_text = "\n".join(str(item.get("tool", "")) for item in operations)
    for required_tool in [
        "render_pdf.py",
        "collect_visual_region_metrics.py",
        "plan_visual_region_repairs.py",
        "evaluate_pdf_quality.py",
    ]:
        if required_tool not in tool_text:
            errors.append(f"product_quality visual closure missing tool invocation: {required_tool}")

    d7 = decision_by_id(decisions, "D7_similarity_gate")
    d8 = decision_by_id(decisions, "D8_minimal_repair_selection")
    if d7 and isinstance(d7.get("model_output"), dict) and d7["model_output"].get("verdict") == "fail":
        if not d8:
            errors.append("D7 failed but D8_minimal_repair_selection is missing")
        elif isinstance(d8.get("model_output"), dict) and d8["model_output"].get("verdict") == "skipped":
            errors.append("D7 failed but D8_minimal_repair_selection was skipped")
        elif d8.get("next_state") == "S_FAIL_QUALITY" and not has_unrepairable_reason(d8):
            errors.append("D8 routed to S_FAIL_QUALITY without repair plan or unrepairable reason")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate(run_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    state_path = run_dir / "state_trace.json"
    decision_path = run_dir / "decision_log.jsonl"
    operation_path = run_dir / "operation_log.jsonl"
    state_trace = []
    decisions = []
    operations = []
    if not state_path.exists():
        errors.append("missing state_trace.json")
    else:
        state_trace = json.loads(state_path.read_text(encoding="utf-8"))
    if not decision_path.exists():
        errors.append("missing decision_log.jsonl")
    else:
        decisions = read_jsonl(decision_path)
    if not operation_path.exists():
        errors.append("missing operation_log.jsonl")
    else:
        operations = read_jsonl(operation_path)

    for idx, item in enumerate(state_trace):
        missing = REQUIRED_TRACE_FIELDS - set(item)
        if missing:
            errors.append(f"state_trace[{idx}] missing fields: {sorted(missing)}")
        validate_boundary_check(run_dir, item, f"state_trace[{idx}]", errors)
    states_seen = {item.get("from") for item in state_trace} | {item.get("to") for item in state_trace}
    terminal_states = {
        "S_DONE_PROCESS_VALIDATED",
        "S_DONE_PRODUCT_ACCEPTED",
        "S_FAIL_PROCESS_CONTRACT",
        "S_FAIL_QUALITY",
        "S_FAIL_TOOLING",
        "S_FAIL_CAPABILITY",
    }
    if not (states_seen & terminal_states):
        errors.append("state trace does not reach a valid terminal state")

    decision_ids = set()
    for idx, item in enumerate(decisions):
        decision_id = item.get("decision_id", f"<index:{idx}>")
        decision_ids.add(decision_id)
        missing = REQUIRED_DECISION_FIELDS - set(item)
        if missing:
            errors.append(f"decision {decision_id} missing fields: {sorted(missing)}")
        model_output = item.get("model_output")
        if not isinstance(model_output, dict):
            errors.append(f"decision {decision_id} model_output is not an object")
        elif model_output.get("verdict") not in {"pass", "fail", "warn", "skipped"}:
            errors.append(f"decision {decision_id} has invalid verdict: {model_output.get('verdict')}")
        if not item.get("input_artifacts"):
            errors.append(f"decision {decision_id} has no input_artifacts")
    missing_decisions = REQUIRED_DECISIONS - decision_ids
    if missing_decisions:
        errors.append(f"missing required decisions: {sorted(missing_decisions)}")

    if not operations:
        errors.append("operation log is empty")
    else:
        for idx, item in enumerate(operations):
            for field in ["operation_id", "state", "tool", "status"]:
                if field not in item:
                    errors.append(f"operation_log[{idx}] missing {field}")
            validate_boundary_check(run_dir, item, f"operation_log[{idx}]", errors)

    validate_product_quality_visual_closure(run_dir, state_trace, decisions, operations, errors)

    return {
        "tool": "validate_process_artifacts",
        "process_contract_verdict": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "states_seen": sorted(s for s in states_seen if s),
        "decisions_seen": sorted(decision_ids),
        "operation_count": len(operations),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = validate(Path(args.run_dir))
    write_json(Path(args.out), result)
    print(args.out)
    return 0 if result["process_contract_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
