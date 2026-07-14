"""
tool_name: safe_heading_width_expansion / safe_flow_width_expansion
category: repair executor
input_contract: one MultiColumnLayoutPlan and one rule-proven safe right boundary
output_contract: the same plan with only the selected placement x1 expanded
failure_signals: target missing, leftward/shrinking request, or region escape
fallback: reject the patch and retain the previous plan
anti_overfit_statement: target, owner scope and safe right boundary come from the current rule decision; x0 and all unrelated placements remain unchanged
"""

from __future__ import annotations

from dataclasses import replace

from ..models import MultiColumnLayoutPlan


def apply_safe_heading_width_expansion(
    plan: MultiColumnLayoutPlan,
    *,
    container_id: str,
    safe_right: float,
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    return _apply_safe_width_expansion(
        plan,
        container_id=container_id,
        safe_right=safe_right,
        horizontal_policy="safe_heading_whitespace_expand",
        operation_type="safe_heading_width_expansion",
    )


def apply_safe_flow_width_expansion(
    plan: MultiColumnLayoutPlan,
    *,
    container_id: str,
    safe_right: float,
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    return _apply_safe_width_expansion(
        plan,
        container_id=container_id,
        safe_right=safe_right,
        horizontal_policy="safe_flow_whitespace_expand",
        operation_type="safe_flow_width_expansion",
    )


def _apply_safe_width_expansion(
    plan: MultiColumnLayoutPlan,
    *,
    container_id: str,
    safe_right: float,
    horizontal_policy: str,
    operation_type: str,
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    placements = list(plan.placements)
    index = next((index for index, item in enumerate(placements) if item.container_id == container_id), None)
    if index is None:
        raise ValueError("safe_heading_width_target_missing")
    target = placements[index]
    x0, y0, current_right, y1 = target.output_bbox
    if safe_right <= current_right + 0.01:
        raise ValueError("safe_heading_width_does_not_expand")
    placements[index] = replace(
        target,
        output_bbox=(x0, y0, round(safe_right, 4), y1),
        horizontal_policy=horizontal_policy,
    )
    return replace(plan, placements=tuple(placements)), {
        "operation_type": operation_type,
        "status": "applied",
        "container_id": container_id,
        "before_right": current_right,
        "after_right": round(safe_right, 4),
        "hard_constraints": {
            "left_anchor_unchanged": True,
            "vertical_bbox_unchanged": True,
            "unrelated_placements_unchanged": True,
        },
    }
