from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache
from statistics import median

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle
from toolboxes.body.flow_text.single.tools.models import TextContainer, ToolboxFinding
from toolboxes.body.flow_text.single.tools.p4_layout_planner import P4_PROFILES, _font_variant, _minimum_text_height, _rendered_lines
from toolboxes.body.flow_text.single.tools.p4_models import P4LayoutProfile, P4Placement

from . import TOOLBOX_KEY
from .layout_pattern import build_flow_bands, infer_multi_band_variant
from .models import ColumnLayoutSelection, MultiColumnLayoutPlan, MultiColumnTemplate, P5RepairAttempt, StructuralAnchor
from .template_builder import _page_background_image_ids
from .validators.semantic_paragraph_spacing_rule import evaluate_semantic_paragraph_spacing_target


def build_best_multi_plan(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    translations: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    font_resource: str = "p5cjk",
    structural_anchors: tuple[StructuralAnchor, ...] = (),
) -> tuple[MultiColumnLayoutPlan, tuple[P5RepairAttempt, ...], tuple[ToolboxFinding, ...]]:
    translated = {item.container_id: item.translated_text for item in translations.translations}
    expected = [item.container_id for item in template.containers]
    if list(translated) != expected:
        raise ValueError("translation_ids_do_not_match_multi_template_order")

    assignment = {item.container_id: item.column_id for item in template.assignments}
    flow_bands = build_flow_bands(template)
    content_bands = [item for item in flow_bands if item.mode in {"single", "multi"}]
    repeated_multi_bands = sum(item.mode == "multi" for item in content_bands) > 1
    multi_variant = infer_multi_band_variant(template)
    spans = [item for item in template.containers if assignment[item.container_id] == "span"]
    fixed = [item for item in template.containers if assignment[item.container_id] == "fixed"]
    margins = [item for item in template.containers if assignment[item.container_id] == "margin"]
    if repeated_multi_bands:
        planned_spans = spans
        late_spans: list[TextContainer] = []
    else:
        planned_spans, late_spans = _partition_spans(template, spans, assignment)
    span_placements, span_findings = _plan_spans(template, planned_spans, translated, font_file, font_resource)
    fixed_placements, fixed_findings = _plan_fixed_overlays(
        facts,
        template,
        fixed,
        translated,
        font_file,
        font_resource,
    )
    margin_placements, margin_findings = _plan_margins(
        facts,
        template,
        margins,
        translated,
        font_file,
        font_resource,
    )
    leading_span_ids: set[str]
    if repeated_multi_bands:
        first_multi_index = next(
            index for index, band in enumerate(content_bands) if band.mode == "multi"
        )
        leading_span_ids = {
            container_id
            for band in content_bands[:first_multi_index]
            for container_id in band.container_ids
        }
    else:
        leading_span_ids = {item.container_id for item in span_placements}
    span_floor = max(
        (
            item.output_bbox[3]
            for item in span_placements
            if item.container_id in leading_span_ids
        ),
        default=0.0,
    )
    # 页首标题尚未执行安全扩宽/结构带收敛时，不能用其临时高度把后续栏流推离源栏起点。
    column_span_floor = min(span_floor, min(item.content_top for item in template.columns))
    placements_by_id = {
        item.container_id: item
        for item in (*span_placements, *fixed_placements, *margin_placements)
    }
    selections: list[ColumnLayoutSelection] = []
    attempts: list[P5RepairAttempt] = []
    findings = list(span_findings + fixed_findings + margin_findings)

    if multi_variant == "paired_row_columns":
        selected_paired: tuple[list[P4Placement], tuple[ToolboxFinding, ...], object] | None = None
        for profile in P4_PROFILES:
            paired_placements, paired_findings = _plan_paired_columns(
                facts=facts,
                template=template,
                assignment=assignment,
                translated=translated,
                font_file=font_file,
                font_resource=font_resource,
                profile=profile,
                span_floor=column_span_floor,
            )
            content_placements = paired_placements
            candidate_findings = paired_findings
            if repeated_multi_bands:
                content_placements, band_findings = _reflow_repeated_content_bands(
                    template=template,
                    flow_bands=flow_bands,
                    placements=[*span_placements, *paired_placements],
                )
                candidate_findings = paired_findings + band_findings
            fit = not any(item.severity == "HARD" for item in candidate_findings)
            for column in template.columns:
                attempts.append(P5RepairAttempt(column.column_id, profile.profile_id, profile.font_scale, profile.line_height, profile.gap_scale, fit, candidate_findings))
            selected_paired = (content_placements, candidate_findings, profile)
            if fit:
                break
        if selected_paired is None:
            raise RuntimeError("p5_paired_column_plan_missing")
        paired_placements, paired_findings, profile = selected_paired
        placements_by_id.update((item.container_id, item) for item in paired_placements)
        findings.extend(paired_findings)
        for column in template.columns:
            selections.append(ColumnLayoutSelection(column.column_id, profile.profile_id, profile.font_scale, profile.line_height, profile.gap_scale, not any(item.severity == "HARD" for item in paired_findings)))
    else:
        for column in template.columns:
            containers = [item for item in template.containers if assignment[item.container_id] == column.column_id]
            selected: tuple[list[P4Placement], tuple[ToolboxFinding, ...], object] | None = None
            for profile in P4_PROFILES:
                column_placements, column_findings = _plan_column(
                    facts=facts,
                    template=template,
                    column=column,
                    containers=containers,
                    translated=translated,
                    font_file=font_file,
                    font_resource=font_resource,
                    profile=profile,
                    span_floor=column_span_floor,
                )
                fit = not any(item.severity == "HARD" for item in column_findings)
                attempts.append(P5RepairAttempt(column.column_id, profile.profile_id, profile.font_scale, profile.line_height, profile.gap_scale, fit, column_findings))
                selected = (column_placements, column_findings, profile)
                if fit:
                    break
            if selected is None:
                raise RuntimeError(f"p5_column_plan_missing:{column.column_id}")
            column_placements, column_findings, profile = selected
            if not any(item.severity == "HARD" for item in column_findings):
                # 先保证不溢出，再用当前栏真实余量逐级恢复行高和段距；字号与横向框均不改变。
                for beauty_profile in _beautification_profiles(profile):
                    beauty_placements, beauty_findings = _plan_column(
                        facts=facts,
                        template=template,
                        column=column,
                        containers=containers,
                        translated=translated,
                        font_file=font_file,
                        font_resource=font_resource,
                        profile=beauty_profile,
                        span_floor=column_span_floor,
                    )
                    beauty_fit = not any(item.severity == "HARD" for item in beauty_findings)
                    attempts.append(P5RepairAttempt(column.column_id, beauty_profile.profile_id, beauty_profile.font_scale, beauty_profile.line_height, beauty_profile.gap_scale, beauty_fit, beauty_findings))
                    if beauty_fit:
                        column_placements, column_findings, profile = beauty_placements, beauty_findings, beauty_profile
                        break
            placements_by_id.update((item.container_id, item) for item in column_placements)
            findings.extend(column_findings)
            selections.append(ColumnLayoutSelection(column.column_id, profile.profile_id, profile.font_scale, profile.line_height, profile.gap_scale, not any(item.severity == "HARD" for item in column_findings)))

        if repeated_multi_bands:
            content_ids = {
                container_id
                for band in content_bands
                for container_id in band.container_ids
            }
            content_placements, band_findings = _reflow_repeated_content_bands(
                template=template,
                flow_bands=flow_bands,
                placements=[
                    placements_by_id[item.container_id]
                    for item in template.containers
                    if item.container_id in content_ids
                ],
            )
            placements_by_id.update(
                (item.container_id, item) for item in content_placements
            )
            findings.extend(band_findings)
            failed_columns = {
                assignment[item.container_id]
                for item in band_findings
                if assignment[item.container_id] in {column.column_id for column in template.columns}
            }
            selections = [
                replace(
                    item,
                    fit=item.fit and item.column_id not in failed_columns,
                )
                for item in selections
            ]

    if late_spans:
        column_ids = {item.column_id for item in template.columns}
        source_floor = max(
            item.source_bbox[3]
            for item in template.containers
            if assignment[item.container_id] in column_ids
        )
        candidate_floor = max(
            placements_by_id[item.container_id].output_bbox[3]
            for item in template.containers
            if assignment[item.container_id] in column_ids
        )
        late_placements, late_findings = _plan_late_spans(
            template,
            late_spans,
            translated,
            font_file,
            font_resource,
            source_floor=source_floor,
            candidate_floor=candidate_floor,
        )
        placements_by_id.update((item.container_id, item) for item in late_placements)
        findings.extend(late_findings)

    placements = tuple(placements_by_id[item.container_id] for item in template.containers)
    return (
        MultiColumnLayoutPlan(
            template.page_id,
            TOOLBOX_KEY,
            source_language,
            target_language,
            font_file,
            font_resource,
            template.columns,
            tuple(selections),
            placements,
            structural_anchors,
            flow_bands,
        ),
        tuple(attempts),
        tuple(findings),
    )


