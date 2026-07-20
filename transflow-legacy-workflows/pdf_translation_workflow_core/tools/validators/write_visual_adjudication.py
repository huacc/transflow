"""Materialize deterministic D7 visual adjudication from visual-region gates.

tool_name: write_visual_adjudication
category: validators
input_contract: visual_region_metrics JSON, optional render manifest / repair plan refs
output_contract: visual_adjudication JSON with verdict, dimensions, evidence refs, and next state
failure_signals: missing/invalid visual metrics
fallback: mark S_FAIL_PROCESS_CONTRACT if adjudication cannot be materialized
anti_overfit_statement: consumes only current-run role gates and artifact refs; never branches on sample filename, page number, coordinates, exact text, or document identity
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, write_json  # noqa: E402


def dimension_from_gate(gate: dict[str, Any]) -> dict[str, Any]:
    status = str(gate.get("status") or "fail").upper()
    if status == "WARN":
        normalized = "PASS_WITH_WARN"
    elif status == "PASS":
        normalized = "PASS"
    else:
        normalized = "FAIL"
    return {
        "dimension": str(gate.get("gate_id") or "region_visual_quality"),
        "status": normalized,
        "blocking": bool(gate.get("blocking", normalized == "FAIL")),
        "failure_count": int(gate.get("failure_count") or 0),
        "warning_count": int(gate.get("warning_count") or 0),
        "region_count": int(gate.get("region_count") or 0),
        "evidence": gate.get("reason") or "role gate from visual_region_metrics",
        "sample": gate.get("sample", [])[:8],
    }


def adjudicate(
    visual_region_metrics: Path,
    out: Path,
    render_manifest: Path | None,
    repair_plan: Path | None,
    case_id: str | None,
) -> dict[str, Any]:
    metrics = read_json(visual_region_metrics)
    if not isinstance(metrics, dict):
        raise ValueError("visual_region_metrics must be a JSON object")
    dimensions = [
        dimension_from_gate(gate)
        for gate in metrics.get("role_gates", [])
        if isinstance(gate, dict)
    ]
    blocking_failures = [item for item in dimensions if item["status"] == "FAIL" and item["blocking"]]
    warnings = [item for item in dimensions if item["status"] == "PASS_WITH_WARN"]
    if blocking_failures:
        verdict = "FAIL"
        next_state = "Lx_RepairLoop"
    elif warnings:
        verdict = "PASS_WITH_WARN"
        next_state = "S9_VerifyProcessContract"
    else:
        verdict = "PASS"
        next_state = "S9_VerifyProcessContract"
    evidence_refs = [rel(visual_region_metrics)]
    if render_manifest is not None:
        evidence_refs.append(rel(render_manifest))
    if repair_plan is not None:
        evidence_refs.append(rel(repair_plan))
    result = {
        "decision_id": "D7_visual_adjudication_from_region_metrics",
        "state": "S8_VerifyProductQuality",
        "case_id": case_id,
        "verdict": verdict,
        "dimensions": dimensions,
        "blocking_failure_count": len(blocking_failures),
        "warning_dimension_count": len(warnings),
        "evidence_refs": evidence_refs,
        "backend_model_call_made": False,
        "adjudicator": "deterministic_visual_region_gate_materializer",
        "next_state": next_state,
        "notes": "This artifact materializes current-run deterministic visual gates. A human/model review may replace it when source-vs-output image judgement is required.",
    }
    write_json(out, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--visual-region-metrics", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--render-manifest", default=None)
    parser.add_argument("--repair-plan", default=None)
    parser.add_argument("--case-id", default=None)
    args = parser.parse_args()
    result = adjudicate(
        resolve_workspace_path(args.visual_region_metrics),
        Path(args.out),
        resolve_workspace_path(args.render_manifest) if args.render_manifest else None,
        resolve_workspace_path(args.repair_plan) if args.repair_plan else None,
        args.case_id,
    )
    print(args.out)
    print(f"verdict={result['verdict']}; blocking_failure_count={result['blocking_failure_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
