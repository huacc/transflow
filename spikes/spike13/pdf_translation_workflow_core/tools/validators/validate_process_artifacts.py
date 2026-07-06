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
