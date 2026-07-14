from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.flow_text.single.tools.models import ToolboxDecision, ToolboxFinding
from toolboxes.body.flow_text.single.tools.renderer import render_candidate

from .diagnostic_renderer import render_unfit_multi_plan_candidate
from .judge import judge_multi_candidate
from .layout_pattern import LayoutPatternAdjudicator, build_layout_pattern_rule_decision, infer_multi_band_variant
from .layout_planner import build_best_multi_plan, refresh_post_repair_planning_findings
from .orchestrator.layout_repair_loop import apply_deterministic_multi_layout_repairs
from .orchestrator.typography_runtime import run_typography_repair_loop
from .probes.semantic_paragraph_spacing_probe import probe_semantic_paragraph_transitions
from .probes.structural_anchor_probe import probe_horizontal_structural_anchors
from .repairs.rendered_semantic_spacing_reflow import apply_rendered_semantic_spacing_reflow
from .template_builder import build_multi_column_template_with_repairs
from .translation_validation import canonicalize_with_targeted_retry
from .typography_adjudication import TypographyAdjudicator
from .validators.rendered_semantic_spacing_rule import evaluate_rendered_semantic_spacing
from .validators.tolerant_route_rule import evaluate_tolerant_multi_route


@dataclass(frozen=True)
class P5RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    selected_column_profiles: tuple[str, ...]


