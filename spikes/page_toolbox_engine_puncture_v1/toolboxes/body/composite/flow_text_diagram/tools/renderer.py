from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.diagram.tools.models import (
    DiagramContainer,
    DiagramLayoutPlan,
    DiagramPlacement,
)
from toolboxes.body.diagram.tools.renderer import render_diagram_candidate

from .. import TOOLBOX_KEY
from .models import CompositeFinding, CompositeLayoutPlan, CompositePageTemplate, Rect


def render_composite_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: CompositePageTemplate,
    plan: CompositeLayoutPlan,
    evidence_dir: Path,
) -> tuple[
    tuple[CompositeFinding, ...],
    dict[str, object],
    DiagramLayoutPlan,
]:
    render_plan, diagnostics = _materialize_unfit_translations(template, plan.render_plan)
    render_template = _render_template(template, render_plan, diagnostics)
    child_findings, evidence = render_diagram_candidate(
        source_pdf=source_pdf,
        candidate_pdf=candidate_pdf,
        facts=facts,
        template=render_template,
        plan=render_plan,
        evidence_dir=evidence_dir,
        allow_partial=True,
    )
    findings = [
        CompositeFinding(
            code=item.code,
            severity=item.severity,
            owner=item.owner,
            container_id=item.container_id,
            message=item.message,
            evidence=item.evidence,
        )
        for item in child_findings
    ]
    findings.extend(
        CompositeFinding(
            code="P18_TRANSLATED_DIAGNOSTIC_MATERIALIZATION",
            severity="HARD",
            owner=record["owner"],
            container_id=record["container_id"],
            message="An unfit child placement was moved to a diagnostic slot; it is not product-acceptable.",
            evidence=record,
        )
        for record in diagnostics
    )
    evidence.update(
        {
            "schema_version": "p18-flow-text-diagram-render/v1",
            "toolbox_key": TOOLBOX_KEY,
            "flow_mode": template.flow_mode,
            "diagram_region": template.diagram_region,
            "owner_allowed_bboxes": {
                item.composite_id: item.allowed_bbox for item in template.containers
            },
            "diagnostic_materialization": diagnostics,
            "translated_container_count": len(render_plan.placements),
            "translated_unfit_container_ids": [
                item.container_id for item in plan.render_plan.placements if not item.fit
            ],
            "omitted_unfit_container_ids": [
                item.container_id for item in render_plan.placements if not item.fit
            ],
        }
    )
    return _deduplicate(tuple(findings)), evidence, render_plan


def _render_template(
    template: CompositePageTemplate,
    plan: DiagramLayoutPlan,
    diagnostics: tuple[dict[str, object], ...],
):
    placement_by_id = {item.container_id: item for item in plan.placements}
    diagnostic_ids = {str(item["container_id"]) for item in diagnostics}
    diagram_by_id = {
        item.container_id: item for item in template.diagram_template.containers
    }
    flow_by_id = {
        item.container_id: item for item in template.flow_template.containers
    }
    containers = []
    diagram_base_to_composite = {
        item.base_container_id: item.composite_id
        for item in template.containers
        if item.owner == "diagram"
    }
    for item in template.containers:
        placement = placement_by_id[item.composite_id]
        allowed = _union_rect(item.allowed_bbox, placement.output_bbox)
        if item.owner == "diagram":
            base = diagram_by_id[item.base_container_id]
            node_id = None if item.composite_id in diagnostic_ids else base.node_id
            containers.append(
                replace(
                    base,
                    container_id=item.composite_id,
                    owner_kind=base.owner_kind,
                    owner_id=item.composite_id,
                    node_id=node_id,
                    allowed_bbox=allowed,
                    reading_order=item.reading_order,
                )
            )
        else:
            base = flow_by_id[item.base_container_id]
            containers.append(
                DiagramContainer(
                    container_id=item.composite_id,
                    owner_kind=item.owner,
                    owner_id=item.composite_id,
                    node_id=None,
                    source_object_ids=item.source_object_ids,
                    source_text=item.source_text,
                    source_bbox=item.source_bbox,
                    allowed_bbox=allowed,
                    reading_order=item.reading_order,
                    required_literals=item.required_literals,
                    role=item.role,
                    font_name="Bold" if getattr(base, "font_weight", "regular") == "bold" else "Regular",
                    font_size=base.font_size,
                    color_srgb=base.color_srgb,
                    alignment=placement.alignment,
                )
            )
    nodes = tuple(
        replace(
            node,
            container_ids=tuple(
                diagram_base_to_composite[item]
                for item in node.container_ids
                if item in diagram_base_to_composite
                and diagram_base_to_composite[item] not in diagnostic_ids
            ),
        )
        for node in template.diagram_template.nodes
    )
    structure = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "topology_sha256": template.topology_sha256,
            "containers": containers,
            "protected_object_ids": template.protected_object_ids,
        }
    )
    return replace(
        template.diagram_template,
        toolbox_key=TOOLBOX_KEY,
        nodes=nodes,
        containers=tuple(containers),
        protected_object_ids=template.protected_object_ids,
        topology_sha256=template.topology_sha256,
        structure_sha256=structure,
        layout_strategy="OWNER_FIT",
    )


