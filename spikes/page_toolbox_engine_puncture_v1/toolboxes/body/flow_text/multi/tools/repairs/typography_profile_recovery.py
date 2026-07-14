from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from toolboxes.body.flow_text.single.tools.models import ToolboxFinding
from toolboxes.body.flow_text.single.tools.p4_layout_planner import (
    _font_variant,
    _minimum_text_height,
    _rendered_lines,
)

from ..layout_pattern import infer_multi_band_variant
from ..models import MultiColumnLayoutPlan, MultiColumnTemplate


def apply_typography_profile_change(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    target_column_ids: tuple[str, ...],
    font_scale: float | None = None,
    line_height: float | None = None,
) -> tuple[MultiColumnLayoutPlan, tuple[ToolboxFinding, ...]]:
    if (font_scale is None) == (line_height is None):
        raise ValueError("exactly_one_typography_parameter_must_change")
    variant = infer_multi_band_variant(template)
    all_column_ids = tuple(item.column_id for item in template.columns)
    if variant == "paired_row_columns" and target_column_ids != all_column_ids:
        raise ValueError("paired_row_typography_recovery_requires_all_columns")
    if variant == "independent_columns" and len(target_column_ids) != 1:
        raise ValueError("independent_typography_recovery_requires_one_column")

    target_ids = set(target_column_ids)
    selection_by_id = {item.column_id: item for item in plan.column_selections}
    unknown = target_ids - set(selection_by_id)
    if unknown:
        raise ValueError(f"unknown_typography_target_columns:{sorted(unknown)}")
    new_selections = []
    for selection in plan.column_selections:
        if selection.column_id not in target_ids:
            new_selections.append(selection)
            continue
        next_font_scale = font_scale if font_scale is not None else selection.font_scale
        next_line_height = line_height if line_height is not None else selection.line_height
        suffix = f"font-{round(next_font_scale * 100):03d}" if font_scale is not None else f"line-{round(next_line_height * 100):03d}"
        new_selections.append(
            replace(
                selection,
                profile_id=f"{selection.profile_id}-typography-{suffix}",
                font_scale=round(next_font_scale, 4),
                line_height=round(next_line_height, 4),
            )
        )

    if variant == "paired_row_columns":
        replacements, findings = _reflow_paired_columns(
            template=template,
            plan=plan,
            target_column_ids=target_column_ids,
            font_scale=font_scale,
            line_height=line_height,
        )
    else:
        replacements, findings = _reflow_independent_column(
            template=template,
            plan=plan,
            column_id=target_column_ids[0],
            font_scale=font_scale,
            line_height=line_height,
        )
    placements = tuple(replacements.get(item.container_id, item) for item in plan.placements)
    return replace(plan, column_selections=tuple(new_selections), placements=placements), findings


