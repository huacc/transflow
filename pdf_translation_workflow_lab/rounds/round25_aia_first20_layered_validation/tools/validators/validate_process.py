import argparse
import json
import re
from pathlib import Path


FORBIDDEN_TOOL_IMPORTS = ["pdf_" + "translation_workflow_core"]
FORBIDDEN_RUNTIME_PATH_TOKENS = ["D:\\", "D:/", "C:\\", "C:/", "offline_reference_compare"]
FIXED_PAGE_BRANCH_RE = re.compile(r"\bpage_(?:index|number|no)\s*(?:==|!=|in)\s*(?:\d+|\{|\[)", re.IGNORECASE)


def run(round_root: Path, output: Path) -> None:
    reports = round_root / "reports"
    required_reports = [
        reports / "run_request.json",
        reports / "contract_load_record.json",
        reports / "workspace_boundary_preflight.json",
        reports / "tool_probe.json",
        reports / "source_structure.json",
        reports / "page_strategy.json",
        reports / "semantic_translation_validation.json",
        reports / "role_plan.json",
        reports / "layout_plan.json",
        reports / "generation_evidence.json",
        reports / "quality_gates.json",
        reports / "quality_signals.json",
        reports / "visual_adjudication.json",
        reports / "repair_plan_0.json",
        reports / "repair_patch_0001.json",
        reports / "repair_patch_application_0001.json",
        reports / "layout_plan.repair0001.json",
        reports / "generation_evidence.repair0001.json",
        reports / "quality_gates.repair0001.json",
        reports / "quality_signals.repair0001.json",
        reports / "visual_adjudication.repair0001.json",
        reports / "repair_loop_0001.json",
        reports / "state_trace.json",
        reports / "decision_log.jsonl",
        reports / "operation_log.jsonl",
        reports / "model_interactions.jsonl",
    ]
    required_static = [
        round_root / "README.md",
        round_root / "EXECUTION.md",
        round_root / "run_round25_layered_case.py",
        round_root / "run_round25_batch.py",
        round_root / "contracts" / "state_machine.md",
        round_root / "contracts" / "tool_contracts.md",
        round_root / "contracts" / "execution_procedure.md",
        round_root / "contracts" / "failure_dispatch_table.json",
        round_root / "prompts" / "templates" / "S5_materialize_translation.prompt.json",
        round_root / "prompts" / "templates" / "visual_quality_adjudication.prompt.json",
        round_root / "prompts" / "templates" / "repair_selection.prompt.json",
        round_root / "prompts" / "templates" / "S8A_quality_signal_normalization.prompt.json",
        round_root / "prompts" / "templates" / "S8B_quality_triage.prompt.json",
        round_root / "prompts" / "templates" / "S8C_repair_patch_binding.prompt.json",
        round_root / "prompts" / "templates" / "Lx_repair_loop_execution.prompt.json",
        round_root / "tools" / "generate_round22_layout_candidate.py",
        round_root / "tools" / "probes" / "probe_runtime.py",
        round_root / "tools" / "probes" / "extract_source_structure.py",
        round_root / "tools" / "planners" / "plan_roles.py",
        round_root / "tools" / "planners" / "plan_layout.py",
        round_root / "tools" / "generators" / "generate_candidate.py",
        round_root / "tools" / "judges" / "compare_source_candidate.py",
        round_root / "tools" / "translators" / "materialize_google_gtx_translations.py",
        round_root / "tools" / "validators" / "validate_quality.py",
        round_root / "tools" / "validators" / "validate_process.py",
        round_root / "tools" / "repairs" / "plan_repairs.py",
        round_root / "tools" / "repairs" / "build_repair_patch.py",
        round_root / "tools" / "repairs" / "apply_repair_patch.py",
    ]
    required = required_reports + required_static
    missing = [str(path.relative_to(round_root)) for path in required if not path.exists()]

    forbidden_import_hits = []
    forbidden_path_hits = []
    fixed_page_branch_hits = []
    for path in (round_root / "tools").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "validate_process.py":
            continue
        for token in FORBIDDEN_TOOL_IMPORTS:
            if token in text:
                forbidden_import_hits.append({"file": str(path.relative_to(round_root)), "token": token})
        for token in FORBIDDEN_RUNTIME_PATH_TOKENS:
            if token in text:
                forbidden_path_hits.append({"file": str(path.relative_to(round_root)), "token": token})
        if FIXED_PAGE_BRANCH_RE.search(text):
            fixed_page_branch_hits.append({"file": str(path.relative_to(round_root)), "pattern": "fixed_page_branch"})

    run_request = {}
    if (reports / "run_request.json").exists():
        run_request = json.loads((reports / "run_request.json").read_text(encoding="utf-8"))

    failures = []
    if missing:
        failures.append({"gate_id": "required_artifacts", "evidence": missing})
    if forbidden_import_hits:
        failures.append({"gate_id": "core_import_boundary", "evidence": forbidden_import_hits})
    if forbidden_path_hits:
        failures.append({"gate_id": "runtime_path_or_reference_boundary", "evidence": forbidden_path_hits})
    if fixed_page_branch_hits:
        failures.append({"gate_id": "fixed_page_branch_overfit_scan", "evidence": fixed_page_branch_hits})
    if run_request.get("offline_reference_runtime_use") is not False:
        failures.append({"gate_id": "offline_reference_boundary", "evidence": run_request})
    pycache_dirs = [str(path.relative_to(round_root)) for path in round_root.rglob("__pycache__") if path.is_dir()]
    if pycache_dirs:
        failures.append({"gate_id": "python_bytecode_pollution", "evidence": pycache_dirs})

    report = {
        "tool": "validate_process",
        "process_contract_verdict": "FAIL" if failures else "PASS",
        "failure_count": len(failures),
        "failures": failures,
        "checked_required_artifacts": [str(path.relative_to(round_root)) for path in required],
        "checked_tool_files": [str(path.relative_to(round_root)) for path in (round_root / "tools").rglob("*.py")],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.round_root.resolve(), args.output)


if __name__ == "__main__":
    main()
