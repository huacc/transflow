"""
tool_name: rendered_semantic_spacing_reflow
category: repair executor
input_contract: one rendered semantic-spacing failure and its current multi-column layout plan
output_contract: the same plan with only the later owner-local flow shifted vertically
failure_signals: missing transition target, unsupported owner, zero correction, or structural-boundary escape
fallback: retain the current plan and fail product quality
anti_overfit_statement: correction distance comes only from current source/candidate rhythm ratios and current-page geometry
"""

from __future__ import annotations

from dataclasses import replace

from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _minimum_text_height

from ..layout_planner import _mixed_flow_column_bottom_limits, _reflow_repeated_content_bands
from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..probes.structural_anchor_probe import structural_zone


def apply_rendered_semantic_spacing_reflow(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    decision: dict[str, object],
) -> tuple[MultiColumnLayoutPlan, dict[str, object]]:
    """按实际渲染字形修正一个段距病因，并带动同一所有者下游内容等量移动。"""

    previous_id = str(decision.get("previous_container_id") or "")
    next_id = str(decision.get("next_container_id") or "")
    if not previous_id or not next_id:
        raise ValueError("rendered_spacing_transition_target_missing")

    assignment = {item.container_id: item.column_id for item in template.assignments}
    container_by_id = {item.container_id: item for item in template.containers}
    placement_by_id = {item.container_id: item for item in plan.placements}
    if previous_id not in placement_by_id or next_id not in placement_by_id:
        raise ValueError("rendered_spacing_plan_target_missing")
    current_transition_gap = max(
        0.0,
        placement_by_id[next_id].output_bbox[1] - placement_by_id[previous_id].output_bbox[3],
    )
    owner = assignment.get(next_id)
    if owner is None or owner in {"fixed", "margin"}:
        raise ValueError("rendered_spacing_owner_not_reflowable")

    source_ratio = float(decision["source_transition_ratio"])
    candidate_ratio = float(decision["candidate_transition_ratio"])
    candidate_step = float(decision["candidate_line_step_pt"])
    requested_shift = (source_ratio - candidate_ratio) * candidate_step

    if decision.get("selected_failure_class") == "rendered_text_overlap":
        visible_overlap = max(0.0, float(decision.get("candidate_visible_overlap_pt") or 0.0))
        typographic_scale = float(decision.get("candidate_typographic_scale_pt") or candidate_step)
        requested_shift = max(
            requested_shift,
            visible_overlap + max(0.01, typographic_scale * 0.05),
        )

    # 裁决依据是实际字形间距，不是文本框是否相接；框可轻微交叠，但每次移动后必须重渲染复测字形。
    applied_shift = requested_shift
    if abs(applied_shift) < 0.01:
        raise ValueError("rendered_spacing_correction_below_precision")
    if applied_shift < 0.0 and "candidate_visible_gap_pt" in decision:
        visible_gap = float(decision["candidate_visible_gap_pt"])
        typographic_scale = float(decision.get("candidate_typographic_scale_pt") or candidate_step)
        if visible_gap + applied_shift < typographic_scale * 0.05:
            raise ValueError("rendered_spacing_reflow_would_overlap_glyphs")

    target_zone = structural_zone(
        container_by_id[next_id].source_bbox,
        plan.structural_anchors,
        template.height,
    )[0]
    affected_ids: list[str] = []
    target_band = next(
        (band for band in plan.flow_bands if next_id in band.container_ids),
        None,
    )
    target_band_ids = set(target_band.container_ids) if target_band is not None else None
    reached_target = False
    for container in template.containers:
        if container.container_id == next_id:
            reached_target = True
        if not reached_target or assignment.get(container.container_id) != owner:
            continue
        # owner 相同不代表同一局部流；页首和页尾通栏必须由 FlowBand 隔离。
        if target_band_ids is not None and container.container_id not in target_band_ids:
            continue
        if owner == "span":
            zone = structural_zone(container.source_bbox, plan.structural_anchors, template.height)[0]
            if zone != target_zone:
                continue
        affected_ids.append(container.container_id)
    if not affected_ids:
        raise ValueError("rendered_spacing_owner_local_flow_missing")

    column_by_id = {item.column_id: item for item in template.columns}
    dynamic_column_limits = _mixed_flow_column_bottom_limits(template=template, plan=plan)
    placements = list(plan.placements)
    for index, current in enumerate(placements):
        if current.container_id not in affected_ids:
            continue
        y0 = current.output_bbox[1] + applied_shift
        y1 = current.output_bbox[3] + applied_shift
        if owner == "span":
            _, lower, upper = structural_zone(
                container_by_id[current.container_id].source_bbox,
                plan.structural_anchors,
                template.height,
            )
        else:
            column = column_by_id[owner]
            lower = column.content_top
            upper = dynamic_column_limits.get(current.container_id, column.content_bottom)
        if y0 < lower - 0.01 or y1 > upper + 0.01:
            raise ValueError("rendered_spacing_reflow_crosses_owner_boundary")
        placements[index] = replace(
            current,
            output_bbox=(
                current.output_bbox[0],
                round(y0, 4),
                current.output_bbox[2],
                round(y1, 4),
            ),
            target_gap=(
                round(max(0.0, current_transition_gap + applied_shift), 4)
                if current.container_id == next_id
                else current.target_gap
            ),
            vertical_policy=current.vertical_policy + "+rendered_semantic_spacing_reflow",
        )

    repaired_plan = replace(plan, placements=tuple(placements))
    normalized_placements, normalization_findings = _reflow_repeated_content_bands(
        template=template,
        flow_bands=plan.flow_bands,
        placements=list(repaired_plan.placements),
    )
    page_flow_fit_font_scale: float | None = None
    page_flow_fit_container_ids: tuple[str, ...] = ()
    if any(item.severity == "HARD" for item in normalization_findings):
        repaired_plan, page_flow_fit_font_scale, page_flow_fit_container_ids = _fit_single_band_font_scale(
            template=template,
            plan=repaired_plan,
            target_band=target_band,
            container_by_id=container_by_id,
        )
    else:
        repaired_plan = replace(repaired_plan, placements=tuple(normalized_placements))

    return repaired_plan, {
        "operation_type": "rendered_semantic_spacing_reflow",
        "status": "applied",
        "previous_container_id": previous_id,
        "next_container_id": next_id,
        "owner": owner,
        "requested_shift_pt": round(requested_shift, 4),
        "applied_shift_pt": round(applied_shift, 4),
        "affected_container_ids": affected_ids,
        "page_flow_fit_font_scale": page_flow_fit_font_scale,
        "page_flow_fit_container_ids": list(page_flow_fit_container_ids),
        "hard_constraints": {
            "horizontal_bboxes_unchanged": True,
            "owner_unchanged": True,
            "reading_order_unchanged": True,
            "structural_zone_unchanged": True,
            "flow_band_unchanged": True,
            "page_flow_refit_after_spacing_change": True,
        },
    }


