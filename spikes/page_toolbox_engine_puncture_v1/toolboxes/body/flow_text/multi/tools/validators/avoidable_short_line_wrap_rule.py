"""
tool_name: avoidable_short_line_wrap_rule
category: validators
input_contract: current PageFacts, repaired MultiColumnTemplate and MultiColumnLayoutPlan
output_contract: PASS or one avoidable flow-text wrap finding
failure_signals: a flow-text container wraps more lines than it would inside its proven single-region or owner-column boundary
fallback: retain source width when the wider region is obstructed or still cannot reduce wrapping
anti_overfit_statement: eligibility, free width and line counts are derived from current-page semantics, geometry and current translation
"""

from __future__ import annotations

from page_toolbox_puncture.contracts import PageFacts
from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _rendered_lines

from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..template_builder import _page_background_image_ids


def evaluate_avoidable_short_line_wrap(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> dict[str, object]:
    """一次只返回一个可由安全横向空白消除的 flow-text 换行。"""

    by_id = {item.container_id: item for item in template.containers}
    assignment = {item.container_id: item.column_id for item in template.assignments}
    columns = {item.column_id: item for item in template.columns}
    for placement in plan.placements:
        container = by_id[placement.container_id]
        column_id = assignment[placement.container_id]
        # fixed / margin 是锁定覆盖层，不属于普通 span 或 column 文字流。
        if column_id != "span" and column_id not in columns:
            continue
        font_file, font_resource = _font_variant(
            plan.font_file,
            plan.font_resource,
            placement.font_weight,
        )
        current_lines = _rendered_lines(
            page_width=template.width,
            page_height=template.height,
            width=placement.output_bbox[2] - placement.output_bbox[0],
            height=placement.output_bbox[3] - placement.output_bbox[1],
            text=placement.translated_text,
            font_size=placement.font_size,
            line_height=placement.line_height,
            font_file=font_file,
            font_resource=font_resource,
            color_srgb=placement.color_srgb,
        )
        current_right = placement.output_bbox[2]
        region_right = max(item.right for item in template.columns) if column_id == "span" else columns[column_id].right
        safe_right = _safe_right_bound(
            facts=facts,
            template=template,
            container_id=container.container_id,
            current_right=current_right,
            region_right=region_right,
            vertical_band=container.source_bbox[1:4:2],
            typographic_scale=placement.font_size,
        )
        if safe_right > current_right + 0.01:
            safe_lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=safe_right - placement.output_bbox[0],
                height=placement.output_bbox[3] - placement.output_bbox[1],
                text=placement.translated_text,
                font_size=placement.font_size,
                line_height=placement.line_height,
                font_file=font_file,
                font_resource=font_resource,
                color_srgb=placement.color_srgb,
            )
        else:
            safe_lines = current_lines
        if len(current_lines) > len(safe_lines):
            heading = container.role == "heading"
            return {
                "rule_verdict": "FAIL",
                "selected_failure_class": (
                    "avoidable_short_line_wrap_with_safe_space"
                    if heading
                    else "avoidable_flow_wrap_with_safe_space"
                ),
                "repair_atom": (
                    "safe_heading_width_expansion"
                    if heading
                    else "safe_flow_width_expansion"
                ),
                "horizontal_policy": (
                    "safe_heading_whitespace_expand"
                    if heading
                    else "safe_flow_whitespace_expand"
                ),
                "expansion_direction": "right",
                "container_id": placement.container_id,
                "column_id": column_id,
                "source_right": container.source_bbox[2],
                "current_right": current_right,
                "safe_right": round(safe_right, 4),
                "current_line_count": len(current_lines),
                "safe_line_count": len(safe_lines),
                "evidence": {
                    "left_anchor_unchanged": True,
                    "safe_region_has_no_material_obstacle": True,
                    "boundary_scope": "single_text_region" if column_id == "span" else "owner_column",
                    "source_glyph_width_is_not_a_hard_boundary": True,
                },
            }
        if column_id == "span" and container.role == "heading":
            current_left = placement.output_bbox[0]
            safe_left = _safe_left_bound(
                facts=facts,
                template=template,
                container_id=container.container_id,
                current_left=current_left,
                region_left=min(item.left for item in template.columns),
                vertical_band=container.source_bbox[1:4:2],
                typographic_scale=placement.font_size,
            )
            if safe_left < current_left - 0.01:
                left_lines = _rendered_lines(
                    page_width=template.width,
                    page_height=template.height,
                    width=placement.output_bbox[2] - safe_left,
                    height=placement.output_bbox[3] - placement.output_bbox[1],
                    text=placement.translated_text,
                    font_size=placement.font_size,
                    line_height=placement.line_height,
                    font_file=font_file,
                    font_resource=font_resource,
                    color_srgb=placement.color_srgb,
                )
                if len(current_lines) > len(left_lines):
                    return {
                        "rule_verdict": "FAIL",
                        "selected_failure_class": "avoidable_right_anchored_heading_wrap_with_safe_left_space",
                        "repair_atom": "safe_heading_left_expansion",
                        "expansion_direction": "left",
                        "container_id": placement.container_id,
                        "column_id": column_id,
                        "source_left": container.source_bbox[0],
                        "current_left": current_left,
                        "safe_left": round(safe_left, 4),
                        "current_line_count": len(current_lines),
                        "safe_line_count": len(left_lines),
                        "evidence": {
                            "right_anchor_unchanged": True,
                            "safe_region_has_no_material_obstacle": True,
                            "single_band_heading_is_eligible": True,
                        },
                    }
    return {
        "rule_verdict": "PASS",
        "selected_failure_class": None,
        "repair_atom": None,
    }


