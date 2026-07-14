from __future__ import annotations

from dataclasses import replace

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle, TranslationResult
from toolboxes.body.flow_text.single.tools.p4_layout_planner import build_best_p4_plan
from toolboxes.body.table.tools.layout_planner import plan_table_layout

from . import TOOLBOX_KEY
from .models import (
    CompositeFinding,
    CompositeLayoutPlan,
    CompositePageTemplate,
    CompositePlanEvidence,
    Rect,
    TableRegionTransform,
)
from .translation_request import split_translation_bundle


def plan_composite_layout(
    *,
    facts: PageFacts,
    template: CompositePageTemplate,
    translations: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    bold_font_file: str | None,
) -> tuple[CompositeLayoutPlan, tuple[CompositeFinding, ...], CompositePlanEvidence]:
    flow_bundles, table_bundle = split_translation_bundle(template, translations)
    flow_plans = []
    all_attempts = []
    findings: list[CompositeFinding] = []

    source_by_id = {item.object_id: item for item in facts.text_objects}
    for region, bundle in zip(template.flow_regions, flow_bundles):
        object_ids = {
            object_id
            for container in region.template.containers
            for object_id in container.source_object_ids
        }
        region_objects = [source_by_id[object_id] for object_id in object_ids]
        region_objects.extend(
            _protected_text_objects_in_region(template, facts, region.allowed_bbox)
        )
        if region.allowed_bbox[3] < template.height - 0.5:
            planner_height = region.allowed_bbox[3] + 17.0
        else:
            planner_height = template.height
        region_facts = replace(
            facts,
            height=planner_height,
            native_text_object_count=len(region_objects),
            text_objects=tuple(region_objects),
            text_objects_sha256=None,
        )
        region_template = replace(region.template, height=planner_height)
        plan, attempts = build_best_p4_plan(
            facts=region_facts,
            template=region_template,
            translations=bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            font_resource=f"p7flow_{len(flow_plans)}",
        )
        if plan is None:
            raise ValueError(f"flow_region_has_no_plan:{region.region_id}")
        plan = _fit_anchored_grid_to_region(plan, region.allowed_bbox)
        plan = _avoid_earlier_placement_overlaps(plan, region.allowed_bbox)
        plan = _fit_flow_plan_to_region(plan, region.allowed_bbox)
        flow_plans.append(plan)
        all_attempts.append(attempts)
        if attempts:
            findings.extend(
                _flow_finding(item)
                for item in attempts[-1].findings
                if not _resolved_vertical_escape(item, plan)
            )

    table_region_transforms = _build_table_region_transforms(template, tuple(flow_plans), facts)
    repaint_ids = _overlapping_protected_cell_ids(template.table_template)
    table_plan, table_findings = _plan_table_with_transforms(
        template.table_template,
        table_bundle,
        table_region_transforms,
        repaint_ids,
        font_file,
        bold_font_file,
    )
    elastic_region_scales = _elastic_table_region_scales(
        template.table_template,
        table_plan,
        table_region_transforms,
    )
    reserved_flow_plans = _reserve_following_flow_space(
        template,
        tuple(flow_plans),
        table_region_transforms,
        elastic_region_scales,
    )
    if reserved_flow_plans != tuple(flow_plans):
        flow_plans = list(reserved_flow_plans)
        table_region_transforms = _build_table_region_transforms(template, tuple(flow_plans), facts)
    expanded_transforms = _expand_elastic_table_regions(
        template,
        tuple(flow_plans),
        facts,
        table_region_transforms,
        elastic_region_scales,
    )
    if expanded_transforms != table_region_transforms:
        table_region_transforms = expanded_transforms
        table_plan, table_findings = _plan_table_with_transforms(
            template.table_template,
            table_bundle,
            table_region_transforms,
            repaint_ids,
            font_file,
            bold_font_file,
        )
    findings.extend(
        CompositeFinding(
            item.code,
            item.severity,
            item.owner,
            item.container_id,
            item.message,
            item.evidence,
        )
        for item in table_findings
    )
    plan = CompositeLayoutPlan(
        template.page_id,
        TOOLBOX_KEY,
        source_language,
        target_language,
        table_region_transforms,
        tuple(flow_plans),
        table_plan,
    )
    findings.extend(validate_owner_boundaries(template, plan, facts))
    return plan, tuple(_deduplicate_findings(findings)), CompositePlanEvidence(tuple(all_attempts))