def _materialize_unfit_translations(
    template: CompositePageTemplate,
    plan: DiagramLayoutPlan,
) -> tuple[DiagramLayoutPlan, tuple[dict[str, object], ...]]:
    containers = {item.composite_id: item for item in template.containers}
    placements: list[DiagramPlacement] = []
    records: list[dict[str, object]] = []
    for placement in plan.placements:
        if placement.fit:
            placements.append(placement)
            continue
        container = containers[placement.container_id]
        selected = _diagnostic_fit(template, container, placement)
        if selected is None:
            placements.append(placement)
            continue
        bbox, font_size, line_height, policy = selected
        placements.append(
            replace(
                placement,
                node_id=None,
                output_bbox=bbox,
                font_size=font_size,
                line_height=line_height,
                fit_profile=f"{policy}/diagnostic-translated",
                fit=True,
                glyph_bbox=None,
            )
        )
        records.append(
            {
                "container_id": placement.container_id,
                "owner": container.owner,
                "operation_type": "translated_diagnostic_render",
                "slot_policy": policy,
                "output_bbox": bbox,
                "font_size": font_size,
                "line_height": line_height,
                "product_acceptance": False,
            }
        )
    return replace(plan, placements=tuple(placements)), tuple(records)


def _diagnostic_fit(template, container, placement):
    margin_x = max(6.0, template.width * 0.015)
    margin_y = max(6.0, template.height * 0.015)
    page_lane = (
        margin_x,
        max(margin_y, min(container.source_bbox[1], template.height - margin_y - 1.0)),
        template.width - margin_x,
        template.height - margin_y,
    )
    candidates = (
        ("owner-safe-wrap", container.allowed_bbox),
        ("page-diagnostic-wrap", page_lane),
    )
    source_size = max(placement.font_size, 0.75)
    sizes = tuple(
        dict.fromkeys(
            round(max(0.75, source_size * scale), 4)
            for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2)
        )
    )
    for policy, bbox in candidates:
        if bbox[2] <= bbox[0] + 1.0 or bbox[3] <= bbox[1] + 1.0:
            continue
        for font_size in sizes:
            line_height = 0.92
            if _probe(
                template.width,
                template.height,
                bbox,
                placement.translated_text,
                placement.font_file,
                placement.font_resource,
                font_size,
                line_height,
                placement.alignment,
            ):
                return bbox, font_size, line_height, policy
    return None


def _probe(
    width: float,
    height: float,
    bbox: Rect,
    text: str,
    font_file: str,
    font_resource: str,
    font_size: float,
    line_height: float,
    alignment: str,
) -> bool:
    with fitz.open() as document:
        page = document.new_page(width=width, height=height)
        spare = page.insert_textbox(
            fitz.Rect(bbox),
            text,
            fontname=font_resource,
            fontfile=font_file,
            fontsize=font_size,
            lineheight=line_height,
            align={"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}.get(
                alignment,
                fitz.TEXT_ALIGN_LEFT,
            ),
        )
        return spare >= 0


def _union_rect(left: Rect, right: Rect) -> Rect:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _deduplicate(findings: tuple[CompositeFinding, ...]) -> tuple[CompositeFinding, ...]:
    output = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.container_id, finding.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return tuple(output)