def _beautification_profiles(selected: P4LayoutProfile) -> tuple[P4LayoutProfile, ...]:
    """同字号下从疏朗到保守试探纵向美化档；数值均为字号或源段距的相对比例。"""

    candidates = (
        (1.08, 0.80, "spacious"),
        (1.05, 0.65, "balanced"),
        (1.02, 0.55, "relaxed"),
    )
    return tuple(
        P4LayoutProfile(
            f"{selected.profile_id}-beauty-{label}",
            selected.font_scale,
            line_height,
            gap_scale,
        )
        for line_height, gap_scale, label in candidates
        if line_height > selected.line_height + 0.001 or gap_scale > selected.gap_scale + 0.001
    )


def _partition_spans(
    template: MultiColumnTemplate,
    spans: list[TextContainer],
    assignment: dict[str, str],
) -> tuple[list[TextContainer], list[TextContainer]]:
    """只接受多栏前奏和多栏后记；栏流中段的跨栏正文必须另行细粒度裁决。"""

    if not spans:
        return [], []
    column_ids = {item.column_id for item in template.columns}
    structure_top = min(item.content_top for item in template.columns)
    source_floor = max(
        item.source_bbox[3]
        for item in template.containers
        if assignment[item.container_id] in column_ids
    )
    top: list[TextContainer] = []
    late: list[TextContainer] = []
    for container in spans:
        if container.source_bbox[1] <= structure_top + template.height * 0.04:
            top.append(container)
        elif container.source_bbox[1] >= source_floor - container.font_size * 2.0:
            late.append(container)
        else:
            raise ValueError(f"p5_mid_flow_span_requires_adjudication:{container.container_id}")
    return top, late