def _safe_right_bound(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    container_id: str,
    current_right: float,
    region_right: float,
    vertical_band: tuple[float, float],
    typographic_scale: float,
) -> float:
    safe_right = region_right
    clearance = typographic_scale * 0.50
    target_container = next(item for item in template.containers if item.container_id == container_id)
    source_ids = set(target_container.source_object_ids)
    text_obstacles = [
        (item.bbox, item.font_size) for item in facts.text_objects if item.object_id not in source_ids
    ]
    page_area = max(facts.width * facts.height, 1.0)
    background_image_ids = _page_background_image_ids(facts)
    locked_obstacles = [
        item.bbox
        for item in facts.image_objects
        if item.object_id not in background_image_ids
        and not _materially_underlays(target_container.source_bbox, item.bbox)
        and ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])) / page_area < 0.45
    ] + [
        item.bbox
        for item in facts.drawing_objects
        if ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])) / page_area < 0.45
    ]
    y0, y1 = vertical_band
    target_center = (y0 + y1) / 2.0
    for obstacle, obstacle_font_size in text_obstacles:
        ox0, oy0, ox1, oy1 = obstacle
        obstacle_center = (oy0 + oy1) / 2.0
        same_visual_line = abs(obstacle_center - target_center) <= max(typographic_scale, obstacle_font_size) * 0.75
        if not same_visual_line or ox1 <= current_right or ox0 >= safe_right:
            continue
        if ox0 <= current_right:
            return current_right
        safe_right = min(safe_right, ox0 - clearance)
    for obstacle in locked_obstacles:
        ox0, oy0, ox1, oy1 = obstacle
        vertical_overlap = max(0.0, min(y1, oy1) - max(y0, oy0))
        if vertical_overlap < min(y1 - y0, oy1 - oy0) * 0.25 or ox1 <= current_right or ox0 >= safe_right:
            continue
        if ox0 <= current_right:
            return current_right
        safe_right = min(safe_right, ox0 - clearance)
    return round(max(current_right, safe_right), 4)


def _safe_left_bound(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    container_id: str,
    current_left: float,
    region_left: float,
    vertical_band: tuple[float, float],
    typographic_scale: float,
) -> float:
    safe_left = region_left
    clearance = typographic_scale * 0.50
    target_container = next(item for item in template.containers if item.container_id == container_id)
    source_ids = set(target_container.source_object_ids)
    text_obstacles = [(item.bbox, item.font_size) for item in facts.text_objects if item.object_id not in source_ids]
    page_area = max(facts.width * facts.height, 1.0)
    background_image_ids = _page_background_image_ids(facts)
    locked_obstacles = [
        item.bbox
        for item in facts.image_objects
        if item.object_id not in background_image_ids
        and not _materially_underlays(target_container.source_bbox, item.bbox)
        and ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])) / page_area < 0.45
    ] + [
        item.bbox
        for item in facts.drawing_objects
        if ((item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])) / page_area < 0.45
    ]
    y0, y1 = vertical_band
    target_center = (y0 + y1) / 2.0
    for obstacle, obstacle_font_size in text_obstacles:
        ox0, oy0, ox1, oy1 = obstacle
        obstacle_center = (oy0 + oy1) / 2.0
        same_visual_line = abs(obstacle_center - target_center) <= max(typographic_scale, obstacle_font_size) * 0.75
        if not same_visual_line or ox0 >= current_left or ox1 <= safe_left:
            continue
        if ox1 >= current_left:
            return current_left
        safe_left = max(safe_left, ox1 + clearance)
    for obstacle in locked_obstacles:
        ox0, oy0, ox1, oy1 = obstacle
        vertical_overlap = max(0.0, min(y1, oy1) - max(y0, oy0))
        if vertical_overlap < min(y1 - y0, oy1 - oy0) * 0.25 or ox0 >= current_left or ox1 <= safe_left:
            continue
        if ox1 >= current_left:
            return current_left
        safe_left = max(safe_left, ox1 + clearance)
    return round(min(current_left, safe_left), 4)


def _materially_underlays(
    target: tuple[float, float, float, float],
    visual: tuple[float, float, float, float],
) -> bool:
    target_width = max(target[2] - target[0], 1.0)
    target_height = max(target[3] - target[1], 1.0)
    horizontal_overlap = max(0.0, min(target[2], visual[2]) - max(target[0], visual[0]))
    vertical_overlap = max(0.0, min(target[3], visual[3]) - max(target[1], visual[1]))
    return horizontal_overlap >= target_width * 0.60 and vertical_overlap >= target_height * 0.25
