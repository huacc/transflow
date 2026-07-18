from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from statistics import median

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle
from toolboxes.body.chart.tools.layout_planner import plan_chart_layout
from toolboxes.body.chart.tools.models import (
    ChartFinding,
    ChartLayoutPlan,
    ChartPlacement,
    Rect,
)
from toolboxes.body.flow_text.single.tools.p4_layout_planner import (
    P4_PROFILES,
    plan_with_profile,
)

from .. import TOOLBOX_KEY
from .models import FlowRegionPlan, FlowTextChartLayoutPlan, FlowTextChartTemplate


def plan_flow_text_chart_layout(
    *,
    facts: PageFacts,
    template: FlowTextChartTemplate,
    bundle: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[
    FlowTextChartLayoutPlan,
    tuple[ChartFinding, ...],
    tuple[dict[str, object], ...],
]:
    translated_ids = tuple(item.container_id for item in bundle.translations)
    if len(translated_ids) != len(set(translated_ids)):
        raise ValueError("P17_DUPLICATE_TRANSLATION_CONTAINER_ID")
    render_order = tuple(item.container_id for item in template.render_template.containers)
    if any(container_id not in render_order for container_id in translated_ids):
        raise ValueError("P17_TRANSLATION_ID_OUTSIDE_RENDER_TEMPLATE")

    findings: list[ChartFinding] = []
    trace: list[dict[str, object]] = []
    flow_region_plans: list[FlowRegionPlan] = []
    flow_placements: list[ChartPlacement] = []

    for region in template.flow_regions:
        if region.mode != "single":
            raise ValueError(f"P17_UNSUPPORTED_FLOW_REGION_MODE:{region.mode}")
        region_ids = {
            item.container_id
            for item in region.template.containers
        }
        selected_containers = tuple(
            item
            for item in region.template.containers
            if item.container_id in translated_ids
        )
        if not selected_containers:
            continue
        region_template = replace(region.template, containers=selected_containers)
        region_bundle = _subset_bundle(
            bundle,
            tuple(item.container_id for item in selected_containers),
        )
        flow_plan = None
        selected_findings: tuple[ChartFinding, ...] = ()
        selected_profile_id = ""
        selected_trace_index: int | None = None

        def evaluate(profile, attempt_index):
            attempt_plan, p4_findings = plan_with_profile(
                facts=facts,
                template=region_template,
                translations=region_bundle,
                source_language=source_language,
                target_language=target_language,
                font_file=font_file,
                font_resource="p17flow",
                profile=profile,
            )
            converted = _flow_chart_placements(attempt_plan)
            boundary_findings = _flow_boundary_findings(
                region.region_id,
                region.allowed_bbox,
                template.chart_guard_regions,
                converted,
            )
            attempt_findings = tuple(
                ChartFinding(
                    item.code,
                    item.severity,
                    "flow_layout_planner",
                    region.region_id,
                    item.container_id,
                    item.message,
                    {"profile_id": profile.profile_id},
                )
                for item in p4_findings
            ) + boundary_findings + _flow_line_break_findings(attempt_plan, converted, region.region_id)
            fit = not any(item.severity == "HARD" for item in attempt_findings)
            trace.append(
                {
                    "schema_version": "p17-flow-layout-attempt/v1",
                    "region_id": region.region_id,
                    "region_mode": region.mode,
                    "attempt_index": attempt_index,
                    "profile_id": profile.profile_id,
                    "fit": fit,
                    "finding_codes": [item.code for item in attempt_findings],
                    "selected": False,
                }
            )
            return attempt_plan, attempt_findings, fit

        for attempt_index, profile in enumerate(P4_PROFILES):
            attempt_plan, attempt_findings, fit = evaluate(profile, attempt_index)
            flow_plan = attempt_plan
            selected_findings = attempt_findings
            selected_profile_id = profile.profile_id
            if fit:
                selected_trace_index = len(trace) - 1
                break
        if flow_plan is None:
            raise RuntimeError(f"P17_FLOW_PLAN_NOT_MATERIALIZED:{region.region_id}")
        body_placements = tuple(
            item
            for item in flow_plan.placements
            if item.role in {"body", "list"}
        )
        vertical_slack = region.allowed_bbox[3] - max(
            item.output_bbox[3]
            for item in flow_plan.placements
        )
        if (
            selected_trace_index is not None
            and body_placements
            and vertical_slack >= median(item.font_size for item in body_placements) * 3.0
        ):
            base_profile = next(item for item in P4_PROFILES if item.profile_id == selected_profile_id)
            relaxed_profiles = (
                replace(
                    base_profile,
                    profile_id=f"{base_profile.profile_id}+vertical-relaxed",
                    line_height=max(1.55, base_profile.line_height),
                    gap_scale=max(1.35, base_profile.gap_scale),
                ),
                replace(
                    base_profile,
                    profile_id=f"{base_profile.profile_id}+vertical-balanced",
                    line_height=max(1.40, base_profile.line_height),
                    gap_scale=max(1.15, base_profile.gap_scale),
                ),
            )
            for relaxed_profile in relaxed_profiles:
                attempt_plan, attempt_findings, fit = evaluate(relaxed_profile, len(trace))
                if not fit:
                    continue
                flow_plan = attempt_plan
                selected_findings = attempt_findings
                selected_profile_id = relaxed_profile.profile_id
                selected_trace_index = len(trace) - 1
                break
        if selected_trace_index is None:
            raise RuntimeError(f"P17_FLOW_PROFILE_NOT_SELECTED:{region.region_id}")
        trace[selected_trace_index]["selected"] = True
        flow_region_plans.append(
            FlowRegionPlan(region.region_id, region.mode, region.allowed_bbox, flow_plan)
        )
        findings.extend(selected_findings)
        converted = _flow_chart_placements(flow_plan)
        flow_placements.extend(converted)
        if flow_plan.profile_id != selected_profile_id:
            raise RuntimeError("P17_FLOW_PROFILE_SELECTION_MISMATCH")
        if set(item.container_id for item in converted) - region_ids:
            raise ValueError("P17_FLOW_PLAN_CONTAINER_OUTSIDE_REGION")

    chart_ids = tuple(
        item.container_id
        for item in template.chart_template.containers
        if item.container_id in translated_ids
    )
    if chart_ids:
        chart_bundle = _subset_bundle(bundle, chart_ids)
        chart_plan, chart_findings = plan_chart_layout(
            template.chart_template,
            chart_bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        findings.extend(chart_findings)
    else:
        chart_plan = ChartLayoutPlan(
            template.page_id,
            TOOLBOX_KEY,
            template.chart_template.structure_sha256,
            (),
        )

    combined_by_id = {
        item.container_id: item
        for item in (*flow_placements, *chart_plan.placements)
    }
    if len(combined_by_id) != len(flow_placements) + len(chart_plan.placements):
        raise ValueError("P17_DUPLICATE_RENDER_PLACEMENT")
    if set(combined_by_id) != set(translated_ids):
        raise ValueError("P17_RENDER_PLACEMENT_REQUEST_MISMATCH")
    render_placements = tuple(
        combined_by_id[container_id]
        for container_id in render_order
        if container_id in combined_by_id
    )
    render_plan = ChartLayoutPlan(
        template.page_id,
        TOOLBOX_KEY,
        template.render_template.structure_sha256,
        render_placements,
    )
    findings.extend(_cross_owner_findings(template, render_plan))
    return (
        FlowTextChartLayoutPlan(
            template.page_id,
            TOOLBOX_KEY,
            source_language,
            target_language,
            tuple(flow_region_plans),
            chart_plan,
            render_plan,
            template.structure_sha256,
        ),
        tuple(findings),
        tuple(trace),
    )


def _subset_bundle(
    bundle: PageTranslationBundle,
    ordered_ids: tuple[str, ...],
) -> PageTranslationBundle:
    by_id = {item.container_id: item for item in bundle.translations}
    if any(container_id not in by_id for container_id in ordered_ids):
        raise ValueError("P17_TRANSLATION_SUBSET_ID_MISSING")
    return replace(
        bundle,
        translations=tuple(by_id[container_id] for container_id in ordered_ids),
    )


def _flow_chart_placements(plan) -> tuple[ChartPlacement, ...]:
    placements: list[ChartPlacement] = []
    for item in plan.placements:
        selected_font, selected_resource = _font_variant(
            plan.font_file,
            plan.font_resource,
            item.font_weight,
        )
        placements.append(
            ChartPlacement(
                item.container_id,
                item.translated_text,
                item.output_bbox,
                selected_font,
                selected_resource,
                item.font_size,
                max(5.5, round(item.source_font_size * 0.72, 4)),
                item.line_height,
                item.color_srgb,
                "LEFT",
                f"p4/{plan.profile_id}/{item.horizontal_policy}/{item.vertical_policy}",
                item.fit,
                0,
            )
        )
    return tuple(placements)


def _flow_line_break_findings(
    plan,
    placements: tuple[ChartPlacement, ...],
    region_id: str,
) -> tuple[ChartFinding, ...]:
    findings: list[ChartFinding] = []
    for placement in placements:
        lines = _rendered_lines(
            page_width=max(plan.column_right + 1.0, placement.output_bbox[2] + 1.0),
            page_height=max(plan.content_bottom + 1.0, placement.output_bbox[3] + 1.0),
            width=placement.output_bbox[2] - placement.output_bbox[0],
            height=placement.output_bbox[3] - placement.output_bbox[1],
            text=placement.translated_text,
            font_size=placement.font_size,
            line_height=placement.line_height,
            font_file=placement.font_file,
            font_resource=placement.font_resource,
            color_srgb=placement.color_srgb,
        )
        invalid = tuple(
            line
            for line in lines[1:]
            if re.match(r"^[，。；：！？、）】》”’…]", line)
        )
        if not invalid:
            continue
        findings.append(
            ChartFinding(
                "P17_FLOW_LINE_START_PUNCTUATION",
                "HARD",
                "flow_layout_planner",
                region_id,
                placement.container_id,
                "A wrapped flow line starts with closing punctuation.",
                {"line_prefixes": [line[:12] for line in invalid]},
            )
        )
    return tuple(findings)


def _flow_boundary_findings(
    region_id: str,
    allowed_bbox: Rect,
    chart_regions: tuple[Rect, ...],
    placements: tuple[ChartPlacement, ...],
) -> tuple[ChartFinding, ...]:
    findings: list[ChartFinding] = []
    for placement in placements:
        if not _contains(allowed_bbox, placement.output_bbox):
            findings.append(
                ChartFinding(
                    "P17_FLOW_OWNER_ESCAPE",
                    "HARD",
                    "flow_layout_planner",
                    region_id,
                    placement.container_id,
                    "Flow-owned translated text escaped its FlowBand.",
                    {
                        "allowed_bbox": allowed_bbox,
                        "output_bbox": placement.output_bbox,
                    },
                )
            )
        invaded = tuple(
            region
            for region in chart_regions
            if _intersection_area(region, placement.output_bbox) > 0.5
        )
        if invaded:
            findings.append(
                ChartFinding(
                    "P17_FLOW_OWNER_INVADES_CHART",
                    "HARD",
                    "flow_layout_planner",
                    region_id,
                    placement.container_id,
                    "Flow-owned translated text invaded a chart guard region.",
                    {"output_bbox": placement.output_bbox, "chart_regions": invaded},
                )
            )
    return tuple(findings)


def _cross_owner_findings(
    template: FlowTextChartTemplate,
    plan: ChartLayoutPlan,
) -> tuple[ChartFinding, ...]:
    owners = {item.container_id: item.owner for item in template.container_ownerships}
    placements = tuple(item for item in plan.placements if item.fit)
    candidate_pairs: list[tuple[ChartPlacement, ChartPlacement]] = []
    for index, left in enumerate(placements):
        left_owner = owners[left.container_id]
        for right in placements[index + 1 :]:
            right_owner = owners[right.container_id]
            if left_owner == right_owner or "shared" in {left_owner, right_owner}:
                continue
            if _intersection_area(left.output_bbox, right.output_bbox) > 0.5:
                candidate_pairs.append((left, right))

    measured = {
        placement.container_id: _placement_glyph_bbox(template.width, template.height, placement)
        for pair in candidate_pairs
        for placement in pair
    }
    findings: list[ChartFinding] = []
    for left, right in candidate_pairs:
        left_owner = owners[left.container_id]
        right_owner = owners[right.container_id]
        left_bbox = measured[left.container_id]
        right_bbox = measured[right.container_id]
        overlap = _intersection_area(left_bbox, right_bbox)
        if overlap <= 0.5:
            continue
        findings.append(
            ChartFinding(
                "P17_CROSS_OWNER_COLLISION",
                "HARD",
                "flow_text_chart_layout_planner",
                None,
                left.container_id,
                "Translated glyphs owned by flow and chart overlap.",
                {
                    "left_container_id": left.container_id,
                    "left_owner": left_owner,
                    "left_glyph_bbox": left_bbox,
                    "right_container_id": right.container_id,
                    "right_owner": right_owner,
                    "right_glyph_bbox": right_bbox,
                    "intersection_area": round(overlap, 4),
                },
            )
        )
    return tuple(findings)


def _placement_glyph_bbox(
    page_width: float,
    page_height: float,
    placement: ChartPlacement,
) -> Rect:
    with fitz.open() as document:
        page = document.new_page(width=page_width, height=page_height)
        spare = page.insert_textbox(
            fitz.Rect(placement.output_bbox),
            placement.translated_text,
            fontname=placement.font_resource,
            fontfile=placement.font_file,
            fontsize=placement.font_size,
            lineheight=placement.line_height,
            color=(0.0, 0.0, 0.0),
            align={"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(placement.alignment, 0),
            rotate=placement.rotation,
        )
        if spare < 0:
            return placement.output_bbox
        boxes = [
            tuple(float(value) for value in character["bbox"])
            for block in page.get_text("rawdict").get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            for character in span.get("chars", [])
            if str(character.get("c") or "").strip() and character.get("bbox")
        ]
    if not boxes:
        return placement.output_bbox
    return (
        min(item[0] for item in boxes),
        min(item[1] for item in boxes),
        max(item[2] for item in boxes),
        max(item[3] for item in boxes),
    )


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.01) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _font_variant(font_file: str, font_resource: str, font_weight: str) -> tuple[str, str]:
    if font_weight != "bold":
        return font_file, font_resource
    path = Path(font_file)
    candidates = []
    if path.name.casefold() == "msyh.ttc":
        candidates.append(path.with_name("msyhbd.ttc"))
    candidates.append(path.with_name(f"{path.stem}-Bold{path.suffix}"))
    candidates.append(path.with_name(f"{path.stem}bd{path.suffix}"))
    bold_file = next((candidate for candidate in candidates if candidate.is_file()), None)
    return (str(bold_file), f"{font_resource}_bold") if bold_file else (font_file, font_resource)


def _rendered_lines(
    *,
    page_width: float,
    page_height: float,
    width: float,
    height: float,
    text: str,
    font_size: float,
    line_height: float,
    font_file: str,
    font_resource: str,
    color_srgb: int,
) -> tuple[str, ...]:
    with fitz.open() as probe:
        page = probe.new_page(width=page_width, height=max(page_height, height + 10.0))
        page.insert_textbox(
            fitz.Rect(0, 0, width, height + 2.0),
            text,
            fontname=font_resource,
            fontfile=font_file,
            fontsize=font_size,
            lineheight=line_height,
            color=(
                ((color_srgb >> 16) & 255) / 255.0,
                ((color_srgb >> 8) & 255) / 255.0,
                (color_srgb & 255) / 255.0,
            ),
        )
        return tuple(line.strip() for line in page.get_text("text").splitlines() if line.strip())
