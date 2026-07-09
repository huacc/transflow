import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def clean_runtime_dirs(root: Path) -> None:
    for name in ("reports", "output", "previews"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


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
    append_jsonl(
        operation_log,
        {
            "state": state,
            "command": command,
            "started_at": started,
            "ended_at": now(),
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-4000:],
        },
    )
    append_jsonl(
        decision_log,
        {
            "state": state,
            "decision_type": "tool_dispatch",
            "selected_tool": command[1] if len(command) > 1 else command[0],
            "returncode": proc.returncode,
        },
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


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


def boundary_report(root: Path, planned_paths: list[Path]) -> dict[str, Any]:
    checked = []
    verdict = "PASS"
    for path in planned_paths:
        inside = is_inside(root, path)
        checked.append({"path": str(path), "resolved": str(path.resolve()), "inside_round_root": inside})
        if not inside:
            verdict = "FAIL"
    return {
        "tool": "round25_workspace_boundary_preflight",
        "round_root": str(root.resolve()),
        "workspace_boundary_verdict": verdict,
        "checked_paths": checked,
    }


def build_page_strategy(source_structure: Path, output: Path) -> dict[str, Any]:
    data = json.loads(source_structure.read_text(encoding="utf-8"))
    pages = []
    for page in data.get("pages", []):
        lines = page.get("lines", [])
        stats = page.get("page_stats", {})
        accent_count = len(stats.get("accent_colors") or [])
        pages.append(
            {
                "page_index": page.get("page_index"),
                "line_count": len(lines),
                "font_q50": stats.get("font_q50"),
                "font_max": stats.get("font_max"),
                "accent_color_count": accent_count,
                "page_type_guess": "dense_or_mixed_layout" if len(lines) > 80 or accent_count >= 2 else "simple_flow_or_panel",
                "strategy_source": "current_run_source_structure",
            }
        )
    result = {
        "tool": "round25_page_strategy",
        "page_count": data.get("page_count"),
        "pages": pages,
        "anti_overfit_statement": "Only current-run line count, font stats, and accent-color stats are used.",
    }
    write_json(output, result)
    return result


def validate_presupplied_translations(path: Path, output: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    units = data.get("units") or []
    target_field = data.get("target_text_field") or "translation_target_text"
    empty = []
    pseudo = []
    pseudo_prefixes = ("This line reports", "This line describes", "本行说明", "本行列示")
    for unit in units:
        text = str(unit.get(target_field) or unit.get("translation_target_text") or "").strip()
        if not text:
            empty.append(unit.get("unit_id"))
        if text.startswith(pseudo_prefixes):
            pseudo.append(unit.get("unit_id"))
    verdict = (
        "PASS"
        if data.get("translation_quality") == "semantic_translation"
        and data.get("semantic_coverage") == "full_semantic_translation"
        and not empty
        and not pseudo
        else "FAIL"
    )
    result = {
        "tool": "round25_presupplied_translation_validation",
        "translation_provider": data.get("translation_provider"),
        "source_language": data.get("source_language"),
        "target_language": data.get("target_language"),
        "unit_count": len(units),
        "empty_unit_count": len(empty),
        "pseudo_unit_count": len(pseudo),
        "translation_validation_verdict": verdict,
        "note": "Round25 validates semantic translations and records the translation materialization path.",
    }
    write_json(output, result)
    return result


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def prompt_log(model_log: Path, state: str, template: str, inputs: list[str], output_schema: str, reason: str) -> None:
    append_jsonl(
        model_log,
        {
            "state": state,
            "prompt_template": template,
            "model_backend": "not_invoked",
            "input_slots": inputs,
            "expected_output_schema": output_schema,
            "reason": reason,
        },
    )


def quality_summary(gates: dict[str, Any], adjudication: dict[str, Any]) -> dict[str, Any]:
    failures = gates.get("blocking_failures") or []
    return {
        "product_quality_verdict": gates.get("product_quality_verdict"),
        "blocking_failure_count": gates.get("blocking_failure_count"),
        "gate_counts": dict(Counter(item.get("gate_id") for item in failures)),
        "failure_class_counts": adjudication.get("failure_class_counts") or dict(Counter(item.get("failure_class") for item in failures)),
        "selected_failure_class": adjudication.get("selected_failure_class"),
        "dispatch_result": adjudication.get("dispatch_result"),
        "selected_repair_family": adjudication.get("selected_repair_family"),
        "human_readable_result": adjudication.get("human_readable_result"),
        "tool_selection_reason": adjudication.get("tool_selection_reason"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", default="input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf")
    parser.add_argument(
        "--translations-json",
        default="AUTO",
    )
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--target-language", default="en")
    parser.add_argument("--case-id", default="R25_CASE")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    clean_runtime_dirs(root)
    reports = root / "reports"
    output = root / "output"
    previews = root / "previews"
    source_pdf = root / args.source_pdf
    auto_translate = args.translations_json.upper() == "AUTO"
    translations_json = reports / "semantic_translations.json" if auto_translate else root / args.translations_json
    translations_arg = rel(root, translations_json) if auto_translate else args.translations_json
    decision_log = reports / "decision_log.jsonl"
    operation_log = reports / "operation_log.jsonl"
    model_log = reports / "model_interactions.jsonl"
    trace: list[dict[str, Any]] = []

    initial_pdf = output / f"{args.case_id}_initial_candidate.pdf"
    repaired_pdf = output / f"{args.case_id}_repair0001_candidate.pdf"
    source_structure = reports / "source_structure.json"
    page_strategy = reports / "page_strategy.json"
    semantic_validation = reports / "semantic_translation_validation.json"
    role_plan = reports / "role_plan.json"
    layout_plan = reports / "layout_plan.json"
    repaired_layout_plan = reports / "layout_plan.repair0001.json"
    quality_gates = reports / "quality_gates.json"
    quality_signals = reports / "quality_signals.json"
    visual_adjudication = reports / "visual_adjudication.json"
    repair_plan = reports / "repair_plan_0.json"
    repair_patch = reports / "repair_patch_0001.json"
    repair_application = reports / "repair_patch_application_0001.json"
    repaired_generation = reports / "generation_evidence.repair0001.json"
    repaired_quality = reports / "quality_gates.repair0001.json"
    repaired_signals = reports / "quality_signals.repair0001.json"
    repaired_adjudication = reports / "visual_adjudication.repair0001.json"

    run_request = {
        "case_id": args.case_id,
        "source_pdf": args.source_pdf,
        "translations_json": "AUTO" if auto_translate else args.translations_json,
        "source_language": args.source_language,
        "target_language": args.target_language,
        "run_mode": "round25_state_machine_layered_judge_repair_patch",
        "process_design": "docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md",
        "offline_reference_runtime_use": False,
    }
    write_json(reports / "run_request.json", run_request)
    append_jsonl(decision_log, {"state": "S0_Request", "decision_type": "run_request", **run_request})
    log_state(
        trace,
        state="S0_Request",
        purpose="确认输入、输出、run mode、非目标",
        input_artifacts=[args.source_pdf, "AUTO" if auto_translate else args.translations_json],
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
        root / "contracts" / "failure_dispatch_table.json",
        root / "prompts" / "templates" / "S5_materialize_translation.prompt.json",
        root / "prompts" / "templates" / "S8A_quality_signal_normalization.prompt.json",
        root / "prompts" / "templates" / "S8B_quality_triage.prompt.json",
        root / "prompts" / "templates" / "S8C_repair_patch_binding.prompt.json",
        root / "prompts" / "templates" / "Lx_repair_loop_execution.prompt.json",
    ]
    boundary_paths = [reports, output, previews, source_pdf, translations_json]
    boundary = boundary_report(root, boundary_paths)
    missing = [rel(root, path) for path in required_contracts if not path.exists()]
    contract_record = {
        "state": "S1_ContractLoad",
        "missing_contracts": missing,
        "workspace_boundary_verdict": boundary["workspace_boundary_verdict"],
        "verdict": "PASS" if not missing and boundary["workspace_boundary_verdict"] == "PASS" else "FAIL",
    }
    write_json(reports / "workspace_boundary_preflight.json", boundary)
    write_json(reports / "contract_load_record.json", contract_record)
    append_jsonl(decision_log, {"state": "S1_ContractLoad", "decision_type": "contract_load", **contract_record})
    if contract_record["verdict"] != "PASS":
        write_json(reports / "state_trace.json", trace)
        raise SystemExit(2)
    log_state(
        trace,
        state="S1_ContractLoad",
        purpose="读取流程、契约、分层提示词和工具说明，并验证执行根写入边界",
        input_artifacts=[rel(root, path) for path in required_contracts],
        output_artifacts=["reports/contract_load_record.json", "reports/workspace_boundary_preflight.json"],
        decision="contracts_and_prompt_templates_loaded",
        next_state="S2_ToolProbe",
    )

    run_cmd(root, [sys.executable, "tools/probes/probe_runtime.py"], "S2_ToolProbe", operation_log, decision_log)
    log_state(trace, state="S2_ToolProbe", purpose="探测 Python、PDF 库、字体和渲染能力", input_artifacts=[], output_artifacts=["reports/tool_probe.json"], decision="tool_probe_pass", next_state="S3_SourceExtract")

    run_cmd(root, [sys.executable, "tools/probes/extract_source_structure.py", "--source-pdf", args.source_pdf, "--output", rel(root, source_structure)], "S3_SourceExtract", operation_log, decision_log)
    log_state(trace, state="S3_SourceExtract", purpose="提取源 PDF 页尺寸、文字、bbox、字体、颜色和当前页统计", input_artifacts=[args.source_pdf], output_artifacts=["reports/source_structure.json"], decision="source_structure_extracted", next_state="S4_PageStrategy")

    strategy = build_page_strategy(source_structure, page_strategy)
    append_jsonl(decision_log, {"state": "S4_PageStrategy", "decision_type": "page_strategy", "page_count": strategy.get("page_count"), "reason": "current-run source structure only"})
    log_state(trace, state="S4_PageStrategy", purpose="判断页面类型和区域角色的上游约束", input_artifacts=["reports/source_structure.json"], output_artifacts=["reports/page_strategy.json"], decision="page_strategy_recorded", next_state="S5_TranslationPlan")

    if auto_translate:
        run_cmd(
            root,
            [
                sys.executable,
                "tools/translators/materialize_google_gtx_translations.py",
                "--source-structure",
                rel(root, source_structure),
                "--source-language",
                args.source_language,
                "--target-language",
                args.target_language,
                "--output",
                rel(root, translations_json),
                "--cache",
                f"reports/translation_cache_{args.case_id}.json",
            ],
            "S5_TranslationPlan:S5C_MaterializeSemanticTranslations",
            operation_log,
            decision_log,
        )
        append_jsonl(
            model_log,
            {
                "state": "S5_TranslationPlan",
                "prompt_template": "S5_materialize_translation.prompt.json",
                "model_backend": "google_translate_web_gtx_public_endpoint",
                "input_slots": ["source_structure", "source_language", "target_language"],
                "expected_output_schema": "semantic translations with unit_id alignment",
                "reason": "Round25 materializes translations inside S5 from current-run source extraction. No reference PDF is used.",
            },
        )
    else:
        prompt_log(model_log, "S5_TranslationPlan", "pre_supplied_translation_validation", [args.translations_json], "semantic_translation_validation", "No backend translation model invoked because a pre-supplied translation JSON was provided.")
    translation_report = validate_presupplied_translations(translations_json, semantic_validation)
    append_jsonl(decision_log, {"state": "S5_TranslationPlan", "decision_type": "semantic_translation_validation", **translation_report})
    if translation_report["translation_validation_verdict"] != "PASS":
        write_json(reports / "state_trace.json", trace)
        raise SystemExit(3)
    log_state(trace, state="S5_TranslationPlan", purpose="物化或校验语义译文是否满足产品质量输入前提", input_artifacts=["reports/source_structure.json" if auto_translate else args.translations_json], output_artifacts=[rel(root, translations_json), "reports/semantic_translation_validation.json"], decision="semantic_translation_pass", next_state="S6_LayoutPlan")

    run_cmd(root, [sys.executable, "tools/planners/plan_roles.py", "--source-structure", rel(root, source_structure), "--translations-json", translations_arg, "--output", rel(root, role_plan)], "S6_LayoutPlan:S6D_BuildRolePlan", operation_log, decision_log)
    run_cmd(root, [sys.executable, "tools/planners/plan_layout.py", "--source-pdf", args.source_pdf, "--role-plan", rel(root, role_plan), "--output", rel(root, layout_plan)], "S6_LayoutPlan:S6E_BuildLayoutPlan", operation_log, decision_log)
    log_state(trace, state="S6_LayoutPlan", purpose="生成角色计划和 generator-consumable 布局计划", input_artifacts=["reports/source_structure.json", translations_arg, "reports/page_strategy.json"], output_artifacts=["reports/role_plan.json", "reports/layout_plan.json"], decision="layout_plan_ready", next_state="S7_GenerateCandidate")

    run_cmd(root, [sys.executable, "tools/generators/generate_candidate.py", "--source-pdf", args.source_pdf, "--layout-plan", rel(root, layout_plan), "--output-pdf", rel(root, initial_pdf), "--reports-dir", "reports", "--previews-dir", "previews/initial"], "S7_GenerateCandidate:initial", operation_log, decision_log)
    log_state(trace, state="S7_GenerateCandidate", purpose="擦除源语文本并回填目标语候选 PDF", input_artifacts=[args.source_pdf, "reports/layout_plan.json"], output_artifacts=[rel(root, initial_pdf), "reports/generation_evidence.json"], decision="initial_candidate_generated", next_state="S8_VerifyProductQuality")

    run_cmd(root, [sys.executable, "tools/validators/validate_quality.py", "--generation-evidence", "reports/generation_evidence.json", "--output", "reports/quality_gates.json"], "S8_VerifyProductQuality:S8A_LocalGates", operation_log, decision_log)
    run_cmd(root, [sys.executable, "tools/judges/compare_source_candidate.py", "--generation-evidence", "reports/generation_evidence.json", "--quality-gates", "reports/quality_gates.json", "--output-signals", "reports/quality_signals.json", "--output-adjudication", "reports/visual_adjudication.json"], "S8_VerifyProductQuality:S8A_S8B_SourceCandidateJudge", operation_log, decision_log)
    prompt_log(model_log, "S8_VerifyProductQuality", "S8A_quality_signal_normalization.prompt.json", ["source_structure", "generation_evidence", "quality_gates"], "QualitySignal array", "Local deterministic judge executed this template contract without backend model call.")
    prompt_log(model_log, "S8_VerifyProductQuality", "S8B_quality_triage.prompt.json", ["quality_signals", "page_strategy", "layout_plan"], "blocking_failure_classes and selected_failure_class", "Local deterministic triage executed this template contract without backend model call.")
    run_cmd(root, [sys.executable, "tools/repairs/plan_repairs.py", "--quality-gates", "reports/quality_gates.json", "--output", "reports/repair_plan_0.json", "--loop-index", "0"], "S8_VerifyProductQuality:S8C_LegacyRepairSelection", operation_log, decision_log)
    run_cmd(root, [sys.executable, "tools/repairs/build_repair_patch.py", "--layout-plan", "reports/layout_plan.json", "--quality-signals", "reports/quality_signals.json", "--visual-adjudication", "reports/visual_adjudication.json", "--output", "reports/repair_patch_0001.json"], "S8_VerifyProductQuality:S8C_BindRepairPatch", operation_log, decision_log)
    prompt_log(model_log, "S8_VerifyProductQuality", "S8C_repair_patch_binding.prompt.json", ["visual_adjudication", "failure_dispatch_table", "quality_signals", "layout_plan"], "RepairPatch operations", "Local deterministic RepairPatch binder executed this template contract without backend model call.")

    before_gates = load_json(quality_gates)
    before_adj = load_json(visual_adjudication)
    patch = load_json(repair_patch)
    before_summary = quality_summary(before_gates, before_adj)
    next_after_s8 = "Lx_RepairLoop" if before_gates.get("product_quality_verdict") != "PASS" and patch.get("patch_verdict") == "PATCH_READY" else "S9_VerifyProcessContract"
    append_jsonl(decision_log, {"state": "S8_VerifyProductQuality", "decision_type": "source_candidate_visual_adjudication", **before_summary, "patch_verdict": patch.get("patch_verdict"), "next_state": next_after_s8})
    log_state(trace, state="S8_VerifyProductQuality", purpose="对候选译文与原文做分层对比研判，归一质量信号并绑定 RepairPatch", input_artifacts=["reports/generation_evidence.json", "reports/quality_gates.json"], output_artifacts=["reports/quality_signals.json", "reports/visual_adjudication.json", "reports/repair_plan_0.json", "reports/repair_patch_0001.json"], decision=f"product_quality={before_gates.get('product_quality_verdict')}; patch={patch.get('patch_verdict')}", next_state=next_after_s8)

    if next_after_s8 == "Lx_RepairLoop":
        run_cmd(root, [sys.executable, "tools/repairs/apply_repair_patch.py", "--layout-plan", "reports/layout_plan.json", "--repair-patch", "reports/repair_patch_0001.json", "--output-layout-plan", "reports/layout_plan.repair0001.json", "--output-report", "reports/repair_patch_application_0001.json"], "Lx_RepairLoop:ApplyRepairPatch", operation_log, decision_log)
        prompt_log(model_log, "Lx_RepairLoop", "Lx_repair_loop_execution.prompt.json", ["repair_patch", "layout_plan", "quality_before"], "loop_verdict and next_state", "Local deterministic repair executor executed this template contract without backend model call.")
        log_state(trace, state="Lx_RepairLoop", purpose="应用一个可执行 RepairPatch，并回跳到布局/生成/质量复判闭环", input_artifacts=["reports/repair_patch_0001.json", "reports/layout_plan.json"], output_artifacts=["reports/layout_plan.repair0001.json", "reports/repair_patch_application_0001.json"], decision="repair_patch_applied", next_state="S7_GenerateCandidate")

        run_cmd(root, [sys.executable, "tools/generators/generate_candidate.py", "--source-pdf", args.source_pdf, "--layout-plan", rel(root, repaired_layout_plan), "--output-pdf", rel(root, repaired_pdf), "--reports-dir", "reports/repair0001_tmp", "--previews-dir", "previews/repair0001"], "S7_GenerateCandidate:repair0001", operation_log, decision_log)
        tmp_generation = reports / "repair0001_tmp" / "generation_evidence.json"
        shutil.copyfile(tmp_generation, repaired_generation)
        log_state(trace, state="S7_GenerateCandidate", purpose="使用修复后的布局计划重新生成候选 PDF", input_artifacts=[args.source_pdf, "reports/layout_plan.repair0001.json"], output_artifacts=[rel(root, repaired_pdf), "reports/generation_evidence.repair0001.json"], decision="repair_candidate_generated", next_state="S8_VerifyProductQuality")

        run_cmd(root, [sys.executable, "tools/validators/validate_quality.py", "--generation-evidence", "reports/generation_evidence.repair0001.json", "--output", "reports/quality_gates.repair0001.json"], "S8_VerifyProductQuality:S8A_LocalGates_repair0001", operation_log, decision_log)
        run_cmd(root, [sys.executable, "tools/judges/compare_source_candidate.py", "--generation-evidence", "reports/generation_evidence.repair0001.json", "--quality-gates", "reports/quality_gates.repair0001.json", "--output-signals", "reports/quality_signals.repair0001.json", "--output-adjudication", "reports/visual_adjudication.repair0001.json"], "S8_VerifyProductQuality:S8A_S8B_SourceCandidateJudge_repair0001", operation_log, decision_log)
    else:
        shutil.copyfile(layout_plan, repaired_layout_plan)
        shutil.copyfile(reports / "generation_evidence.json", repaired_generation)
        shutil.copyfile(quality_gates, repaired_quality)
        shutil.copyfile(quality_signals, repaired_signals)
        shutil.copyfile(visual_adjudication, repaired_adjudication)
        write_json(repair_application, {"tool": "apply_repair_patch", "applied_operation_count": 0, "human_readable_result": "No repair was needed or no executable patch was available."})
        repaired_pdf = initial_pdf

    after_gates = load_json(repaired_quality)
    after_adj = load_json(repaired_adjudication)
    after_summary = quality_summary(after_gates, after_adj)
    application = load_json(repair_application)
    before_count = int(before_gates.get("blocking_failure_count") or 0)
    repair_after_count = int(after_gates.get("blocking_failure_count") or 0)
    before_failure_counts = before_summary.get("failure_class_counts") or {}
    after_failure_counts = after_summary.get("failure_class_counts") or {}
    selected_failure_class = before_adj.get("selected_failure_class")
    selected_before = int(before_failure_counts.get(selected_failure_class) or 0)
    selected_after = int(after_failure_counts.get(selected_failure_class) or 0)
    hard_regressions = {}
    for failure_class, after_value in after_failure_counts.items():
        if failure_class == selected_failure_class:
            continue
        before_value = int(before_failure_counts.get(failure_class) or 0)
        after_value = int(after_value or 0)
        if after_value > before_value:
            hard_regressions[failure_class] = {"before": before_value, "after": after_value}

    if repair_after_count == 0:
        loop_verdict = "PASS"
    elif selected_after < selected_before and repair_after_count < before_count and not hard_regressions:
        loop_verdict = "IMPROVED"
    else:
        loop_verdict = "REJECTED_ROLLBACK"
    repair_accepted = loop_verdict in {"PASS", "IMPROVED"}
    accepted_pdf = repaired_pdf if repair_accepted else initial_pdf
    accepted_gates = after_gates if repair_accepted else before_gates
    accepted_summary = after_summary if repair_accepted else before_summary
    accepted_count = repair_after_count if repair_accepted else before_count
    repair_loop = {
        "loop_id": "Lx_0001",
        "loop_iteration": 1,
        "entered_from_state": "S8_VerifyProductQuality",
        "repair_patch": "reports/repair_patch_0001.json",
        "repair_patch_application": "reports/repair_patch_application_0001.json",
        "applied_operation_count": application.get("applied_operation_count"),
        "before": before_summary,
        "after": after_summary,
        "repair_accepted": repair_accepted,
        "accepted_candidate_pdf": rel(root, accepted_pdf),
        "rejected_candidate_pdf": rel(root, repaired_pdf) if not repair_accepted else None,
        "acceptance_rule": "accept only if selected failure improves, total blocking count decreases, and non-selected hard failure classes do not regress",
        "selected_failure_before_after": {
            "failure_class": selected_failure_class,
            "before": selected_before,
            "after": selected_after,
        },
        "hard_failure_regressions": hard_regressions,
        "loop_verdict": loop_verdict,
        "next_state": "S9_VerifyProcessContract",
        "human_readable_result": f"修复前阻塞 {before_count} 个，修复候选阻塞 {repair_after_count} 个；闭环结果为 {loop_verdict}。",
    }
    write_json(reports / "repair_loop_0001.json", repair_loop)
    append_jsonl(decision_log, {"state": "Lx_RepairLoop", "decision_type": "repair_loop_result", **repair_loop})
    log_state(trace, state="S8_VerifyProductQuality", purpose="对修复候选重新执行源/候选对比质量研判，并决定接受或回滚", input_artifacts=["reports/generation_evidence.repair0001.json", "reports/quality_gates.repair0001.json"], output_artifacts=["reports/quality_signals.repair0001.json", "reports/visual_adjudication.repair0001.json", "reports/repair_loop_0001.json"], decision=f"repair_loop={loop_verdict}; accepted={repair_accepted}; product_quality={accepted_gates.get('product_quality_verdict')}", next_state="S9_VerifyProcessContract")

    write_json(reports / "state_trace.json", trace)
    run_cmd(root, [sys.executable, "tools/validators/validate_process.py", "--round-root", ".", "--output", "reports/process_audit.json"], "S9_VerifyProcessContract", operation_log, decision_log)
    process = load_json(reports / "process_audit.json")
    terminal = "S_DONE_PRODUCT_ACCEPTED" if process.get("process_contract_verdict") == "PASS" and accepted_gates.get("product_quality_verdict") == "PASS" else "S_FAIL_QUALITY" if process.get("process_contract_verdict") == "PASS" else "S_FAIL_PROCESS_CONTRACT"
    final = {
        "case_id": args.case_id,
        "candidate_pdf": rel(root, accepted_pdf),
        "initial_candidate_pdf": rel(root, initial_pdf),
        "rejected_candidate_pdf": rel(root, repaired_pdf) if not repair_accepted else None,
        "process_contract_verdict": process.get("process_contract_verdict"),
        "product_quality_verdict": accepted_gates.get("product_quality_verdict"),
        "terminal_state": terminal,
        "before_blocking_failure_count": before_count,
        "repair_after_blocking_failure_count": repair_after_count,
        "accepted_blocking_failure_count": accepted_count,
        "loop_verdict": loop_verdict,
        "repair_accepted": repair_accepted,
        "selected_failure_before_after": repair_loop["selected_failure_before_after"],
        "hard_failure_regressions": hard_regressions,
        "selected_failure_class": before_adj.get("selected_failure_class"),
        "selected_repair_family": before_adj.get("selected_repair_family"),
        "deferred_failure_classes": patch.get("deferred_failure_classes"),
        "applied_operation_count": application.get("applied_operation_count"),
    }
    write_json(reports / "round25_final_verdict.json", final)
    log_state(trace, state="S9_VerifyProcessContract", purpose="验证状态 trace、工具证据、提示词绑定、写入边界和最终终态", input_artifacts=["reports/state_trace.json", "reports/decision_log.jsonl", "reports/operation_log.jsonl", "reports/model_interactions.jsonl", "reports/repair_loop_0001.json"], output_artifacts=["reports/process_audit.json", "reports/round25_final_verdict.json"], decision=f"process={final['process_contract_verdict']}; product={final['product_quality_verdict']}", next_state=terminal)
    write_json(reports / "state_trace.json", trace)
    append_jsonl(decision_log, {"state": "S9_VerifyProcessContract", "decision_type": "final_verdict", **final})

    report = [
        "# Round25 State-Machine RepairPatch Run Report",
        "",
        "## 1. 运行目标",
        "",
        "本轮验证新的分层状态机是否能做到：源/候选对比研判 -> 质量信号归一 -> Triage -> RepairPatch 绑定 -> Patch 应用 -> 再生成 -> 再研判。",
        "",
        "## 2. 最终结论",
        "",
        f"- 过程契约：`{final['process_contract_verdict']}`",
        f"- 产品质量：`{final['product_quality_verdict']}`",
        f"- 终态：`{final['terminal_state']}`",
        f"- 修复前阻塞数：`{before_count}`",
        f"- 修复候选阻塞数：`{repair_after_count}`",
        f"- 当前接受候选阻塞数：`{accepted_count}`",
        f"- Loop 结果：`{loop_verdict}`",
        f"- 修复候选是否接受：`{repair_accepted}`",
        f"- 目标 failure 前后：`{repair_loop['selected_failure_before_after']}`",
        f"- 非目标硬 failure 回退：`{hard_regressions}`",
        f"- 应用 RepairPatch 操作数：`{application.get('applied_operation_count')}`",
        f"- Triage failure class：`{before_adj.get('selected_failure_class')}`",
        f"- Dispatch repair family：`{before_adj.get('selected_repair_family')}`",
        f"- Deferred failure classes：`{patch.get('deferred_failure_classes')}`",
        "",
        "## 3. 人可读研判结果",
        "",
        f"- 初始研判：{before_summary.get('human_readable_result')}",
        f"- 初始工具选择：{before_summary.get('tool_selection_reason')}",
        f"- Dispatch 结果：{before_adj.get('dispatch_result')}",
        f"- 修复后研判：{after_summary.get('human_readable_result')}",
        f"- 当前接受候选研判：{accepted_summary.get('human_readable_result')}",
        f"- 修复闭环：{repair_loop['human_readable_result']}",
        "",
        "## 3.1 本轮发现的能力缺口",
        "",
        (
            f"- `{before_adj.get('selected_repair_family')}` 本轮被回测拒绝：目标 failure 或总阻塞数没有形成可接受改善，或非目标硬 failure 出现回退。hard_failure_regressions={hard_regressions}。后续应将该 repair atom 改为 obstacle-aware repair，或在下一轮 loop 中尝试更局部的 RepairPatch。"
            if not repair_accepted
            else "- 本轮未发现被回滚的 repair atom 能力缺口。"
        ),
        "",
        "## 4. 分层提示词模板",
        "",
        "| 状态 | 模板 | 输入槽位 | 输出维度 | 本轮后端模型 |",
        "|---|---|---|---|---|",
        "| S8A | `S8A_quality_signal_normalization.prompt.json` | source_structure, generation_evidence, quality_gates | QualitySignal, page_signal_summary | not_invoked，使用本地确定性工具 |",
        "| S8B | `S8B_quality_triage.prompt.json` | quality_signals, page_strategy, layout_plan | failure_class, selected_failure_class, needs_more_evidence | not_invoked，使用本地确定性工具 |",
        "| S8C | `S8C_repair_patch_binding.prompt.json` | visual_adjudication, failure_dispatch_table, quality_signals, layout_plan | RepairPatch operations | not_invoked，使用本地确定性工具 |",
        "| Lx | `Lx_repair_loop_execution.prompt.json` | repair_patch, layout_plan, quality_before | loop_verdict, before/after delta | not_invoked，使用本地确定性工具 |",
        "",
        "## 5. 关键证据文件",
        "",
        "- `reports/quality_signals.json`：修复前源/候选对比信号",
        "- `reports/visual_adjudication.json`：修复前人可读裁决",
        "- `reports/repair_patch_0001.json`：可执行 RepairPatch",
        "- `reports/repair_patch_application_0001.json`：Patch 应用结果",
        "- `reports/quality_signals.repair0001.json`：修复后源/候选对比信号",
        "- `reports/repair_loop_0001.json`：闭环前后差异",
        "- `reports/state_trace.json`：完整状态迁移",
        "- `reports/model_interactions.jsonl`：提示词模板及模型调用记录",
        "",
        "## 6. 反过拟合边界",
        "",
        "本轮运行未读取 `offline_reference_compare`，未使用人工对照 PDF，RepairPatch 只引用当前运行的 group_id、bbox、字号、fit 状态和重叠增量。",
    ]
    (reports / "round25_state_machine_repair_patch_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
