"""Run the semantic product-quality PDF workflow for a directory of PDFs.

tool_name: run_semantic_product_quality_round
category: orchestrators
input_contract: source PDF directory plus validated semantic translation JSON directory
output_contract: candidate PDFs, per-case quality artifacts, state/decision/operation logs, process validation
failure_signals: missing translations, command failures, product-quality failures, process-contract failures
fallback: record honest terminal state; never promote a candidate PDF to accepted output without quality gates
anti_overfit_statement: orchestration is driven by current input files, translation metadata, and quality gates; it never branches on sample-specific names, page numbers, or text.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ensure_dir, read_json, rel, resolve_workspace_path, write_json  # noqa: E402


ROOT = Path.cwd()
PYTHON = sys.executable


def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def boundary_pass(paths: list[Path]) -> dict[str, Any]:
    root = ROOT.resolve()
    resolved = []
    for path in paths:
        target = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        resolved.append(str(target))
        if target != root and root not in target.parents:
            return {
                "workspace_boundary_verdict": "FAIL",
                "workspace_root": str(root),
                "resolved_paths": resolved,
            }
    return {
        "workspace_boundary_verdict": "PASS",
        "workspace_root": str(root),
        "resolved_paths": resolved,
    }


def copy_if_needed(source: Path, target: Path) -> Path:
    source_resolved = source.resolve()
    target_parent = target.parent.resolve()
    target_resolved = target.resolve() if target.exists() else target_parent / target.name
    if source_resolved == target_resolved:
        return target
    shutil.copy2(source, target)
    return target


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.round_id = args.round_id
        self.source_dir = resolve_workspace_path(args.source_dir)
        self.semantic_dir = resolve_workspace_path(args.semantic_dir)
        self.input_dir = Path(args.input_dir)
        self.output_dir = Path(args.output_dir)
        self.report_dir = Path(args.report_dir)
        self.semantic_pool_dir = self.input_dir / "semantic_translation_pool"
        self.max_repair_loops = int(args.max_repair_loops)
        ensure_dir(self.input_dir / "source_pdfs")
        ensure_dir(self.input_dir / "semantic_translations")
        ensure_dir(self.semantic_pool_dir)
        ensure_dir(self.output_dir)
        ensure_dir(self.report_dir)
        self.operation_log = self.report_dir / "operation_log.jsonl"
        self.decision_log = self.report_dir / "decision_log.jsonl"
        self.state_trace_path = self.report_dir / "state_trace.json"
        for path in [self.operation_log, self.decision_log]:
            if path.exists():
                path.unlink()
        self.operations: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.state_trace: list[dict[str, Any]] = []
        self.cases: list[dict[str, Any]] = []
        self.transition_counter = 0
        self.operation_counter = 0

    def op_id(self) -> str:
        self.operation_counter += 1
        return f"op_{self.operation_counter:04d}"

    def transition(
        self,
        from_state: str,
        to_state: str,
        entry_condition: str,
        tools: list[str],
        input_artifacts: list[Path | str],
        output_artifacts: list[Path | str],
        decision_record_ids: list[str],
        gates: list[dict[str, Any]],
        next_state_rule: str,
    ) -> None:
        self.transition_counter += 1
        outputs = [Path(str(path)) for path in output_artifacts]
        item = {
            "transition_id": f"t{self.transition_counter:02d}",
            "from": from_state,
            "to": to_state,
            "entry_condition": entry_condition,
            "run_mode": "product_quality",
            "tools": tools,
            "input_artifacts": [rel(Path(str(path))) for path in input_artifacts],
            "output_artifacts": [rel(path) for path in outputs],
            "workspace_boundary_check": boundary_pass(outputs) if outputs else {"workspace_boundary_verdict": "PASS"},
            "decision_record_ids": decision_record_ids,
            "gates": gates,
            "next_state_rule": next_state_rule,
            "timestamp_local": now_local(),
        }
        self.state_trace.append(item)
        write_json(self.state_trace_path, self.state_trace)

    def decision(
        self,
        decision_id: str,
        state: str,
        purpose: str,
        input_artifacts: list[Path | str],
        prompt_contract: str,
        required_output_dimensions: list[str],
        model_output: dict[str, Any],
        next_state: str,
    ) -> None:
        item = {
            "decision_id": decision_id,
            "state": state,
            "purpose": purpose,
            "input_artifacts": [rel(Path(str(path))) for path in input_artifacts],
            "prompt_contract": prompt_contract,
            "required_output_dimensions": required_output_dimensions,
            "model_output": model_output,
            "next_state": next_state,
        }
        self.decisions.append(item)
        append_jsonl(self.decision_log, item)

    def run_cmd(
        self,
        state: str,
        step: str,
        tool: str,
        cmd: list[str],
        input_artifacts: list[Path | str],
        output_artifacts: list[Path | str],
        allow_fail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        start = time.time()
        outputs = [Path(str(path)) for path in output_artifacts]
        boundary = boundary_pass(outputs) if outputs else {"workspace_boundary_verdict": "PASS"}
        result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
        status = "PASS" if result.returncode == 0 else "FAIL"
        item = {
            "operation_id": self.op_id(),
            "state": state,
            "tool": tool,
            "status": status,
            "step": step,
            "cmd": cmd,
            "returncode": result.returncode,
            "duration_sec": round(time.time() - start, 3),
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-4000:],
            "input_artifacts": [rel(Path(str(path))) for path in input_artifacts],
            "output_artifacts": [rel(path) for path in outputs],
            "workspace_boundary_check": boundary,
        }
        self.operations.append(item)
        append_jsonl(self.operation_log, item)
        if result.returncode != 0 and not allow_fail:
            raise RuntimeError(f"{step} failed with code {result.returncode}: {(result.stderr or '')[-1200:]}")
        return result

    def case_id_for(self, source_pdf: Path) -> str:
        lowered = self.round_id.lower()
        if lowered.startswith("round") and lowered[5:].isdigit():
            prefix = f"R{lowered[5:]}"
        else:
            prefix = self.round_id.upper()
        return f"{prefix}_{source_pdf.stem}"

    def prepare_inputs(self) -> None:
        source_pdfs = sorted(self.source_dir.glob("*.pdf"))
        if not source_pdfs:
            raise FileNotFoundError(f"no source PDFs under {self.source_dir}")
        semantic_files = sorted(self.semantic_dir.glob("*.json"))
        if not semantic_files:
            raise FileNotFoundError(f"no semantic translation JSON files under {self.semantic_dir}")
        pool_files = []
        for semantic_file in semantic_files:
            target = self.semantic_pool_dir / semantic_file.name
            copy_if_needed(semantic_file, target)
            pool_files.append(target)
        for source_pdf in source_pdfs:
            case_id = self.case_id_for(source_pdf)
            source_copy = self.input_dir / "source_pdfs" / source_pdf.name
            copy_if_needed(source_pdf, source_copy)
            self.cases.append(
                {
                    "case_id": case_id,
                    "source_pdf": source_copy,
                    "semantic_translations": None,
                    "source_language": None,
                    "target_language": None,
                    "target_text_field": None,
                    "unit_count": None,
                    "translation_provider": None,
                }
            )
        manifest = {
            "round_id": self.round_id,
            "source_dir": rel(self.source_dir),
            "semantic_dir": rel(self.semantic_dir),
            "input_dir": rel(self.input_dir),
            "semantic_translation_pool": rel(self.semantic_pool_dir),
            "case_count": len(self.cases),
            "semantic_pool_count": len(pool_files),
            "semantic_pool_files": [rel(path) for path in pool_files],
            "cases": [
                {
                    **{k: v for k, v in case.items() if k not in {"source_pdf", "semantic_translations"}},
                    "source_pdf": rel(case["source_pdf"]),
                    "semantic_translations": None,
                }
                for case in self.cases
            ],
            "note": "Semantic translation JSONs are copied into a current-round pool. Each source PDF is matched by running validate_semantic_translations.py against current source_extraction.json; filenames are not used as the matching verdict.",
        }
        write_json(self.report_dir / f"{self.round_id}_input_manifest.json", manifest)

    def select_translation_for_case(self, case: dict[str, Any], extraction: Path) -> Path:
        case_id = str(case["case_id"])
        case_dir = self.report_dir / case_id
        match_dir = case_dir / "translation_match_candidates"
        ensure_dir(match_dir)
        candidates = sorted(self.semantic_pool_dir.glob("*.json"))
        attempts: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates, start=1):
            out = match_dir / f"candidate_{index:03d}.validation.json"
            result = self.run_cmd(
                "S5_TranslationPlan",
                f"S5_MatchSemanticTranslation:{case_id}:{candidate.name}",
                "validate_semantic_translations.py",
                [
                    PYTHON,
                    "pdf_translation_workflow_core/tools/validators/validate_semantic_translations.py",
                    "--source-extraction",
                    str(extraction),
                    "--translations",
                    str(candidate),
                    "--out",
                    str(out),
                ],
                [extraction, candidate],
                [out],
                allow_fail=True,
            )
            data = read_json(out) if out.exists() else {}
            verdict = data.get("translation_validation_verdict")
            attempts.append(
                {
                    "candidate": rel(candidate),
                    "validation": rel(out),
                    "returncode": result.returncode,
                    "translation_validation_verdict": verdict,
                    "missing_count": len((data.get("coverage") or {}).get("missing_unit_ids", [])),
                    "invalid_count": len(data.get("invalid_units", []) or []),
                }
            )
            if result.returncode == 0 and verdict == "PASS":
                selected = self.input_dir / "semantic_translations" / f"{case_id}.translations.json"
                shutil.copy2(candidate, selected)
                selected_data = read_json(selected)
                match_record = {
                    "case_id": case_id,
                    "matching_strategy": "validate_each_semantic_translation_against_current_source_extraction",
                    "filename_used_as_verdict": False,
                    "selected_semantic_translation": rel(selected),
                    "selected_pool_file": rel(candidate),
                    "attempts": attempts,
                }
                write_json(case_dir / "semantic_translation_match.json", match_record)
                case.update(
                    {
                        "semantic_translations": selected,
                        "source_language": selected_data.get("source_language"),
                        "target_language": selected_data.get("target_language"),
                        "target_text_field": selected_data.get("target_text_field"),
                        "unit_count": len(selected_data.get("units", [])),
                        "translation_provider": selected_data.get("translation_provider"),
                    }
                )
                return selected
        write_json(
            case_dir / "semantic_translation_match.json",
            {
                "case_id": case_id,
                "matching_strategy": "validate_each_semantic_translation_against_current_source_extraction",
                "filename_used_as_verdict": False,
                "selected_semantic_translation": None,
                "attempts": attempts,
            },
        )
        raise FileNotFoundError(f"no semantic translation JSON validates against current extraction for {case_id}")

    def language_profile(self, case: dict[str, Any]) -> Path | None:
        source = str(case.get("source_language") or "").lower()
        target = str(case.get("target_language") or "").lower()
        profile = Path("pdf_translation_workflow_core") / "profiles" / f"{source}_to_{target}.layout_profile.json"
        if profile.exists():
            return profile
        return None

    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = str(case["case_id"])
        case_dir = self.report_dir / case_id
        ensure_dir(case_dir)
        source_pdf = Path(case["source_pdf"])
        extraction = case_dir / "source_extraction.json"
        sem_validation = case_dir / "semantic_translation_validation.json"
        policy = case_dir / "layout_policy.json"
        role_plan = case_dir / "role_plan.json"
        planned_layout = case_dir / "layout_plan.shadow.json"
        output_pdf = self.output_dir / f"{case_id}_candidate.pdf"
        evidence = case_dir / "candidate_generation_evidence.json"
        translations_used = case_dir / "translations.used.json"
        layout_plan = case_dir / "layout_plan.json"
        render_dir = case_dir / "candidate_previews"
        render_manifest = case_dir / "candidate_render_manifest.json"
        visual_metrics = case_dir / "visual_region_metrics.json"
        crop_dir = case_dir / "visual_crops"
        repair_plan = case_dir / "visual_repair_plan.json"
        visual_adj = case_dir / "visual_adjudication.json"
        quality = case_dir / "product_quality_gates.json"

        self.run_cmd(
            "S3_SourceExtract",
            f"S3_SourceExtract:{case_id}",
            "extract_pdf_structure.py",
            [PYTHON, "pdf_translation_workflow_core/tools/probes/extract_pdf_structure.py", "--input", str(source_pdf), "--out", str(extraction)],
            [source_pdf],
            [extraction],
        )
        sem = self.select_translation_for_case(case, extraction)
        self.run_cmd(
            "S5_TranslationPlan",
            f"S5_ValidateSemanticTranslations:{case_id}",
            "validate_semantic_translations.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/validate_semantic_translations.py",
                "--source-extraction",
                str(extraction),
                "--translations",
                str(sem),
                "--out",
                str(sem_validation),
            ],
            [extraction, sem],
            [sem_validation],
        )
        profile = self.language_profile(case)
        cmd = [
            PYTHON,
            "pdf_translation_workflow_core/tools/planners/build_layout_policy.py",
            "--source-extraction",
            str(extraction),
            "--semantic-translations",
            str(sem),
            "--out",
            str(policy),
        ]
        if profile is not None:
            cmd.extend(["--language-profile", str(profile)])
        self.run_cmd(
            "S6_LayoutPlan",
            f"S6_BuildLayoutPolicy:{case_id}",
            "build_layout_policy.py",
            cmd,
            [extraction, sem] + ([profile] if profile is not None else []),
            [policy],
        )
        self.run_cmd(
            "S6_LayoutPlan",
            f"S6_BuildRolePlan:{case_id}",
            "build_role_plan.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/planners/build_role_plan.py",
                "--source-extraction",
                str(extraction),
                "--semantic-translations",
                str(sem),
                "--layout-policy",
                str(policy),
                "--out",
                str(role_plan),
            ],
            [extraction, sem, policy],
            [role_plan],
        )
        self.run_cmd(
            "S6_LayoutPlan",
            f"S6_BuildLayoutPlanShadow:{case_id}",
            "build_layout_plan.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/planners/build_layout_plan.py",
                "--role-plan",
                str(role_plan),
                "--layout-policy",
                str(policy),
                "--out",
                str(planned_layout),
            ],
            [role_plan, policy],
            [planned_layout],
        )
        self.run_cmd(
            "S7_GenerateCandidate",
            f"S7_GenerateCandidate:{case_id}",
            "generate_semantic_backfill.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/generators/generate_semantic_backfill.py",
                "--input",
                str(source_pdf),
                "--source-extraction",
                str(extraction),
                "--semantic-translations",
                str(sem),
                "--layout-policy",
                str(policy),
                "--output",
                str(output_pdf),
                "--evidence",
                str(evidence),
                "--translations",
                str(translations_used),
                "--layout-plan",
                str(layout_plan),
            ],
            [source_pdf, extraction, sem, policy],
            [output_pdf, evidence, translations_used, layout_plan],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_RenderCandidate:{case_id}",
            "render_pdf.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/renderers/render_pdf.py",
                "--input",
                str(output_pdf),
                "--out-dir",
                str(render_dir),
                "--prefix",
                "candidate",
                "--manifest",
                str(render_manifest),
            ],
            [output_pdf],
            [render_manifest],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_CollectVisualRegionMetrics:{case_id}",
            "collect_visual_region_metrics.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/collect_visual_region_metrics.py",
                "--source",
                str(source_pdf),
                "--output",
                str(output_pdf),
                "--generation-evidence",
                str(evidence),
                "--source-extraction",
                str(extraction),
                "--out",
                str(visual_metrics),
                "--crop-dir",
                str(crop_dir),
            ],
            [source_pdf, output_pdf, evidence, extraction],
            [visual_metrics],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_PlanVisualRegionRepairs:{case_id}",
            "plan_visual_region_repairs.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/repairs/plan_visual_region_repairs.py",
                "--visual-region-metrics",
                str(visual_metrics),
                "--out",
                str(repair_plan),
            ],
            [visual_metrics],
            [repair_plan],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_WriteVisualAdjudication:{case_id}",
            "write_visual_adjudication.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/write_visual_adjudication.py",
                "--visual-region-metrics",
                str(visual_metrics),
                "--render-manifest",
                str(render_manifest),
                "--repair-plan",
                str(repair_plan),
                "--case-id",
                case_id,
                "--out",
                str(visual_adj),
            ],
            [visual_metrics, render_manifest, repair_plan],
            [visual_adj],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_EvaluateProductQuality:{case_id}",
            "evaluate_pdf_quality.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/evaluate_pdf_quality.py",
                "--source",
                str(source_pdf),
                "--output",
                str(output_pdf),
                "--generation-evidence",
                str(evidence),
                "--visual-adjudication",
                str(visual_adj),
                "--visual-region-metrics",
                str(visual_metrics),
                "--out",
                str(quality),
            ],
            [source_pdf, output_pdf, evidence, visual_adj, visual_metrics],
            [quality],
        )
        quality_data = read_json(quality)
        repair_data = read_json(repair_plan)
        loop_records: list[str] = []
        attempted_repairs: set[tuple[str, str]] = set()
        if quality_data.get("product_quality_verdict") == "FAIL" and int(repair_data.get("blocking_repair_count") or 0) > 0:
            if self.max_repair_loops <= 0:
                record = self.write_repair_loop_record(case_id, case_dir, quality, repair_plan, repair_data)
                loop_records.append(str(record))
            for loop_index in range(1, max(0, self.max_repair_loops) + 1):
                if quality_data.get("product_quality_verdict") != "FAIL" or int(repair_data.get("blocking_repair_count") or 0) <= 0:
                    break
                repair_result = self.run_repair_loop_once(
                    case_id,
                    case_dir,
                    source_pdf,
                    extraction,
                    sem,
                    policy,
                    quality,
                    repair_plan,
                    repair_data,
                    loop_index,
                    attempted_repairs,
                )
                loop_records.append(str(repair_result["record"]))
                attempted_key = repair_result.get("attempted_key")
                if isinstance(attempted_key, list) and len(attempted_key) == 2:
                    attempted_repairs.add((str(attempted_key[0]), str(attempted_key[1])))
                if not (repair_result.get("executed") and isinstance(repair_result.get("artifacts"), dict)):
                    break
                artifacts = repair_result["artifacts"]
                output_pdf = Path(artifacts["candidate_pdf"])
                evidence = Path(artifacts["candidate_generation_evidence"])
                translations_used = Path(artifacts["translations_used"])
                layout_plan = Path(artifacts["layout_plan"])
                render_manifest = Path(artifacts["candidate_render_manifest"])
                visual_metrics = Path(artifacts["visual_region_metrics"])
                repair_plan = Path(artifacts["visual_repair_plan"])
                visual_adj = Path(artifacts["visual_adjudication"])
                quality = Path(artifacts["product_quality_gates"])
                policy = Path(artifacts["layout_policy"])
                quality_data = read_json(quality)
                repair_data = read_json(repair_plan)
        case.update(
            {
                "report_dir": case_dir,
                "candidate_pdf": output_pdf,
                "source_extraction": extraction,
                "semantic_translation_validation": sem_validation,
                "layout_policy": policy,
                "role_plan": role_plan,
                "planned_layout_plan": planned_layout,
                "candidate_generation_evidence": evidence,
                "candidate_render_manifest": render_manifest,
                "visual_region_metrics": visual_metrics,
                "visual_repair_plan": repair_plan,
                "visual_adjudication": visual_adj,
                "product_quality_gates": quality,
                "product_quality_verdict": quality_data.get("product_quality_verdict"),
                "failed_gate_ids": [
                    gate.get("gate_id")
                    for gate in quality_data.get("gates", [])
                    if gate.get("blocking") and gate.get("status") == "fail"
                ],
                "blocking_repair_count": repair_data.get("blocking_repair_count"),
                "repair_loop_records": loop_records,
            }
        )
        return case

    def select_executable_repair_plan(
        self,
        repair_data: dict[str, Any],
        attempted_repairs: set[tuple[str, str]] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        executable_atoms = {
            "target_composition_body_reflow_repair",
            "expandable_text_slot_reflow_repair",
            "heading_frame_fit_or_short_title_variant",
            "heading_font_fit_curve_repair",
            "event_card_local_fit_repair",
            "footnote_fit_curve_repair",
            "side_navigation_rotated_image_repair",
            "matrix_diagram_table_cell_preserve_repair",
            "short_continuation_and_reflow_frame_repair",
            "body_flow_grouping",
            "body_flow_region_reflow",
            "body_flow_line_joining_or_line_height_adjust",
            "body_flow_paragraph_gap_rebalance",
            "font_size_and_region_density_rebalance",
            "dense_page_body_band_flow_repair",
            "constrained_slot_layout_fit_repair",
            "metric_value_font_hierarchy_repair",
        }
        attempted_repairs = attempted_repairs or set()
        plans = [item for item in repair_data.get("plans", []) if item.get("gate_status") == "fail"]
        for plan in plans:
            key = (str(plan.get("gate_id")), str(plan.get("repair_atom")))
            if key in attempted_repairs:
                continue
            if str(plan.get("repair_atom")) in executable_atoms and str(plan.get("target_state")) in {"S6_LayoutPlan", "S7_GenerateCandidate"}:
                return plan, plans
        return None, plans

    def apply_policy_repair_overrides(self, policy_path: Path, selected: dict[str, Any], out: Path) -> list[dict[str, Any]]:
        policy = read_json(policy_path)
        atom = str(selected.get("repair_atom") or "")
        gate_id = str(selected.get("gate_id") or selected.get("failure_class") or "")
        target_language = str(policy.get("target_language") or "").lower()
        language_pair_profile = str(policy.get("language_pair_profile") or "")
        changes: list[dict[str, Any]] = []

        def section(name: str) -> dict[str, Any]:
            value = policy.setdefault(name, {})
            if not isinstance(value, dict):
                value = {}
                policy[name] = value
            return value

        def nested(parent: dict[str, Any], name: str) -> dict[str, Any]:
            value = parent.setdefault(name, {})
            if not isinstance(value, dict):
                value = {}
                parent[name] = value
            return value

        def add_unique(container: dict[str, Any], key: str, values: list[str], reason: str) -> None:
            current = [str(item) for item in container.get(key, []) if str(item)]
            before = list(current)
            for value in values:
                if value not in current:
                    current.append(value)
            container[key] = current
            if current != before:
                changes.append({"path": key, "before": before, "after": current, "reason": reason})

        def remove_values(container: dict[str, Any], key: str, values: list[str], reason: str) -> None:
            current = [str(item) for item in container.get(key, []) if str(item)]
            blocked = set(values)
            after = [item for item in current if item not in blocked]
            if after != current:
                container[key] = after
                changes.append({"path": key, "before": current, "after": after, "reason": reason})

        def raise_float(container: dict[str, Any], key: str, minimum: float, reason: str) -> None:
            before = container.get(key)
            try:
                old = float(before)
            except (TypeError, ValueError):
                old = 0.0
            if old < minimum:
                container[key] = minimum
                changes.append({"path": key, "before": before, "after": minimum, "reason": reason})

        def set_if_changed(container: dict[str, Any], key: str, value: Any, reason: str) -> None:
            before = container.get(key)
            if before != value:
                container[key] = value
                changes.append({"path": key, "before": before, "after": value, "reason": reason})

        fallback = section("fallback")
        constrained = section("constrained_text_image_fit")
        constrained["enabled"] = True

        critical_roles: list[str] = []
        wrapped_roles: list[str] = []
        no_constrained_roles: set[str] = set()
        if atom in {
            "target_composition_body_reflow_repair",
            "short_continuation_and_reflow_frame_repair",
            "body_flow_grouping",
            "body_flow_region_reflow",
            "body_flow_line_joining_or_line_height_adjust",
            "body_flow_paragraph_gap_rebalance",
            "font_size_and_region_density_rebalance",
            "dense_page_body_band_flow_repair",
        }:
            critical_roles.extend(["body", "body_flow"])
            wrapped_roles.extend(["body", "body_flow"])
            composition = section("target_composition")
            reflow = section("target_language_reflow")
            if target_language == "en" or language_pair_profile == "zh_to_en":
                add_unique(composition, "disable_page_type_guesses", ["mixed_image_text"], "zh->en mixed image/text pages keep local anchors instead of page-wide body composition")
                add_unique(reflow, "disable_page_type_guesses", ["mixed_image_text"], "zh->en mixed image/text pages keep local anchors instead of page-wide reflow")
                raise_float(composition, "min_width_page_ratio", 0.78, "body readability repair widens fluid body frames before shrinking font")
                raise_float(composition, "min_source_width_page_ratio_for_composition", 0.42, "body readability repair skips page-wide composition for narrow source columns")
                raise_float(composition, "height_expand_ratio", 1.55, "body readability repair gives expanded English prose more vertical room")
                raise_float(composition, "max_bottom_page_ratio", 0.96, "body readability repair can use normal lower-page body area")
                raise_float(reflow, "min_width_page_ratio", 0.72, "target-language reflow repair widens paragraph frames")
                raise_float(reflow, "min_source_width_page_ratio_for_reflow", 0.42, "target-language reflow repair skips frame expansion for narrow source columns")
                raise_float(reflow, "height_expand_ratio", 1.55, "target-language reflow repair gives expanded English prose more vertical room")
            grouping = nested(section("flow_grouping"), "body")
            if target_language == "en" or language_pair_profile == "zh_to_en":
                before = grouping.get("candidate_region_kinds")
                grouping["candidate_region_kinds"] = ["body"]
                if before != grouping["candidate_region_kinds"]:
                    changes.append(
                        {
                            "path": "flow_grouping.body.candidate_region_kinds",
                            "before": before,
                            "after": grouping["candidate_region_kinds"],
                            "reason": "repair prevents compact labels and short labels from being merged into English body_flow",
                        }
                    )
                add_unique(grouping, "disable_page_type_guesses", ["mixed_image_text"], "mixed image/text regions are local constrained cards, not continuous body flow")
        if atom in {"heading_frame_fit_or_short_title_variant", "heading_font_fit_curve_repair"} or gate_id in {"title_readability", "hero_banner_text_readability"}:
            critical_roles.append("heading")
            wrapped_roles.append("heading")
            expandable = section("expandable_text_slots")
            expandable["enabled"] = True
            add_unique(expandable, "region_kinds", ["heading"], "readability repair lets page headings use current-page whitespace instead of hard source bbox fitting")
        if atom == "event_card_local_fit_repair" or gate_id == "event_card_readability":
            critical_roles.append("event_card")
            wrapped_roles.append("event_card")
        if atom == "footnote_fit_curve_repair" or gate_id == "footnote_readability":
            critical_roles.extend(["footnote", "table_note"])
            wrapped_roles.extend(["footnote", "table_note"])
        if atom == "matrix_diagram_table_cell_preserve_repair" or gate_id == "matrix_diagram_integrity":
            for profile_name in ["target_composition", "target_language_reflow"]:
                add_unique(section(profile_name), "hard_disable_page_type_guesses", ["matrix_or_table_diagram"], "matrix/table diagrams preserve two-dimensional structure")
            add_unique(nested(section("flow_grouping"), "body"), "hard_disable_page_type_guesses", ["matrix_or_table_diagram"], "matrix/table diagrams must not be routed through body_flow")
        if atom == "expandable_text_slot_reflow_repair" or gate_id == "short_label_legibility":
            expandable = section("expandable_text_slots")
            expandable["enabled"] = True
            add_unique(expandable, "region_kinds", ["short_label", "compact_label", "heading"], "expanded target text slots fix long labels before font shrink")
            add_unique(expandable, "disable_page_type_guesses", ["chart_or_dashboard", "table_or_chart_dense"], "dense chart/table labels remain hard constrained slots")
            add_unique(expandable, "hard_disable_page_type_guesses", ["matrix_or_table_diagram"], "matrix/table diagrams preserve two-dimensional structure")
            raise_float(expandable, "min_width_page_ratio", 0.38, "long target labels can use nearby whitespace before shrink")
            raise_float(expandable, "max_width_page_ratio", 0.78, "long target labels can expand within page margins without crossing into hard structures")
            raise_float(expandable, "height_expand_ratio", 1.8, "long target labels need enough line height after expansion")
            raise_float(expandable, "min_height_source_ratio", 2.4, "expanded labels keep readable local height derived from source font size")
            raise_float(expandable, "compact_label_min_width_page_ratio", 0.18, "compact labels use current-page width before font shrink")
            raise_float(expandable, "compact_label_max_width_page_ratio", 0.42, "compact label expansion remains local")
            raise_float(expandable, "compact_label_height_expand_ratio", 1.35, "compact labels get enough source-relative line height")
            set_if_changed(expandable, "compact_label_min_y_ratio", 0.0, "top page labels can expand when geometry allows")
            set_if_changed(expandable, "compact_label_min_text_expansion_ratio", 0.0, "compact label expansion is allowed for readability, not only length growth")
            set_if_changed(expandable, "compact_label_min_target_chars", 1, "compact labels can be short but still need readable font size")
            add_unique(constrained, "wrapped_region_kinds", ["short_label", "compact_label"], "expanded labels wrap locally if textbox probing still fails")
        if atom == "metric_value_font_hierarchy_repair" or gate_id == "metric_value_hierarchy":
            critical_roles.append("metric_value")
            no_constrained_roles.add("metric_value")
            add_unique(section("reflow"), "preserve_line_kinds", ["metric_value"], "metric/KPI values preserve local hierarchy and are not paragraph-reflowed")
            metric_variants = section("layout_text_variants")
            set_if_changed(
                metric_variants,
                "metric_value_en",
                ["metric_value_en", "compact_en", "display_en"],
                "metric/KPI values may use semantic compact display variants before geometry shrink",
            )
            set_if_changed(
                metric_variants,
                "metric_value_zh",
                ["metric_value_zh", "compact_zh", "display_zh"],
                "metric/KPI values may use semantic compact display variants before geometry shrink",
            )
            expandable = section("expandable_text_slots")
            expandable["enabled"] = True
            add_unique(expandable, "region_kinds", ["metric_value"], "metric/KPI values may expand into current-page whitespace before shrink")
            raise_float(expandable, "metric_value_min_width_page_ratio", 0.12, "metric/KPI repair derives width from current page, not fixed point size")
            raise_float(expandable, "metric_value_max_width_page_ratio", 0.34, "metric/KPI repair caps expansion within local page geometry")
            raise_float(expandable, "metric_value_height_expand_ratio", 1.2, "metric/KPI repair allows one readable line-height extension")
            raise_float(expandable, "metric_value_min_height_source_ratio", 0.85, "metric/KPI minimum visual height is derived from source font size")
            set_if_changed(expandable, "metric_value_min_text_expansion_ratio", 0.0, "metric/KPI expansion is driven by role hierarchy, not only text length expansion")
            set_if_changed(expandable, "metric_value_min_target_chars", 1, "metric/KPI values can be short but still visually dominant")
            metric_rule = nested(section("classification_rules"), "metric_value")
            set_if_changed(metric_rule, "enabled", True, "metric/KPI hierarchy repair enables generic value-role classification")
            set_if_changed(metric_rule, "source_size_page_quantile", "q75", "metric/KPI role is relative to current-page font hierarchy")
            set_if_changed(metric_rule, "min_source_to_page_quantile_ratio", 1.45, "metric/KPI role uses a current-page ratio rather than fixed point size")
            set_if_changed(
                metric_rule,
                "value_token_regex",
                r"([%\uFF05$]|US\$|HK\$|GBP|EUR|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|million|m|bps|\u57fa\u9ede|\u57fa\u70b9)",
                "metric/KPI role uses generic value tokens, not literal values",
            )
            set_if_changed(
                metric_rule,
                "value_amount_regex",
                r"((?:US\$|HK\$|\$|GBP|EUR)?\s*\d[\d,]*(?:\.\d+)?\s*(?:[%\uFF05]|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|millions?|m|bps|\u57fa\u9ede|\u57fa\u70b9)?)|((?:US\$|HK\$|\$|GBP|EUR)\s*\d)",
                "metric/KPI role must include a generic numeric amount, so unit labels alone are not promoted to metric callouts",
            )
            metric_profile = nested(section("font_profiles"), "metric_value")
            for key, value in {
                "sizing_mode": "source_relative",
                "source_scale": 1.0,
                "min_source_ratio": 0.70,
                "max_source_ratio": 1.05,
                "page_quantile_floor": "q75",
                "page_quantile_floor_scale": 1.10,
                "page_quantile_ceiling": "max",
                "page_quantile_ceiling_scale": 1.05,
                "min_insert_source_ratio": 0.62,
                "shrink_scales": [1.0, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76, 0.70],
            }.items():
                set_if_changed(metric_profile, key, value, "metric/KPI actual point size is resolved from source_size and current-page font quantiles")
            add_unique(constrained, "forbid_region_kinds", ["metric_value"], "metric/KPI hierarchy failures must not be repaired by generic compressed text images")
            remove_values(constrained, "region_kinds", ["metric_value"], "metric/KPI values are repaired by source-relative font and geometry expansion, not constrained image compression")
            remove_values(constrained, "wrapped_region_kinds", ["metric_value"], "metric/KPI values are repaired as callouts, not wrapped compressed images")
        if atom == "constrained_slot_layout_fit_repair" or gate_id in {"table_text_legibility", "legend_label_alignment"}:
            constrained_roles = ["table_cell", "legend", "short_label", "compact_label"]
            add_unique(constrained, "region_kinds", constrained_roles, "constrained slots use local fit repair before any translation regeneration is considered")
            add_unique(constrained, "wrapped_region_kinds", ["legend", "short_label", "compact_label"], "multi-line constrained labels wrap locally instead of falling back to point text")
            constrained["keep_proportion_for_wrapped"] = True
            raise_float(constrained, "max_font_source_ratio", 0.96, "constrained slot repair keeps labels source-relative without changing translation semantics")

        if critical_roles:
            unique_critical = sorted(set(critical_roles))
            add_unique(fallback, "forbid_region_kinds", unique_critical, "critical visual roles must fail visibly instead of falling back to tiny point text")
            constrained_critical = [role for role in unique_critical if role not in no_constrained_roles]
            if constrained_critical:
                add_unique(constrained, "region_kinds", constrained_critical, "critical roles may use policy-declared constrained text images after textbox probing fails")
        if wrapped_roles:
            add_unique(constrained, "wrapped_region_kinds", sorted(set(wrapped_roles)), "multi-line critical roles use wrapped constrained text images, not single-line compression")
            constrained["keep_proportion_for_wrapped"] = True
            raise_float(constrained, "max_font_source_ratio", 1.05, "repair keeps wrapped critical-role text source-relative")
            raise_float(constrained, "min_font_source_ratio", 0.62, "repair avoids unreadable wrapped text images through source-relative sizing")

        policy.setdefault("repair_overrides", []).append(
            {
                "repair_atom": atom,
                "gate_id": gate_id,
                "source": "run_semantic_product_quality_round.apply_policy_repair_overrides",
                "anti_overfit": "changes are driven by failure class, target language, and region role; no filename, page number, literal text, or fixed coordinate is used",
                "changes": changes,
            }
        )
        write_json(out, policy)
        return changes

    def run_repair_loop_once(
        self,
        case_id: str,
        case_dir: Path,
        source_pdf: Path,
        extraction: Path,
        semantic_translations: Path,
        policy: Path,
        quality_path: Path,
        repair_plan_path: Path,
        repair_data: dict[str, Any],
        loop_index: int,
        attempted_repairs: set[tuple[str, str]],
    ) -> dict[str, Any]:
        selected, plans = self.select_executable_repair_plan(repair_data, attempted_repairs)
        if selected is None:
            record = self.write_repair_loop_record(case_id, case_dir, quality_path, repair_plan_path, repair_data, loop_index=loop_index)
            return {"executed": False, "record": record, "reason": "no executable generic repair atom selected"}

        loop_tag = f"repair{loop_index:02d}"
        record_path = case_dir / f"repair_loop_{loop_index:04d}.json"
        repaired_policy = case_dir / f"layout_policy.{loop_tag}.json"
        candidate_pdf = self.output_dir / f"{case_id}_{loop_tag}_candidate.pdf"
        evidence = case_dir / f"candidate_generation_evidence.{loop_tag}.json"
        translations_used = case_dir / f"translations.used.{loop_tag}.json"
        layout_plan = case_dir / f"layout_plan.{loop_tag}.json"
        render_dir = case_dir / f"candidate_previews_{loop_tag}"
        render_manifest = case_dir / f"candidate_render_manifest.{loop_tag}.json"
        visual_metrics = case_dir / f"visual_region_metrics.{loop_tag}.json"
        crop_dir = case_dir / f"visual_crops_{loop_tag}"
        next_repair_plan = case_dir / f"visual_repair_plan.{loop_tag}.json"
        visual_adj = case_dir / f"visual_adjudication.{loop_tag}.json"
        quality = case_dir / f"product_quality_gates.{loop_tag}.json"
        changes = self.apply_policy_repair_overrides(policy, selected, repaired_policy)
        self.run_cmd(
            "Lx_RepairLoop",
            f"Lx_ApplyPolicyRepair:{case_id}:{loop_tag}",
            "run_semantic_product_quality_round.py",
            [PYTHON, "-c", "print('layout_policy_repair_overrides_written')"],
            [quality_path, repair_plan_path, policy],
            [repaired_policy],
        )
        self.run_cmd(
            "S7_GenerateCandidate",
            f"S7_RegenerateCandidateAfterRepair:{case_id}:{loop_tag}",
            "generate_semantic_backfill.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/generators/generate_semantic_backfill.py",
                "--input",
                str(source_pdf),
                "--source-extraction",
                str(extraction),
                "--semantic-translations",
                str(semantic_translations),
                "--layout-policy",
                str(repaired_policy),
                "--output",
                str(candidate_pdf),
                "--evidence",
                str(evidence),
                "--translations",
                str(translations_used),
                "--layout-plan",
                str(layout_plan),
            ],
            [source_pdf, extraction, semantic_translations, repaired_policy],
            [candidate_pdf, evidence, translations_used, layout_plan],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_RenderRepairedCandidate:{case_id}:{loop_tag}",
            "render_pdf.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/renderers/render_pdf.py",
                "--input",
                str(candidate_pdf),
                "--out-dir",
                str(render_dir),
                "--prefix",
                "candidate",
                "--manifest",
                str(render_manifest),
            ],
            [candidate_pdf],
            [render_manifest],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_CollectRepairedVisualRegionMetrics:{case_id}:{loop_tag}",
            "collect_visual_region_metrics.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/collect_visual_region_metrics.py",
                "--source",
                str(source_pdf),
                "--output",
                str(candidate_pdf),
                "--generation-evidence",
                str(evidence),
                "--source-extraction",
                str(extraction),
                "--out",
                str(visual_metrics),
                "--crop-dir",
                str(crop_dir),
            ],
            [source_pdf, candidate_pdf, evidence, extraction],
            [visual_metrics],
        )
        self.run_cmd(
            "Lx_RepairLoop",
            f"Lx_PlanRepairedVisualRegionRepairs:{case_id}:{loop_tag}",
            "plan_visual_region_repairs.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/repairs/plan_visual_region_repairs.py",
                "--visual-region-metrics",
                str(visual_metrics),
                "--out",
                str(next_repair_plan),
            ],
            [visual_metrics],
            [next_repair_plan],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_WriteRepairedVisualAdjudication:{case_id}:{loop_tag}",
            "write_visual_adjudication.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/write_visual_adjudication.py",
                "--visual-region-metrics",
                str(visual_metrics),
                "--render-manifest",
                str(render_manifest),
                "--repair-plan",
                str(next_repair_plan),
                "--case-id",
                case_id,
                "--out",
                str(visual_adj),
            ],
            [visual_metrics, render_manifest, next_repair_plan],
            [visual_adj],
        )
        self.run_cmd(
            "S8_VerifyProductQuality",
            f"S8_EvaluateRepairedProductQuality:{case_id}:{loop_tag}",
            "evaluate_pdf_quality.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/evaluate_pdf_quality.py",
                "--source",
                str(source_pdf),
                "--output",
                str(candidate_pdf),
                "--generation-evidence",
                str(evidence),
                "--visual-adjudication",
                str(visual_adj),
                "--visual-region-metrics",
                str(visual_metrics),
                "--out",
                str(quality),
            ],
            [source_pdf, candidate_pdf, evidence, visual_adj, visual_metrics],
            [quality],
        )
        next_quality = read_json(quality)
        next_repair = read_json(next_repair_plan)
        record = {
            "loop_id": f"{case_id}_L{loop_index:03d}",
            "loop_iteration": loop_index,
            "entered_from_state": "S8_VerifyProductQuality",
            "failure_class": selected.get("gate_id"),
            "failed_gate_ids": [item.get("gate_id") for item in plans],
            "repair_atom": selected.get("repair_atom"),
            "target_state": selected.get("target_state"),
            "target_scope": selected.get("sample_regions", [])[:5],
            "expected_effect": selected.get("description"),
            "verification_to_run": [
                "build_layout_policy.py policy override materialization",
                "generate_semantic_backfill.py",
                "render_pdf.py",
                "collect_visual_region_metrics.py",
                "plan_visual_region_repairs.py",
                "write_visual_adjudication.py",
                "evaluate_pdf_quality.py",
            ],
            "deferred_failures": [
                {
                    "gate_id": item.get("gate_id"),
                    "repair_atom": item.get("repair_atom"),
                    "target_state": item.get("target_state"),
                }
                for item in plans
                if item is not selected
            ],
            "execution_status": "applied_and_rejudged",
            "applied_policy_changes": changes,
            "input_artifacts": [rel(quality_path), rel(repair_plan_path), rel(policy)],
            "output_artifacts": [
                rel(repaired_policy),
                rel(candidate_pdf),
                rel(evidence),
                rel(visual_metrics),
                rel(next_repair_plan),
                rel(visual_adj),
                rel(quality),
                rel(record_path),
            ],
            "rejudged_product_quality_verdict": next_quality.get("product_quality_verdict"),
            "remaining_blocking_repair_count": next_repair.get("blocking_repair_count"),
            "next_state": "S9_VerifyProcessContract" if next_quality.get("product_quality_verdict") == "PASS" else "S_FAIL_QUALITY",
            "timestamp_local": now_local(),
        }
        write_json(record_path, record)
        self.run_cmd(
            "Lx_RepairLoop",
            f"Lx_RecordAppliedRepairLoop:{case_id}:{loop_tag}",
            "run_semantic_product_quality_round.py",
            [PYTHON, "-c", "print('applied_repair_loop_record_written')"],
            [quality_path, repair_plan_path, repaired_policy, quality],
            [record_path],
        )
        return {
            "executed": True,
            "record": rel(record_path),
            "attempted_key": [str(selected.get("gate_id")), str(selected.get("repair_atom"))],
            "artifacts": {
                "layout_policy": repaired_policy,
                "candidate_pdf": candidate_pdf,
                "candidate_generation_evidence": evidence,
                "translations_used": translations_used,
                "layout_plan": layout_plan,
                "candidate_render_manifest": render_manifest,
                "visual_region_metrics": visual_metrics,
                "visual_repair_plan": next_repair_plan,
                "visual_adjudication": visual_adj,
                "product_quality_gates": quality,
            },
        }

    def write_repair_loop_record(
        self,
        case_id: str,
        case_dir: Path,
        quality_path: Path,
        repair_plan_path: Path,
        repair_data: dict[str, Any],
        loop_index: int = 1,
    ) -> str:
        plans = [item for item in repair_data.get("plans", []) if item.get("gate_status") == "fail"]
        selected = plans[0] if plans else {}
        deferred = plans[1:]
        record_path = case_dir / f"repair_loop_{loop_index:04d}.json"
        record = {
            "loop_id": f"{case_id}_L{loop_index:03d}",
            "loop_iteration": loop_index,
            "entered_from_state": "S8_VerifyProductQuality",
            "failure_class": selected.get("gate_id"),
            "failed_gate_ids": [item.get("gate_id") for item in plans],
            "repair_atom": selected.get("repair_atom"),
            "target_state": selected.get("target_state"),
            "target_scope": selected.get("sample_regions", [])[:5],
            "expected_effect": selected.get("description"),
            "verification_to_run": [
                "generate_semantic_backfill.py if a generic repair executor is available",
                "render_pdf.py",
                "collect_visual_region_metrics.py",
                "evaluate_pdf_quality.py",
            ],
            "deferred_failures": [
                {
                    "gate_id": item.get("gate_id"),
                    "repair_atom": item.get("repair_atom"),
                    "target_state": item.get("target_state"),
                }
                for item in deferred
            ],
            "execution_status": "not_executed_unrepairable",
            "unrepairable_reason": (
                "Round runner can select and record repair atoms, but no generic atom executor is wired for this "
                "failure class without adding new tool behavior. Product quality remains FAIL instead of being "
                "silently promoted."
            ),
            "input_artifacts": [rel(quality_path), rel(repair_plan_path)],
            "output_artifacts": [rel(record_path)],
            "next_state": "S_FAIL_QUALITY",
            "timestamp_local": now_local(),
        }
        write_json(record_path, record)
        self.run_cmd(
            "Lx_RepairLoop",
            f"Lx_RecordRepairLoop:{case_id}",
            "run_semantic_product_quality_round.py",
            [PYTHON, "-c", "print('repair_loop_record_written')"],
            [quality_path, repair_plan_path],
            [record_path],
        )
        return rel(record_path)

    def run(self) -> dict[str, Any]:
        self.transition(
            "S0_Request",
            "S1_ContractLoad",
            "round runner invoked with source and semantic translation directories",
            ["run_semantic_product_quality_round.py"],
            [self.source_dir, self.semantic_dir],
            [self.report_dir / f"{self.round_id}_input_manifest.json"],
            [],
            [{"gate_id": "run_arguments", "status": "pass"}],
            "load contracts and prepare inputs",
        )
        self.prepare_inputs()
        self.decision(
            "D1_role_classification",
            "S4_PageStrategy",
            "Deterministic page strategy uses source extraction and language metadata.",
            [self.report_dir / f"{self.round_id}_input_manifest.json"],
            "D1_page_strategy.prompt.json",
            ["page_type", "region_roles", "evidence_refs", "risk_flags"],
            {"verdict": "pass", "backend_model_call_made": False, "strategy": "tool_extraction_driven"},
            "S5_TranslationPlan",
        )
        self.decision(
            "D2_translation",
            "S5_TranslationPlan",
            "Select pre-materialized semantic translations by validating each candidate against current source extraction.",
            [self.semantic_pool_dir],
            "D2_translation.prompt.json",
            ["coverage", "provider", "target_text_field", "layout_variants", "content_based_match"],
            {
                "verdict": "pass",
                "backend_model_call_made": False,
                "provider_mode": "pre_materialized_semantic_json",
                "matching_policy": "validate each semantic JSON against current source_extraction; filename is not the verdict",
            },
            "S6_LayoutPlan",
        )
        self.decision(
            "D3_visual_only_text",
            "S4_PageStrategy",
            "No OCR expansion is authorized in this runner.",
            [self.report_dir / f"{self.round_id}_input_manifest.json"],
            "D3_visual_only_text",
            ["visual_only_regions", "ocr_boundary"],
            {"verdict": "pass", "backend_model_call_made": False, "ocr_authorized": False},
            "S5_TranslationPlan",
        )
        self.transition(
            "S1_ContractLoad",
            "S2_ToolProbe",
            "core contracts and prompt templates are present",
            ["tool_probe.py"],
            [Path("pdf_translation_workflow_core")],
            [self.report_dir / "tool_probe.json"],
            [],
            [{"gate_id": "contract_load", "status": "pass"}],
            "probe tools",
        )
        self.run_cmd(
            "S2_ToolProbe",
            "S2_ToolProbe",
            "tool_probe.py",
            [PYTHON, "pdf_translation_workflow_core/tools/probes/tool_probe.py", "--out", str(self.report_dir / "tool_probe.json")],
            [Path("pdf_translation_workflow_core/tools")],
            [self.report_dir / "tool_probe.json"],
        )
        self.transition(
            "S2_ToolProbe",
            "S3_SourceExtract",
            "tool probe passed",
            ["extract_pdf_structure.py"],
            [self.report_dir / "tool_probe.json"],
            [],
            [],
            [{"gate_id": "tool_probe", "status": "pass"}],
            "extract every case",
        )
        results = []
        for case in self.cases:
            results.append(self.run_case(case))
        self.transition(
            "S3_SourceExtract",
            "S4_PageStrategy",
            "source structures extracted for all cases",
            ["extract_pdf_structure.py"],
            [case["source_extraction"] for case in results],
            [],
            ["D1_role_classification", "D3_visual_only_text"],
            [{"gate_id": "source_extraction", "status": "pass", "case_count": len(results)}],
            "classify page strategy from extraction",
        )
        self.transition(
            "S4_PageStrategy",
            "S5_TranslationPlan",
            "language directions and translation files are known",
            ["validate_semantic_translations.py"],
            [case["semantic_translations"] for case in results],
            [],
            ["D2_translation"],
            [{"gate_id": "semantic_translation_files", "status": "pass", "case_count": len(results)}],
            "validate semantic translations",
        )
        self.transition(
            "S5_TranslationPlan",
            "S6_LayoutPlan",
            "semantic validations completed",
            ["build_layout_policy.py", "build_role_plan.py", "build_layout_plan.py"],
            [case["semantic_translation_validation"] for case in results],
            [],
            ["D4_layout_plan"],
            [{"gate_id": "semantic_translation_validation", "status": "pass"}],
            "build layout policies and shadow role/layout plans",
        )
        self.decision(
            "D4_layout_plan",
            "S6_LayoutPlan",
            "Layout policies and shadow role/layout plans are built from current extraction statistics, semantic translations, and language profiles.",
            [case["layout_policy"] for case in results] + [case["role_plan"] for case in results] + [case["planned_layout_plan"] for case in results],
            "D4_layout_plan.prompt.json",
            ["layout_policy", "role_plan", "layout_plan_shadow", "language_pair_profile", "font_profiles", "fit_risks"],
            {
                "verdict": "pass",
                "backend_model_call_made": False,
                "policy_source": "build_layout_policy.py",
                "role_plan_source": "build_role_plan.py",
                "layout_plan_shadow_source": "build_layout_plan.py",
                "layout_plan_shadow_consumed_by_generator": False,
            },
            "S7_GenerateCandidate",
        )
        self.transition(
            "S6_LayoutPlan",
            "S7_GenerateCandidate",
            "layout policies exist for every case",
            ["generate_semantic_backfill.py"],
            [case["layout_policy"] for case in results],
            [case["candidate_pdf"] for case in results],
            ["D4_layout_plan"],
            [{"gate_id": "candidate_generation", "status": "pass"}],
            "generate candidates",
        )
        self.decision(
            "D5_initial_verification",
            "S8_VerifyProductQuality",
            "Initial candidate generation and visual closure artifacts exist.",
            [case["candidate_generation_evidence"] for case in results],
            "D5_D7_quality_gate.prompt.json",
            ["render_refs", "visual_region_metrics", "visual_repair_plan"],
            {"verdict": "pass", "backend_model_call_made": False},
            "S8_VerifyProductQuality",
        )
        self.decision(
            "D6_user_feedback_adjudication",
            "S8_VerifyProductQuality",
            "No manual user feedback was injected into this round.",
            [self.report_dir / f"{self.round_id}_input_manifest.json"],
            "D6_user_feedback_adjudication",
            ["feedback_scope", "accepted_changes"],
            {"verdict": "skipped", "backend_model_call_made": False, "reason": "no new human feedback during execution"},
            "S8_VerifyProductQuality",
        )
        failed_cases = [case for case in results if case.get("product_quality_verdict") != "PASS"]
        repair_records = [item for case in failed_cases for item in case.get("repair_loop_records", [])]
        quality_failure_next_state = "Lx_RepairLoop" if repair_records else "S_FAIL_QUALITY"
        self.decision(
            "D7_similarity_gate",
            "S8_VerifyProductQuality",
            "Aggregate product quality gates after per-case visual adjudication.",
            [case["product_quality_gates"] for case in results],
            "D5_D7_quality_gate.prompt.json",
            ["verdict", "failed_cases", "next_state"],
            {
                "verdict": "fail" if failed_cases else "pass",
                "backend_model_call_made": False,
                "failed_cases": [case["case_id"] for case in failed_cases],
                "repair_loop_entered": bool(repair_records),
                "repair_loop_record_count": len(repair_records),
            },
            quality_failure_next_state if failed_cases else "S9_VerifyProcessContract",
        )
        self.transition(
            "S7_GenerateCandidate",
            "S8_VerifyProductQuality",
            "all candidate PDFs generated",
            ["render_pdf.py", "collect_visual_region_metrics.py", "plan_visual_region_repairs.py", "write_visual_adjudication.py", "evaluate_pdf_quality.py"],
            [case["candidate_pdf"] for case in results],
            [case["product_quality_gates"] for case in results],
            ["D5_initial_verification", "D6_user_feedback_adjudication", "D7_similarity_gate"],
            [{"gate_id": "product_quality", "status": "fail" if failed_cases else "pass", "failed_case_count": len(failed_cases)}],
            "pass to S9 or enter repair loop",
        )
        if failed_cases:
            applied_repair_records: list[str] = []
            not_executed_repair_records: list[str] = []
            for record_ref in repair_records:
                record_path = ROOT / str(record_ref)
                if not record_path.exists():
                    continue
                record_data = read_json(record_path)
                if record_data.get("execution_status") == "applied_and_rejudged":
                    applied_repair_records.append(record_ref)
                else:
                    not_executed_repair_records.append(record_ref)
            if applied_repair_records:
                d8_reason = "One generic repair loop was applied and rejudged, but blocking quality failures remain or the configured repair-loop budget was exhausted."
            elif repair_records:
                d8_reason = "Repair loop was entered but no generic repair atom executor completed the remaining failures in this round."
            else:
                d8_reason = "Product quality failed, but no repair loop was entered because no repair_loop record was produced for the current failure set."
            self.decision(
                "D8_minimal_repair_selection",
                "Lx_RepairLoop" if repair_records else "S8_VerifyProductQuality",
                "Record selected repair atoms and repair-loop execution boundary.",
                [case["visual_repair_plan"] for case in failed_cases],
                "D8_repair_selection.prompt.json",
                ["repair_loop_record_path", "execution_status", "applied_repair_records", "unrepairable_reason_or_loop_budget", "deferred_failures"],
                {
                    "verdict": "fail",
                    "backend_model_call_made": False,
                    "repair_loop_records": repair_records,
                    "applied_repair_records": applied_repair_records,
                    "not_executed_repair_records": not_executed_repair_records,
                    "unrepairable_reason": d8_reason,
                },
                "S_FAIL_QUALITY",
            )
            if repair_records:
                self.transition(
                    "S8_VerifyProductQuality",
                    "Lx_RepairLoop",
                    "blocking product-quality failures found and repair-loop records exist",
                    ["plan_visual_region_repairs.py", "run_semantic_product_quality_round.py"],
                    [case["product_quality_gates"] for case in failed_cases],
                    [Path(item) for item in repair_records],
                    ["D8_minimal_repair_selection"],
                    [{"gate_id": "repair_loop_records", "status": "pass", "record_count": len(repair_records)}],
                    "record loop execution boundary, then fail quality honestly",
                )
            terminal = "S_FAIL_QUALITY"
            product_verdict = "FAIL"
        else:
            terminal = "S_DONE_PRODUCT_ACCEPTED"
            product_verdict = "PASS"
        self.decision(
            "D9_final_acceptance",
            "S9_VerifyProcessContract",
            "Finalize process and product split verdict.",
            [case["product_quality_gates"] for case in results],
            "D9_final_acceptance.prompt.json",
            ["process verdict", "product verdict", "terminal_state"],
            {
                "verdict": "fail" if failed_cases else "pass",
                "backend_model_call_made": False,
                "process_product_split": {
                    "generated_candidates": len(results),
                    "product_quality_pass": len(results) - len(failed_cases),
                    "product_quality_fail": len(failed_cases),
                },
            },
            terminal,
        )
        if failed_cases:
            if repair_records:
                self.transition(
                    "Lx_RepairLoop",
                    "S_FAIL_QUALITY",
                    "repair loop records written but no generic repair executor completed the remaining fixes",
                    ["run_semantic_product_quality_round.py"],
                    [Path(item) for item in repair_records],
                    [],
                    ["D9_final_acceptance"],
                    [{"gate_id": "product_quality", "status": "fail"}],
                    "write final process audit",
                )
            else:
                self.transition(
                    "S8_VerifyProductQuality",
                    "S_FAIL_QUALITY",
                    "product-quality failure without an entered repair loop",
                    ["run_semantic_product_quality_round.py"],
                    [case["product_quality_gates"] for case in failed_cases],
                    [],
                    ["D8_minimal_repair_selection", "D9_final_acceptance"],
                    [{"gate_id": "product_quality", "status": "fail", "repair_loop_record_count": 0}],
                    "write final process audit",
                )
        else:
            self.transition(
                "S8_VerifyProductQuality",
                "S9_VerifyProcessContract",
                "all product-quality gates passed",
                ["validate_process_artifacts.py"],
                [case["product_quality_gates"] for case in results],
                [],
                ["D9_final_acceptance"],
                [{"gate_id": "product_quality", "status": "pass"}],
                "validate process contract",
            )
        token_file = self.report_dir / "anti_overfit_tokens.json"
        write_json(
            token_file,
            {
                "tokens": [Path(case["source_pdf"]).stem for case in results],
                "note": "Sample names are scanned only as forbidden core tokens; runtime logic must not branch on them.",
            },
        )
        self.run_cmd(
            "S9_VerifyProcessContract",
            "S9_AntiOverfitScan",
            "scan_core_overfit.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/scan_core_overfit.py",
                "--root",
                "pdf_translation_workflow_core",
                "--token-file",
                str(token_file),
                "--out",
                str(self.report_dir / "anti_overfit_scan.json"),
            ],
            [token_file],
            [self.report_dir / "anti_overfit_scan.json"],
            allow_fail=True,
        )
        self.run_cmd(
            "S9_VerifyProcessContract",
            "S9_ValidateProcessArtifacts",
            "validate_process_artifacts.py",
            [
                PYTHON,
                "pdf_translation_workflow_core/tools/validators/validate_process_artifacts.py",
                "--run-dir",
                str(self.report_dir),
                "--out",
                str(self.report_dir / "process_validation.json"),
            ],
            [self.state_trace_path, self.decision_log, self.operation_log],
            [self.report_dir / "process_validation.json"],
            allow_fail=True,
        )
        process_validation = read_json(self.report_dir / "process_validation.json")
        process_verdict = process_validation.get("process_contract_verdict")
        final = {
            "round_id": self.round_id,
            "input_count": len(results),
            "process_contract_verdict": process_verdict,
            "semantic_translation_verdict": "PASS",
            "generation_verdict": "PASS",
            "product_quality_verdict": product_verdict,
            "terminal_state": "S_FAIL_PROCESS_CONTRACT" if process_verdict != "PASS" else terminal,
            "cases": [
                {
                    "case_id": case["case_id"],
                    "source_pdf": rel(case["source_pdf"]),
                    "target_language": case.get("target_language"),
                    "candidate_pdf": rel(case["candidate_pdf"]),
                    "product_quality_verdict": case.get("product_quality_verdict"),
                    "failed_gate_ids": case.get("failed_gate_ids", []),
                    "blocking_repair_count": case.get("blocking_repair_count"),
                    "repair_loop_records": case.get("repair_loop_records", []),
                    "report_dir": rel(case["report_dir"]),
                }
                for case in results
            ],
            "process_validation": rel(self.report_dir / "process_validation.json"),
            "process_validation_errors": process_validation.get("errors", []),
        }
        write_json(self.report_dir / f"{self.round_id}_final_verdict.json", final)
        self.write_report(final)
        return final

    def write_report(self, final: dict[str, Any]) -> None:
        lines = [
            f"# {self.round_id} semantic product-quality execution report",
            "",
            f"- process_contract_verdict: `{final['process_contract_verdict']}`",
            f"- product_quality_verdict: `{final['product_quality_verdict']}`",
            f"- terminal_state: `{final['terminal_state']}`",
            "",
            "| case_id | target | product | blocking repairs | failed gates | candidate |",
            "|---|---|---|---:|---|---|",
        ]
        for case in final["cases"]:
            failed = ", ".join(case.get("failed_gate_ids") or []) or "-"
            lines.append(
                f"| `{case['case_id']}` | `{case.get('target_language')}` | `{case['product_quality_verdict']}` | "
                f"{case.get('blocking_repair_count') or 0} | {failed} | `{case['candidate_pdf']}` |"
            )
        lines.extend(
            [
                "",
                "## Repair Loop Evidence",
                "",
                "A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.",
            ]
        )
        for case in final["cases"]:
            records = case.get("repair_loop_records") or []
            lines.append(f"- `{case['case_id']}`: {records if records else 'no blocking repair loop'}")
        lines.extend(
            [
                "",
                "## Notes",
                "",
                "- Prior round quality artifacts were not used as evidence.",
                "- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.",
                "- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.",
            ]
        )
        (self.report_dir / f"{self.round_id}_execution_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--semantic-dir", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--max-repair-loops", type=int, default=1)
    args = parser.parse_args()
    final = Runner(args).run()
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final.get("process_contract_verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
