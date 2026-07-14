"""
tool_name: deterministic_multi_layout_repair_loop
category: orchestrator
input_contract: current PageFacts, repaired MultiColumnTemplate and valid MultiColumnLayoutPlan
output_contract: repaired plan plus ordered one-rule/one-patch/application records
failure_signals: selected patch cannot be applied or remains actionable after the page-derived bound
fallback: caller records capability failure rather than silently accepting the disease
anti_overfit_statement: each iteration dispatches one registered failure class to one repair atom using current-page evidence
"""

from __future__ import annotations

from page_toolbox_puncture.contracts import PageFacts

from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..repairs.post_heading_width_vertical_reflow import apply_post_heading_width_vertical_reflow
from ..repairs.safe_heading_left_expansion import apply_safe_heading_left_expansion
from ..repairs.safe_heading_width_expansion import (
    apply_safe_flow_width_expansion,
    apply_safe_heading_width_expansion,
)
from ..validators.avoidable_short_line_wrap_rule import evaluate_avoidable_short_line_wrap


def apply_deterministic_multi_layout_repairs(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> tuple[MultiColumnLayoutPlan, tuple[dict[str, object], ...]]:
    current = plan
    records: list[dict[str, object]] = []
    # 每轮只消除一个“安全空白未利用”病因实例，之后重新测量实际行数。
    for _ in range(max(1, len(plan.placements))):
        decision = evaluate_avoidable_short_line_wrap(facts=facts, template=template, plan=current)
        if decision["rule_verdict"] != "FAIL":
            break
        if decision.get("expansion_direction") == "left":
            repaired, application = apply_safe_heading_left_expansion(
                current,
                container_id=str(decision["container_id"]),
                safe_left=float(decision["safe_left"]),
            )
            bound_tool = "tools/repairs/safe_heading_left_expansion.py"
        elif decision.get("repair_atom") == "safe_flow_width_expansion":
            repaired, application = apply_safe_flow_width_expansion(
                current,
                container_id=str(decision["container_id"]),
                safe_right=float(decision["safe_right"]),
            )
            bound_tool = "tools/repairs/safe_heading_width_expansion.py"
        else:
            repaired, application = apply_safe_heading_width_expansion(
                current,
                container_id=str(decision["container_id"]),
                safe_right=float(decision["safe_right"]),
            )
            bound_tool = "tools/repairs/safe_heading_width_expansion.py"
        patch = {
            "schema_version": "repair-patch/v1",
            "patch_verdict": "PATCH_READY",
            "selected_failure_class": decision["selected_failure_class"],
            "dispatch_result": {
                "dispatch_table": "contracts/failure_dispatch_table.json",
                "selected_repair_family": (
                    "flow_safe_space_reflow"
                    if decision.get("repair_atom") == "safe_flow_width_expansion"
                    else "heading_safe_space_reflow"
                ),
                "selected_repair_atom": decision["repair_atom"],
                "bound_tool": bound_tool,
            },
            "operations": [
                {
                    "operation_type": decision["repair_atom"],
                    "container_id": decision["container_id"],
                    "expansion_direction": decision.get("expansion_direction", "right"),
                    "safe_boundary": decision.get("safe_left", decision.get("safe_right")),
                    "current_line_count": decision["current_line_count"],
                    "safe_line_count": decision["safe_line_count"],
                }
            ],
            "anti_overfit_statement": "Safe width and line-count improvement are current-page measured evidence.",
        }
        records.append({"rule_decision": decision, "repair_patch": patch, "application": application})
        current = repaired
    else:
        remaining = evaluate_avoidable_short_line_wrap(facts=facts, template=template, plan=current)
        if remaining["rule_verdict"] == "FAIL":
            raise RuntimeError("multi_layout_repair_loop_exhausted_page_bound")
    if records:
        has_flow_width_expansion = any(
            item["rule_decision"].get("repair_atom") == "safe_flow_width_expansion"
            for item in records
        )
        post_failure_class = (
            "post_flow_width_vertical_staleness"
            if has_flow_width_expansion
            else "post_heading_width_vertical_staleness"
        )
        post_repair_atom = (
            "post_flow_width_vertical_reflow"
            if has_flow_width_expansion
            else "post_heading_width_vertical_reflow"
        )
        repaired, application = apply_post_heading_width_vertical_reflow(
            template=template,
            plan=current,
        )
        records.append(
            {
                "rule_decision": {
                    "rule_verdict": "FAIL",
                    "selected_failure_class": post_failure_class,
                    "repair_atom": post_repair_atom,
                    "evidence": {
                        "safe_width_expansion_count": len(records),
                        "dependent_vertical_flow_requires_refresh": True,
                    },
                },
                "repair_patch": {
                    "schema_version": "repair-patch/v1",
                    "patch_verdict": "PATCH_READY",
                    "selected_failure_class": post_failure_class,
                    "dispatch_result": {
                        "dispatch_table": "contracts/failure_dispatch_table.json",
                        "selected_repair_family": (
                            "flow_safe_space_reflow"
                            if has_flow_width_expansion
                            else "heading_safe_space_reflow"
                        ),
                        "selected_repair_atom": post_repair_atom,
                        "bound_tool": "tools/repairs/post_heading_width_vertical_reflow.py",
                    },
                    "operations": [{"operation_type": post_repair_atom}],
                    "anti_overfit_statement": "All heights, gaps and owner-local bounds are remeasured from the current plan.",
                },
                "application": application,
            }
        )
        current = repaired
    return current, tuple(records)
