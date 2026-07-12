"""
tool_name: paragraph_density_rebalance
category: repair executor
input_contract: P4LayoutPlan, current-run density targets, page geometry, and source-derived line-height requests
output_contract: repaired P4LayoutPlan with vertically reflowed downstream text and fit evidence
failure_signals: missing target, no vertical room, page/footer escape, or new overlap
fallback: retain the previous candidate through the caller's rollback path
anti_overfit_statement: targets and requested line heights come only from current-run source/candidate metrics
"""

from __future__ import annotations

from dataclasses import replace

from ..p4_layout_planner import _font_variant, _minimum_text_height
from ..p4_models import P4LayoutPlan


def apply_paragraph_density_rebalance(
    plan: P4LayoutPlan,
    *,
    page_width: float,
    page_height: float,
    requested_line_heights: dict[str, float],
) -> tuple[P4LayoutPlan, dict[str, object]]:
    known_ids = {item.container_id for item in plan.placements}
    missing = sorted(set(requested_line_heights) - known_ids)
    if missing:
        raise ValueError(f"paragraph_density_target_missing:{','.join(missing)}")
    if not requested_line_heights:
        raise ValueError("paragraph_density_targets_required")
    for container_id, value in requested_line_heights.items():
        current = next(item.line_height for item in plan.placements if item.container_id == container_id)
        if value <= current:
            raise ValueError(f"paragraph_density_line_height_must_increase:{container_id}")

    best_plan = plan
    best_strength = 0.0
    low, high = 0.0, 1.0
    for _ in range(9):
        strength = (low + high) / 2.0
        candidate = _apply_strength(
            plan,
            page_width=page_width,
            page_height=page_height,
            requested_line_heights=requested_line_heights,
            strength=strength,
        )
        if _fits(candidate):
            best_plan = candidate
            best_strength = strength
            low = strength
        else:
            high = strength
    if best_strength <= 0.01:
        raise RuntimeError("paragraph_density_rebalance_has_no_vertical_room")

    before = {item.container_id: item for item in plan.placements}
    after = {item.container_id: item for item in best_plan.placements}
    target_evidence = []
    for container_id, requested in requested_line_heights.items():
        target_evidence.append(
            {
                "container_id": container_id,
                "before_line_height": before[container_id].line_height,
                "requested_line_height": requested,
                "applied_line_height": after[container_id].line_height,
                "before_bbox": before[container_id].output_bbox,
                "after_bbox": after[container_id].output_bbox,
            }
        )
    main_before = [item for item in plan.placements if item.role != "margin"]
    main_after = [item for item in best_plan.placements if item.role != "margin"]
    return best_plan, {
        "operation_type": "font_size_and_region_density_rebalance",
        "status": "applied",
        "strength": round(best_strength, 4),
        "targets": target_evidence,
        "before_last_bottom_pt": round(main_before[-1].output_bbox[3], 4),
        "after_last_bottom_pt": round(main_after[-1].output_bbox[3], 4),
        "content_bottom_guard_pt": plan.content_bottom,
        "hard_constraints": {
            "font_sizes_unchanged": True,
            "x_coordinates_unchanged": True,
            "widths_unchanged": True,
            "margin_objects_unchanged": True,
        },
    }


def _apply_strength(
    plan: P4LayoutPlan,
    *,
    page_width: float,
    page_height: float,
    requested_line_heights: dict[str, float],
    strength: float,
) -> P4LayoutPlan:
    placements = []
    downstream_shift = 0.0
    for placement in plan.placements:
        if placement.role == "margin":
            placements.append(placement)
            continue
        x0, y0, x1, y1 = placement.output_bbox
        original_height = y1 - y0
        target_line_height = placement.line_height
        if placement.container_id in requested_line_heights:
            requested = requested_line_heights[placement.container_id]
            target_line_height += (requested - placement.line_height) * strength
            font_file, font_resource = _font_variant(plan.font_file, plan.font_resource, placement.font_weight)
            target_height = _minimum_text_height(
                page_width=page_width,
                page_height=page_height,
                width=x1 - x0,
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=target_line_height,
                font_file=font_file,
                font_resource=font_resource,
                color_srgb=placement.color_srgb,
            )
        else:
            target_height = original_height
        target_y0 = y0 + downstream_shift
        target_bbox = (x0, target_y0, x1, target_y0 + target_height)
        placements.append(
            replace(
                placement,
                output_bbox=tuple(round(value, 4) for value in target_bbox),
                line_height=round(target_line_height, 4),
                vertical_policy=f"{placement.vertical_policy}+paragraph_density_rebalance" if placement.container_id in requested_line_heights else placement.vertical_policy,
            )
        )
        downstream_shift += target_height - original_height
    return replace(plan, placements=tuple(placements))


def _fits(plan: P4LayoutPlan) -> bool:
    main = [item for item in plan.placements if item.role != "margin"]
    if not main or main[-1].output_bbox[3] > plan.content_bottom + 0.01:
        return False
    return all(second.output_bbox[1] >= first.output_bbox[3] - 0.01 for first, second in zip(main, main[1:]))