def repair_horizontal_table_rule_overlaps(
    plan: CompositeLayoutPlan,
    findings: tuple[CompositeFinding, ...],
) -> tuple[CompositeLayoutPlan, tuple[dict[str, object], ...]]:
    required_shifts: dict[str, float] = {}
    for finding in findings:
        if finding.code != "TABLE_LINE_TEXT_OVERLAP":
            continue
        for overlap in finding.evidence.get("overlaps", []):
            if overlap.get("orientation") != "horizontal":
                continue
            container_id = str(overlap.get("container_id") or "")
            glyph_bbox = overlap.get("glyph_bbox")
            rule_coordinate = overlap.get("rule_coordinate")
            if not container_id or not isinstance(glyph_bbox, (list, tuple)) or len(glyph_bbox) != 4:
                continue
            if not isinstance(rule_coordinate, (int, float)):
                continue
            if not float(glyph_bbox[1]) < float(rule_coordinate) < float(glyph_bbox[3]):
                continue
            shift = float(glyph_bbox[3]) - float(rule_coordinate) + 0.5
            required_shifts[container_id] = max(required_shifts.get(container_id, 0.0), shift)

    placements = list(plan.table_plan.placements)
    records: list[dict[str, object]] = []
    for index, placement in enumerate(placements):
        shift = required_shifts.get(placement.container_id)
        if shift is None:
            continue
        proposed = (
            placement.output_bbox[0],
            round(placement.output_bbox[1] - shift, 4),
            placement.output_bbox[2],
            round(placement.output_bbox[3] - shift, 4),
        )
        if not _contains(placement.allowed_bbox, proposed):
            continue
        if any(
            other.container_id != placement.container_id
            and _horizontal_overlap(proposed, other.output_bbox) > 0.5
            and _intersects(proposed, other.output_bbox)
            for other in placements
        ):
            continue
        placements[index] = replace(
            placement,
            output_bbox=proposed,
            anchor=(placement.anchor[0], proposed[1]),
        )
        records.append(
            {
                "operation_type": "horizontal_rule_overlap_upward_shift",
                "container_id": placement.container_id,
                "shift": round(shift, 4),
                "before_bbox": placement.output_bbox,
                "after_bbox": proposed,
            }
        )
    if not records:
        return plan, ()
    table_plan = replace(plan.table_plan, placements=tuple(placements))
    return replace(plan, table_plan=table_plan), tuple(records)


def _protected_text_objects_in_region(template, facts, allowed_bbox: Rect):
    protected_ids = {
        item.object_id for item in template.ownerships if item.owner == "protected"
    }
    return tuple(
        item
        for item in facts.text_objects
        if item.object_id in protected_ids
        and allowed_bbox[0] <= (item.bbox[0] + item.bbox[2]) / 2.0 <= allowed_bbox[2]
        and allowed_bbox[1] <= (item.bbox[1] + item.bbox[3]) / 2.0 <= allowed_bbox[3]
    )


def _fit_anchored_grid_to_region(plan, allowed_bbox: Rect):
    grid = [item for item in plan.placements if item.role == "anchored_grid"]
    if not grid:
        return plan
    upward_shift = max(item.output_bbox[3] for item in grid) - allowed_bbox[3]
    if upward_shift <= 0.0 or min(item.output_bbox[1] for item in grid) - upward_shift < allowed_bbox[1]:
        return plan
    placements = tuple(
        replace(
            item,
            output_bbox=(
                item.output_bbox[0],
                round(item.output_bbox[1] - upward_shift, 4),
                item.output_bbox[2],
                round(item.output_bbox[3] - upward_shift, 4),
            ),
            vertical_policy="spatial_row_group_region_clamp",
        )
        if item.role == "anchored_grid"
        else item
        for item in plan.placements
    )
    return replace(plan, placements=placements)


