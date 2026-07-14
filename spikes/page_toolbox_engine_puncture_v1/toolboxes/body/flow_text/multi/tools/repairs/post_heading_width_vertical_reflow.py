"""
tool_name: post_heading_width_vertical_reflow
category: repair executor
input_contract: one MultiColumnTemplate and a plan containing rule-proven safe flow-text width expansions
output_contract: the same plan with fresh text heights and owner-local vertical flow
failure_signals: expanded target missing or a reflowed placement leaves its page/column boundary
fallback: reject the candidate as product failure
anti_overfit_statement: all widths, gaps, font metrics, owners and bounds come from the current template and plan
"""

from __future__ import annotations

from dataclasses import replace

from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _minimum_text_height

from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..layout_pattern import infer_multi_band_variant
from ..probes.structural_anchor_probe import structural_zone


def apply_post_heading_width_vertical_reflow(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    """标题安全扩宽后重新计算实际高度，再只沿所属页首流/栏流纵向回排。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    container_by_id = {item.container_id: item for item in template.containers}
    placement = {item.container_id: item for item in plan.placements}
    expanded_ids = [
        item.container_id for item in plan.placements
        if item.horizontal_policy in {
            "safe_heading_whitespace_expand",
            "safe_heading_left_whitespace_expand",
            "safe_flow_whitespace_expand",
        }
    ]
    if not expanded_ids:
        raise ValueError("post_width_reflow_has_no_expanded_flow_text")

    for container_id in expanded_ids:
        current = placement[container_id]
        font_file, font_resource = _font_variant(plan.font_file, plan.font_resource, current.font_weight)
        x0, y0, x1, _ = current.output_bbox
        height = _minimum_text_height(
            template.width,
            template.height,
            x1 - x0,
            current.translated_text,
            current.font_size,
            current.line_height,
            font_file,
            font_resource,
            current.color_srgb,
        )
        placement[container_id] = replace(current, output_bbox=(x0, y0, x1, round(y0 + height, 4)))

    structure_top = min(item.content_top for item in template.columns)
    content_bands = [item for item in plan.flow_bands if item.mode in {"single", "multi"}]
    if content_bands:
        first_multi_index = next(
            (index for index, item in enumerate(content_bands) if item.mode == "multi"),
            len(content_bands),
        )
        top_span_ids = [
            container_id
            for band in content_bands[:first_multi_index]
            if band.mode == "single"
            for container_id in band.container_ids
        ]
    else:
        top_span_ids = [
            item.container_id for item in template.containers
            if assignment[item.container_id] == "span"
            and item.source_bbox[1] <= structure_top + template.height * 0.04
        ]
    late_span_ids = [
        item.container_id for item in template.containers
        if assignment[item.container_id] == "span" and item.container_id not in top_span_ids
    ]
    _reflow_top_spans(top_span_ids, placement, container_by_id, template.height)
    span_group_fits = _fit_top_span_groups(
        template=template,
        plan=plan,
        top_span_ids=top_span_ids,
        placement=placement,
        container_by_id=container_by_id,
        assignment=assignment,
    )
    span_floor = max((placement[item].output_bbox[3] for item in top_span_ids), default=0.0)

    if content_bands:
        _reflow_single_content_bands(
            content_bands=content_bands,
            placement=placement,
            container_by_id=container_by_id,
            page_height=template.height,
        )
        from ..layout_planner import _reflow_repeated_content_bands

        reflowed, band_findings = _reflow_repeated_content_bands(
            template=template,
            flow_bands=plan.flow_bands,
            placements=[
                placement[item.container_id]
                for item in template.containers
            ],
        )
        placement.update((item.container_id, item) for item in reflowed)
        ordered = tuple(placement[item.container_id] for item in template.containers)
        return replace(plan, placements=ordered), {
            "operation_type": "post_heading_width_vertical_reflow",
            "status": "applied",
            "expanded_container_ids": expanded_ids,
            "reflowed_top_span_count": len(top_span_ids),
            "top_span_group_fits": span_group_fits,
            "reflowed_content_band_count": len(content_bands),
            "band_reflow_finding_count": len(band_findings),
            "hard_constraints": {
                "left_anchors_unchanged": True,
                "column_widths_unchanged": True,
                "owner_unchanged": True,
                "flow_band_order_unchanged": True,
            },
        }

    paired_rows = infer_multi_band_variant(template) == "paired_row_columns"
    if not paired_rows:
        for column in template.columns:
            ids = [
                item.container_id for item in template.containers
                if assignment[item.container_id] == column.column_id
            ]
            cursor: float | None = None
            for container_id in ids:
                current = placement[container_id]
                source = container_by_id[container_id]
                height = current.output_bbox[3] - current.output_bbox[1]
                if cursor is None:
                    y0 = (
                        max(source.source_bbox[1], span_floor + max(0.0, source.source_bbox[1] - column.content_top))
                        if span_floor > source.source_bbox[1]
                        else source.source_bbox[1]
                    )
                else:
                    y0 = cursor + current.target_gap
                y1 = y0 + height
                placement[container_id] = replace(
                    current,
                    output_bbox=(current.output_bbox[0], round(y0, 4), current.output_bbox[2], round(y1, 4)),
                    vertical_policy=current.vertical_policy + "+post_heading_width_reflow",
                    fit=y1 <= column.content_bottom + 0.01,
                )
                cursor = y1

    if late_span_ids:
        column_ids = {item.column_id for item in template.columns}
        source_floor = max(
            item.source_bbox[3] for item in template.containers
            if assignment[item.container_id] in column_ids
        )
        candidate_floor = max(
            placement[item.container_id].output_bbox[3] for item in template.containers
            if assignment[item.container_id] in column_ids
        )
        source_cursor = source_floor
        cursor = candidate_floor
        for container_id in late_span_ids:
            current = placement[container_id]
            source = container_by_id[container_id]
            height = current.output_bbox[3] - current.output_bbox[1]
            source_gap = max(0.0, source.source_bbox[1] - source_cursor)
            y0 = max(source.source_bbox[1], cursor + source_gap)
            y1 = y0 + height
            placement[container_id] = replace(
                current,
                output_bbox=(current.output_bbox[0], round(y0, 4), current.output_bbox[2], round(y1, 4)),
                vertical_policy=current.vertical_policy + "+post_heading_width_reflow",
                fit=y1 <= template.height - 20.0,
            )
            source_cursor = source.source_bbox[3]
            cursor = y1

    ordered = tuple(placement[item.container_id] for item in template.containers)
    return replace(plan, placements=ordered), {
        "operation_type": "post_heading_width_vertical_reflow",
        "status": "applied",
        "expanded_container_ids": expanded_ids,
        "reflowed_top_span_count": len(top_span_ids),
        "top_span_group_fits": span_group_fits,
        "reflowed_column_count": 0 if paired_rows else len(template.columns),
        "skipped_column_reason": "paired_row_columns_use_synchronous_reflow" if paired_rows else None,
        "hard_constraints": {
            "left_anchors_unchanged": True,
            "column_widths_unchanged": True,
            "owner_unchanged": True,
        },
    }


def _reflow_top_spans(top_span_ids, placement, container_by_id, page_height: float) -> None:
    cursor: float | None = None
    for container_id in top_span_ids:
        current = placement[container_id]
        source = container_by_id[container_id]
        height = current.output_bbox[3] - current.output_bbox[1]
        y0 = source.source_bbox[1] if cursor is None else max(source.source_bbox[1], cursor + current.target_gap)
        y1 = y0 + height
        placement[container_id] = replace(
            current,
            output_bbox=(current.output_bbox[0], round(y0, 4), current.output_bbox[2], round(y1, 4)),
            vertical_policy=current.vertical_policy + "+post_heading_width_reflow",
            fit=y1 <= page_height - 20.0,
        )
        cursor = y1


def _reflow_single_content_bands(
    *,
    content_bands,
    placement: dict,
    container_by_id: dict,
    page_height: float,
) -> None:
    for band in content_bands:
        if band.mode != "single":
            continue
        cursor: float | None = None
        for container_id in sorted(
            band.container_ids,
            key=lambda item: (
                container_by_id[item].source_bbox[1],
                container_by_id[item].source_bbox[0],
            ),
        ):
            current = placement[container_id]
            height = current.output_bbox[3] - current.output_bbox[1]
            y0 = (
                current.output_bbox[1]
                if cursor is None
                else max(current.output_bbox[1], cursor + current.target_gap)
            )
            y1 = y0 + height
            placement[container_id] = replace(
                current,
                output_bbox=(current.output_bbox[0], round(y0, 4), current.output_bbox[2], round(y1, 4)),
                vertical_policy=current.vertical_policy + "+post_heading_band_local_reflow",
                fit=y1 <= page_height - 20.0,
            )
            cursor = y1


def _fit_top_span_groups(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    top_span_ids: list[str],
    placement: dict,
    container_by_id: dict,
    assignment: dict[str, str],
) -> list[dict[str, object]]:
    """页首独立语义组只在当前结构带内按相对字号整体收敛，避免穿越栏目起点或分隔线。"""

    if not top_span_ids:
        return []
    groups: list[list[str]] = []
    for container_id in top_span_ids:
        if groups:
            previous = container_by_id[groups[-1][-1]]
            current = container_by_id[container_id]
            source_gap = current.source_bbox[1] - previous.source_bbox[3]
            if source_gap > max(previous.font_size, current.font_size) * 4.0:
                groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(container_id)

    first_column_source_top = min(
        item.source_bbox[1] for item in template.containers
        if assignment[item.container_id].startswith("column-")
    )
    results: list[dict[str, object]] = []
    for index, group in enumerate(groups):
        first_source = container_by_id[group[0]]
        if index + 1 < len(groups):
            guard = container_by_id[groups[index + 1][0]].source_bbox[1]
        else:
            guard = first_column_source_top
        if plan.structural_anchors:
            _, _, anchor_upper = structural_zone(first_source.source_bbox, plan.structural_anchors, template.height)
            guard = min(guard, anchor_upper)
        current_bottom = max(placement[item].output_bbox[3] for item in group)
        if current_bottom <= guard + 0.01:
            results.append({"container_ids": group, "font_scale": 1.0, "guard": round(guard, 4), "fit": True})
            continue

        fitted = False
        for scale in (0.98, 0.95, 0.92, 0.88, 0.84, 0.80, 0.75, 0.72):
            simulated: dict[str, tuple[float, float, float]] = {}
            cursor: float | None = None
            for container_id in group:
                current = placement[container_id]
                source = container_by_id[container_id]
                font_size = max(6.0, current.source_font_size * scale)
                font_file, font_resource = _font_variant(plan.font_file, plan.font_resource, current.font_weight)
                width = current.output_bbox[2] - current.output_bbox[0]
                height = _minimum_text_height(template.width, template.height, width, current.translated_text, font_size, current.line_height, font_file, font_resource, current.color_srgb)
                target_gap = current.target_gap * scale
                y0 = source.source_bbox[1] if cursor is None else max(source.source_bbox[1], cursor + target_gap)
                y1 = y0 + height
                simulated[container_id] = (font_size, y0, y1)
                cursor = y1
            if cursor is None or cursor > guard + 0.01:
                continue
            for container_id, (font_size, y0, y1) in simulated.items():
                current = placement[container_id]
                placement[container_id] = replace(
                    current,
                    output_bbox=(current.output_bbox[0], round(y0, 4), current.output_bbox[2], round(y1, 4)),
                    font_size=round(font_size, 4),
                    target_gap=round(current.target_gap * scale, 4),
                    vertical_policy=current.vertical_policy + "+structural_anchor_zone_fit",
                    fit=True,
                )
            results.append({"container_ids": group, "font_scale": scale, "guard": round(guard, 4), "fit": True})
            fitted = True
            break
        if not fitted:
            results.append({"container_ids": group, "font_scale": None, "guard": round(guard, 4), "fit": False})
    return results
