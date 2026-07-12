from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .layout_planner import _color
from .models import SingleColumnLayoutPlan, SingleColumnTemplate, ToolboxFinding
from .p4_layout_planner import _font_variant


def render_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: SingleColumnTemplate,
    plan: SingleColumnLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[ToolboxFinding, ...], dict[str, object]]:
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_layout_plan")
    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in template.containers}
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(".pdf.tmp")
    insertion_receipts: list[dict[str, object]] = []
    prefix_regions: list[tuple[float, float, float, float]] = []
    required_font_resources: set[str] = set()

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        for container in template.containers:
            for object_id in container.source_object_ids:
                page.add_redact_annot(fitz.Rect(source_by_id[object_id].bbox), fill=None)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
        for placement in plan.placements:
            font_file, font_resource = _font_variant(
                plan.font_file,
                plan.font_resource,
                getattr(placement, "font_weight", "regular"),
            )
            required_font_resources.add(font_resource)
            result = page.insert_textbox(
                fitz.Rect(placement.output_bbox),
                placement.translated_text,
                fontname=font_resource,
                fontfile=font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                color=_color(placement.color_srgb),
                overlay=True,
            )
            if result < 0:
                raise RuntimeError(f"layout_probe_render_disagreement:{placement.container_id}")
            insertion_receipts.append(
                {
                    "container_id": placement.container_id,
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                    "insert_textbox_spare_height": result,
                    "fit": True,
                }
            )
            container = container_by_id[placement.container_id]
            if container.preserved_prefix:
                marker = next(
                    (
                        source_by_id[object_id]
                        for object_id in container.source_object_ids
                        if source_by_id[object_id].text.strip() in {container.preserved_prefix, "\uf0b7", "•", "●", "▪"}
                    ),
                    None,
                )
                if marker is None:
                    raise RuntimeError(f"preserved_prefix_source_object_missing:{placement.container_id}")
                marker_output_bbox = (
                    marker.bbox[0],
                    placement.output_bbox[1],
                    placement.output_bbox[0] - 2.0,
                    placement.output_bbox[3],
                )
                prefix_regions.extend((marker.bbox, marker_output_bbox))
                marker_result = page.insert_textbox(
                    fitz.Rect(marker_output_bbox),
                    container.preserved_prefix,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=placement.font_size,
                    lineheight=placement.line_height,
                    color=_color(placement.color_srgb),
                    overlay=True,
                )
                if marker_result < 0:
                    raise RuntimeError(f"preserved_prefix_render_failed:{placement.container_id}")
                insertion_receipts.append(
                    {
                        "container_id": placement.container_id,
                        "preserved_prefix": container.preserved_prefix,
                        "insert_textbox_spare_height": marker_result,
                        "fit": True,
                    }
                )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    findings: list[ToolboxFinding] = []
    if candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(ToolboxFinding("LOCKED_OBJECT_CHANGED", "HARD", "pdf_renderer", None, "页框、图片或绘图对象发生变化"))
    diff_ratio = outside_region_diff_ratio(
        source_pdf,
        candidate_pdf,
        [placement.output_bbox for placement in plan.placements]
        + [container.source_bbox for container in template.containers]
        + prefix_regions,
        page_index=facts.page_index,
    )
    if diff_ratio > 0.01:
        findings.append(ToolboxFinding("OUTSIDE_ALLOWED_REGION_CHANGED", "HARD", "pdf_renderer", None, "允许文字区域之外出现大范围渲染变化"))
    elif diff_ratio > 0.00001:
        findings.append(ToolboxFinding("OUTSIDE_ALLOWED_REGION_RENDER_DRIFT", "SOFT", "pdf_renderer", None, "锁定对象哈希一致，但 PDF 重写后允许区外存在轻微抗锯齿差异"))
    missing_fonts = missing_embedded_resources(candidate_pdf, required_font_resources, facts.page_index)
    if missing_fonts:
        findings.append(ToolboxFinding("FONT_NOT_EMBEDDED", "HARD", "pdf_renderer", None, "目标字体未嵌入候选 PDF"))

    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_png = evidence_dir / "source.png"
    candidate_png = evidence_dir / "candidate.png"
    comparison_png = evidence_dir / "comparison.png"
    render_page(source_pdf, source_png, page_index=facts.page_index, zoom=2.0)
    render_page(candidate_pdf, candidate_png, page_index=facts.page_index, zoom=2.0)
    render_contact_sheet(source_pdf, candidate_pdf, comparison_png, page_index=facts.page_index, zoom=1.5)
    evidence = {
        "source_pdf_sha256": source_hash,
        "candidate_pdf_sha256": sha256_file(candidate_pdf),
        "source_locked_objects_sha256": facts.locked_objects_sha256,
        "candidate_locked_objects_sha256": candidate_facts.locked_objects_sha256,
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "container_count": len(container_by_id),
        "insertion_receipts": insertion_receipts,
    }
    return tuple(findings), evidence
