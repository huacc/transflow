from __future__ import annotations

from pathlib import Path

from page_toolbox_puncture.contracts import PageFacts
from toolboxes.body.chart.tools.models import (
    ChartLayoutPlan,
    ChartPlacement,
    ChartTemplate,
    ChartTextContainer,
)
from toolboxes.body.chart.tools.layout_planner import materialize_translated_diagnostic_plan
from toolboxes.body.chart.tools.renderer import render_chart_candidate

from .models import (
    CompositeFinding,
    CompositeLayoutPlan,
    CompositePageTemplate,
)


def render_composite_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: CompositePageTemplate,
    plan: CompositeLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[CompositeFinding, ...], dict[str, object]]:
    render_template, render_plan = _render_contract(template, plan)
    translated_unfit_container_ids = [
        item.container_id for item in render_plan.placements if not item.fit
    ]
    diagnostic_materialization: tuple[dict[str, object], ...] = ()
    if translated_unfit_container_ids:
        render_template, render_plan, diagnostic_materialization = (
            materialize_translated_diagnostic_plan(render_template, render_plan)
        )
    chart_findings, evidence = render_chart_candidate(
        source_pdf=source_pdf,
        candidate_pdf=candidate_pdf,
        facts=facts,
        template=render_template,
        plan=render_plan,
        evidence_dir=evidence_dir,
    )
    owner_by_id = {item.composite_id: item.owner for item in template.containers}
    findings = tuple(
        CompositeFinding(
            code=_owner_code(item.code, owner_by_id.get(item.container_id or "")),
            severity=item.severity,
            owner=item.owner,
            region_owner=owner_by_id.get(item.container_id or ""),
            container_id=item.container_id,
            message=item.message,
            evidence=item.evidence,
        )
        for item in chart_findings
    )
    evidence["composite_owners"] = {
        owner: sum(1 for item in template.containers if item.owner == owner)
        for owner in ("anchored", "chart", "shared")
    }
    evidence["rendered_container_count"] = len(render_plan.placements)
    evidence["translated_unfit_container_ids"] = translated_unfit_container_ids
    evidence["omitted_unfit_container_ids"] = []
    evidence["diagnostic_materialization"] = diagnostic_materialization
    evidence["single_pass_composite_render"] = True
    return findings, evidence


def _render_contract(
    template: CompositePageTemplate,
    plan: CompositeLayoutPlan,
) -> tuple[ChartTemplate, ChartLayoutPlan]:
    if template.anchored_template is None or template.chart_template is None:
        raise ValueError("P15_LEAF_TEMPLATE_REQUIRED")
    anchored = {
        item.container_id: item for item in template.anchored_template.containers
    }
    chart = {item.container_id: item for item in template.chart_template.containers}
    render_containers = []
    for item in template.containers:
        if item.owner == "anchored":
            source = anchored[item.base_container_id]
            render_containers.append(
                ChartTextContainer(
                    container_id=item.composite_id,
                    role="ANNOTATION",
                    association_id=source.block_owner_id,
                    source_object_ids=source.source_object_ids,
                    source_text=source.source_text,
                    source_bbox=source.source_bbox,
                    allowed_bbox=source.allowed_bbox,
                    anchor_object_ids=(),
                    anchor_relation="WITHIN",
                    reading_order=item.reading_order,
                    required_literals=source.required_literals,
                    font_name=source.font_name,
                    font_size=source.font_size,
                    color_srgb=source.color_srgb,
                    alignment=source.alignment,
                )
            )
        else:
            source = chart[item.base_container_id]
            render_containers.append(
                ChartTextContainer(
                    container_id=item.composite_id,
                    role=source.role,
                    association_id=source.association_id,
                    source_object_ids=source.source_object_ids,
                    source_text=source.source_text,
                    source_bbox=source.source_bbox,
                    allowed_bbox=source.allowed_bbox,
                    anchor_object_ids=source.anchor_object_ids,
                    anchor_relation=source.anchor_relation,
                    reading_order=item.reading_order,
                    required_literals=source.required_literals,
                    font_name=source.font_name,
                    font_size=source.font_size,
                    color_srgb=source.color_srgb,
                    alignment=source.alignment,
                    rotation=source.rotation,
                )
            )
    render_template = ChartTemplate(
        page_id=template.page_id,
        toolbox_key=template.toolbox_key,
        width=template.width,
        height=template.height,
        visual_regions=template.chart_template.visual_regions,
        containers=tuple(render_containers),
        protected_object_ids=template.protected_object_ids,
        locked_objects_sha256=template.chart_template.locked_objects_sha256,
        structure_sha256=template.structure_sha256,
    )
    render_plan = ChartLayoutPlan(
        page_id=plan.page_id,
        toolbox_key=plan.toolbox_key,
        structure_sha256=plan.structure_sha256,
        placements=tuple(
            ChartPlacement(
                container_id=item.composite_id,
                translated_text=item.translated_text,
                output_bbox=item.output_bbox,
                font_file=item.font_file,
                font_resource=item.font_resource,
                font_size=item.font_size,
                minimum_font_size=item.minimum_font_size,
                line_height=item.line_height,
                color_srgb=item.color_srgb,
                alignment=item.alignment,
                profile=item.profile,
                fit=item.fit,
                rotation=item.rotation,
            )
            for item in plan.placements
        ),
    )
    return render_template, render_plan


def _owner_code(code: str, owner: str | None) -> str:
    if owner != "anchored":
        return code
    return {
        "CHART_TRANSLATION_MISSING": "ANCHORED_TRANSLATION_MISSING",
        "CHART_SOURCE_RESIDUE": "ANCHORED_SOURCE_RESIDUE",
        "CHART_GLYPH_OUTSIDE_SLOT": "ANCHORED_GLYPH_OUTSIDE_SLOT",
        "CHART_IMAGE_TEXT_OVERLAID": "ANCHORED_IMAGE_TEXT_OVERLAID",
        "CHART_TEXT_GRAPHIC_COLLISION": "ANCHORED_TEXT_GRAPHIC_COLLISION",
    }.get(code, code)
