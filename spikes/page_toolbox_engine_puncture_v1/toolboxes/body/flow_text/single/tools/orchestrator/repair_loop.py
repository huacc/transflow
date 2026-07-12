"""
tool_name: deterministic_repair_loop
category: orchestrator
input_contract: current P4LayoutPlan
output_contract: repaired P4LayoutPlan and ordered RepairPatch/application records
failure_signals: repair executor rejects a selected patch or the same rule remains actionable after page-bounded iterations
fallback: caller retains the last mechanically valid plan and records capability failure
anti_overfit_statement: dispatch uses failure class and repair atom only; every target id and value comes from current-run rule evidence
"""

from __future__ import annotations

from pathlib import Path

from page_toolbox_puncture.contracts import PageFacts

from ..models import SingleColumnTemplate
from ..p4_models import P4LayoutPlan
from ..probes.inline_graphic_control_probe import probe_inline_graphic_controls
from ..repairs.inline_graphic_control_relayout import apply_inline_graphic_control_relayout
from ..repairs.section_spacing_reflow import apply_section_spacing_reflow
from ..validators.inline_graphic_control_alignment_rule import evaluate_inline_graphic_control_alignment
from ..validators.section_spacing_outlier_rule import evaluate_section_spacing_outlier


def apply_deterministic_layout_repairs(
    plan: P4LayoutPlan,
) -> tuple[P4LayoutPlan, tuple[dict[str, object], ...]]:
    current = plan
    records: list[dict[str, object]] = []
    main_count = sum(item.role != "margin" for item in plan.placements)
    # 每轮只接受一个病因和一个 RepairPatch，修后立即重算规则；循环上限来自本页转场数。
    for _ in range(max(1, main_count - 1)):
        decision = evaluate_section_spacing_outlier(current)
        if decision["rule_verdict"] != "FAIL":
            break
        repaired, application = apply_section_spacing_reflow(
            current,
            previous_container_id=str(decision["previous_container_id"]),
            next_container_id=str(decision["next_container_id"]),
            target_gap_pt=float(decision["target_plan_gap_pt"]),
        )
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": "section_spacing_reflow",
                "selected_repair_atom": decision["repair_atom"],
                "bound_tool": "tools/repairs/section_spacing_reflow.py",
            },
            "operations": [
                {
                    "operation_type": decision["repair_atom"],
                    "previous_container_id": decision["previous_container_id"],
                    "next_container_id": decision["next_container_id"],
                    "source_gap_pt": decision["source_gap_pt"],
                    "candidate_plan_gap_pt": decision["candidate_plan_gap_pt"],
                    "target_plan_gap_pt": decision["target_plan_gap_pt"],
                }
            ],
            "anti_overfit_statement": "Container ids and target gap are current-run rule evidence; dispatch contains no sample-specific branch.",
        }
        records.append({"rule_decision": decision, "repair_patch": patch, "application": application})
        if repaired == current or application["status"] != "applied":
            break
        current = repaired
    else:
        raise RuntimeError("deterministic_layout_repair_loop_exhausted_page_bound")
    return current, tuple(records)


def apply_deterministic_candidate_repairs(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: SingleColumnTemplate,
    plan: P4LayoutPlan,
) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    # 候选级图形修补同样是一轮一处，循环上限来自当前页图形对象数量。
    for _ in range(max(1, len(facts.drawing_objects))):
        probes = probe_inline_graphic_controls(
            source_pdf=source_pdf,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=plan,
        )
        selected = next(
            (
                (probe, evaluate_inline_graphic_control_alignment(probe))
                for probe in probes
                if evaluate_inline_graphic_control_alignment(probe)["rule_verdict"] == "FAIL"
            ),
            None,
        )
        if selected is None:
            break
        probe, decision = selected
        repaired_pdf = candidate_pdf.with_name(f"{candidate_pdf.stem}.repairing{candidate_pdf.suffix}")
        repaired_pdf.unlink(missing_ok=True)
        application = apply_inline_graphic_control_relayout(
            input_pdf=candidate_pdf,
            output_pdf=repaired_pdf,
            page_index=facts.page_index,
            source_control_bboxes=tuple(tuple(item) for item in probe["source_control_bboxes"]),
            target_control_bboxes=tuple(tuple(item) for item in probe["target_control_bboxes"]),
            stroke_color=tuple(probe["stroke_color"]),
            stroke_width=float(probe["stroke_width"]),
        )
        repaired_pdf.replace(candidate_pdf)
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": "image_overlay_text_relayout",
                "selected_repair_atom": decision["repair_atom"],
                "bound_tool": "tools/repairs/inline_graphic_control_relayout.py",
            },
            "operations": [
                {
                    "operation_type": decision["repair_atom"],
                    "container_id": probe["container_id"],
                    "source_control_bboxes": probe["source_control_bboxes"],
                    "target_control_bboxes": probe["target_control_bboxes"],
                    "stroke_color": probe["stroke_color"],
                    "stroke_width": probe["stroke_width"],
                }
            ],
            "anti_overfit_statement": "Control geometry, symbol mapping, style, and container movement are current-run probe evidence.",
        }
        records.append({"rule_decision": decision, "repair_patch": patch, "application": application})
    else:
        remaining = [
            evaluate_inline_graphic_control_alignment(item)
            for item in probe_inline_graphic_controls(
                source_pdf=source_pdf,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
            )
        ]
        if any(item["rule_verdict"] == "FAIL" for item in remaining):
            raise RuntimeError("deterministic_candidate_repair_loop_exhausted_page_bound")
    return tuple(records)
