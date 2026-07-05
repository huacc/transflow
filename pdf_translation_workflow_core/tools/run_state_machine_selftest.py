"""Run the workflow state machine against regression inputs.

tool_name: run_state_machine_selftest
category: validators
input_contract: regression manifest and workflow core tools
output_contract: per-regression state trace, operation log, decision log, quality gates, validation report, audit report
failure_signals: subprocess failures, missing artifacts, invalid process validation
fallback: terminal state S_FAIL_PROCESS_CONTRACT, S_FAIL_TOOLING, or S_FAIL_QUALITY
anti_overfit_statement: runner uses manifest paths only and never branches on sample filename, exact text, page number, or coordinates
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from _common import TOOL_ROOT, WORKSPACE_ROOT, ensure_dir, now_local, read_json, rel, resolve_workspace_path, write_json, write_jsonl


def run_cmd(cmd: list[str], cwd: Path, operation: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    operation["command"] = cmd
    operation["returncode"] = proc.returncode
    operation["stdout"] = proc.stdout.strip()
    operation["stderr"] = proc.stderr.strip()
    operation["status"] = "pass" if proc.returncode == 0 else "fail"
    return operation


def decision(
    decision_id: str,
    state: str,
    purpose: str,
    input_artifacts: list[dict[str, str]],
    prompt_contract: str,
    dims: list[str],
    verdict: str,
    summary: str,
    next_state: str,
    loop_id: str | None = None,
    tool_outputs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "state": state,
        "loop_id": loop_id,
        "purpose": purpose,
        "input_artifacts": input_artifacts,
        "prompt_contract": prompt_contract,
        "required_output_dimensions": dims,
        "model_output": {
            "verdict": verdict,
            "summary": summary,
        },
        "tool_outputs": tool_outputs or [],
        "next_state": next_state,
    }


def transition(
    idx: int,
    from_state: str,
    to_state: str,
    run_mode: str,
    entry: str,
    tools: list[str],
    inputs: list[str],
    outputs: list[str],
    decisions: list[str],
    gates: list[dict[str, Any]],
    next_rule: str,
) -> dict[str, Any]:
    return {
        "transition_id": f"T{idx:02d}",
        "from": from_state,
        "to": to_state,
        "entry_condition": entry,
        "run_mode": run_mode,
        "tools": tools,
        "input_artifacts": inputs,
        "output_artifacts": outputs,
        "decision_record_ids": decisions,
        "gates": gates,
        "next_state_rule": next_rule,
        "timestamp_local": now_local(),
    }


def write_audit(
    run_dir: Path,
    regression_id: str,
    run_mode: str,
    validation: dict[str, Any],
    quality: dict[str, Any] | None,
    terminal_state: str | None = None,
) -> None:
    product_verdict = "NOT_ATTEMPTED"
    terminal = "S_DONE_PROCESS_VALIDATED"
    if quality:
        product_verdict = quality.get("product_quality_verdict", "FAIL")
        terminal = "S_DONE_PRODUCT_ACCEPTED" if product_verdict == "PASS" else "S_FAIL_QUALITY"
    if terminal_state:
        terminal = terminal_state
    lines = [
        f"# Workflow Selftest Audit - {regression_id}",
        "",
        f"generated_at_local: {now_local()}",
        f"run_mode: {run_mode}",
        f"process_contract_verdict: {validation.get('process_contract_verdict')}",
        f"product_quality_verdict: {product_verdict}",
        f"terminal_state: {terminal if validation.get('process_contract_verdict') == 'PASS' else 'S_FAIL_PROCESS_CONTRACT'}",
        "",
        "## Evidence",
        "",
        "- state_trace.json",
        "- operation_log.jsonl",
        "- decision_log.jsonl",
        "- source_extraction.json",
        "- tool_probe.json",
        "- process_validation.json",
    ]
    if quality:
        lines.append("- product_quality_gates.json")
        lines.append("- candidate.pdf")
    if validation.get("errors"):
        lines.extend(["", "## Process Errors", ""])
        lines.extend(f"- {err}" for err in validation["errors"])
    if quality and quality.get("blocking_failure_count"):
        lines.extend(["", "## Blocking Product Failures", ""])
        for gate in quality.get("gates", []):
            if gate.get("blocking") and gate.get("status") == "fail":
                lines.append(f"- {gate.get('gate_id')}: {gate.get('evidence')}")
    (run_dir / "audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def aggregate_jsonl(out_base: Path, results: list[dict[str, Any]], filename: str) -> None:
    records: list[dict[str, Any]] = []
    for result in results:
        source = resolve_workspace_path(result["run_dir"]) / filename
        if not source.exists():
            continue
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            item.setdefault("regression_id", result["regression_id"])
            item.setdefault("run_mode", result["run_mode"])
            records.append(item)
    write_jsonl(out_base / filename, records)


def run_one(
    item: dict[str, Any],
    run_mode: str,
    base_out: Path,
    generator: str,
    semantic_translations_dir: Path | None,
) -> dict[str, Any]:
    regression_id = item["id"]
    source_rel = item["path"]
    source_path = resolve_workspace_path(source_rel)
    semantic_translations_path = None
    if semantic_translations_dir is not None:
        semantic_translations_path = semantic_translations_dir / f"{regression_id}.translations.json"
    run_dir = ensure_dir(base_out / run_mode / regression_id)
    renders_dir = ensure_dir(run_dir / "renders")
    outputs_dir = ensure_dir(run_dir / "outputs")
    operation_log: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []

    def op(state: str, tool: str, inputs: list[str], outputs: list[str]) -> dict[str, Any]:
        return {
            "operation_id": f"OP{len(operation_log) + 1:03d}",
            "state": state,
            "tool": tool,
            "input_artifacts": inputs,
            "output_artifacts": outputs,
            "started_at_local": now_local(),
        }

    traces.append(
        transition(
            1,
            "S0_Request",
            "S1_ContractLoad",
            run_mode,
            "selftest runner started from regression manifest",
            ["Python"],
            ["pdf_translation_workflow_core/regression/regression_manifest.json"],
            [rel(run_dir)],
            [],
            [{"gate_id": "run_mode_declared", "status": "pass", "evidence": run_mode}],
            "load contracts",
        )
    )
    contract_files = [
        "pdf_translation_workflow_core/contracts/run_modes.md",
        "pdf_translation_workflow_core/contracts/state_machine.md",
        "pdf_translation_workflow_core/contracts/tool_contracts.md",
        "pdf_translation_workflow_core/contracts/decision_contracts.md",
        "pdf_translation_workflow_core/contracts/product_quality_contract.md",
        "pdf_translation_workflow_core/contracts/page_type_repair_matrix.md",
    ]
    missing_contracts = [path for path in contract_files if not resolve_workspace_path(path).exists()]
    traces.append(
        transition(
            2,
            "S1_ContractLoad",
            "S2_ToolProbe" if not missing_contracts else "S_FAIL_PROCESS_CONTRACT",
            run_mode,
            "contract files checked",
            ["Python"],
            contract_files,
            [],
            [],
            [{"gate_id": "contracts_present", "status": "pass" if not missing_contracts else "fail", "evidence": missing_contracts or "all present"}],
            "probe tools or fail process",
        )
    )

    tool_probe = run_dir / "tool_probe.json"
    cmd = [sys.executable, str(TOOL_ROOT / "tools" / "probes" / "tool_probe.py"), "--out", str(tool_probe)]
    operation_log.append(run_cmd(cmd, WORKSPACE_ROOT, op("S2_ToolProbe", "tool_probe.py", [], [rel(tool_probe)])))

    extraction = run_dir / "source_extraction.json"
    cmd = [
        sys.executable,
        str(TOOL_ROOT / "tools" / "probes" / "extract_pdf_structure.py"),
        "--input",
        source_rel,
        "--out",
        str(extraction),
    ]
    operation_log.append(run_cmd(cmd, WORKSPACE_ROOT, op("S3_SourceExtract", "extract_pdf_structure.py", [source_rel], [rel(extraction)])))

    render_manifest = run_dir / "source_render_manifest.json"
    cmd = [
        sys.executable,
        str(TOOL_ROOT / "tools" / "renderers" / "render_pdf.py"),
        "--input",
        source_rel,
        "--out-dir",
        str(renders_dir),
        "--prefix",
        "source",
        "--manifest",
        str(render_manifest),
    ]
    operation_log.append(run_cmd(cmd, WORKSPACE_ROOT, op("S3_SourceExtract", "render_pdf.py", [source_rel], [rel(render_manifest)])))

    traces.append(
        transition(
            3,
            "S2_ToolProbe",
            "S3_SourceExtract",
            run_mode,
            "tool probe completed",
            ["Python", "PyMuPDF"],
            [],
            [rel(tool_probe)],
            [],
            [{"gate_id": "tool_probe_written", "status": "pass" if tool_probe.exists() else "fail", "evidence": rel(tool_probe)}],
            "extract source",
        )
    )
    traces.append(
        transition(
            4,
            "S3_SourceExtract",
            "S4_PageStrategy",
            run_mode,
            "source extraction and rendering completed",
            ["Python", "PyMuPDF"],
            [source_rel],
            [rel(extraction), rel(render_manifest)],
            ["D1_role_classification", "D3_visual_only_text"],
            [{"gate_id": "source_extraction_written", "status": "pass" if extraction.exists() else "fail", "evidence": rel(extraction)}],
            "classify pages",
        )
    )

    extraction_data = read_json(extraction)
    page_types = [page.get("page_type_guess", "unknown") for page in extraction_data.get("pages", [])]
    decisions.append(
        decision(
            "D1_role_classification",
            "S4_PageStrategy",
            "Classify page roles from extracted structural evidence.",
            [{"path": rel(extraction), "kind": "source_extraction"}],
            "Use structural extraction and renders only; do not infer from sample filename or known page identity.",
            ["page_index", "page_type", "role", "confidence", "evidence_signal", "risk_flags"],
            "pass",
            f"classified page_type_guess values: {page_types}",
            "S5_TranslationPlan",
            tool_outputs=[{"path": rel(extraction), "kind": "page_type_guess"}],
        )
    )
    decisions.append(
        decision(
            "D3_visual_only_text",
            "S4_PageStrategy",
            "Identify visual-only text handling boundary.",
            [{"path": rel(render_manifest), "kind": "source_renders"}],
            "Record OCR/no-OCR boundary honestly; do not pretend image text is extractable.",
            ["visual_item_id", "visible_text", "region_hint", "treatment", "risk"],
            "warn",
            "selftest does not OCR; visual-only text would require separate OCR-enabled repair atom",
            "S5_TranslationPlan",
        )
    )
    traces.append(
        transition(
            5,
            "S4_PageStrategy",
            "S5_TranslationPlan",
            run_mode,
            "page strategy decisions recorded",
            ["Codex/OpenAI model"],
            [rel(extraction), rel(render_manifest)],
            [],
            ["D1_role_classification", "D3_visual_only_text"],
            [{"gate_id": "page_strategy_decisions_recorded", "status": "pass", "evidence": "D1,D3"}],
            "build translation plan",
        )
    )

    translation_inputs = [{"path": rel(extraction), "kind": "source_extraction"}]
    if semantic_translations_path is not None:
        translation_inputs.append({"path": rel(semantic_translations_path), "kind": "semantic_translations"})
    product_translation_ready = bool(semantic_translations_path and semantic_translations_path.exists())
    decisions.append(
        decision(
            "D2_translation",
            "S5_TranslationPlan",
            "Define translation policy for current run.",
            translation_inputs,
            "Product runs must consume complete semantic translations; placeholder text is allowed only in backfill_candidate_validation.",
            ["unit_id", "source_text", "translation_zh", "term_decisions", "semantic_coverage", "layout_risk", "provider", "prompt_artifacts"],
            "pass" if run_mode != "product_quality" or product_translation_ready else "fail",
            (
                f"semantic translations found: {rel(semantic_translations_path)}"
                if product_translation_ready
                else "product_quality requires semantic translations before candidate generation"
                if run_mode == "product_quality"
                else "candidate-validation may use deterministic placeholder Chinese and must fail semantic product quality"
            ),
            "S6_LayoutPlan" if run_mode != "product_quality" or product_translation_ready else "S_FAIL_CAPABILITY",
        )
    )
    decisions.append(
        decision(
            "D4_layout_plan",
            "S6_LayoutPlan",
            "Define layout plan boundary.",
            [{"path": rel(extraction), "kind": "source_extraction"}],
            "Layout must be region-driven; selftest does not hardcode sample coordinates.",
            ["layout_policy", "classification_rules", "font_profiles", "reflow", "fallback", "evidence_refs"],
            "warn",
            "layout policy is generated from current extraction statistics; D4 may revise policy but generator must not hide hardcoded visual constants",
            "S7_GenerateCandidate" if run_mode in {"backfill_candidate_validation", "product_quality"} else "S9_VerifyProcessContract",
        )
    )
    traces.append(
        transition(
            6,
            "S5_TranslationPlan",
            "S6_LayoutPlan",
            run_mode,
            "translation and layout boundaries recorded",
            ["Codex/OpenAI model"],
            [rel(extraction)],
            [],
            ["D2_translation", "D4_layout_plan"],
            [{"gate_id": "translation_layout_decisions_recorded", "status": "pass", "evidence": "D2,D4"}],
            "generate candidate in candidate-validation modes or validate process",
        )
    )

    quality: dict[str, Any] | None = None
    candidate_pdf = outputs_dir / "candidate.pdf"
    published_artifacts: list[str] = []
    if run_mode in {"backfill_candidate_validation", "product_quality"}:
        generation_evidence = run_dir / "candidate_generation_evidence.json"
        translations_path = run_dir / "translations.json"
        layout_plan_path = run_dir / "layout_plan.json"
        layout_policy_path = run_dir / "layout_policy.json"
        semantic_validation_path = run_dir / "semantic_translation_validation.json"
        generator_inputs = [source_rel, rel(extraction)]
        generator_outputs: list[str] = []
        generator_tool = ""
        generation_failure_terminal: str | None = None
        generation_failure_summary: str | None = None
        cmd: list[str] | None = None
        if generator == "smoke_copy":
            cmd = [
                sys.executable,
                str(TOOL_ROOT / "tools" / "generators" / "generate_minimal_candidate.py"),
                "--input",
                source_rel,
                "--output",
                str(candidate_pdf),
                "--evidence",
                str(generation_evidence),
            ]
            generator_tool = "generate_minimal_candidate.py"
            generator_outputs = [rel(candidate_pdf), rel(generation_evidence)]
        elif generator == "semantic_backfill":
            generator_tool = "generate_semantic_backfill.py"
            if semantic_translations_path is None or not semantic_translations_path.exists():
                generation_failure_terminal = "S_FAIL_CAPABILITY"
                generation_failure_summary = "missing semantic translations JSON; product_quality cannot use placeholder fallback"
                write_json(
                    semantic_validation_path,
                    {
                        "tool": "validate_semantic_translations",
                        "translation_validation_verdict": "FAIL",
                        "reason": "missing_semantic_translations",
                        "expected_path": None if semantic_translations_path is None else rel(semantic_translations_path),
                    },
                )
                failed_op = op(
                    "S5_TranslationPlan",
                    "validate_semantic_translations.py",
                    [rel(extraction)],
                    [rel(semantic_validation_path)],
                )
                failed_op.update(
                    {
                        "command": [],
                        "returncode": 2,
                        "stdout": "",
                        "stderr": generation_failure_summary,
                        "status": "fail",
                    }
                )
                operation_log.append(failed_op)
                generator_outputs = [rel(semantic_validation_path)]
            else:
                validation_cmd = [
                    sys.executable,
                    str(TOOL_ROOT / "tools" / "validators" / "validate_semantic_translations.py"),
                    "--source-extraction",
                    rel(extraction),
                    "--translations",
                    rel(semantic_translations_path),
                    "--out",
                    str(semantic_validation_path),
                ]
                validation_op = run_cmd(
                    validation_cmd,
                    WORKSPACE_ROOT,
                    op(
                        "S5_TranslationPlan",
                        "validate_semantic_translations.py",
                        [rel(extraction), rel(semantic_translations_path)],
                        [rel(semantic_validation_path)],
                    ),
                )
                operation_log.append(validation_op)
                generator_outputs = [rel(semantic_validation_path)]
                if validation_op["returncode"] != 0:
                    generation_failure_terminal = "S_FAIL_CAPABILITY"
                    generation_failure_summary = "semantic translations failed validation; product candidate not generated"
                else:
                    policy_cmd = [
                        sys.executable,
                        str(TOOL_ROOT / "tools" / "planners" / "build_layout_policy.py"),
                        "--source-extraction",
                        rel(extraction),
                        "--semantic-translations",
                        rel(semantic_translations_path),
                        "--out",
                        str(layout_policy_path),
                    ]
                    policy_op = run_cmd(
                        policy_cmd,
                        WORKSPACE_ROOT,
                        op(
                            "S6_LayoutPlan",
                            "build_layout_policy.py",
                            [rel(extraction), rel(semantic_translations_path)],
                            [rel(layout_policy_path)],
                        ),
                    )
                    operation_log.append(policy_op)
                    if policy_op["returncode"] != 0:
                        generation_failure_terminal = "S_FAIL_PROCESS_CONTRACT"
                        generation_failure_summary = policy_op.get("stderr") or "layout policy generation failed"
                    else:
                        generator_outputs.append(rel(layout_policy_path))
                if generation_failure_terminal is None and validation_op["returncode"] == 0:
                    cmd = [
                        sys.executable,
                        str(TOOL_ROOT / "tools" / "generators" / "generate_semantic_backfill.py"),
                        "--input",
                        source_rel,
                        "--source-extraction",
                        rel(extraction),
                        "--semantic-translations",
                        rel(semantic_translations_path),
                        "--layout-policy",
                        rel(layout_policy_path),
                        "--output",
                        str(candidate_pdf),
                        "--evidence",
                        str(generation_evidence),
                        "--translations",
                        str(translations_path),
                        "--layout-plan",
                        str(layout_plan_path),
                    ]
                    generator_inputs = [source_rel, rel(extraction), rel(semantic_translations_path), rel(layout_policy_path)]
                    generator_outputs = [
                        rel(candidate_pdf),
                        rel(generation_evidence),
                        rel(translations_path),
                        rel(layout_plan_path),
                        rel(semantic_validation_path),
                        rel(layout_policy_path),
                    ]
        else:
            cmd = [
                sys.executable,
                str(TOOL_ROOT / "tools" / "generators" / "generate_backfill_candidate.py"),
                "--input",
                source_rel,
                "--source-extraction",
                rel(extraction),
                "--output",
                str(candidate_pdf),
                "--evidence",
                str(generation_evidence),
                "--translations",
                str(translations_path),
                "--layout-plan",
                str(layout_plan_path),
            ]
            generator_tool = "generate_backfill_candidate.py"
            generator_outputs = [rel(candidate_pdf), rel(generation_evidence), rel(translations_path), rel(layout_plan_path)]
        if cmd is not None:
            generation_op = run_cmd(cmd, WORKSPACE_ROOT, op("S7_GenerateCandidate", generator_tool, generator_inputs, generator_outputs))
            operation_log.append(generation_op)
            if generation_op["returncode"] != 0 or not candidate_pdf.exists():
                generation_failure_terminal = "S_FAIL_CAPABILITY" if generator == "semantic_backfill" else "S_FAIL_TOOLING"
                generation_failure_summary = generation_op.get("stderr") or "candidate PDF was not generated"

        if generation_failure_terminal:
            failure_path = run_dir / "generation_failure.json"
            write_json(
                failure_path,
                {
                    "tool": generator_tool,
                    "terminal_state": generation_failure_terminal,
                    "reason": generation_failure_summary,
                    "generator": generator,
                    "semantic_translations": None if semantic_translations_path is None else rel(semantic_translations_path),
                },
            )
            decisions.append(
                decision(
                    "D5_initial_verification",
                    "S8_VerifyProductQuality",
                    "Product quality cannot be verified because candidate generation failed.",
                    [{"path": rel(failure_path), "kind": "generation_failure"}],
                    "Product-quality runs must fail capability instead of falling back to placeholders when semantic translations are missing or invalid.",
                    ["gate", "status", "finding_type", "evidence", "repair_hint", "next_state"],
                    "fail",
                    generation_failure_summary or "candidate generation failed",
                    generation_failure_terminal,
                )
            )
            decisions.append(
                decision(
                    "D7_similarity_gate",
                    "S8_VerifyProductQuality",
                    "Similarity gate cannot be attempted without a candidate PDF.",
                    [{"path": rel(failure_path), "kind": "generation_failure"}],
                    "Do not compare visuals or claim quality when no real semantic candidate exists.",
                    ["metric", "scope", "status", "blocking"],
                    "fail",
                    "candidate PDF absent",
                    generation_failure_terminal,
                )
            )
            decisions.append(
                decision(
                    "D8_minimal_repair_selection",
                    "Lx_RepairLoop",
                    "Declare capability failure instead of product repair.",
                    [{"path": rel(failure_path), "kind": "generation_failure"}],
                    "Missing or invalid semantic translations are a capability/input failure, not a layout repair.",
                    ["repair_id", "failed_gate", "skip_reason"],
                    "fail",
                    generation_failure_summary or "semantic backfill capability unavailable",
                    generation_failure_terminal,
                    loop_id="L1",
                )
            )
            traces.append(
                transition(
                    7,
                    "S6_LayoutPlan",
                    "S7_GenerateCandidate",
                    run_mode,
                    f"{run_mode} mode requires semantic candidate generation",
                    ["Python", "PyMuPDF"],
                    generator_inputs,
                    generator_outputs + [rel(failure_path)],
                    [],
                    [
                        {"gate_id": "candidate_written", "status": "fail", "evidence": rel(candidate_pdf)},
                        {"gate_id": "semantic_backfill_capability", "status": "fail", "evidence": rel(failure_path)},
                    ],
                    generation_failure_terminal,
                )
            )
            traces.append(
                transition(
                    8,
                    "S7_GenerateCandidate",
                    generation_failure_terminal,
                    run_mode,
                    "candidate generation failed before quality evaluation",
                    ["Python"],
                    generator_outputs + [rel(failure_path)],
                    [],
                    ["D5_initial_verification", "D7_similarity_gate", "D8_minimal_repair_selection"],
                    [{"gate_id": "generation_verdict", "status": "fail", "evidence": generation_failure_summary}],
                    "terminal capability/tooling failure",
                )
            )
            final_before_process = generation_failure_terminal
        else:
            candidate_render_manifest = run_dir / "candidate_render_manifest.json"
            cmd = [
                sys.executable,
                str(TOOL_ROOT / "tools" / "renderers" / "render_pdf.py"),
                "--input",
                rel(candidate_pdf),
                "--out-dir",
                str(outputs_dir / "previews"),
                "--prefix",
                "candidate",
                "--manifest",
                str(candidate_render_manifest),
            ]
            operation_log.append(run_cmd(cmd, WORKSPACE_ROOT, op("S7_GenerateCandidate", "render_pdf.py", [rel(candidate_pdf)], [rel(candidate_render_manifest)])))
            publish_op = op("S7_GenerateCandidate", "publish_candidate_outputs", [rel(candidate_pdf), rel(candidate_render_manifest)], [])
            try:
                publish_dir = ensure_dir(WORKSPACE_ROOT / "docs" / "output")
                preview_publish_dir = ensure_dir(publish_dir / "previews")
                published_pdf = publish_dir / f"{regression_id}_{generator}_candidate.pdf"
                shutil.copy2(candidate_pdf, published_pdf)
                published_artifacts.append(rel(published_pdf))
                if candidate_render_manifest.exists():
                    manifest_data = read_json(candidate_render_manifest)
                    for image in manifest_data.get("images", []):
                        src_image = resolve_workspace_path(image["path"])
                        dst_image = preview_publish_dir / f"{regression_id}_{generator}_page_{int(image['page_index']) + 1:02d}.png"
                        shutil.copy2(src_image, dst_image)
                        published_artifacts.append(rel(dst_image))
                publish_op["output_artifacts"] = published_artifacts
                publish_op["status"] = "pass"
            except Exception as exc:  # noqa: BLE001 - recorded in operation log for process audit
                publish_op["status"] = "fail"
                publish_op["failure_signal"] = str(exc)
            operation_log.append(publish_op)
            quality_path = run_dir / "product_quality_gates.json"
            cmd = [
                sys.executable,
                str(TOOL_ROOT / "tools" / "validators" / "evaluate_pdf_quality.py"),
                "--source",
                source_rel,
                "--output",
                rel(candidate_pdf),
                "--out",
                str(quality_path),
                "--generation-evidence",
                rel(generation_evidence),
            ]
            operation_log.append(run_cmd(cmd, WORKSPACE_ROOT, op("S8_VerifyProductQuality", "evaluate_pdf_quality.py", [source_rel, rel(candidate_pdf), rel(generation_evidence)], [rel(quality_path)])))
            quality = read_json(quality_path)
            product_verdict = quality.get("product_quality_verdict", "FAIL")
            decisions.append(
                decision(
                    "D5_initial_verification",
                    "S8_VerifyProductQuality",
                    "Check residue, geometry, and initial quality gates.",
                    [{"path": rel(quality_path), "kind": "product_quality_gates"}],
                    "Zero ASCII and authentic semantic coverage are mandatory in product_quality; failures block success.",
                    ["gate", "status", "finding_type", "evidence", "repair_hint", "next_state"],
                    "pass" if product_verdict == "PASS" else "fail",
                    f"product_quality_verdict={product_verdict}",
                    "S_DONE_PRODUCT_ACCEPTED" if product_verdict == "PASS" else "Lx_RepairLoop",
                )
            )
            decisions.append(
                decision(
                    "D7_similarity_gate",
                    "S8_VerifyProductQuality",
                    "Record visual and text metrics for product gates.",
                    [{"path": rel(quality_path), "kind": "product_quality_gates"}],
                    "Use structural source-vs-output metrics; do not call quality passed if blocking gates fail.",
                    ["metric", "scope", "source_value", "output_value", "ratio_or_delta", "threshold", "status", "blocking"],
                    "pass" if product_verdict == "PASS" else "fail",
                    "candidate generation occurred; product must still fail on any blocking gate such as text residue, semantic coverage, text fit, or visual quality",
                    "S_DONE_PRODUCT_ACCEPTED" if product_verdict == "PASS" else "Lx_RepairLoop",
                )
            )
            traces.append(
                transition(
                    7,
                    "S6_LayoutPlan",
                    "S7_GenerateCandidate",
                    run_mode,
                    f"{run_mode} mode requires a candidate",
                    ["Python", "PyMuPDF"],
                    generator_inputs,
                    generator_outputs + published_artifacts,
                    [],
                    [
                        {"gate_id": "candidate_written", "status": "pass" if candidate_pdf.exists() else "fail", "evidence": rel(candidate_pdf)},
                        {"gate_id": "candidate_published", "status": "pass" if published_artifacts else "fail", "evidence": published_artifacts},
                    ],
                    "verify product quality",
                )
            )
            traces.append(
                transition(
                    8,
                    "S7_GenerateCandidate",
                    "S8_VerifyProductQuality",
                    run_mode,
                    "candidate generated",
                    ["Python", "PyMuPDF"],
                    [rel(candidate_pdf)],
                    [rel(quality_path)],
                    ["D5_initial_verification", "D7_similarity_gate"],
                    [{"gate_id": "product_quality_verdict", "status": "pass" if product_verdict == "PASS" else "fail", "evidence": product_verdict}],
                    "accept product or enter repair/fail quality",
                )
            )
            if product_verdict == "PASS":
                final_before_process = "S_DONE_PRODUCT_ACCEPTED"
            else:
                decisions.append(
                    decision(
                        "D8_minimal_repair_selection",
                        "Lx_RepairLoop",
                        "Select minimal repair or declare quality failure for selftest.",
                        [{"path": rel(quality_path), "kind": "product_quality_gates"}],
                        "If candidate quality is blocked, select the smallest repair or terminate honestly.",
                        ["repair_id", "failed_gate", "patch_scope", "expected_effect", "verification_to_run"],
                        "fail",
                        "blocking product-quality gates remain after candidate generation",
                        "S_FAIL_QUALITY",
                        loop_id="L1",
                    )
                )
                traces.append(
                    transition(
                        9,
                        "S8_VerifyProductQuality",
                        "Lx_RepairLoop",
                        run_mode,
                        "blocking product gate failed",
                        ["Codex/OpenAI model"],
                        [rel(quality_path)],
                        [],
                        ["D8_minimal_repair_selection"],
                        [{"gate_id": "repair_or_fail_quality", "status": "fail", "evidence": "blocking product-quality gates remain"}],
                        "terminal S_FAIL_QUALITY",
                    )
                )
                final_before_process = "S_FAIL_QUALITY"
    else:
        decisions.append(
            decision(
                "D5_initial_verification",
                "S8_VerifyProductQuality",
                "Product quality not attempted in process_validation mode.",
                [{"path": rel(extraction), "kind": "source_extraction"}],
                "Record skipped product gate explicitly.",
                ["gate", "status", "finding_type", "evidence", "repair_hint", "next_state"],
                "skipped",
                "run_mode=process_validation",
                "S9_VerifyProcessContract",
            )
        )
        decisions.append(
            decision(
                "D7_similarity_gate",
                "S8_VerifyProductQuality",
                "Similarity gate not attempted in process_validation mode.",
                [{"path": rel(render_manifest), "kind": "source_render"}],
                "Record skipped similarity gate explicitly.",
                ["metric", "scope", "status", "blocking"],
                "skipped",
                "run_mode=process_validation",
                "S9_VerifyProcessContract",
            )
        )
        decisions.append(
            decision(
                "D8_minimal_repair_selection",
                "Lx_RepairLoop",
                "Repair loop not required in process_validation mode.",
                [{"path": rel(extraction), "kind": "source_extraction"}],
                "Record skipped repair explicitly.",
                ["repair_id", "failed_gate", "skip_reason"],
                "skipped",
                "no product candidate generated",
                "S9_VerifyProcessContract",
                loop_id="L1",
            )
        )
        final_before_process = "S_DONE_PROCESS_VALIDATED"

    decisions.append(
        decision(
            "D6_user_feedback_adjudication",
            "S9_VerifyProcessContract",
            "Adjudicate known round02/round03 failure mode.",
            [{"path": "docs/业务流程/01_source_pdf_中文回填_详细流程记录.md", "kind": "process_doc"}],
            "Do not confuse process pass with product pass; record split verdicts.",
            ["feedback_id", "failed_dimensions", "repair_dimensions", "metric_to_add"],
            "pass",
            "runner writes split process/product verdicts and blocks product success on quality failure",
            "S9_VerifyProcessContract",
        )
    )
    decisions.append(
        decision(
            "D9_final_acceptance",
            "S9_VerifyProcessContract",
            "Split final process and product verdicts.",
            [{"path": rel(run_dir / "state_trace.json"), "kind": "state_trace"}],
            "Never infer product success from process success.",
            ["process_contract_verdict", "product_quality_verdict", "terminal_state", "known_risks"],
            "pass",
            f"terminal before process validation: {final_before_process}",
            final_before_process,
        )
    )

    traces.append(
        transition(
            10,
            final_before_process,
            "S9_VerifyProcessContract",
            run_mode,
            "product branch completed or skipped; process artifacts ready",
            ["Python"],
            [rel(run_dir)],
            [rel(run_dir / "state_trace.json"), rel(run_dir / "decision_log.jsonl"), rel(run_dir / "operation_log.jsonl")],
            ["D6_user_feedback_adjudication", "D9_final_acceptance"],
            [{"gate_id": "decision_records_complete", "status": "pass", "evidence": "D1-D9 written"}],
            "run process validator",
        )
    )
    final_terminal = final_before_process
    traces.append(
        transition(
            11,
            "S9_VerifyProcessContract",
            final_terminal,
            run_mode,
            "state trace, decisions and operations ready for validation",
            ["Python"],
            [rel(run_dir)],
            [],
            ["D9_final_acceptance"],
            [{"gate_id": "terminal_state_declared", "status": "pass", "evidence": final_terminal}],
            "write audit",
        )
    )

    write_jsonl(run_dir / "operation_log.jsonl", operation_log)
    write_jsonl(run_dir / "decision_log.jsonl", decisions)
    write_json(run_dir / "state_trace.json", traces)

    validation_path = run_dir / "process_validation.json"
    cmd = [
        sys.executable,
        str(TOOL_ROOT / "tools" / "validators" / "validate_process_artifacts.py"),
        "--run-dir",
        str(run_dir),
        "--out",
        str(validation_path),
    ]
    validation_op = run_cmd(cmd, WORKSPACE_ROOT, op("S9_VerifyProcessContract", "validate_process_artifacts.py", [rel(run_dir)], [rel(validation_path)]))
    operation_log.append(validation_op)
    write_jsonl(run_dir / "operation_log.jsonl", operation_log)
    # Re-run validation after recording the validator invocation itself.
    subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), text=True, capture_output=True)
    validation = read_json(validation_path)
    write_audit(run_dir, regression_id, run_mode, validation, quality, final_terminal)
    return {
        "regression_id": regression_id,
        "run_mode": run_mode,
        "run_dir": rel(run_dir),
        "process_contract_verdict": validation.get("process_contract_verdict"),
        "product_quality_verdict": quality.get("product_quality_verdict") if quality else "NOT_ATTEMPTED",
        "terminal_state": final_terminal if validation.get("process_contract_verdict") == "PASS" else "S_FAIL_PROCESS_CONTRACT",
        "process_errors": validation.get("errors", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="pdf_translation_workflow_core/regression/regression_manifest.json")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--modes", nargs="+", default=["process_validation", "backfill_candidate_validation"])
    parser.add_argument("--generator", choices=["backfill_placeholder", "smoke_copy", "semantic_backfill"], default="backfill_placeholder")
    parser.add_argument("--semantic-translations-dir", default="docs/input/semantic_translations")
    args = parser.parse_args()

    if "product_quality" in args.modes:
        if args.generator != "semantic_backfill":
            raise SystemExit(
                "Unsupported configuration: product_quality requires --generator semantic_backfill. "
                "Use --modes backfill_candidate_validation for placeholder generation mechanics."
            )

    manifest_path = resolve_workspace_path(args.manifest)
    manifest = read_json(manifest_path)
    semantic_translations_dir = resolve_workspace_path(args.semantic_translations_dir) if args.semantic_translations_dir else None
    out_base = (
        Path(args.out_dir)
        if args.out_dir
        else WORKSPACE_ROOT
        / "docs"
        / "reports"
        / "pdf_translation_workflow_core"
        / ("selftest_" + now_local().replace(":", "").replace(" ", "_").replace("-", ""))
    )
    ensure_dir(out_base)
    results = []
    supported_modes = {"process_validation", "backfill_candidate_validation", "product_quality"}
    for mode in args.modes:
        if mode not in supported_modes:
            raise SystemExit(f"Unsupported mode: {mode}")
        for item in manifest["inputs"]:
            results.append(run_one(item, mode, out_base, args.generator, semantic_translations_dir))
    aggregate_jsonl(out_base, results, "operation_log.jsonl")
    aggregate_jsonl(out_base, results, "decision_log.jsonl")
    summary = {
        "tool": "run_state_machine_selftest",
        "generated_at_local": now_local(),
        "manifest": rel(manifest_path),
        "out_dir": rel(out_base),
        "generator": args.generator,
        "semantic_translations_dir": None if semantic_translations_dir is None else rel(semantic_translations_dir),
        "results": results,
        "overall_process_contract_verdict": "PASS" if all(r["process_contract_verdict"] == "PASS" for r in results) else "FAIL",
    }
    write_json(out_base / "selftest_summary.json", summary)
    print(out_base)
    return 0 if summary["overall_process_contract_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
