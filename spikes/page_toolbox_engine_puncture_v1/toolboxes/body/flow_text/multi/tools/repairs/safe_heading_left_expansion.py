"""
tool_name: safe_heading_left_expansion
category: repair executor
input_contract: one MultiColumnLayoutPlan and one rule-proven safe left boundary for a page-level heading
output_contract: the same plan with only the selected heading x0 expanded leftward
failure_signals: target missing, rightward/shrinking request, or page escape
fallback: reject the patch and retain the previous plan
anti_overfit_statement: target and safe left boundary come from current-page obstacle evidence; x1 and unrelated placements remain unchanged
"""

from __future__ import annotations

from dataclasses import replace

from ..models import MultiColumnLayoutPlan


def apply_safe_heading_left_expansion(
    plan: MultiColumnLayoutPlan,
    *,
    container_id: str,
    safe_left: float,
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    placements = list(plan.placements)
    index = next((index for index, item in enumerate(placements) if item.container_id == container_id), None)
    if index is None:
        raise ValueError("safe_heading_left_target_missing")
    target = placements[index]
    current_left, y0, right, y1 = target.output_bbox
    if safe_left >= current_left - 0.01 or safe_left < 0.0:
        raise ValueError("safe_heading_left_does_not_expand")
    placements[index] = replace(
        target,
        output_bbox=(round(safe_left, 4), y0, right, y1),
        horizontal_policy="safe_heading_left_whitespace_expand",
    )
    return replace(plan, placements=tuple(placements)), {
        "operation_type": "safe_heading_left_expansion",
        "status": "applied",
        "container_id": container_id,
        "before_left": current_left,
        "after_left": round(safe_left, 4),
        "hard_constraints": {
            "right_anchor_unchanged": True,
            "vertical_bbox_unchanged": True,
            "ordinary_body_unchanged": True,
        },
    }