def _fit_single_band_font_scale(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    target_band,
    container_by_id: dict,
) -> tuple[MultiColumnLayoutPlan, float, tuple[str, ...]]:
    if target_band is None or target_band.mode != "single":
        raise ValueError("rendered_spacing_reflow_cannot_fit_page_flow")

    placement_by_id = {item.container_id: item for item in plan.placements}
    ordered_ids = tuple(
        sorted(
            target_band.container_ids,
            key=lambda item: (placement_by_id[item].output_bbox[1], placement_by_id[item].output_bbox[0]),
        )
    )
    current_scale = min(
        placement_by_id[item].font_size / max(placement_by_id[item].source_font_size, 0.01)
        for item in ordered_ids
    )
    for scale in (0.98, 0.95, 0.92, 0.88, 0.84, 0.80, 0.75, 0.72):
        if scale >= current_scale - 0.005:
            continue
        candidate = dict(placement_by_id)
        cursor: float | None = None
        for container_id in ordered_ids:
            current = candidate[container_id]
            source = container_by_id[container_id]
            font_size = max(6.0, current.source_font_size * scale)
            font_file, font_resource = _font_variant(plan.font_file, plan.font_resource, current.font_weight)
            x0, current_y0, x1, _ = current.output_bbox
            height = _minimum_text_height(
                template.width,
                template.height,
                x1 - x0,
                current.translated_text,
                font_size,
                current.line_height,
                font_file,
                font_resource,
                current.color_srgb,
            )
            y0 = current_y0 if cursor is None else cursor + current.target_gap
            y1 = y0 + height
            candidate[container_id] = replace(
                current,
                output_bbox=(x0, round(y0, 4), x1, round(y1, 4)),
                font_size=round(font_size, 4),
                vertical_policy=current.vertical_policy + "+single_band_font_fit",
            )
            cursor = y1
        normalized, findings = _reflow_repeated_content_bands(
            template=template,
            flow_bands=plan.flow_bands,
            placements=[candidate[item.container_id] for item in template.containers],
        )
        if not any(item.severity == "HARD" for item in findings):
            return replace(plan, placements=tuple(normalized)), scale, ordered_ids
    raise ValueError("rendered_spacing_reflow_cannot_fit_page_flow")