def run_p5_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    source_language: str,
    target_language: str,
    layout_pattern_adjudicator: LayoutPatternAdjudicator | None = None,
    typography_adjudicator: TypographyAdjudicator | None = None,
) -> P5RunResult:
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_snapshot)
    if source_hash != sha256_file(source_pdf):
        raise RuntimeError("source_snapshot_hash_mismatch")
    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p5-page-run/v1",
            "toolbox_key": "body.flow_text.multi",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "horizontal_rule": "column x0, width and gutter are invariant; no cross-column write",
            "vertical_rule": "each column reflows independently within its own bottom guard",
            "layout_pattern_rule": "rule and Qwen must agree before band-specific refill",
            "single_band_rule": "single bands use a multi-owned vertical refill implementation",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P5 页级运行包\n\n原文、模板、译文、逐栏计划、候选 PDF、并排图和裁决报告均保存在本目录。\n",
        encoding="utf-8",
    )
    trace: list[dict[str, str]] = [{"state": "P5_PACKAGE_READY", "owner": "p5_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    selected_profiles: tuple[str, ...] = ()
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "P5_FACTS_READY", "owner": "shared_pdf_kernel"})
        template, template_repairs = build_multi_column_template_with_repairs(facts)
        write_json(run_dir / "reports" / "template_repair_trace.json", template_repairs)
        for repair_index, repair in enumerate(template_repairs, start=1):
            write_json(run_dir / "reports" / f"template_rule_{repair_index:04d}.json", repair["rule_decision"])
            write_json(run_dir / "reports" / f"template_patch_{repair_index:04d}.json", repair["repair_patch"])
            write_json(run_dir / "reports" / f"template_patch_application_{repair_index:04d}.json", repair["application"])
            trace.append({"state": "P5_TEMPLATE_REPAIR_PATCH_APPLIED", "owner": "deterministic_template_repair_loop"})
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "P5_COLUMN_TEMPLATE_READY", "owner": "p5_template_builder"})
        route_decision = evaluate_tolerant_multi_route(template)
        write_json(run_dir / "reports" / "routing_decision.json", route_decision)
        if not str(route_decision["route_verdict"]).startswith("ACCEPT_"):
            raise ValueError("p5_route_requires_fine_grained_adjudication")
        trace.append({"state": "P5_ROUTE_ACCEPTED", "owner": "p5_tolerant_route_rule"})

        pattern_rule = build_layout_pattern_rule_decision(template)
        write_json(run_dir / "reports" / "layout_pattern_rule_decision.json", pattern_rule)
        trace.append({"state": "P5_LAYOUT_PATTERN_RULE_READY", "owner": "p5_layout_pattern_rule"})
        if layout_pattern_adjudicator is not None:
            pattern_qwen = layout_pattern_adjudicator.adjudicate(pattern_rule)
            write_json(run_dir / "reports" / "layout_pattern_qwen_decision.json", pattern_qwen)
            if (
                pattern_qwen["pattern"] != pattern_rule["pattern"]
                or pattern_qwen["multi_band_variant"] != pattern_rule["multi_band_variant"]
            ):
                raise ValueError("p5_layout_pattern_disagreement_requires_fine_grained_adjudication")
            trace.append({"state": "P5_LAYOUT_PATTERN_AGREED", "owner": "p5_layout_pattern_adjudicator"})

        units = tuple(TranslationUnit(item.container_id, item.source_text, item.reading_order) for item in template.containers)
        request = PageTranslationRequest(f"p5-{page_id}-{source_language}-{target_language}", page_id, source_language, target_language, units)
        write_json(run_dir / "input" / "translation_request.json", request)
        translation = provider.translate(request)
        translation.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.raw.json", translation)
        translation, retries = canonicalize_with_targeted_retry(request=request, translation=translation, template=template, provider=provider)
        write_json(run_dir / "reports" / "translation_retry_trace.json", retries)
        write_json(run_dir / "output" / "translation_bundle.json", translation)
        trace.append({"state": "P5_TRANSLATION_READY", "owner": "translation_provider"})

        structural_anchors = probe_horizontal_structural_anchors(
            source_snapshot,
            page_index=facts.page_index,
        )
        write_json(run_dir / "reports" / "structural_anchor_probe.json", structural_anchors)
        plan, attempts, planning_findings = build_best_multi_plan(facts=facts, template=template, translations=translation, source_language=source_language, target_language=target_language, font_file=font_file, structural_anchors=structural_anchors)
        write_json(run_dir / "reports" / "repair_trace.json", attempts)
        plan, layout_repairs = apply_deterministic_multi_layout_repairs(
            facts=facts,
            template=template,
            plan=plan,
        )
        write_json(run_dir / "reports" / "deterministic_layout_repair_trace.json", layout_repairs)
        for repair_index, repair in enumerate(layout_repairs, start=1):
            write_json(run_dir / "reports" / f"layout_rule_{repair_index:04d}.json", repair["rule_decision"])
            write_json(run_dir / "reports" / f"layout_patch_{repair_index:04d}.json", repair["repair_patch"])
            write_json(run_dir / "reports" / f"layout_patch_application_{repair_index:04d}.json", repair["application"])
            trace.append({"state": "P5_LAYOUT_REPAIR_PATCH_APPLIED", "owner": "deterministic_multi_layout_repair_loop"})
        planning_findings = refresh_post_repair_planning_findings(
            facts=facts,
            template=template,
            plan=plan,
            findings=planning_findings,
        )
        write_json(run_dir / "reports" / "post_repair_planning_findings.json", planning_findings)
        write_json(run_dir / "output" / "layout_plan.json", plan)
        selected_profiles = tuple(f"{item.column_id}:{item.profile_id}" for item in plan.column_selections)
        trace.append({"state": "P5_COLUMN_LAYOUT_READY", "owner": "p5_layout_planner"})
        if any(item.severity == "HARD" for item in planning_findings):
            # 质量门禁失败仍输出译文诊断候选，让人工直接看到溢出、挤占或错栏结果。
            candidate_pdf = run_dir / "output" / "diagnostic_candidate.pdf"
            diagnostic_evidence = render_unfit_multi_plan_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            write_json(run_dir / "reports" / "diagnostic_render_evidence.json", diagnostic_evidence)
            write_json(
                run_dir / "reports" / "typography_repair_memory.json",
                {
                    "schema_version": "p5-typography-repair-memory/v1",
                    "page_id": page_id,
                    "initial_state_hash": None,
                    "attempted_action_keys": [],
                    "seen_state_hashes": [],
                    "attempts": [],
                    "terminal_reason": "NOT_REACHED_PLANNING_HARD",
                    "final_verdict": "NOT_REACHED",
                    "final_profiles": list(selected_profiles),
                },
            )
            trace.append({"state": "P5_DIAGNOSTIC_CANDIDATE_READY", "owner": "p5_diagnostic_renderer"})
            decision = ToolboxDecision(page_id, "PASS", "FAIL", "P5_PRODUCT_FAIL", planning_findings)
        else:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            rendered_spacing_repairs: list[dict[str, object]] = []
            repair_limit = max(1, len(plan.placements))
            ignored_relative_spacing_columns = (
                tuple(item.column_id for item in template.columns)
                if infer_multi_band_variant(template) == "paired_row_columns"
                else ()
            )
            while True:
                # 每次从不可变原页重渲染；一次只修一个实际字形段距病因，再重新测量。
                render_findings, render_evidence = render_candidate(source_pdf=source_snapshot, candidate_pdf=candidate_pdf, facts=facts, template=template, plan=plan, evidence_dir=run_dir / "previews")
                semantic_transitions = probe_semantic_paragraph_transitions(
                    candidate_pdf=candidate_pdf,
                    facts=facts,
                    template=template,
                    plan=plan,
                )
                rendered_spacing_decision = evaluate_rendered_semantic_spacing(
                    semantic_transitions,
                    ignore_relative_spacing_columns=ignored_relative_spacing_columns,
                )
                failure_class = str(rendered_spacing_decision.get("selected_failure_class") or "")
                if rendered_spacing_decision["rule_verdict"] != "FAIL" or failure_class not in {
                    "semantic_paragraph_spacing_loss",
                    "semantic_paragraph_spacing_amplification",
                    "rendered_text_overlap",
                }:
                    break
                if len(rendered_spacing_repairs) >= repair_limit:
                    break
                try:
                    repaired_plan, application = apply_rendered_semantic_spacing_reflow(
                        template=template,
                        plan=plan,
                        decision=rendered_spacing_decision,
                    )
                except ValueError as exc:
                    rendered_spacing_repairs.append(
                        {
                            "rule_decision": rendered_spacing_decision,
                            "repair_patch": {"patch_verdict": "PATCH_REJECTED"},
                            "application": {"status": "rejected", "reason": str(exc)},
                        }
                    )
                    break
                patch = {
                    "schema_version": "repair-patch/v1",
                    "patch_verdict": "PATCH_READY",
                    "selected_failure_class": failure_class,
                    "dispatch_result": {
                        "dispatch_table": "contracts/failure_dispatch_table.json",
                        "selected_repair_family": "source_relative_semantic_spacing_reflow",
                        "selected_repair_atom": "rendered_semantic_spacing_reflow",
                        "bound_tool": "tools/repairs/rendered_semantic_spacing_reflow.py",
                    },
                    "operations": [
                        {
                            "operation_type": "rendered_semantic_spacing_reflow",
                            "previous_container_id": rendered_spacing_decision["previous_container_id"],
                            "next_container_id": rendered_spacing_decision["next_container_id"],
                        }
                    ],
                    "anti_overfit_statement": "Shift is derived from current rendered source/candidate rhythm only.",
                }
                repair_index = len(rendered_spacing_repairs) + 1
                record = {
                    "rule_decision": rendered_spacing_decision,
                    "repair_patch": patch,
                    "application": application,
                }
                rendered_spacing_repairs.append(record)
                write_json(run_dir / "reports" / f"rendered_spacing_rule_{repair_index:04d}.json", rendered_spacing_decision)
                write_json(run_dir / "reports" / f"rendered_spacing_patch_{repair_index:04d}.json", patch)
                write_json(run_dir / "reports" / f"rendered_spacing_patch_application_{repair_index:04d}.json", application)
                trace.append({"state": "P5_RENDERED_SPACING_REPAIR_PATCH_APPLIED", "owner": "deterministic_rendered_spacing_repair_loop"})
                plan = repaired_plan

            write_json(run_dir / "output" / "layout_plan.json", plan)
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            write_json(run_dir / "reports" / "rendered_spacing_repair_trace.json", rendered_spacing_repairs)
            trace.append({"state": "P5_CANDIDATE_READY", "owner": "pdf_renderer"})
            write_json(run_dir / "reports" / "semantic_paragraph_spacing_probe.json", semantic_transitions)
            write_json(run_dir / "reports" / "rendered_semantic_spacing_decision.json", rendered_spacing_decision)
            typography_findings: tuple[ToolboxFinding, ...] = ()
            if typography_adjudicator is not None:
                typography_result = run_typography_repair_loop(
                    source_pdf=source_snapshot,
                    candidate_pdf=candidate_pdf,
                    run_dir=run_dir,
                    facts=facts,
                    template=template,
                    plan=plan,
                    planning_findings=planning_findings,
                    render_findings=render_findings,
                    render_evidence=render_evidence,
                    semantic_transitions=semantic_transitions,
                    rendered_spacing_decision=rendered_spacing_decision,
                    pattern_rule=pattern_rule,
                    adjudicator=typography_adjudicator,
                )
                plan = typography_result.plan
                render_findings = typography_result.render_findings
                render_evidence = typography_result.render_evidence
                semantic_transitions = typography_result.semantic_transitions
                rendered_spacing_decision = typography_result.rendered_spacing_decision
                typography_findings = typography_result.typography_findings
                selected_profiles = tuple(f"{item.column_id}:{item.profile_id}" for item in plan.column_selections)
                write_json(run_dir / "output" / "layout_plan.json", plan)
                write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
                write_json(run_dir / "reports" / "semantic_paragraph_spacing_probe.json", semantic_transitions)
                write_json(run_dir / "reports" / "rendered_semantic_spacing_decision.json", rendered_spacing_decision)
                for attempt in typography_result.repair_memory["attempts"]:
                    trace.append(
                        {
                            "state": f"P5_TYPOGRAPHY_REPAIR_{attempt['outcome']}",
                            "owner": "p5_typography_repair_loop",
                        }
                    )
            decision = judge_multi_candidate(
                candidate_pdf=candidate_pdf,
                template=template,
                plan=plan,
                upstream_findings=planning_findings + render_findings + typography_findings,
                rendered_spacing_decision=rendered_spacing_decision,
            )
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "P5_JUDGED", "owner": "p5_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "p5_quality_judge"})
    except Exception as exc:
        failure_owner = _owner(trace)
        decision = ToolboxDecision(page_id, "FAIL", "NOT_REACHED", "P5_CAPABILITY_FAILED", (ToolboxFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc)),))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "P5_CAPABILITY_FAILED", "owner": failure_owner})

    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P5RunResult(page_id, str(run_dir), str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None, decision.process_verdict, decision.product_verdict, decision.terminal_state, failure_owner, selected_profiles)
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _owner(trace: list[dict[str, str]]) -> str:
    last = trace[-1]["state"] if trace else ""
    return {
        "P5_PACKAGE_READY": "shared_pdf_kernel",
        "P5_FACTS_READY": "p5_template_builder",
        "P5_TEMPLATE_REPAIR_PATCH_APPLIED": "p5_template_builder",
        "P5_COLUMN_TEMPLATE_READY": "p5_tolerant_route_rule",
        "P5_ROUTE_ACCEPTED": "translation_provider",
        "P5_LAYOUT_PATTERN_RULE_READY": "p5_layout_pattern_adjudicator",
        "P5_LAYOUT_PATTERN_AGREED": "translation_provider",
        "P5_TRANSLATION_READY": "p5_layout_planner",
        "P5_LAYOUT_REPAIR_PATCH_APPLIED": "p5_layout_planner",
        "P5_COLUMN_LAYOUT_READY": "pdf_renderer",
        "P5_CANDIDATE_READY": "p5_quality_judge",
    }.get(last, "p5_orchestrator")