def _fit_flow_plan_to_region(plan, allowed_bbox: Rect):
    movable = [item for item in plan.placements if item.role != "margin"]
    if not movable:
        return plan
    bottom_limit = min(allowed_bbox[3], plan.content_bottom)
    upward_shift = max(item.output_bbox[3] for item in movable) - bottom_limit
    if upward_shift <= 0.0:
        return plan
    if min(item.output_bbox[1] for item in movable) - upward_shift < allowed_bbox[1]:
        return plan
    fixed = [item for item in plan.placements if item.role == "margin"]
    shifted_boxes = {
        item.container_id: (
            item.output_bbox[0],
            round(item.output_bbox[1] - upward_shift, 4),
            item.output_bbox[2],
            round(item.output_bbox[3] - upward_shift, 4),
        )
        for item in movable
    }
    if any(
        _horizontal_overlap(shifted_boxes[item.container_id], margin.output_bbox) > 0.5
        and _intersects(shifted_boxes[item.container_id], margin.output_bbox)
        for item in movable
        for margin in fixed
    ):
        return plan
    spatial_roles = {"anchored", "anchored_grid", "image_anchored"}
    placements = tuple(
        replace(
            item,
            output_bbox=shifted_boxes[item.container_id],
            vertical_policy=f"{item.vertical_policy}+region_upward_clamp",
            fit=(
                item.fit
                if item.role in spatial_roles
                else shifted_boxes[item.container_id][3] <= bottom_limit + 0.01
            ),
        )
        if item.container_id in shifted_boxes
        else item
        for item in plan.placements
    )
    return replace(plan, placements=placements)


def _resolved_vertical_escape(finding, plan) -> bool:
    if finding.code != "P4_VERTICAL_PAGE_ESCAPE" or finding.container_id is None:
        return False
    placement = next(
        (item for item in plan.placements if item.container_id == finding.container_id),
        None,
    )
    return placement is not None and placement.fit


def _avoid_earlier_placement_overlaps(plan, allowed_bbox: Rect):
    resolved = []
    spatial_roles = {"margin", "anchored", "anchored_grid", "image_anchored"}
    placements = plan.placements
    index = 0
    while index < len(placements):
        placement = placements[index]
        if placement.role == "anchored_grid":
            group = [placement]
            next_index = index + 1
            while (
                next_index < len(placements)
                and placements[next_index].role == "anchored_grid"
                and any(
                    min(item.output_bbox[3], placements[next_index].output_bbox[3])
                    - max(item.output_bbox[1], placements[next_index].output_bbox[1])
                    > 0.5
                    for item in group
                )
            ):
                group.append(placements[next_index])
                next_index += 1
            downward_shift = max(
                (
                    earlier.output_bbox[3] + 0.5 - item.output_bbox[1]
                    for item in group
                    for earlier in resolved
                    if _horizontal_overlap(item.output_bbox, earlier.output_bbox) > 0.5
                    and _intersects(item.output_bbox, earlier.output_bbox)
                ),
                default=0.0,
            )
            if downward_shift > 0.0:
                group = [
                    replace(
                        item,
                        output_bbox=(
                            item.output_bbox[0],
                            round(item.output_bbox[1] + downward_shift, 4),
                            item.output_bbox[2],
                            round(item.output_bbox[3] + downward_shift, 4),
                        ),
                        vertical_policy=f"{item.vertical_policy}+earlier_text_obstacle",
                        fit=item.fit and item.output_bbox[3] + downward_shift <= allowed_bbox[3] + 0.01,
                    )
                    for item in group
                ]
            resolved.extend(group)
            index = next_index
            continue
        if placement.role in spatial_roles:
            resolved.append(placement)
            index += 1
            continue
        output_bbox = placement.output_bbox
        shifted = False
        while True:
            blockers = [
                earlier
                for earlier in resolved
                if _horizontal_overlap(output_bbox, earlier.output_bbox) > 0.5
                and _intersects(output_bbox, earlier.output_bbox)
            ]
            if not blockers:
                break
            target_y0 = max(item.output_bbox[3] for item in blockers) + 0.5
            delta = target_y0 - output_bbox[1]
            output_bbox = (
                output_bbox[0],
                round(output_bbox[1] + delta, 4),
                output_bbox[2],
                round(output_bbox[3] + delta, 4),
            )
            shifted = True
        if shifted:
            placement = replace(
                placement,
                output_bbox=output_bbox,
                vertical_policy=f"{placement.vertical_policy}+earlier_row_obstacle",
                fit=placement.fit and output_bbox[3] <= allowed_bbox[3] + 0.01,
            )
        resolved.append(placement)
        index += 1
    return replace(plan, placements=tuple(resolved))


