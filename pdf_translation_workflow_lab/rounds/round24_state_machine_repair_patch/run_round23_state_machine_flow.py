import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def clean_runtime_dirs(root: Path) -> None:
    for name in ("reports", "output", "previews"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def boundary_report(root: Path, planned: list[Path]) -> dict[str, Any]:
    items = []
    verdict = "PASS"
    for path in planned:
        inside = is_inside(root, path)
        items.append(
            {
                "path": str(path),
                "resolved": str(path.resolve()),
                "inside_execution_root": inside,
            }
        )
        if not inside:
            verdict = "FAIL"
    return {
        "tool": "round23_inline_workspace_boundary_preflight",
        "execution_root": str(root.resolve()),
        "workspace_boundary_verdict": verdict,
        "checked_paths": items,
    }


def log_state(
    trace: list[dict[str, Any]],
    *,
    state: str,
    purpose: str,
    input_artifacts: list[str],
    output_artifacts: list[str],
    decision: str,
    next_state: str,
) -> None:
    trace.append(
        {
            "state": state,
            "purpose": purpose,
            "input_artifacts": input_artifacts,
            "output_artifacts": output_artifacts,
            "decision": decision,
            "next_state": next_state,
            "timestamp_local": now(),
        }
    )


def run_cmd(root: Path, command: list[str], state: str, operation_log: Path, decision_log: Path) -> None:
    started = now()
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        command,
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
    )
    ended = now()
    record = {
        "state": state,
        "command": command,
        "started_at": started,
        "ended_at": ended,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-4000:],
    }
    append_jsonl(operation_log, record)
    append_jsonl(
        decision_log,
        {
            "state": state,
            "decision_type": "tool_dispatch",
            "selected_tool": command[1] if len(command) > 1 else command[0],
            "reason": "PDF_语义翻译回填_状态机与工具编排设计.md state contract",
            "returncode": proc.returncode,
        },
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def build_page_strategy(source_structure: Path, output: Path) -> dict[str, Any]:
    data = json.loads(source_structure.read_text(encoding="utf-8"))
    pages = []
    for page in data.get("pages", []):
        lines = page.get("lines", [])
        stats = page.get("page_stats", {})
        accent_count = len(stats.get("accent_colors") or [])
        page_type = "mixed_text_table_or_panel" if accent_count >= 2 or len(lines) > 80 else "body_or_dashboard"
        pages.append(
            {
                "page_index": page.get("page_index"),
                "page_type_guess": page_type,
                "line_count": len(lines),
                "font_q50": stats.get("font_q50"),
                "font_max": stats.get("font_max"),
                "accent_color_count": accent_count,
                "strategy_source": "current_run_source_structure",
            }
        )
    report = {
        "tool": "round23_inline_page_strategy",
        "page_count": data.get("page_count"),
        "pages": pages,
        "anti_overfit_statement": "Page strategy uses current-run line counts, font statistics, and accent color counts only.",
    }
    write_json(output, report)
    return report


def validate_presupplied_translations(translations_json: Path, output: Path) -> dict[str, Any]:
    data = json.loads(translations_json.read_text(encoding="utf-8"))
    coverage = data.get("coverage") or {}
    units = data.get("units") or []
    missing = coverage.get("missing_unit_ids") or []
    pseudo_prefixes = ("This line reports", "This line describes", "本行说明", "本行列示")
    pseudo_units = []
    empty_units = []
    target_field = data.get("target_text_field") or "translation_target_text"
    for unit in units:
        text = str(unit.get(target_field) or unit.get("translation_target_text") or "").strip()
        if not text:
            empty_units.append(unit.get("unit_id"))
        if text.startswith(pseudo_prefixes):
            pseudo_units.append(unit.get("unit_id"))
    verdict = (
        "PASS"
        if data.get("translation_quality") == "semantic_translation"
        and data.get("semantic_coverage") == "full_semantic_translation"
        and not missing
        and not empty_units
        and not pseudo_units
        else "FAIL"
    )
    report = {
        "tool": "round23_inline_presupplied_semantic_translation_validation",
        "translation_provider": data.get("translation_provider"),
        "source_language": data.get("source_language"),
        "target_language": data.get("target_language"),
        "translation_quality": data.get("translation_quality"),
        "semantic_coverage": data.get("semantic_coverage"),
        "unit_count": len(units),
        "coverage": coverage,
        "empty_unit_count": len(empty_units),
        "pseudo_unit_count": len(pseudo_units),
        "translation_validation_verdict": verdict,
        "note": "Round23 consumes pre-supplied semantic translations from round22 input; it does not call a translation model.",
    }
    write_json(output, report)
    return report


def write_visual_adjudication(quality_gates: Path, repair_plan: Path, output: Path) -> dict[str, Any]:
    gates = json.loads(quality_gates.read_text(encoding="utf-8"))
    repair = json.loads(repair_plan.read_text(encoding="utf-8"))
    verdict = "PASS" if gates.get("product_quality_verdict") == "PASS" else "FAIL"
    report = {
        "tool": "round23_inline_visual_adjudication",
        "verdict": verdict,
        "product_quality_verdict": gates.get("product_quality_verdict"),
        "blocking_failure_count": gates.get("blocking_failure_count"),
        "selected_repair_family": repair.get("selected_repair_family"),
        "dimensions": {
            "all_groups_fit": any(item.get("gate_id") == "all_groups_fit" for item in gates.get("blocking_failures", [])),
            "source_relative_font_floor": any(item.get("gate_id") == "source_relative_font_floor" for item in gates.get("blocking_failures", [])),
            "local_text_overlap": any(item.get("gate_id") == "local_text_overlap" for item in gates.get("blocking_failures", [])),
        },
        "model_backend": "not_invoked",
        "reason": "Round23 uses deterministic round22 local quality gates; no backend vision model call was made.",
    }
    write_json(output, report)
    return report


def write_repair_loop_record(
    output: Path,
    *,
    repair_plan: dict[str, Any],
    quality_gates: dict[str, Any],
    target_state: str,
) -> dict[str, Any]:
    selected = repair_plan.get("selected_repair_family")
    record = {
        "loop_id": "Lx_0001",
        "loop_iteration": 1,
        "entered_from_state": "S8_VerifyProductQuality",
        "selected_failure_class": selected,
        "blocking_failure_count": quality_gates.get("blocking_failure_count"),
        "target_state": target_state,
        "repair_patch_ref": None,
        "repair_patch_operation_count": 0,
        "execution_status": "not_executed_unrepairable",
        "reason": "Round22 tool package provides repair selection but no generic auto-apply RepairPatch executor for this failure family.",
        "exit_state": "S_FAIL_QUALITY",
    }
    write_json(output, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", default="input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf")
    parser.add_argument(
        "--translations-json",
        default="input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json",
    )
    parser.add_argument("--case-id", default="R23_GEN_ZH_TO_EN_00005_pages_001_020")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    clean_runtime_dirs(root)
    reports = root / "reports"
    output_pdf = root / "output" / f"{args.case_id}_candidate.pdf"
    source_structure = reports / "source_structure.json"
    page_strategy = reports / "page_strategy.json"
    semantic_validation = reports / "semantic_translation_validation.json"
    role_plan = reports / "role_plan.json"
    layout_plan = reports / "layout_plan.json"
    visual_adjudication = reports / "visual_adjudication.json"
    repair_loop = reports / "repair_loop_0001.json"
    state_trace = reports / "state_trace.json"
    decision_log = reports / "decision_log.jsonl"
    operation_log = reports / "operation_log.jsonl"
    model_log = reports / "model_interactions.jsonl"
    trace: list[dict[str, Any]] = []

    source_pdf = root / args.source_pdf
    translations_json = root / args.translations_json

    run_request = {
        "case_id": args.case_id,
        "source_pdf": args.source_pdf,
        "translations_json": args.translations_json,
        "run_mode": "product_quality_lab_probe",
        "process_design": "docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md",
        "package_root": str(root),
        "offline_reference_runtime_use": False,
    }
    write_json(reports / "run_request.json", run_request)
    append_jsonl(decision_log, {"state": "S0_Request", "decision_type": "run_request", **run_request})
    append_jsonl(
        model_log,
        {
            "state": "S0_Request",
            "model_backend": "not_invoked",
            "reason": "round23 uses pre-supplied semantic translations and deterministic local quality gates",
        },
    )
    log_state(
        trace,
        state="S0_Request",
        purpose="确认输入、输出、run mode、非目标",
        input_artifacts=[args.source_pdf, args.translations_json],
        output_artifacts=["reports/run_request.json"],
        decision="inputs_declared",
        next_state="S1_ContractLoad",
    )

    required_contracts = [
        root / "README.md",
        root / "EXECUTION.md",
        root / "docs" / "设计" / "PDF_语义翻译回填_状态机与工具编排设计.md",
        root / "contracts" / "state_machine.md",
        root / "contracts" / "tool_contracts.md",
        root / "contracts" / "execution_procedure.md",
        root / "prompts" / "templates" / "visual_quality_adjudication.prompt.json",
        root / "prompts" / "templates" / "repair_selection.prompt.json",
    ]
    planned_paths = [reports, root / "output", root / "previews", source_pdf, translations_json]
    boundary = boundary_report(root, planned_paths)
    missing_contracts = [rel(root, path) for path in required_contracts if not path.exists()]
    contract_record = {
        "state": "S1_ContractLoad",
        "missing_contracts": missing_contracts,
        "workspace_boundary_preflight": "reports/workspace_boundary_preflight.json",
        "verdict": "PASS" if not missing_contracts and boundary["workspace_boundary_verdict"] == "PASS" else "FAIL",
    }
    write_json(reports / "workspace_boundary_preflight.json", boundary)
    write_json(reports / "contract_load_record.json", contract_record)
    append_jsonl(decision_log, {"state": "S1_ContractLoad", "decision_type": "contract_load", **contract_record})
    if contract_record["verdict"] != "PASS":
        write_json(state_trace, trace)
        raise SystemExit(2)
    log_state(
        trace,
        state="S1_ContractLoad",
        purpose="读取流程、契约、提示词、工具说明并验证执行根写入边界",
        input_artifacts=[rel(root, path) for path in required_contracts],
        output_artifacts=["reports/contract_load_record.json", "reports/workspace_boundary_preflight.json"],
        decision="contracts_loaded_and_workspace_boundary_pass",
        next_state="S2_ToolProbe",
    )

    run_cmd(root, [sys.executable, "tools/probes/probe_runtime.py"], "S2_ToolProbe", operation_log, decision_log)
    log_state(
        trace,
        state="S2_ToolProbe",
        purpose="探测 Python、PDF 库、字体和渲染能力",
        input_artifacts=[],
        output_artifacts=["reports/tool_probe.json"],
        decision="tool_probe_pass",
        next_state="S3_SourceExtract",
    )

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
        "S3_SourceExtract",
        operation_log,
        decision_log,
    )
    log_state(
        trace,
        state="S3_SourceExtract",
        purpose="提取源 PDF 页尺寸、文本、bbox、字体、颜色和当前页统计",
        input_artifacts=[args.source_pdf],
        output_artifacts=["reports/source_structure.json"],
        decision="source_structure_extracted",
        next_state="S4_PageStrategy",
    )

    strategy = build_page_strategy(source_structure, page_strategy)
    append_jsonl(
        decision_log,
        {
            "state": "S4_PageStrategy",
            "decision_type": "page_strategy",
            "page_count": strategy.get("page_count"),
            "model_backend": "not_invoked",
            "reason": "current-run source-structure heuristic",
        },
    )
    log_state(
        trace,
        state="S4_PageStrategy",
        purpose="判断页面类型和区域角色的上游约束",
        input_artifacts=["reports/source_structure.json"],
        output_artifacts=["reports/page_strategy.json"],
        decision="page_strategy_recorded",
        next_state="S5_TranslationPlan",
    )

    translation_report = validate_presupplied_translations(translations_json, semantic_validation)
    append_jsonl(
        model_log,
        {
            "state": "S5_TranslationPlan",
            "model_backend": "not_invoked",
            "reason": "semantic translations are pre-supplied in round22 input",
            "translation_provider": translation_report.get("translation_provider"),
            "unit_count": translation_report.get("unit_count"),
        },
    )
    append_jsonl(decision_log, {"state": "S5_TranslationPlan", "decision_type": "semantic_translation_validation", **translation_report})
    if translation_report["translation_validation_verdict"] != "PASS":
        write_json(state_trace, trace)
        raise SystemExit(3)
    log_state(
        trace,
        state="S5_TranslationPlan",
        purpose="校验预供应语义译文是否满足 product-quality 输入前提",
        input_artifacts=[args.translations_json],
        output_artifacts=["reports/semantic_translation_validation.json"],
        decision="presupplied_semantic_translation_pass",
        next_state="S6_LayoutPlan",
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
        "S6_LayoutPlan:S6D_BuildRolePlan",
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
        "S6_LayoutPlan:S6E_BuildGeneratorConsumableLayoutPlan",
        operation_log,
        decision_log,
    )
    log_state(
        trace,
        state="S6_LayoutPlan",
        purpose="生成角色计划和 generator-consumable 布局计划",
        input_artifacts=["reports/source_structure.json", args.translations_json, "reports/page_strategy.json"],
        output_artifacts=["reports/role_plan.json", "reports/layout_plan.json"],
        decision="layout_plan_ready",
        next_state="S7_GenerateCandidate",
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
        "S7_GenerateCandidate",
        operation_log,
        decision_log,
    )
    log_state(
        trace,
        state="S7_GenerateCandidate",
        purpose="擦除源语文本并回填目标语候选 PDF",
        input_artifacts=[args.source_pdf, "reports/layout_plan.json"],
        output_artifacts=[rel(root, output_pdf), "reports/generation_evidence.json"],
        decision="candidate_generated",
        next_state="S8_VerifyProductQuality",
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
        "S8_VerifyProductQuality:S8B_CollectVisualRegionMetrics",
        operation_log,
        decision_log,
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
        "S8_VerifyProductQuality:S8F_BindRepairAtom",
        operation_log,
        decision_log,
    )
    gates = json.loads((reports / "quality_gates.json").read_text(encoding="utf-8"))
    repair = json.loads((reports / "repair_plan_0.json").read_text(encoding="utf-8"))
    visual = write_visual_adjudication(reports / "quality_gates.json", reports / "repair_plan_0.json", visual_adjudication)
    append_jsonl(
        model_log,
        {
            "state": "S8_VerifyProductQuality",
            "model_backend": "not_invoked",
            "reason": "deterministic local quality gates used instead of backend visual model",
            "visual_adjudication": visual,
        },
    )
    next_after_s8 = "S9_VerifyProcessContract" if gates.get("product_quality_verdict") == "PASS" else "Lx_RepairLoop"
    append_jsonl(
        decision_log,
        {
            "state": "S8_VerifyProductQuality",
            "decision_type": "product_quality_verdict",
            "product_quality_verdict": gates.get("product_quality_verdict"),
            "blocking_failure_count": gates.get("blocking_failure_count"),
            "next_state": next_after_s8,
        },
    )
    log_state(
        trace,
        state="S8_VerifyProductQuality",
        purpose="执行候选质量 gate、多信号融合和修复族选择",
        input_artifacts=["reports/generation_evidence.json"],
        output_artifacts=["reports/quality_gates.json", "reports/repair_plan_0.json", "reports/visual_adjudication.json"],
        decision=f"product_quality={gates.get('product_quality_verdict')}",
        next_state=next_after_s8,
    )

    terminal_product_state = "S_DONE_PRODUCT_ACCEPTED"
    if gates.get("product_quality_verdict") != "PASS":
        selected_family = repair.get("selected_repair_family")
        target_state = "S6_LayoutPlan" if selected_family else "S_FAIL_QUALITY"
        loop_record = write_repair_loop_record(repair_loop, repair_plan=repair, quality_gates=gates, target_state=target_state)
        append_jsonl(decision_log, {"state": "Lx_RepairLoop", "decision_type": "repair_loop_execution", **loop_record})
        log_state(
            trace,
            state="Lx_RepairLoop",
            purpose="对一个阻塞失败执行修复闭环；本实验包只能选择修复族，不能应用 RepairPatch",
            input_artifacts=["reports/quality_gates.json", "reports/repair_plan_0.json"],
            output_artifacts=["reports/repair_loop_0001.json"],
            decision="repair_selection_recorded_but_patch_not_executed",
            next_state="S_FAIL_QUALITY",
        )
        terminal_product_state = "S_FAIL_QUALITY"

    write_json(state_trace, trace)
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
        "S9_VerifyProcessContract",
        operation_log,
        decision_log,
    )
    process = json.loads((reports / "process_audit.json").read_text(encoding="utf-8"))
    final = {
        "case_id": args.case_id,
        "candidate_pdf": rel(root, output_pdf),
        "process_contract_verdict": process.get("process_contract_verdict"),
        "product_quality_verdict": gates.get("product_quality_verdict"),
        "terminal_state": "S_DONE_PRODUCT_ACCEPTED"
        if process.get("process_contract_verdict") == "PASS" and terminal_product_state == "S_DONE_PRODUCT_ACCEPTED"
        else terminal_product_state,
        "blocking_failure_count": gates.get("blocking_failure_count"),
        "selected_repair_family": repair.get("selected_repair_family"),
        "round23_flow_design": "docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md",
    }
    log_state(
        trace,
        state="S9_VerifyProcessContract",
        purpose="验证状态 trace、操作日志、写入边界、过程契约和最终终态",
        input_artifacts=[
            "reports/state_trace.json",
            "reports/decision_log.jsonl",
            "reports/operation_log.jsonl",
            "reports/model_interactions.jsonl",
            "reports/quality_gates.json",
            "reports/repair_loop_0001.json",
        ],
        output_artifacts=["reports/process_audit.json", "reports/round23_final_verdict.json"],
        decision=f"process={process.get('process_contract_verdict')}; product={gates.get('product_quality_verdict')}",
        next_state=final["terminal_state"],
    )
    write_json(state_trace, trace)
    write_json(reports / "round23_final_verdict.json", final)
    append_jsonl(decision_log, {"state": "S9_VerifyProcessContract", "decision_type": "final_verdict", **final})

    report_lines = [
        "# Round23 State-Machine Flow Run Report",
        "",
        "## Input",
        "",
        f"- Source PDF: `{args.source_pdf}`",
        f"- Semantic translations: `{args.translations_json}`",
        f"- Case ID: `{args.case_id}`",
        "",
        "## Verdict",
        "",
        f"- Process contract: `{final['process_contract_verdict']}`",
        f"- Product quality: `{final['product_quality_verdict']}`",
        f"- Terminal state: `{final['terminal_state']}`",
        f"- Blocking failure count: `{final['blocking_failure_count']}`",
        f"- Selected repair family: `{final['selected_repair_family']}`",
        "",
        "## Notes",
        "",
        "- This run uses the new state-machine design document as orchestration guidance.",
        "- Runtime layout/generation tools are inherited from round22.",
        "- Translation model and visual model were not invoked; this is explicitly recorded in `model_interactions.jsonl`.",
        "- Because the inherited round22 package has repair selection but no generic RepairPatch executor, a product-quality failure enters `Lx_RepairLoop` and terminates honestly at `S_FAIL_QUALITY`.",
    ]
    (reports / "round23_state_machine_run_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