def typography_plan_state_hash(plan: MultiColumnLayoutPlan) -> str:
    payload = {
        "columns": [
            {
                "column_id": item.column_id,
                "font_scale": round(item.font_scale, 4),
                "line_height": round(item.line_height, 4),
                "gap_scale": round(item.gap_scale, 4),
            }
            for item in plan.column_selections
        ],
        "placements": [
            {
                "container_id": item.container_id,
                "bbox": [round(value, 3) for value in item.output_bbox],
                "font_size": round(item.font_size, 3),
                "line_height": round(item.line_height, 4),
            }
            for item in plan.placements
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def profile_evidence(plan: MultiColumnLayoutPlan, column_ids: tuple[str, ...]) -> tuple[str, ...]:
    targets = set(column_ids)
    return tuple(
        f"{item.column_id}:{item.profile_id}[font_scale={item.font_scale:.4f},line_height={item.line_height:.4f},gap_scale={item.gap_scale:.4f}]"
        for item in plan.column_selections
        if item.column_id in targets
    )


def prospective_profile_evidence(
    plan: MultiColumnLayoutPlan,
    column_ids: tuple[str, ...],
    *,
    font_scale: float | None = None,
    line_height: float | None = None,
) -> tuple[str, ...]:
    targets = set(column_ids)
    rows = []
    for item in plan.column_selections:
        if item.column_id not in targets:
            continue
        next_font_scale = font_scale if font_scale is not None else item.font_scale
        next_line_height = line_height if line_height is not None else item.line_height
        suffix = f"font-{round(next_font_scale * 100):03d}" if font_scale is not None else f"line-{round(next_line_height * 100):03d}"
        rows.append(
            f"{item.column_id}:{item.profile_id}-typography-{suffix}[font_scale={next_font_scale:.4f},line_height={next_line_height:.4f},gap_scale={item.gap_scale:.4f}]"
        )
    return tuple(rows)


def _reflow_independent_column(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    column_id: str,
    font_scale: float | None,
    line_height: float | None,
) -> tuple[dict[str, object], tuple[ToolboxFinding, ...]]:
    assignment = {item.container_id: item.column_id for item in template.assignments}
    ordered_ids = [item.container_id for item in template.containers if assignment[item.container_id] == column_id]
    placement_by_id = {item.container_id: item for item in plan.placements}
    column = next(item for item in template.columns if item.column_id == column_id)
    replacements: dict[str, object] = {}
    findings: list[ToolboxFinding] = []
    cursor: float | None = None
    previous = None
    for container_id in ordered_ids:
        current = placement_by_id[container_id]
        gap = 0.0 if previous is None else max(0.0, current.output_bbox[1] - previous.output_bbox[3])
        y0 = current.output_bbox[1] if cursor is None else cursor + gap
        replacement, item_findings = _resize_placement(
            template=template,
            plan=plan,
            placement=current,
            y0=y0,
            content_bottom=column.content_bottom,
            target_gap=gap,
            font_scale=font_scale,
            line_height=line_height,
        )
        replacements[container_id] = replacement
        findings.extend(item_findings)
        cursor = replacement.output_bbox[3]
        previous = current
    return replacements, tuple(findings)


def _reflow_paired_columns(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    target_column_ids: tuple[str, ...],
    font_scale: float | None,
    line_height: float | None,
) -> tuple[dict[str, object], tuple[ToolboxFinding, ...]]:
    assignment = {item.container_id: item.column_id for item in template.assignments}
    column_by_id = {item.column_id: item for item in template.columns}
    placements = [item for item in plan.placements if assignment.get(item.container_id) in set(target_column_ids)]
    rows: list[list[object]] = []
    for placement in sorted(placements, key=lambda item: (item.output_bbox[1], item.output_bbox[0])):
        if rows and abs(rows[-1][0].output_bbox[1] - placement.output_bbox[1]) <= 0.1:
            rows[-1].append(placement)
        else:
            rows.append([placement])
    replacements: dict[str, object] = {}
    findings: list[ToolboxFinding] = []
    cursor: float | None = None
    previous_bottom: float | None = None
    for row in rows:
        current_top = min(item.output_bbox[1] for item in row)
        gap = 0.0 if previous_bottom is None else max(0.0, current_top - previous_bottom)
        y0 = current_top if cursor is None else cursor + gap
        next_row = []
        for current in row:
            column_id = assignment[current.container_id]
            replacement, item_findings = _resize_placement(
                template=template,
                plan=plan,
                placement=current,
                y0=y0,
                content_bottom=column_by_id[column_id].content_bottom,
                target_gap=gap,
                font_scale=font_scale,
                line_height=line_height,
            )
            replacements[current.container_id] = replacement
            findings.extend(item_findings)
            next_row.append(replacement)
        cursor = max(item.output_bbox[3] for item in next_row)
        previous_bottom = max(item.output_bbox[3] for item in row)
    return replacements, tuple(findings)


def _resize_placement(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    placement,
    y0: float,
    content_bottom: float,
    target_gap: float,
    font_scale: float | None,
    line_height: float | None,
):
    next_font_size = placement.font_size if font_scale is None else max(6.0, placement.source_font_size * font_scale)
    next_line_height = placement.line_height if line_height is None else line_height
    font_file, font_resource = _font_variant(plan.font_file, plan.font_resource, placement.font_weight)
    width = placement.output_bbox[2] - placement.output_bbox[0]
    height = _minimum_text_height(
        template.width,
        template.height,
        width,
        placement.translated_text,
        next_font_size,
        next_line_height,
        font_file,
        font_resource,
        placement.color_srgb,
    )
    lines = _rendered_lines(
        page_width=template.width,
        page_height=template.height,
        width=width,
        height=height,
        text=placement.translated_text,
        font_size=next_font_size,
        line_height=next_line_height,
        font_file=font_file,
        font_resource=font_resource,
        color_srgb=placement.color_srgb,
    )
    y1 = y0 + height
    fit = y1 <= content_bottom + 0.01
    findings: list[ToolboxFinding] = []
    if len(lines) > 1 and re.fullmatch(r"[，。；：！？、）】》”’…]+", lines[-1]):
        findings.append(ToolboxFinding("P5_ORPHAN_PUNCTUATION", "HARD", "p5_typography_profile_recovery", placement.container_id, "排版恢复后句末标点被单独挤到新行"))
    if not fit:
        findings.append(ToolboxFinding("P5_COLUMN_VERTICAL_ESCAPE", "HARD", "p5_typography_profile_recovery", placement.container_id, "排版恢复后文字越过本栏可用底边"))
    policy = placement.vertical_policy
    if "+typography_profile_recovery" not in policy:
        policy += "+typography_profile_recovery"
    replacement = replace(
        placement,
        output_bbox=(placement.output_bbox[0], round(y0, 4), placement.output_bbox[2], round(y1, 4)),
        font_size=round(next_font_size, 4),
        line_height=round(next_line_height, 4),
        vertical_policy=policy,
        target_gap=round(target_gap, 4),
        fit=fit,
    )
    return replacement, tuple(findings)