def _plan_table_with_transforms(
    table_template,
    table_bundle,
    transforms,
    repaint_ids,
    font_file,
    bold_font_file,
):
    table_template = replace(
        table_template,
        structure=replace(
            table_template.structure,
            direct_evidence=tuple(
                dict.fromkeys((*table_template.structure.direct_evidence, "composite_table_layout"))
            ),
        ),
    )
    moved = any(item.moved for item in transforms)
    if not moved and not repaint_ids:
        return plan_table_layout(
            table_template,
            table_bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
    moved_cell_ids = {
        cell.container_id
        for cell in table_template.cells
        if (transform := _transform_for_rect(cell.cell_bbox, transforms)) is not None
        and transform.moved
    }
    planning_base = _transform_table_template(table_template, transforms) if moved else table_template
    translated_by_id = {item.container_id: item.translated_text for item in table_bundle.translations}
    planning_cells = tuple(
        replace(
            cell,
            role=(
                "protected_repaint"
                if not cell.translatable and cell.container_id in moved_cell_ids | repaint_ids
                else cell.role
            ),
            translatable=(
                cell.translatable
                or cell.container_id in moved_cell_ids
                or cell.container_id in repaint_ids
            ),
            protected_tokens=cell.protected_tokens if cell.translatable else (),
        )
        for cell in planning_base.cells
    )
    planning_template = replace(planning_base, cells=planning_cells)
    planning_bundle = PageTranslationBundle(
        table_bundle.request_id,
        table_bundle.page_id,
        table_bundle.provider,
        table_bundle.model,
        tuple(
            TranslationResult(
                cell.container_id,
                translated_by_id.get(cell.container_id, cell.source_text),
            )
            for cell in planning_template.translatable_cells
        ),
        table_bundle.provider_request_id,
        table_bundle.latency_ms,
        table_bundle.response_sha256,
    )
    return plan_table_layout(
        planning_template,
        planning_bundle,
        font_file=font_file,
        bold_font_file=bold_font_file,
    )


def _elastic_table_region_scales(table_template, table_plan, transforms) -> dict[int, float]:
    placements = {item.container_id: item for item in table_plan.placements}
    output = {}
    for index, transform in enumerate(transforms):
        font_ratios = []
        for cell in table_template.translatable_cells:
            if _transform_for_rect(cell.cell_bbox, (transform,)) is None:
                continue
            placement = placements.get(cell.container_id)
            if placement is not None:
                font_ratios.append(placement.font_size / max(cell.font_size, 0.1))
        if font_ratios and min(font_ratios) < 0.90:
            output[index] = round(min(1.50, max(1.05, 0.90 / min(font_ratios))), 4)
    return output


def _reserve_following_flow_space(template, flow_plans, transforms, elastic_region_scales):
    if not elastic_region_scales:
        return flow_plans
    plans = list(flow_plans)
    flow_index = {region.region_id: index for index, region in enumerate(template.flow_regions)}
    excluded_roles = {"margin", "anchored", "image_anchored"}
    for index in sorted(elastic_region_scales):
        transform = transforms[index]
        following = next(
            (
                region
                for region in template.flow_regions
                if abs(region.allowed_bbox[1] - transform.source_bbox[3]) <= 0.75
            ),
            None,
        )
        if following is None:
            continue
        plan_index = flow_index[following.region_id]
        plan = plans[plan_index]
        movable = [item for item in plan.placements if item.role not in excluded_roles]
        if not movable:
            continue
        table_height = transform.source_bbox[3] - transform.source_bbox[1]
        desired_bottom = transform.target_bbox[1] + table_height * elastic_region_scales[index]
        required_shift = max(0.0, desired_bottom + 4.0 - min(item.output_bbox[1] for item in movable))
        available_shift = max(
            0.0,
            min(following.allowed_bbox[3], plan.content_bottom)
            - max(item.output_bbox[3] for item in movable),
        )
        shift = min(required_shift, available_shift)
        if shift <= 0.05:
            continue
        movable_ids = {item.container_id for item in movable}
        placements = tuple(
            replace(
                item,
                output_bbox=(
                    item.output_bbox[0],
                    round(item.output_bbox[1] + shift, 4),
                    item.output_bbox[2],
                    round(item.output_bbox[3] + shift, 4),
                ),
                vertical_policy=f"{item.vertical_policy}+table_elastic_space",
                fit=item.fit and item.output_bbox[3] + shift <= min(following.allowed_bbox[3], plan.content_bottom) + 0.01,
            )
            if item.container_id in movable_ids
            else item
            for item in plan.placements
        )
        plans[plan_index] = replace(plan, placements=placements)
    return tuple(plans)


def _expand_elastic_table_regions(
    template,
    flow_plans,
    facts,
    transforms,
    elastic_region_scales,
):
    if not elastic_region_scales:
        return transforms
    flow_by_region = {
        region.region_id: flow_plan
        for region, flow_plan in zip(template.flow_regions, flow_plans)
    }
    expanded = []
    excluded_roles = {"margin", "anchored", "image_anchored"}
    for index, transform in enumerate(transforms):
        source_bbox = transform.source_bbox
        if index not in elastic_region_scales or _contains_locked_table_artwork(source_bbox, facts):
            expanded.append(transform)
            continue
        table_height = source_bbox[3] - source_bbox[1]
        target_top = transform.target_bbox[1]
        target_bottom = transform.target_bbox[3]
        preceding = next(
            (
                region
                for region in template.flow_regions
                if abs(region.allowed_bbox[3] - source_bbox[1]) <= 0.75
            ),
            None,
        )
        if preceding is not None:
            rows = [
                item
                for item in flow_by_region[preceding.region_id].placements
                if item.role not in excluded_roles
            ]
            if rows:
                candidate_top = max(
                    source_bbox[1] - table_height * 0.25,
                    max(item.output_bbox[3] for item in rows) + 4.0,
                )
                target_top = min(target_top, candidate_top)
        following = next(
            (
                region
                for region in template.flow_regions
                if abs(region.allowed_bbox[1] - source_bbox[3]) <= 0.75
            ),
            None,
        )
        following_rows = []
        if following is not None:
            following_rows = list(flow_by_region[following.region_id].placements)
        bottom_limit = (
            min(item.output_bbox[1] for item in following_rows)
            if following_rows
            else _next_locked_object_top(template, facts, source_bbox)
        )
        candidate_bottom = min(
            target_top + table_height * elastic_region_scales[index],
            bottom_limit - 4.0,
        )
        target_bottom = max(target_bottom, candidate_bottom)
        target_bbox = (
            transform.target_bbox[0],
            round(target_top, 4),
            transform.target_bbox[2],
            round(target_bottom, 4),
        )
        expanded.append(
            replace(
                transform,
                target_bbox=target_bbox,
                target_gap=(
                    round(target_top - max(
                        item.output_bbox[3]
                        for item in flow_by_region[preceding.region_id].placements
                        if item.role not in excluded_roles
                    ), 4)
                    if preceding is not None
                    and any(item.role not in excluded_roles for item in flow_by_region[preceding.region_id].placements)
                    else transform.target_gap
                ),
            )
        )
    return tuple(expanded)


def _next_locked_object_top(template, facts: PageFacts, source_bbox: Rect) -> float:
    protected_ids = {
        item.object_id for item in template.ownerships if item.owner == "protected"
    }
    bboxes = [
        item.bbox
        for item in facts.text_objects
        if item.object_id in protected_ids
        and item.bbox[1] >= source_bbox[3] - 0.75
        and _horizontal_overlap(item.bbox, source_bbox) > 0.5
    ]
    bboxes.extend(
        item.bbox
        for item in (*facts.image_objects, *facts.drawing_objects)
        if item.bbox[1] >= source_bbox[3] - 0.75
        and _horizontal_overlap(item.bbox, source_bbox) > 0.5
    )
    return min((item[1] for item in bboxes), default=facts.height)


def validate_owner_boundaries(
    template: CompositePageTemplate,
    plan: CompositeLayoutPlan,
    facts: PageFacts | None = None,
) -> tuple[CompositeFinding, ...]:
    findings: list[CompositeFinding] = []
    protected_rows = []
    if facts is not None:
        protected_ids = {
            item.object_id for item in template.ownerships if item.owner == "protected"
        }
        protected_rows = [
            (item.object_id, item.bbox)
            for item in facts.text_objects
            if item.object_id in protected_ids
        ]
    flow_rows: list[tuple[str, Rect]] = []
    for region, flow_plan in zip(template.flow_regions, plan.flow_plans):
        for placement in flow_plan.placements:
            flow_rows.append((placement.container_id, placement.output_bbox))
            if not _contains(region.allowed_bbox, placement.output_bbox):
                findings.append(
                    _finding(
                        "FLOW_REGION_WRITE_ESCAPE",
                        "flow_layout_planner",
                        placement.container_id,
                        "正文译文离开其已分配区域",
                        region_id=region.region_id,
                        allowed_bbox=region.allowed_bbox,
                        output_bbox=placement.output_bbox,
                    )
                )
            for table_bbox in (item.target_bbox for item in plan.table_region_transforms):
                if _intersects(placement.output_bbox, table_bbox):
                    findings.append(
                        _finding(
                            "CROSS_REGION_WRITE",
                            "composite_layout_planner",
                            placement.container_id,
                            "正文译文进入表格所有权区域",
                            flow_bbox=placement.output_bbox,
                            table_bbox=table_bbox,
                        )
                    )
            for protected_id, protected_bbox in protected_rows:
                if _intersects(placement.output_bbox, protected_bbox):
                    findings.append(
                        _finding(
                            "PROTECTED_TEXT_WRITE_OVERLAP",
                            "composite_layout_planner",
                            placement.container_id,
                            "正文译文写入区域与受保护文字对象相交",
                            protected_object_id=protected_id,
                            flow_bbox=placement.output_bbox,
                            protected_bbox=protected_bbox,
                        )
                    )

    table_rows = []
    for placement in plan.table_plan.placements:
        table_rows.append((placement.container_id, placement.output_bbox))
        target_table_regions = tuple(item.target_bbox for item in plan.table_region_transforms)
        if not any(_contains(table_bbox, placement.output_bbox) for table_bbox in target_table_regions):
            findings.append(
                _finding(
                    "TABLE_REGION_WRITE_ESCAPE",
                    "table_layout_planner",
                    placement.container_id,
                    "表格译文离开表格所有权区域",
                    table_regions=target_table_regions,
                    output_bbox=placement.output_bbox,
                )
            )

    for flow_id, flow_bbox in flow_rows:
        for table_id, translated_table_bbox in table_rows:
            if _intersects(flow_bbox, translated_table_bbox):
                findings.append(
                    _finding(
                        "CROSS_OWNER_TEXT_OVERLAP",
                        "composite_layout_planner",
                        flow_id,
                        "正文与表格译文容器相交",
                        other_container_id=table_id,
                        flow_bbox=flow_bbox,
                        table_bbox=translated_table_bbox,
                    )
                )
    return tuple(_deduplicate_findings(findings))


def _flow_finding(item) -> CompositeFinding:
    return CompositeFinding(
        item.code,
        item.severity,
        item.owner,
        item.container_id,
        item.message,
        {},
    )


def _build_table_region_transforms(
    template: CompositePageTemplate,
    flow_plans,
    facts: PageFacts,
) -> tuple[TableRegionTransform, ...]:
    flow_by_region = {
        region.region_id: flow_plan
        for region, flow_plan in zip(template.flow_regions, flow_plans)
    }
    transforms: list[TableRegionTransform] = []
    for source_bbox in template.table_regions:
        if _contains_locked_table_artwork(source_bbox, facts):
            transforms.append(TableRegionTransform(source_bbox, source_bbox, None, None, None))
            continue
        preceding = next(
            (
                region
                for region in template.flow_regions
                if abs(region.allowed_bbox[3] - source_bbox[1]) <= 0.75
            ),
            None,
        )
        if preceding is None:
            transforms.append(TableRegionTransform(source_bbox, source_bbox, None, None, None))
            continue
        excluded_roles = {"margin", "anchored", "image_anchored"}
        source_rows = [item for item in preceding.template.containers if item.role not in excluded_roles]
        target_rows = [item for item in flow_by_region[preceding.region_id].placements if item.role not in excluded_roles]
        if not source_rows or not target_rows:
            transforms.append(TableRegionTransform(source_bbox, source_bbox, preceding.region_id, None, None))
            continue
        source_bottom = max(item.source_bbox[3] for item in source_rows)
        target_bottom = max(item.output_bbox[3] for item in target_rows)
        source_gap = max(4.0, source_bbox[1] - source_bottom)
        desired_top = target_bottom + source_gap
        table_height = source_bbox[3] - source_bbox[1]
        target_top = max(desired_top, source_bbox[1] - table_height * 0.25)
        if source_bbox[1] - target_top < 2.0:
            target_top = source_bbox[1]
        target_bbox = (
            source_bbox[0],
            round(min(source_bbox[1], target_top), 4),
            source_bbox[2],
            source_bbox[3],
        )
        transforms.append(
            TableRegionTransform(
                source_bbox,
                target_bbox,
                preceding.region_id,
                round(source_gap, 4),
                round(target_bbox[1] - target_bottom, 4),
            )
        )
    return tuple(transforms)


def _overlapping_protected_cell_ids(table_template) -> set[str]:
    translated = [cell for cell in table_template.cells if cell.translatable]
    return {
        cell.container_id
        for cell in table_template.cells
        if not cell.translatable
        and any(_intersects(cell.source_bbox, other.source_bbox) for other in translated)
    }


def _contains_locked_table_artwork(source_bbox: Rect, facts: PageFacts) -> bool:
    page_area = facts.width * facts.height
    if any(
        _rect_area(item.bbox) >= page_area * 0.50
        and not _is_page_background_bbox(item.bbox, facts)
        and _intersects(source_bbox, item.bbox)
        for item in facts.drawing_objects
    ):
        return True
    for item in facts.image_objects:
        center_x = (item.bbox[0] + item.bbox[2]) / 2.0
        center_y = (item.bbox[1] + item.bbox[3]) / 2.0
        image_area = (item.bbox[2] - item.bbox[0]) * (item.bbox[3] - item.bbox[1])
        if image_area >= page_area * 0.25:
            continue
        if source_bbox[0] <= center_x <= source_bbox[2] and source_bbox[1] <= center_y <= source_bbox[3]:
            return True
    return False


def _is_page_background_bbox(rect: Rect, facts: PageFacts) -> bool:
    tolerance = 2.0
    return (
        rect[0] <= tolerance
        and rect[1] <= tolerance
        and rect[2] >= facts.width - tolerance
        and rect[3] >= facts.height - tolerance
    )


def _rect_area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _transform_table_template(table_template, transforms):
    transformed_cells = []
    for cell in table_template.cells:
        transform = _transform_for_rect(cell.cell_bbox, transforms)
        if transform is None:
            transformed_cells.append(cell)
            continue
        transformed_cells.append(
            replace(
                cell,
                source_bbox=_transform_rect(cell.source_bbox, transform),
                cell_bbox=_transform_rect(cell.cell_bbox, transform),
            )
        )
    structure = table_template.structure
    row_boundaries = tuple(_transform_y_in_regions(value, transforms) for value in structure.row_boundaries)
    target_regions = tuple(item.target_bbox for item in transforms)
    vertically_expanded = any(
        item.target_bbox[3] - item.target_bbox[1]
        > item.source_bbox[3] - item.source_bbox[1] + 0.5
        for item in transforms
    )
    transformed_structure = replace(
        structure,
        bbox=(
            min(item[0] for item in target_regions),
            min(item[1] for item in target_regions),
            max(item[2] for item in target_regions),
            max(item[3] for item in target_regions),
        ),
        row_boundaries=row_boundaries,
        direct_evidence=(
            tuple(dict.fromkeys((*structure.direct_evidence, "vertical_elastic_rows")))
            if vertically_expanded
            else structure.direct_evidence
        ),
    )
    return replace(table_template, structure=transformed_structure, cells=tuple(transformed_cells))


def _transform_for_rect(rect: Rect, transforms) -> TableRegionTransform | None:
    center_y = (rect[1] + rect[3]) / 2.0
    return next(
        (item for item in transforms if item.source_bbox[1] - 0.75 <= center_y <= item.source_bbox[3] + 0.75),
        None,
    )


def _transform_rect(rect: Rect, transform: TableRegionTransform) -> Rect:
    return (
        rect[0],
        round(_transform_y(rect[1], transform), 4),
        rect[2],
        round(_transform_y(rect[3], transform), 4),
    )


def _transform_y_in_regions(value: float, transforms) -> float:
    transform = next(
        (item for item in transforms if item.source_bbox[1] - 0.01 <= value <= item.source_bbox[3] + 0.01),
        None,
    )
    return round(_transform_y(value, transform), 4) if transform else value


def _transform_y(value: float, transform: TableRegionTransform) -> float:
    source = transform.source_bbox
    target = transform.target_bbox
    scale = (target[3] - target[1]) / (source[3] - source[1])
    return target[1] + (value - source[1]) * scale


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence) -> CompositeFinding:
    return CompositeFinding(code, "HARD", owner, container_id, message, evidence)


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersects(left: Rect, right: Rect, tolerance: float = 0.05) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > tolerance
        and min(left[3], right[3]) - max(left[1], right[1]) > tolerance
    )


def _horizontal_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _deduplicate_findings(findings: list[CompositeFinding]) -> list[CompositeFinding]:
    output = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.container_id, finding.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return output