def _plan_spans(
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    placements: list[P4Placement] = []
    findings: list[ToolboxFinding] = []
    cursor: float | None = None
    previous: TextContainer | None = None
    for container in sorted(containers, key=lambda item: (item.source_bbox[1], item.source_bbox[0])):
        x0, source_y0, x1, source_y1 = container.source_bbox
        source_gap = 0.0 if previous is None else max(0.0, source_y0 - previous.source_bbox[3])
        y0 = source_y0 if cursor is None else max(source_y0, cursor + source_gap)
        font_size = container.font_size
        placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
        height = _minimum_text_height(template.width, template.height, x1 - x0, translated[container.container_id], font_size, 1.15, placement_font_file, placement_resource, container.color_srgb)
        y1 = y0 + height
        fit = y1 <= template.height - 20.0
        if not fit:
            findings.append(ToolboxFinding("P5_SPANNING_VERTICAL_ESCAPE", "HARD", "p5_layout_planner", container.container_id, "跨栏文字越过页面底边"))
        placements.append(P4Placement(container.container_id, translated[container.container_id], container.role, container.source_bbox, (x0, round(y0, 4), x1, round(y1, 4)), "spanning_width_invariant", container.font_size, font_size, 1.15, "multi_owned_single_band_vertical_reflow", source_gap, source_gap, container.color_srgb, container.font_weight, fit))
        cursor = y1
        previous = container
    return placements, tuple(findings)


def _reflow_repeated_content_bands(
    *,
    template: MultiColumnTemplate,
    flow_bands,
    placements: list[P4Placement],
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    """Move alternating page-width and paired-column bands as ordered units."""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    columns = {item.column_id: item for item in template.columns}
    by_id = {item.container_id: item for item in placements}
    content_bands = [item for item in flow_bands if item.mode in {"single", "multi"}]
    previous_source_bottom: float | None = None
    previous_output_bottom: float | None = None
    for band in content_bands:
        if any(container_id not in by_id for container_id in band.container_ids):
            raise ValueError(f"p5_repeated_band_placement_missing:{band.band_id}")
        band_placements = [by_id[container_id] for container_id in band.container_ids]
        current_top = min(item.output_bbox[1] for item in band_placements)
        source_gap = (
            0.0
            if previous_source_bottom is None
            else max(0.0, band.top - previous_source_bottom)
        )
        target_gap = source_gap
        first_placements = [
            item
            for item in band_placements
            if abs(item.output_bbox[1] - current_top) <= 0.01
        ]
        if band.mode == "single" and any(
            "multi_owned_single_band_after_columns" in item.vertical_policy
            for item in first_placements
        ):
            target_gap = min(item.target_gap for item in first_placements)
        target_top = (
            current_top
            if previous_output_bottom is None
            else max(current_top, previous_output_bottom + target_gap)
        )
        shift = max(0.0, target_top - current_top)
        for placement in band_placements:
            x0, y0, x1, y1 = placement.output_bbox
            output_bbox = (
                x0,
                round(y0 + shift, 4),
                x1,
                round(y1 + shift, 4),
            )
            by_id[placement.container_id] = replace(
                placement,
                output_bbox=output_bbox,
                vertical_policy=(
                    placement.vertical_policy + "+page_band_reading_order_reflow"
                    if shift > 0.001
                    else placement.vertical_policy
                ),
            )
        previous_source_bottom = band.bottom
        previous_output_bottom = max(
            by_id[container_id].output_bbox[3]
            for container_id in band.container_ids
        )

    safe_bottom = _content_safe_bottom(template=template, assignment=assignment, placements=by_id)
    by_id = _compact_ordered_content_to_safe_bottom(
        content_bands=content_bands,
        placements=by_id,
        safe_bottom=safe_bottom,
    )
    dynamic_column_limits = _mixed_flow_column_bottom_limits_from_bands(
        template=template,
        flow_bands=flow_bands,
        placements=by_id,
    )
    findings: list[ToolboxFinding] = []
    content_ids = {container_id for band in content_bands for container_id in band.container_ids}
    for container_id in content_ids:
        placement = by_id[container_id]
        owner = assignment[container_id]
        if owner in columns:
            bottom_limit = dynamic_column_limits.get(container_id, columns[owner].content_bottom)
            code = "P5_COLUMN_VERTICAL_ESCAPE"
        else:
            bottom_limit = safe_bottom
            code = "P5_SPANNING_VERTICAL_ESCAPE"
        fit = placement.output_bbox[3] <= bottom_limit + 0.01
        if not fit:
            findings.append(
                ToolboxFinding(
                    code,
                    "HARD",
                    "p5_repeated_band_vertical_reflow",
                    container_id,
                    "ordered content band exceeds its available page region",
                )
            )
        by_id[container_id] = replace(placement, fit=fit)
    return [by_id[item.container_id] for item in placements], tuple(findings)


def _content_safe_bottom(
    *,
    template: MultiColumnTemplate,
    assignment: dict[str, str],
    placements: dict[str, P4Placement],
) -> float:
    content_font_sizes = [
        item.font_size
        for container_id, item in placements.items()
        if assignment.get(container_id) in {"span", *(column.column_id for column in template.columns)}
    ]
    clearance = max(2.0, (median(content_font_sizes) if content_font_sizes else 8.0) * 0.45)
    footer_tops = [
        placements[item.container_id].output_bbox[1]
        for item in template.containers
        if assignment.get(item.container_id) == "margin"
        and item.container_id in placements
        and (item.source_bbox[1] + item.source_bbox[3]) / 2.0 >= template.height * 0.65
    ]
    return round(min([template.height - 20.0, *(top - clearance for top in footer_tops)]), 4)


def _compact_ordered_content_to_safe_bottom(
    *,
    content_bands,
    placements: dict[str, P4Placement],
    safe_bottom: float,
) -> dict[str, P4Placement]:
    segments: list[tuple[str, tuple[str, ...]]] = []
    for band in content_bands:
        if band.mode == "single":
            segments.extend(
                (band.band_id, (container_id,))
                for container_id in sorted(
                    band.container_ids,
                    key=lambda item: (placements[item].output_bbox[1], placements[item].output_bbox[0]),
                )
            )
        else:
            segments.append((band.band_id, tuple(band.container_ids)))
    if len(segments) < 2:
        return placements

    final_bottom = max(placements[item].output_bbox[3] for item in segments[-1][1])
    deficit = max(0.0, final_bottom - safe_bottom)
    if deficit <= 0.01:
        return placements

    capacities: list[float] = []
    for (previous_band_id, previous), (current_band_id, current) in zip(segments, segments[1:]):
        previous_bottom = max(placements[item].output_bbox[3] for item in previous)
        current_top = min(placements[item].output_bbox[1] for item in current)
        actual_gap = max(0.0, current_top - previous_bottom)
        font_size = min(placements[item].font_size for item in (*previous, *current))
        target_gap = placements[current[0]].target_gap if previous_band_id == current_band_id else 0.0
        minimum_gap = min(actual_gap, max(2.0, font_size * 0.45, target_gap))
        capacities.append(max(0.0, actual_gap - minimum_gap))
    total_capacity = sum(capacities)
    if total_capacity <= 0.01:
        return placements

    reduction_ratio = min(1.0, deficit / total_capacity)
    reductions = [capacity * reduction_ratio for capacity in capacities]
    cumulative_shift = 0.0
    compacted = dict(placements)
    for index, (_, segment) in enumerate(segments):
        if index:
            cumulative_shift += reductions[index - 1]
        if cumulative_shift <= 0.001:
            continue
        for container_id in segment:
            current = compacted[container_id]
            x0, y0, x1, y1 = current.output_bbox
            compacted[container_id] = replace(
                current,
                output_bbox=(x0, round(y0 - cumulative_shift, 4), x1, round(y1 - cumulative_shift, 4)),
                vertical_policy=current.vertical_policy + "+page_flow_gap_fit",
            )
    return compacted


def _mixed_flow_column_bottom_limits_from_bands(
    *,
    template: MultiColumnTemplate,
    flow_bands,
    placements: dict[str, P4Placement],
) -> dict[str, float]:
    content_bands = [item for item in flow_bands if item.mode in {"single", "multi"}]
    if not any(item.mode == "single" for item in content_bands) or not any(item.mode == "multi" for item in content_bands):
        return {}
    assignment = {item.container_id: item.column_id for item in template.assignments}
    safe_bottom = _content_safe_bottom(template=template, assignment=assignment, placements=placements)
    limits: dict[str, float] = {}
    for index, band in enumerate(content_bands):
        if band.mode != "multi":
            continue
        following = content_bands[index + 1] if index + 1 < len(content_bands) else None
        limit = (
            min(placements[item].output_bbox[1] for item in following.container_ids)
            if following is not None
            else safe_bottom
        )
        limits.update((container_id, round(limit, 4)) for container_id in band.container_ids)
    return limits


def _mixed_flow_column_bottom_limits(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> dict[str, float]:
    return _mixed_flow_column_bottom_limits_from_bands(
        template=template,
        flow_bands=plan.flow_bands,
        placements={item.container_id: item for item in plan.placements},
    )


def _plan_late_spans(
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    *,
    source_floor: float,
    candidate_floor: float,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    selected: tuple[list[P4Placement], tuple[ToolboxFinding, ...]] | None = None
    for profile in P4_PROFILES:
        selected = _plan_late_spans_with_profile(
            template,
            containers,
            translated,
            font_file,
            font_resource,
            source_floor=source_floor,
            candidate_floor=candidate_floor,
            profile=profile,
        )
        if not any(item.severity == "HARD" for item in selected[1]):
            return selected
    return selected or ([], ())


def _plan_late_spans_with_profile(
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    *,
    source_floor: float,
    candidate_floor: float,
    profile: P4LayoutProfile,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    """在各栏完成后回填页尾跨栏说明，避免它反向把正文栏推到页尾之后。"""

    placements: list[P4Placement] = []
    findings: list[ToolboxFinding] = []
    source_cursor = source_floor
    candidate_cursor = candidate_floor
    for container in sorted(containers, key=lambda item: (item.source_bbox[1], item.source_bbox[0])):
        x0, source_y0, x1, _ = container.source_bbox
        source_gap = max(0.0, source_y0 - source_cursor)
        target_gap = source_gap * profile.gap_scale
        y0 = candidate_cursor + target_gap
        font_size = max(6.0, container.font_size * profile.font_scale)
        placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
        height = _minimum_text_height(
            template.width,
            template.height,
            x1 - x0,
            translated[container.container_id],
            font_size,
            profile.line_height,
            placement_font_file,
            placement_resource,
            container.color_srgb,
        )
        y1 = y0 + height
        fit = y1 <= template.height - 20.0
        if not fit:
            findings.append(ToolboxFinding("P5_SPANNING_VERTICAL_ESCAPE", "HARD", "p5_layout_planner", container.container_id, "页尾跨栏文字越过页面底边"))
        placements.append(P4Placement(container.container_id, translated[container.container_id], container.role, container.source_bbox, (x0, round(y0, 4), x1, round(y1, 4)), "spanning_width_invariant", container.font_size, round(font_size, 4), profile.line_height, f"multi_owned_single_band_after_columns+{profile.profile_id}", source_gap, round(target_gap, 4), container.color_srgb, container.font_weight, fit))
        source_cursor = container.source_bbox[3]
        candidate_cursor = y1
    return placements, tuple(findings)


def _plan_fixed_overlays(
    facts: PageFacts,
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    """签名或局部锁定图附近文字固定底边；短标签仅向已证明的空白侧扩展。"""

    placements: list[P4Placement] = []
    findings: list[ToolboxFinding] = []
    for container in containers:
        x0, source_y0, x1, source_y1 = container.source_bbox
        chosen: tuple[float, float, str, str, float, float, str] | None = None
        for scale in (1.0, 0.92, 0.84, 0.76, 0.72):
            font_size = max(6.0, container.font_size * scale)
            placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
            output_x0, output_x1, horizontal_policy = _fixed_overlay_horizontal_bounds(
                facts=facts,
                template=template,
                container=container,
                translated_text=translated[container.container_id],
                font_size=font_size,
                font_file=placement_font_file,
            )
            height = _minimum_text_height(template.width, template.height, output_x1 - output_x0, translated[container.container_id], font_size, 1.05, placement_font_file, placement_resource, container.color_srgb)
            y0 = min(source_y0, source_y1 - height)
            upper_guard = _fixed_overlay_upper_guard(facts, output_x0, output_x1, source_y1)
            if y0 >= upper_guard - 0.01:
                chosen = (font_size, y0, placement_font_file, placement_resource, output_x0, output_x1, horizontal_policy)
                break
        if chosen is None:
            font_size = max(6.0, container.font_size * 0.72)
            placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
            output_x0, output_x1, horizontal_policy = _fixed_overlay_horizontal_bounds(
                facts=facts,
                template=template,
                container=container,
                translated_text=translated[container.container_id],
                font_size=font_size,
                font_file=placement_font_file,
            )
            height = _minimum_text_height(template.width, template.height, output_x1 - output_x0, translated[container.container_id], font_size, 1.05, placement_font_file, placement_resource, container.color_srgb)
            y0 = min(source_y0, source_y1 - height)
            upper_guard = _fixed_overlay_upper_guard(facts, output_x0, output_x1, source_y1)
            findings.append(ToolboxFinding("P5_FIXED_OVERLAY_VERTICAL_CONFLICT", "HARD", "p5_layout_planner", container.container_id, "局部锁定图说明文字无法在原横向位置安全回填"))
        else:
            font_size, y0, placement_font_file, placement_resource, output_x0, output_x1, horizontal_policy = chosen
        fit = y0 >= upper_guard - 0.01 and y0 >= 0.0
        placements.append(P4Placement(container.container_id, translated[container.container_id], container.role, container.source_bbox, (round(output_x0, 4), round(y0, 4), round(output_x1, 4), source_y1), horizontal_policy, container.font_size, round(font_size, 4), 1.05, "locked_visual_overlay_bottom_anchor", 0.0, 0.0, container.color_srgb, container.font_weight, fit))
    return placements, tuple(findings)


def _fixed_overlay_horizontal_bounds(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    container: TextContainer,
    translated_text: str,
    font_size: float,
    font_file: str,
) -> tuple[float, float, str]:
    """保持原侧锚点，只在同一高度没有其他对象时扩到完整一行所需的最小宽度。"""

    x0, y0, x1, y1 = container.source_bbox
    if "\n" in translated_text:
        return x0, x1, "locked_visual_overlay_width_invariant"
    font = fitz.Font(fontfile=font_file)
    required_width = font.text_length(translated_text, fontsize=font_size) + font_size * 0.35
    if required_width <= x1 - x0 + 0.01:
        return x0, x1, "locked_visual_overlay_width_invariant"

    source_ids = set(container.source_object_ids)
    clearance = max(font_size * 0.25, template.width * 0.002)
    band_top = y0 - clearance
    band_bottom = y1 + clearance
    blockers = [item for item in facts.text_objects if item.object_id not in source_ids]
    blockers.extend(facts.image_objects)
    blockers.extend(facts.drawing_objects)

    # 右半页的签名标签通常以右边界对齐；左半页则保持左边界，避免无依据地移动锚点。
    if (x0 + x1) / 2.0 >= template.width / 2.0:
        safe_left = template.width * 0.02
        for blocker in blockers:
            bx0, by0, bx1, by1 = blocker.bbox
            if min(band_bottom, by1) <= max(band_top, by0) or bx1 > x0 + 0.01:
                continue
            safe_left = max(safe_left, bx1 + clearance)
        expanded_x0 = max(safe_left, x1 - required_width)
        if x1 - expanded_x0 >= required_width - 0.01:
            return expanded_x0, x1, "locked_visual_overlay_safe_left_expand"
    else:
        safe_right = template.width * 0.98
        for blocker in blockers:
            bx0, by0, bx1, by1 = blocker.bbox
            if min(band_bottom, by1) <= max(band_top, by0) or bx0 < x1 - 0.01:
                continue
            safe_right = min(safe_right, bx0 - clearance)
        expanded_x1 = min(safe_right, x0 + required_width)
        if expanded_x1 - x0 >= required_width - 0.01:
            return x0, expanded_x1, "locked_visual_overlay_safe_right_expand"
    return x0, x1, "locked_visual_overlay_width_invariant"


def _fixed_overlay_upper_guard(facts: PageFacts, x0: float, x1: float, source_y1: float) -> float:
    """按最终横向范围计算上方锁定图边界，防止扩宽后压住签名或其他固定图形。"""

    return max(
        (
            locked.bbox[3]
            for locked in (*facts.image_objects, *facts.drawing_objects)
            if locked.bbox[3] <= source_y1
            and max(0.0, min(x1, locked.bbox[2]) - max(x0, locked.bbox[0])) > 0.0
        ),
        default=0.0,
    )


def _plan_margins(
    facts: PageFacts,
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    placements: list[P4Placement] = []
    findings: list[ToolboxFinding] = []
    single_line_choices: dict[str, tuple[float, float, float, float, str]] = {}
    unresolved_ids: set[str] = set()
    for container in containers:
        choice = _margin_single_line_choice(
            facts=facts,
            template=template,
            container=container,
            translated_text=translated[container.container_id],
            font_file=font_file,
            font_resource=font_resource,
        )
        if choice is None:
            unresolved_ids.add(container.container_id)
        else:
            single_line_choices[container.container_id] = choice
    row_reflow_choices = _margin_row_horizontal_reflow(
        facts=facts,
        template=template,
        containers=containers,
        translated=translated,
        font_file=font_file,
        font_resource=font_resource,
        unresolved_ids=unresolved_ids,
    )

    for container in containers:
        x0, source_y0, x1, source_y1 = container.source_bbox
        line_height = 1.05
        chosen = row_reflow_choices.get(
            container.container_id,
            single_line_choices.get(container.container_id),
        )
        wrapped_fallback = chosen is None
        if wrapped_fallback:
            safe_right = _margin_safe_right(facts, container, template.width)
            output_x0 = x0
            output_x1 = safe_right if safe_right > x1 + 2.0 else x1
            horizontal_policy = (
                "safe_margin_right_whitespace_expand"
                if output_x1 > x1 + 0.01
                else "margin_width_invariant"
            )
            font_size = max(5.0, container.font_size * 0.72)
            placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
            height = _minimum_text_height(template.width, template.height, output_x1 - output_x0, translated[container.container_id], font_size, line_height, placement_font_file, placement_resource, container.color_srgb)
            findings.append(ToolboxFinding("P5_MARGIN_WRAP_UNRESOLVED", "HARD", "p5_layout_planner", container.container_id, "页边短文本无法在安全空白和字号下限内保持单行"))
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=output_x1 - output_x0,
                height=max(height, font_size * 3.0),
                text=translated[container.container_id],
                font_size=font_size,
                line_height=line_height,
                font_file=placement_font_file,
                font_resource=placement_resource,
                color_srgb=container.color_srgb,
            )
            if _margin_latin_word_fragmented(translated[container.container_id], lines):
                findings.append(
                    ToolboxFinding(
                        "P5_MARGIN_WORD_FRAGMENTATION",
                        "HARD",
                        "p5_layout_planner",
                        container.container_id,
                        "页眉或页脚译文把拉丁单词拆到了多行",
                    )
                )
        else:
            output_x0, output_x1, font_size, height, horizontal_policy = chosen
        # 页边文字锁定横向和底部基线；译文增高时只向上扩展，不推动正文或相邻栏。
        y0 = min(source_y0, source_y1 - height)
        upper_guard = _margin_upper_guard(
            facts=facts,
            container=container,
            output_x0=output_x0,
            output_x1=output_x1,
            font_size=font_size,
        )
        fit = y0 >= upper_guard - 0.01 and y0 >= 0.0 and source_y1 <= template.height
        if wrapped_fallback and fit:
            findings = [
                item
                for item in findings
                if not (
                    item.code == "P5_MARGIN_WRAP_UNRESOLVED"
                    and item.container_id == container.container_id
                )
            ]
        if not fit:
            findings.append(
                ToolboxFinding(
                    "P5_MARGIN_VERTICAL_ESCAPE",
                    "HARD",
                    "p5_layout_planner",
                    container.container_id,
                    "页边译文无法在固定横向位置内纵向回填",
                )
            )
        placements.append(
            P4Placement(
                container.container_id,
                translated[container.container_id],
                container.role,
                container.source_bbox,
                (round(output_x0, 4), round(y0, 4), round(output_x1, 4), source_y1),
                horizontal_policy,
                container.font_size,
                round(font_size, 4),
                line_height,
                "margin_bottom_anchor_vertical_fit",
                0.0,
                0.0,
                container.color_srgb,
                container.font_weight,
                fit,
            )
        )
    return placements, tuple(findings)


def _margin_single_line_choice(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    container: TextContainer,
    translated_text: str,
    font_file: str,
    font_resource: str,
) -> tuple[float, float, float, float, str] | None:
    x0, _, x1, _ = container.source_bbox
    safe_right = _margin_safe_right(facts, container, template.width)
    widths = [(x1, "margin_width_invariant")]
    if safe_right > x1 + 2.0:
        widths.append((safe_right, "safe_margin_right_whitespace_expand"))
    for scale in (1.0, 0.92, 0.86, 0.80, 0.75, 0.72):
        font_size = max(5.0, container.font_size * scale)
        placement_font_file, placement_resource = _font_variant(
            font_file,
            font_resource,
            container.font_weight,
        )
        for output_x1, horizontal_policy in widths:
            height = _minimum_text_height(
                template.width,
                template.height,
                output_x1 - x0,
                translated_text,
                font_size,
                1.05,
                placement_font_file,
                placement_resource,
                container.color_srgb,
            )
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=output_x1 - x0,
                height=max(height, font_size * 3.0),
                text=translated_text,
                font_size=font_size,
                line_height=1.05,
                font_file=placement_font_file,
                font_resource=placement_resource,
                color_srgb=container.color_srgb,
            )
            if len(lines) == 1:
                return x0, output_x1, font_size, height, horizontal_policy
    return None


def _margin_row_horizontal_reflow(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    unresolved_ids: set[str],
) -> dict[str, tuple[float, float, float, float, str]]:
    """Fit one visual margin row inside page-derived free segments."""

    choices: dict[str, tuple[float, float, float, float, str]] = {}
    background_image_ids = _page_background_image_ids(facts)
    for row in _margin_rows(containers):
        if not unresolved_ids.intersection(item.container_id for item in row):
            continue
        source_ids = {object_id for item in row for object_id in item.source_object_ids}
        row_top = min(item.source_bbox[1] for item in row)
        row_bottom = max(item.source_bbox[3] for item in row)
        clearance = max(1.0, max(item.font_size for item in row) * 0.35)
        obstacle_intervals: list[tuple[float, float]] = []
        for item in facts.text_objects:
            if item.object_id in source_ids:
                continue
            bx0, by0, bx1, by1 = item.bbox
            if min(row_bottom + clearance, by1) > max(row_top - clearance, by0):
                obstacle_intervals.append((bx0 - clearance, bx1 + clearance))
        for item in (*facts.image_objects, *facts.drawing_objects):
            if getattr(item, "object_id", None) in background_image_ids:
                continue
            bx0, by0, bx1, by1 = item.bbox
            area_ratio = max(bx1 - bx0, 0.0) * max(by1 - by0, 0.0) / max(facts.width * facts.height, 1.0)
            if area_ratio >= 0.60:
                continue
            if min(row_bottom + clearance, by1) > max(row_top - clearance, by0):
                obstacle_intervals.append((bx0 - clearance, bx1 + clearance))

        page_padding = min(20.0, template.width * 0.04)
        safe_left = max(0.0, min(page_padding, min(item.source_bbox[0] for item in row)))
        safe_right = min(template.width, max(template.width - page_padding, max(item.source_bbox[2] for item in row)))
        merged: list[list[float]] = []
        for left, right in sorted(obstacle_intervals):
            left = max(safe_left, left)
            right = min(safe_right, right)
            if right <= left:
                continue
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        segments: list[tuple[float, float]] = []
        cursor = safe_left
        for left, right in merged:
            if left > cursor + 0.5:
                segments.append((cursor, left))
            cursor = max(cursor, right)
        if safe_right > cursor + 0.5:
            segments.append((cursor, safe_right))

        row_choices: dict[str, tuple[float, float, float, float, str]] = {}
        row_ids = {item.container_id for item in row}
        for segment_left, segment_right in segments:
            members = [
                item
                for item in row
                if segment_left - 0.01
                <= (item.source_bbox[0] + item.source_bbox[2]) / 2.0
                <= segment_right + 0.01
            ]
            if not unresolved_ids.intersection(item.container_id for item in members):
                continue
            row_choices.update(
                _fit_margin_segment(
                    template=template,
                    containers=members,
                    translated=translated,
                    font_file=font_file,
                    font_resource=font_resource,
                    segment_left=segment_left,
                    segment_right=segment_right,
                )
            )
        if unresolved_ids.intersection(row_ids) - row_choices.keys():
            neighbor_choices: list[
                tuple[float, dict[str, tuple[float, float, float, float, str]]]
            ] = []
            for segment_left, segment_right in segments:
                candidate = _fit_margin_segment(
                    template=template,
                    containers=row,
                    translated=translated,
                    font_file=font_file,
                    font_resource=font_resource,
                    segment_left=segment_left,
                    segment_right=segment_right,
                )
                if not row_ids.issubset(candidate):
                    continue
                displacement = sum(
                    abs(candidate[item.container_id][0] - item.source_bbox[0])
                    for item in row
                )
                neighbor_choices.append((displacement, candidate))
            if neighbor_choices:
                row_choices = min(neighbor_choices, key=lambda item: item[0])[1]
        choices.update(row_choices)
    return choices


def _margin_rows(containers: list[TextContainer]) -> list[list[TextContainer]]:
    rows: list[list[TextContainer]] = []
    for container in sorted(
        containers,
        key=lambda item: (
            (item.source_bbox[1] + item.source_bbox[3]) / 2.0,
            item.source_bbox[0],
        ),
    ):
        row = next(
            (
                values
                for values in rows
                if any(_same_margin_visual_row(container, peer) for peer in values)
            ),
            None,
        )
        if row is None:
            rows.append([container])
        else:
            row.append(container)
    return rows


def _same_margin_visual_row(first: TextContainer, second: TextContainer) -> bool:
    first_center = (first.source_bbox[1] + first.source_bbox[3]) / 2.0
    second_center = (second.source_bbox[1] + second.source_bbox[3]) / 2.0
    tolerance = max(
        first.font_size,
        second.font_size,
        first.source_bbox[3] - first.source_bbox[1],
        second.source_bbox[3] - second.source_bbox[1],
    ) * 0.75
    return abs(first_center - second_center) <= tolerance


def _fit_margin_segment(
    *,
    template: MultiColumnTemplate,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    segment_left: float,
    segment_right: float,
) -> dict[str, tuple[float, float, float, float, str]]:
    ordered = sorted(containers, key=lambda item: item.source_bbox[0])
    if not ordered:
        return {}
    for scale in (1.0, 0.92, 0.86, 0.80, 0.75, 0.72):
        measured: list[tuple[TextContainer, float, float, str, str]] = []
        for container in ordered:
            translated_text = translated[container.container_id]
            if "\n" in translated_text:
                measured = []
                break
            font_size = max(5.0, container.font_size * scale)
            placement_font_file, placement_resource = _font_variant(
                font_file,
                font_resource,
                container.font_weight,
            )
            font = fitz.Font(fontfile=placement_font_file)
            required_width = font.text_length(translated_text, fontsize=font_size) + font_size * 0.35
            width = max(container.source_bbox[2] - container.source_bbox[0], required_width)
            measured.append((container, font_size, width, placement_font_file, placement_resource))
        if len(measured) != len(ordered):
            continue
        gaps = [
            max(
                1.0,
                min(
                    max(0.0, current.source_bbox[0] - previous.source_bbox[2]),
                    max(previous.font_size, current.font_size) * 1.5,
                ),
            )
            for previous, current in zip(ordered, ordered[1:])
        ]
        total_width = sum(item[2] for item in measured) + sum(gaps)
        if total_width > segment_right - segment_left + 0.01:
            continue
        cursor = max(segment_left, min(ordered[0].source_bbox[0], segment_right - total_width))
        candidate: dict[str, tuple[float, float, float, float, str]] = {}
        valid = True
        for index, (container, font_size, width, placement_font_file, placement_resource) in enumerate(measured):
            output_x0 = cursor
            output_x1 = output_x0 + width
            height = _minimum_text_height(
                template.width,
                template.height,
                width,
                translated[container.container_id],
                font_size,
                1.05,
                placement_font_file,
                placement_resource,
                container.color_srgb,
            )
            lines = _rendered_lines(
                page_width=template.width,
                page_height=template.height,
                width=width,
                height=max(height, font_size * 3.0),
                text=translated[container.container_id],
                font_size=font_size,
                line_height=1.05,
                font_file=placement_font_file,
                font_resource=placement_resource,
                color_srgb=container.color_srgb,
            )
            if len(lines) != 1:
                valid = False
                break
            if abs(output_x0 - container.source_bbox[0]) > 0.01:
                policy = "safe_margin_row_horizontal_reflow"
            elif abs(output_x1 - container.source_bbox[2]) > 0.01:
                policy = "safe_margin_right_whitespace_expand"
            else:
                policy = "margin_width_invariant"
            candidate[container.container_id] = (
                output_x0,
                output_x1,
                font_size,
                height,
                policy,
            )
            cursor = output_x1 + (gaps[index] if index < len(gaps) else 0.0)
        if valid:
            return candidate
    return {}


def _margin_latin_word_fragmented(text: str, lines: tuple[str, ...] | list[str]) -> bool:
    compact_lines = "".join(re.sub(r"\s+", "", line).casefold() for line in lines)
    for word in re.findall(r"[A-Za-z]{3,}(?:['’-][A-Za-z]+)*", text):
        folded = word.casefold()
        if folded in compact_lines and not any(folded in line.casefold() for line in lines):
            return True
    return False


def _margin_upper_guard(
    *,
    facts: PageFacts,
    container: TextContainer,
    output_x0: float,
    output_x1: float,
    font_size: float,
) -> float:
    """Return the nearest page-derived boundary above a bottom-anchored margin."""

    _, source_y0, _, source_y1 = container.source_bbox
    source_center = (source_y0 + source_y1) / 2.0
    source_height = max(source_y1 - source_y0, 1.0)
    if source_y0 < facts.height * 0.50:
        return 0.0
    source_ids = set(container.source_object_ids)
    background_image_ids = _page_background_image_ids(facts)
    clearance = max(font_size * 0.20, facts.height * 0.001)
    guards = [0.0]
    for item in (*facts.text_objects, *facts.image_objects, *facts.drawing_objects):
        if getattr(item, "object_id", None) in source_ids:
            continue
        if getattr(item, "object_id", None) in background_image_ids:
            continue
        bx0, by0, bx1, by1 = item.bbox
        same_visual_line = abs(((by0 + by1) / 2.0) - source_center) <= max(font_size, source_height) * 0.75
        if (
            by0 < source_y0
            and by1 <= source_y1
            and not same_visual_line
            and min(output_x1, bx1) > max(output_x0, bx0)
        ):
            guards.append(by1 + clearance)
    return max(guards)


def _margin_safe_right(facts: PageFacts, container: TextContainer, page_width: float) -> float:
    """页脚只能使用同一行右侧经当前页文本事实证明为空的区域。"""

    _, y0, x1, y1 = container.source_bbox
    obstacles = [
        item.bbox[0]
        for item in facts.text_objects
        if item.object_id not in container.source_object_ids
        and item.bbox[0] >= x1 - 0.01
        and min(y1, item.bbox[3]) > max(y0, item.bbox[1])
    ]
    return round(min(obstacles) - 1.0 if obstacles else page_width - 20.0, 4)


def _column_flow_obstacles(
    *,
    facts: PageFacts,
    column,
    containers: list[TextContainer],
) -> tuple[tuple[float, float, float, float], ...]:
    """Return page-derived local visuals that ordinary column flow must avoid."""

    column_width = max(column.right - column.left, 1.0)
    output: list[tuple[float, float, float, float]] = []
    background_image_ids = _page_background_image_ids(facts)
    for locked in (*facts.image_objects, *facts.drawing_objects):
        if getattr(locked, "object_id", None) in background_image_ids:
            continue
        x0, y0, x1, y1 = locked.bbox
        width = max(x1 - x0, 0.0)
        height = max(y1 - y0, 0.0)
        area_ratio = (width * height) / max(facts.width * facts.height, 1.0)
        horizontal_overlap = max(0.0, min(column.right, x1) - max(column.left, x0))
        if (
            area_ratio >= 0.60
            or height < max(2.0, facts.height * 0.003)
            or horizontal_overlap < column_width * 0.30
            or y1 <= column.content_top
            or y0 >= column.content_bottom
        ):
            continue
        has_flow_below = any(
            container.source_bbox[1] >= y1 - max(container.font_size, facts.height * 0.002)
            and max(
                0.0,
                min(container.source_bbox[2], x1) - max(container.source_bbox[0], x0),
            )
            >= min(max(container.source_bbox[2] - container.source_bbox[0], 1.0), max(width, 1.0)) * 0.25
            for container in containers
        )
        if has_flow_below and locked.bbox not in output:
            output.append(locked.bbox)
    return tuple(sorted(output, key=lambda item: (item[1], item[0], item[3], item[2])))


def _measure_column_text(
    *,
    template: MultiColumnTemplate,
    container: TextContainer,
    translated_text: str,
    source_font_size: float,
    profile: P4LayoutProfile,
    font_file: str,
    font_resource: str,
) -> tuple[float, float, float, tuple[str, ...], float, str, str]:
    font_size = max(6.0, source_font_size * profile.font_scale)
    placement_font_file, placement_resource = _font_variant(
        font_file,
        font_resource,
        container.font_weight,
    )
    width = container.source_bbox[2] - container.source_bbox[0]
    height = _minimum_text_height(
        template.width,
        template.height,
        width,
        translated_text,
        font_size,
        profile.line_height,
        placement_font_file,
        placement_resource,
        container.color_srgb,
    )
    lines = tuple(
        _rendered_lines(
            page_width=template.width,
            page_height=template.height,
            width=width,
            height=height,
            text=translated_text,
            font_size=font_size,
            line_height=profile.line_height,
            font_file=placement_font_file,
            font_resource=placement_resource,
            color_srgb=container.color_srgb,
        )
    )
    return (
        font_size,
        profile.line_height,
        height,
        lines,
        _candidate_line_step(
            placement_font_file,
            font_size=font_size,
            line_height=profile.line_height,
        ),
        placement_font_file,
        placement_resource,
    )


def _profiles_no_roomier_than(profile: P4LayoutProfile) -> tuple[P4LayoutProfile, ...]:
    return tuple(
        candidate
        for candidate in P4_PROFILES
        if candidate.font_scale <= profile.font_scale + 0.001
        and candidate.line_height <= profile.line_height + 0.001
    )


def _plan_column(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    column,
    containers: list[TextContainer],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    profile,
    span_floor: float,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    findings: list[ToolboxFinding] = []
    placements: list[P4Placement] = []
    body_sizes = [item.font_size for item in containers if item.role in {"body", "list"}]
    body_baseline = median(body_sizes) if body_sizes else median(item.font_size for item in containers)
    source_line_tops = {
        item.container_id: _source_line_tops(item, facts)
        for item in containers
    }
    source_line_step = _source_line_step(tuple(source_line_tops.values()))
    obstacles = _column_flow_obstacles(
        facts=facts,
        column=column,
        containers=containers,
    )
    cursor: float | None = None
    previous: TextContainer | None = None
    previous_line_count = 0
    previous_candidate_line_step = 0.0
    previous_output_height = 0.0
    for container in containers:
        x0, source_y0, x1, source_y1 = container.source_bbox
        source_gap = 0.0 if previous is None else max(0.0, source_y0 - previous.source_bbox[3])
        source_font_size = body_baseline if container.role in {"body", "list"} else container.font_size
        (
            font_size,
            line_height,
            height,
            lines,
            candidate_line_step,
            placement_font_file,
            placement_resource,
        ) = _measure_column_text(
            template=template,
            container=container,
            translated_text=translated[container.container_id],
            source_font_size=source_font_size,
            profile=profile,
            font_file=font_file,
            font_resource=font_resource,
        )
        target_gap = source_gap if previous is None else source_gap * profile.gap_scale
        vertical_policy = "independent_column_vertical_flow"
        if previous is not None:
            spacing_decision = evaluate_semantic_paragraph_spacing_target(
                previous=previous,
                current=container,
                previous_source_line_tops=source_line_tops[previous.container_id],
                current_source_line_tops=source_line_tops[container.container_id],
                source_line_step=source_line_step,
                previous_output_height=previous_output_height,
                previous_candidate_line_count=previous_line_count,
                previous_candidate_line_step=previous_candidate_line_step,
                current_candidate_line_step=candidate_line_step,
            )
            if spacing_decision["rule_verdict"] == "APPLY":
                target_gap = float(spacing_decision["target_plan_gap_pt"])
                vertical_policy += "+semantic_source_rhythm"
        if cursor is None:
            y0 = max(source_y0, span_floor + max(0.0, source_y0 - column.content_top)) if span_floor > source_y0 else source_y0
        else:
            y0 = cursor + target_gap
        for obstacle in obstacles:
            obstacle_x0, obstacle_y0, obstacle_x1, obstacle_y1 = obstacle
            horizontal_overlap = max(0.0, min(x1, obstacle_x1) - max(x0, obstacle_x0))
            if horizontal_overlap <= 0.0:
                continue
            clearance = max(font_size * 0.35, template.height * 0.002)
            if y0 + height <= obstacle_y0 - clearance or y0 >= obstacle_y1 + clearance:
                continue
            source_tolerance = max(container.font_size, template.height * 0.002)
            if source_y1 <= obstacle_y0 + source_tolerance:
                available_height = obstacle_y0 - clearance - y0
                local_fit = None
                for local_profile in _profiles_no_roomier_than(profile):
                    measured = _measure_column_text(
                        template=template,
                        container=container,
                        translated_text=translated[container.container_id],
                        source_font_size=source_font_size,
                        profile=local_profile,
                        font_file=font_file,
                        font_resource=font_resource,
                    )
                    if measured[2] <= available_height + 0.01:
                        local_fit = measured
                        break
                if local_fit is not None:
                    (
                        font_size,
                        line_height,
                        height,
                        lines,
                        candidate_line_step,
                        placement_font_file,
                        placement_resource,
                    ) = local_fit
                    vertical_policy += "+locked_visual_obstacle_local_fit"
                    continue
                y0 = obstacle_y1 + clearance
                vertical_policy += "+locked_visual_obstacle_downstream_reflow"
            elif source_y0 >= obstacle_y1 - source_tolerance:
                y0 = obstacle_y1 + clearance
                vertical_policy += "+locked_visual_obstacle_downstream_reflow"
            else:
                findings.append(
                    ToolboxFinding(
                        "P5_LOCKED_VISUAL_TEXT_COLLISION",
                        "HARD",
                        "p5_layout_planner",
                        container.container_id,
                        "column text intersects a page-derived locked visual obstacle",
                    )
                )
            if cursor is not None:
                target_gap = max(0.0, y0 - cursor)
        if font_size + 0.01 < max(6.0, source_font_size * 0.72):
            findings.append(ToolboxFinding("P5_FONT_TOO_SMALL", "HARD", "p5_layout_planner", container.container_id, "字号低于源字号 72% 或 6pt"))
        if len(lines) > 1 and re.fullmatch(r"[，。；：！？、）】》”’…]+", lines[-1]):
            findings.append(ToolboxFinding("P5_ORPHAN_PUNCTUATION", "HARD", "p5_layout_planner", container.container_id, "列内句末标点被单独挤到新行"))
        y1 = y0 + height
        if any(
            max(0.0, min(x1, obstacle[2]) - max(x0, obstacle[0])) > 0.0
            and min(y1, obstacle[3]) > max(y0, obstacle[1])
            for obstacle in obstacles
        ):
            findings.append(
                ToolboxFinding(
                    "P5_LOCKED_VISUAL_TEXT_COLLISION",
                    "HARD",
                    "p5_layout_planner",
                    container.container_id,
                    "column text still intersects a page-derived locked visual obstacle",
                )
            )
        fit = y1 <= column.content_bottom + 0.01
        if not fit:
            findings.append(ToolboxFinding("P5_COLUMN_VERTICAL_ESCAPE", "HARD", "p5_layout_planner", container.container_id, "列内文字越过本栏可用底边"))
        placements.append(P4Placement(container.container_id, translated[container.container_id], container.role, container.source_bbox, (x0, round(y0, 4), x1, round(y1, 4)), "column_width_invariant", round(source_font_size, 4), round(font_size, 4), line_height, vertical_policy, round(source_gap, 4), round(target_gap, 4), container.color_srgb, container.font_weight, fit))
        cursor = y1
        previous = container
        previous_line_count = len(lines)
        previous_candidate_line_step = candidate_line_step
        previous_output_height = height
    return placements, tuple(findings)


def _plan_paired_columns(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    assignment: dict[str, str],
    translated: dict[str, str],
    font_file: str,
    font_resource: str,
    profile,
    span_floor: float,
) -> tuple[list[P4Placement], tuple[ToolboxFinding, ...]]:
    """按源页成对行同步推进两栏；单元宽度不变，行高取该行两侧译文最大值。"""

    column_by_id = {item.column_id: item for item in template.columns}
    column_ids = set(column_by_id)
    containers = [item for item in template.containers if assignment[item.container_id] in column_ids]
    body_baseline = {
        column_id: median(
            item.font_size
            for item in containers
            if assignment[item.container_id] == column_id
        )
        for column_id in column_ids
    }
    rows: list[list[TextContainer]] = []
    for container in sorted(containers, key=lambda item: (item.source_bbox[1], item.source_bbox[0])):
        owner = assignment[container.container_id]
        target = next(
            (
                row for row in reversed(rows)
                if all(assignment[item.container_id] != owner for item in row)
                and abs(row[0].source_bbox[1] - container.source_bbox[1])
                <= max(row[0].font_size, container.font_size) * 0.45
            ),
            None,
        )
        if target is None:
            rows.append([container])
        else:
            target.append(container)

    findings: list[ToolboxFinding] = []
    placements: list[P4Placement] = []
    cursor: float | None = None
    previous_source_bottom: float | None = None
    first_content_top = min(item.content_top for item in template.columns)
    for row in rows:
        source_top = min(item.source_bbox[1] for item in row)
        source_bottom = max(item.source_bbox[3] for item in row)
        source_gap = 0.0 if previous_source_bottom is None else max(0.0, source_top - previous_source_bottom)
        target_gap = source_gap if cursor is None else source_gap * profile.gap_scale
        if cursor is None:
            y0 = max(source_top, span_floor + max(0.0, source_top - first_content_top)) if span_floor > source_top else source_top
        else:
            # 译文较短时仍贴近源页成对行起点；只有前一行确实变高时才整体向下推。
            y0 = max(source_top, cursor + target_gap)
        row_placements: list[P4Placement] = []
        for container in row:
            owner = assignment[container.container_id]
            column = column_by_id[owner]
            x0, _, _, _ = container.source_bbox
            x1 = column.right
            source_font_size = body_baseline[owner] if container.role in {"body", "list"} else container.font_size
            font_size = max(6.0, source_font_size * profile.font_scale)
            placement_font_file, placement_resource = _font_variant(font_file, font_resource, container.font_weight)
            paired_line_height = max(profile.line_height, 1.10)
            height = _minimum_text_height(template.width, template.height, x1 - x0, translated[container.container_id], font_size, paired_line_height, placement_font_file, placement_resource, container.color_srgb)
            lines = _rendered_lines(page_width=template.width, page_height=template.height, width=x1 - x0, height=height, text=translated[container.container_id], font_size=font_size, line_height=paired_line_height, font_file=placement_font_file, font_resource=placement_resource, color_srgb=container.color_srgb)
            if len(lines) > 1 and re.fullmatch(r"[，。；：！？、）】》”’…]+", lines[-1]):
                findings.append(ToolboxFinding("P5_ORPHAN_PUNCTUATION", "HARD", "p5_layout_planner", container.container_id, "成对行单元的句末标点被单独挤到新行"))
            y1 = y0 + height
            fit = y1 <= column.content_bottom + 0.01
            if not fit:
                findings.append(ToolboxFinding("P5_COLUMN_VERTICAL_ESCAPE", "HARD", "p5_layout_planner", container.container_id, "成对行单元越过所属栏可用底边"))
            row_placements.append(P4Placement(container.container_id, translated[container.container_id], container.role, container.source_bbox, (x0, round(y0, 4), x1, round(y1, 4)), "paired_row_column_width", round(source_font_size, 4), round(font_size, 4), paired_line_height, "paired_row_synchronous_vertical_reflow", round(source_gap, 4), round(target_gap, 4), container.color_srgb, container.font_weight, fit))
        placements.extend(row_placements)
        cursor = max(item.output_bbox[3] for item in row_placements)
        previous_source_bottom = source_bottom
    return placements, tuple(findings)


def _source_line_tops(container: TextContainer, facts: PageFacts) -> tuple[float, ...]:
    source_by_id = {item.object_id: item for item in facts.text_objects}
    lines: dict[tuple[int, int], list[float]] = {}
    for object_id in container.source_object_ids:
        source = source_by_id.get(object_id)
        if source is None:
            continue
        lines.setdefault((source.block_index, source.line_index), []).append(source.bbox[1])
    return tuple(
        min(values)
        for _, values in sorted(lines.items(), key=lambda item: min(item[1]))
    )


def _source_line_step(groups: tuple[tuple[float, ...], ...]) -> float | None:
    steps = [
        current - previous
        for values in groups
        for previous, current in zip(values, values[1:])
        if current > previous
    ]
    return median(steps) if steps else None


def _candidate_line_step(font_file: str, *, font_size: float, line_height: float) -> float:
    return font_size * line_height * _font_ascender(font_file)


@lru_cache(maxsize=16)
def _font_ascender(font_file: str) -> float:
    return max(0.5, float(fitz.Font(fontfile=font_file).ascender))


def refresh_post_repair_planning_findings(
    *,
    facts: PageFacts | None = None,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    findings: tuple[ToolboxFinding, ...],
) -> tuple[ToolboxFinding, ...]:
    """布局补丁完成后重算几何越界，避免携带补丁前已经消失的病因。"""

    refreshable = {
        "P5_COLUMN_VERTICAL_ESCAPE",
        "P5_SPANNING_VERTICAL_ESCAPE",
        "P5_LOCKED_VISUAL_TEXT_COLLISION",
    }
    current = [item for item in findings if item.code not in refreshable]
    assignment = {item.container_id: item.column_id for item in template.assignments}
    columns = {item.column_id: item for item in template.columns}
    placements_by_id = {item.container_id: item for item in plan.placements}
    dynamic_column_limits = _mixed_flow_column_bottom_limits(template=template, plan=plan)
    content_safe_bottom = _content_safe_bottom(
        template=template,
        assignment=assignment,
        placements=placements_by_id,
    )
    obstacles_by_column = (
        {
            column_id: _column_flow_obstacles(
                facts=facts,
                column=column,
                containers=[
                    item
                    for item in template.containers
                    if assignment[item.container_id] == column_id
                ],
            )
            for column_id, column in columns.items()
        }
        if facts is not None
        else {}
    )
    for placement in plan.placements:
        owner = assignment[placement.container_id]
        column_bottom_limit = dynamic_column_limits.get(
            placement.container_id,
            columns[owner].content_bottom if owner in columns else 0.0,
        )
        if owner in columns and placement.output_bbox[3] > column_bottom_limit + 0.01:
            current.append(
                ToolboxFinding(
                    "P5_COLUMN_VERTICAL_ESCAPE",
                    "HARD",
                    "p5_post_repair_geometry_rule",
                    placement.container_id,
                    "最终布局中的列内文字仍越过本栏可用底边",
                )
            )
        elif owner == "span" and placement.output_bbox[3] > content_safe_bottom + 0.01:
            current.append(
                ToolboxFinding(
                    "P5_SPANNING_VERTICAL_ESCAPE",
                    "HARD",
                    "p5_post_repair_geometry_rule",
                    placement.container_id,
                    "最终布局中的通栏文字仍越过页面安全底边",
                )
            )
        if owner in obstacles_by_column and any(
            min(placement.output_bbox[2], obstacle[2]) > max(placement.output_bbox[0], obstacle[0])
            and min(placement.output_bbox[3], obstacle[3]) > max(placement.output_bbox[1], obstacle[1])
            for obstacle in obstacles_by_column[owner]
        ):
            current.append(
                ToolboxFinding(
                    "P5_LOCKED_VISUAL_TEXT_COLLISION",
                    "HARD",
                    "p5_post_repair_geometry_rule",
                    placement.container_id,
                    "最终布局中的列内文字与锁定视觉对象相交",
                )
            )
    return tuple(current)
