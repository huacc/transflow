from __future__ import annotations

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle
from toolboxes.body.anchored_blocks.tools.layout_planner import plan_anchored_layout
from toolboxes.body.chart.tools.layout_planner import layout_rule_trace, plan_chart_layout

from . import TOOLBOX_KEY
from .models import (
    CompositeFinding,
    CompositeLayoutPlan,
    CompositePageTemplate,
    CompositePlacement,
    Rect,
)
from .translation_request import slice_translation_bundle


def plan_composite_layout(
    template: CompositePageTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None,
) -> tuple[CompositeLayoutPlan, tuple[CompositeFinding, ...], tuple[dict[str, object], ...]]:
    if template.anchored_template is None or template.chart_template is None:
        raise ValueError("P15_LEAF_TEMPLATE_REQUIRED")
    anchored_bundle, chart_bundle = slice_translation_bundle(template, bundle)
    anchored_plan, anchored_findings = plan_anchored_layout(
        template.anchored_template,
        anchored_bundle,
        font_file=font_file,
        bold_font_file=bold_font_file,
    )
    chart_plan, chart_findings = plan_chart_layout(
        template.chart_template,
        chart_bundle,
        font_file=font_file,
        bold_font_file=bold_font_file,
    )

    composite_by_base = {
        (item.owner, item.base_container_id): item for item in template.containers
    }
    anchored_source = {
        item.container_id: item for item in template.anchored_template.containers
    }
    placements: list[CompositePlacement] = []
    for item in anchored_plan.placements:
        container = composite_by_base[("anchored", item.container_id)]
        placements.append(
            CompositePlacement(
                composite_id=container.composite_id,
                owner="anchored",
                base_container_id=item.container_id,
                translated_text=item.translated_text,
                output_bbox=item.output_bbox,
                font_file=item.font_file,
                font_resource=f"p15a{item.font_resource}",
                font_size=item.font_size,
                minimum_font_size=max(
                    5.5,
                    anchored_source[item.container_id].font_size * 0.68,
                ),
                line_height=item.line_height,
                color_srgb=item.color_srgb,
                alignment=item.alignment,
                profile=f"p11/{item.profile}",
                fit=item.fit,
            )
        )

    for item in chart_plan.placements:
        container = next(
            composite_by_base[(owner, item.container_id)]
            for owner in ("chart", "shared")
            if (owner, item.container_id) in composite_by_base
        )
        placements.append(
            CompositePlacement(
                composite_id=container.composite_id,
                owner=container.owner,
                base_container_id=item.container_id,
                translated_text=item.translated_text,
                output_bbox=item.output_bbox,
                font_file=item.font_file,
                font_resource=f"p15c{item.font_resource}",
                font_size=item.font_size,
                minimum_font_size=item.minimum_font_size,
                line_height=item.line_height,
                color_srgb=item.color_srgb,
                alignment=item.alignment,
                profile=f"p13/{item.profile}",
                fit=item.fit,
                rotation=item.rotation,
            )
        )
    order = {item.composite_id: item.reading_order for item in template.containers}
    placements.sort(key=lambda item: order[item.composite_id])

    findings = [
        CompositeFinding(
            item.code,
            item.severity,
            item.owner,
            "anchored",
            f"anchored::{item.container_id}" if item.container_id else None,
            item.message,
            item.evidence,
        )
        for item in anchored_findings
    ]
    chart_owner_by_base = {
        item.base_container_id: item.owner
        for item in template.containers
        if item.owner in {"chart", "shared"}
    }
    findings.extend(
        CompositeFinding(
            item.code,
            item.severity,
            item.owner,
            chart_owner_by_base.get(item.container_id or "", "chart"),
            (
                f"{chart_owner_by_base[item.container_id]}::{item.container_id}"
                if item.container_id in chart_owner_by_base
                else None
            ),
            item.message,
            item.evidence,
        )
        for item in chart_findings
    )
    findings.extend(_cross_owner_findings(template, placements))

    plan = CompositeLayoutPlan(
        page_id=template.page_id,
        toolbox_key=TOOLBOX_KEY,
        structure_sha256=template.structure_sha256,
        placements=tuple(placements),
        anchored_plan=anchored_plan,
        chart_plan=chart_plan,
    )
    trace = tuple(
        [
            {
                "schema_version": "p15-composite-layout-rule/v1",
                "container_id": item.composite_id,
                "owner": item.owner,
                "child_rule": "P11",
                "profile": item.profile,
                "fit": item.fit,
            }
            for item in placements
            if item.owner == "anchored"
        ]
        + [
            {
                **record,
                "schema_version": "p15-composite-layout-rule/v1",
                "container_id": next(
                    item.composite_id
                    for item in template.containers
                    if item.base_container_id == record["container_id"]
                    and item.owner in {"chart", "shared"}
                ),
                "owner": chart_owner_by_base[record["container_id"]],
                "child_rule": "P13",
            }
            for record in layout_rule_trace(template.chart_template, chart_plan)
        ]
    )
    return plan, tuple(findings), trace


def _cross_owner_findings(
    template: CompositePageTemplate,
    placements: list[CompositePlacement],
) -> tuple[CompositeFinding, ...]:
    fitted = [item for item in placements if item.fit]
    glyph_boxes = {item.composite_id: _glyph_bbox(template, item) for item in fitted}
    collisions = []
    for index, left in enumerate(fitted):
        for right in fitted[index + 1:]:
            if left.owner == right.owner:
                continue
            overlap = _intersection_area(glyph_boxes[left.composite_id], glyph_boxes[right.composite_id])
            if overlap > 0.20:
                collisions.append(
                    {
                        "left": left.composite_id,
                        "right": right.composite_id,
                        "left_owner": left.owner,
                        "right_owner": right.owner,
                        "overlap_area": round(overlap, 4),
                    }
                )
    if not collisions:
        return ()
    return (
        CompositeFinding(
            "CROSS_OWNER_TEXT_COLLISION",
            "HARD",
            "composite_layout_planner",
            None,
            None,
            "P11 与 P13 译文字形发生跨 owner 碰撞，禁止借用对方区域。",
            {"collisions": collisions},
        ),
    )


def _glyph_bbox(template: CompositePageTemplate, placement: CompositePlacement) -> Rect:
    with fitz.open() as document:
        page = document.new_page(width=template.width, height=template.height)
        page.insert_textbox(
            fitz.Rect(placement.output_bbox),
            placement.translated_text,
            fontname=placement.font_resource,
            fontfile=placement.font_file,
            fontsize=placement.font_size,
            lineheight=placement.line_height,
            align={
                "LEFT": fitz.TEXT_ALIGN_LEFT,
                "CENTER": fitz.TEXT_ALIGN_CENTER,
                "RIGHT": fitz.TEXT_ALIGN_RIGHT,
            }[placement.alignment],
            rotate=placement.rotation,
        )
        bboxes = [
            tuple(float(value) for value in span["bbox"])
            for block in page.get_text("dict").get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
    if not bboxes:
        return placement.output_bbox
    return (
        min(item[0] for item in bboxes),
        min(item[1] for item in bboxes),
        max(item[2] for item in bboxes),
        max(item[3] for item in bboxes),
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )
