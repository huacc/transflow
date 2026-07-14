"""
tool_name: deterministic_multi_template_repair_loop
category: orchestrator
input_contract: current PageFacts and initial MultiColumnTemplate
output_contract: repaired template and ordered rule/patch/application records
failure_signals: selected patch is rejected or the same disease remains after the page-derived bound
fallback: caller records capability failure; no ambiguous template is silently accepted
anti_overfit_statement: dispatch is by failure class and repair atom; targets and values come only from current-page rule evidence
"""

from __future__ import annotations

from page_toolbox_puncture.contracts import PageFacts

from ..models import MultiColumnTemplate
from ..repairs.cross_column_container_split import apply_cross_column_container_split
from ..repairs.semantic_paragraph_fragment_merge import apply_semantic_paragraph_fragment_merge
from ..repairs.trailing_postlude_reassignment import apply_trailing_postlude_reassignment
from ..validators.cross_column_extraction_merge_rule import evaluate_cross_column_extraction_merge
from ..validators.semantic_paragraph_fragmentation_rule import (
    derive_owner_line_gap_limits,
    evaluate_semantic_paragraph_fragmentation,
)
from ..validators.trailing_postlude_ownership_rule import evaluate_trailing_postlude_ownership


def apply_deterministic_template_repairs(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
) -> tuple[MultiColumnTemplate, tuple[dict[str, object], ...]]:
    current = template
    records: list[dict[str, object]] = []
    span_count = sum(item.column_id == "span" for item in template.assignments)
    # 与 single 一致：每轮只处理一个病因和一个修复原子，修后立即重新匹配规则。
    for _ in range(max(1, span_count)):
        decision = evaluate_cross_column_extraction_merge(facts=facts, template=current)
        if decision["rule_verdict"] != "FAIL":
            break
        repaired, application = apply_cross_column_container_split(
            facts=facts,
            template=current,
            container_id=str(decision["container_id"]),
            source_object_ids_by_column={
                str(column_id): [str(object_id) for object_id in object_ids]
                for column_id, object_ids in dict(decision["source_object_ids_by_column"]).items()
            },
        )
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": "multi_column_template_ownership",
                "selected_repair_atom": decision["repair_atom"],
                "bound_tool": "tools/repairs/cross_column_container_split.py",
            },
            "operations": [
                {
                    "operation_type": decision["repair_atom"],
                    "container_id": decision["container_id"],
                    "source_object_ids_by_column": decision["source_object_ids_by_column"],
                }
            ],
            "anti_overfit_statement": "All ids, column ownership and geometry are current-page rule evidence.",
        }
        records.append({"rule_decision": decision, "repair_patch": patch, "application": application})
        if repaired == current or application["status"] != "applied":
            raise RuntimeError("multi_template_repair_patch_not_applied")
        current = repaired
    else:
        remaining = evaluate_cross_column_extraction_merge(facts=facts, template=current)
        if remaining["rule_verdict"] == "FAIL":
            raise RuntimeError("multi_template_repair_loop_exhausted_page_bound")

    # 提取块拆分稳定后，再单独裁决“局部多栏结束后是否已转回页尾单流”。
    postlude_decision = evaluate_trailing_postlude_ownership(template=current)
    if postlude_decision["rule_verdict"] == "FAIL":
        repaired, application = apply_trailing_postlude_reassignment(
            template=current,
            container_ids=[str(item) for item in postlude_decision["container_ids"]],
        )
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": postlude_decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": "multi_column_template_ownership",
                "selected_repair_atom": postlude_decision["repair_atom"],
                "bound_tool": "tools/repairs/trailing_postlude_reassignment.py",
            },
            "operations": [
                {
                    "operation_type": postlude_decision["repair_atom"],
                    "container_ids": postlude_decision["container_ids"],
                }
            ],
            "anti_overfit_statement": "All ids and transition evidence come from the current page.",
        }
        records.append({"rule_decision": postlude_decision, "repair_patch": patch, "application": application})
        if repaired == current or application["status"] != "applied":
            raise RuntimeError("trailing_postlude_repair_patch_not_applied")
        current = repaired
        if evaluate_trailing_postlude_ownership(template=current)["rule_verdict"] == "FAIL":
            raise RuntimeError("trailing_postlude_repair_did_not_clear_disease")

    # 所有权稳定后，逐对合并同栏/同跨栏区的源行碎片；每轮仍只执行一个修复原子。
    # 行距阈值必须取自合并前的完整源行分布，不能随容器数量下降而重新收紧。
    owner_line_gap_limits = derive_owner_line_gap_limits(current)
    for _ in range(max(1, len(current.containers))):
        fragment_decision = evaluate_semantic_paragraph_fragmentation(
            template=current,
            owner_line_gap_limits=owner_line_gap_limits,
        )
        if fragment_decision["rule_verdict"] != "FAIL":
            break
        repaired, application = apply_semantic_paragraph_fragment_merge(
            template=current,
            previous_container_id=str(fragment_decision["previous_container_id"]),
            current_container_id=str(fragment_decision["current_container_id"]),
        )
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": fragment_decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": "semantic_source_region_grouping",
                "selected_repair_atom": fragment_decision["repair_atom"],
                "bound_tool": "tools/repairs/semantic_paragraph_fragment_merge.py",
            },
            "operations": [
                {
                    "operation_type": fragment_decision["repair_atom"],
                    "previous_container_id": fragment_decision["previous_container_id"],
                    "current_container_id": fragment_decision["current_container_id"],
                }
            ],
            "anti_overfit_statement": "The pair and geometry are selected from current-page semantic-flow evidence.",
        }
        records.append({"rule_decision": fragment_decision, "repair_patch": patch, "application": application})
        if repaired == current or application["status"] != "applied":
            raise RuntimeError("semantic_fragment_merge_patch_not_applied")
        current = repaired
    else:
        if evaluate_semantic_paragraph_fragmentation(
            template=current,
            owner_line_gap_limits=owner_line_gap_limits,
        )["rule_verdict"] == "FAIL":
            raise RuntimeError("semantic_fragment_merge_loop_exhausted_page_bound")
    return current, tuple(records)
