import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True


def rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_cmd(root: Path, command: list[str], state: str, trace: list[dict], operation_log: Path, decision_log: Path) -> None:
    started = datetime.now().isoformat(timespec="seconds")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(command, cwd=root, text=True, capture_output=True, env=env)
    ended = datetime.now().isoformat(timespec="seconds")
    record = {
        "state": state,
        "command": command,
        "started_at": started,
        "ended_at": ended,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-4000:],
    }
    trace.append(record)
    append_jsonl(operation_log, record)
    append_jsonl(
        decision_log,
        {
            "state": state,
            "decision_type": "tool_dispatch",
            "selected_tool": command[1] if len(command) > 1 else command[0],
            "reason": "state_machine_contract",
            "returncode": proc.returncode,
        },
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", default="input/source_pdfs/00005_2025_annual_report_zh_pages_003_005_006.pdf")
    parser.add_argument("--translations-json", default="input/semantic_translations/R22_PAGES_03_05_06_00005_2025_annual_report_zh_pages_003_005_006.translations.json")
    parser.add_argument("--case-id", default="R22_PAGES_03_05_06")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    decision_log = reports / "decision_log.jsonl"
    operation_log = reports / "operation_log.jsonl"
    model_log = reports / "model_interactions.jsonl"
    for log_path in (decision_log, operation_log, model_log):
        if log_path.exists():
            log_path.unlink()
    output_pdf = root / "output" / f"{args.case_id}_candidate.pdf"
    source_structure = reports / "source_structure.json"
    role_plan = reports / "role_plan.json"
    layout_plan = reports / "layout_plan.json"
    trace: list[dict] = []

    run_request = {
        "case_id": args.case_id,
        "source_pdf": args.source_pdf,
        "translations_json": args.translations_json,
        "package_root": str(root),
        "offline_reference_runtime_use": False,
    }
    (reports / "run_request.json").write_text(json.dumps(run_request, ensure_ascii=False, indent=2), encoding="utf-8")
    append_jsonl(
        decision_log,
        {
            "state": "S0_Request",
            "decision_type": "boundary_confirmation",
            "source_pdf": args.source_pdf,
            "translations_json": args.translations_json,
            "offline_reference_runtime_use": False,
        },
    )
    append_jsonl(
        model_log,
        {
            "state": "S0_Request",
            "model_backend": "not_invoked",
            "reason": "round22 runner uses pre-supplied semantic translations and deterministic local visual gates; prompt templates are included for future model adjudication only",
        },
    )

    run_cmd(root, [sys.executable, "tools/probes/probe_runtime.py"], "S1_ToolProbe", trace, operation_log, decision_log)
    run_cmd(
        root,
        [
            sys.executable,
            "tools/probes/extract_source_structure.py",
            "--source-pdf",
            args.source_pdf,
            "--output",
            rel(root, source_structure),
        ],
        "S2_SourceExtract",
        trace,
        operation_log,
        decision_log,
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/planners/plan_roles.py",
            "--source-structure",
            rel(root, source_structure),
            "--translations-json",
            args.translations_json,
            "--output",
            rel(root, role_plan),
        ],
        "S3_RolePlan",
        trace,
        operation_log,
        decision_log,
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/planners/plan_layout.py",
            "--source-pdf",
            args.source_pdf,
            "--role-plan",
            rel(root, role_plan),
            "--output",
            rel(root, layout_plan),
        ],
        "S4_LayoutPlan",
        trace,
        operation_log,
        decision_log,
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/generators/generate_candidate.py",
            "--source-pdf",
            args.source_pdf,
            "--layout-plan",
            rel(root, layout_plan),
            "--output-pdf",
            rel(root, output_pdf),
            "--reports-dir",
            "reports",
            "--previews-dir",
            "previews",
        ],
        "S5_GenerateCandidate",
        trace,
        operation_log,
        decision_log,
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/validators/validate_quality.py",
            "--generation-evidence",
            "reports/generation_evidence.json",
            "--output",
            "reports/quality_gates.json",
        ],
        "S6_QualityGate",
        trace,
        operation_log,
        decision_log,
    )
    quality_report = json.loads((reports / "quality_gates.json").read_text(encoding="utf-8"))
    append_jsonl(
        decision_log,
        {
            "state": "S6_QualityGate",
            "decision_type": "product_quality_verdict",
            "product_quality_verdict": quality_report.get("product_quality_verdict"),
            "blocking_failure_count": quality_report.get("blocking_failure_count"),
        },
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/repairs/plan_repairs.py",
            "--quality-gates",
            "reports/quality_gates.json",
            "--output",
            "reports/repair_plan_0.json",
            "--loop-index",
            "0",
        ],
        "L0_RepairSelection",
        trace,
        operation_log,
        decision_log,
    )
    repair_report = json.loads((reports / "repair_plan_0.json").read_text(encoding="utf-8"))
    append_jsonl(
        decision_log,
        {
            "state": "L0_RepairSelection",
            "decision_type": "repair_family_selection",
            "selected_repair_family": repair_report.get("selected_repair_family"),
            "verdict": repair_report.get("verdict"),
        },
    )
    run_cmd(
        root,
        [
            sys.executable,
            "tools/validators/validate_process.py",
            "--round-root",
            ".",
            "--output",
            "reports/process_audit.json",
        ],
        "S7_ProcessAudit",
        trace,
        operation_log,
        decision_log,
    )
    (reports / "state_trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
