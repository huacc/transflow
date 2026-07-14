"""
tool_name: multi_diagnostic_renderer
category: PDF renderer
input_contract: a translated but unfit multi-column plan, or a translated raw page template
output_contract: a translated diagnostic PDF that exposes overflow, overlap and other failed layout effects
failure_signals: source text object missing or translated text cannot be inserted even on an extended page
fallback: keep the structured failure evidence and report diagnostic-render capability failure
anti_overfit_statement: all text, bboxes, fonts and page extension come from the current run; no sample literal or coordinate is encoded
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.render import render_contact_sheet, render_page
from toolboxes.body.flow_text.single.tools.layout_planner import _color
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate
from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _minimum_text_height
from toolboxes.body.flow_text.single.tools.p4_models import P4Placement
from toolboxes.body.flow_text.single.tools.renderer import _textbox_alignment

from .models import MultiColumnLayoutPlan, MultiColumnTemplate


def render_unfit_multi_plan_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    evidence_dir: Path,
) -> dict[str, object]:
    """强制渲染已翻译但未通过布局门禁的计划，用于直接观察失败效果。"""

    return _render_diagnostic_placements(
        source_pdf=source_pdf,
        candidate_pdf=candidate_pdf,
        facts=facts,
        containers=template.containers,
        placements=plan.placements,
        font_file=plan.font_file,
        font_resource=plan.font_resource,
        evidence_dir=evidence_dir,
        diagnostic_kind="unfit_multi_plan",
    )


def render_raw_template_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: SingleColumnTemplate,
    translation: PageTranslationBundle,
    font_file: str,
    evidence_dir: Path,
) -> dict[str, object]:
    """多栏模板无法建立时，按原文字块 bbox 强制回填译文，暴露真实重叠和挤占。"""

    translated = {item.container_id: item.translated_text for item in translation.translations}
    if list(translated) != [item.container_id for item in template.containers]:
        raise ValueError("diagnostic_translation_ids_do_not_match_raw_template")
    placements: list[P4Placement] = []
    for container in template.containers:
        x0, y0, x1, source_y1 = container.source_bbox
        font_size = max(6.0, container.font_size * 0.78)
        line_height = 0.98
        placement_font_file, placement_font_resource = _font_variant(
            font_file,
            "p5diagnostic",
            container.font_weight,
        )
        required_height = _minimum_text_height(
            template.width,
            template.height,
            x1 - x0,
            translated[container.container_id],
            font_size,
            line_height,
            placement_font_file,
            placement_font_resource,
            container.color_srgb,
        )
        placements.append(
            P4Placement(
                container_id=container.container_id,
                translated_text=translated[container.container_id],
                role=container.role,
                source_bbox=container.source_bbox,
                output_bbox=(x0, y0, x1, round(max(source_y1, y0 + required_height), 4)),
                horizontal_policy="diagnostic_source_width_invariant",
                source_font_size=container.font_size,
                font_size=round(font_size, 4),
                line_height=line_height,
                vertical_policy="diagnostic_source_anchor_without_reflow",
                source_gap=0.0,
                target_gap=0.0,
                color_srgb=container.color_srgb,
                font_weight=container.font_weight,
                fit=False,
            )
        )
    return _render_diagnostic_placements(
        source_pdf=source_pdf,
        candidate_pdf=candidate_pdf,
        facts=facts,
        containers=template.containers,
        placements=tuple(placements),
        font_file=font_file,
        font_resource="p5diagnostic",
        evidence_dir=evidence_dir,
        diagnostic_kind="raw_bbox_translation_fallback",
    )


def _render_diagnostic_placements(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    containers,
    placements: tuple[P4Placement, ...],
    font_file: str,
    font_resource: str,
    evidence_dir: Path,
    diagnostic_kind: str,
) -> dict[str, object]:
    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in containers}
    original_height = facts.height
    required_bottom = max((item.output_bbox[3] for item in placements), default=original_height)
    diagnostic_height = max(original_height, required_bottom + original_height * 0.04)
    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(".pdf.tmp")

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        for container in containers:
            for object_id in container.source_object_ids:
                source = source_by_id.get(object_id)
                if source is None:
                    raise ValueError(f"diagnostic_source_object_missing:{object_id}")
                page.add_redact_annot(fitz.Rect(source.bbox), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        if diagnostic_height > original_height + 0.01:
            # 失败候选允许向下扩页，红线明确标出原页面底边，避免把溢出误认为正常版式。
            page.set_mediabox(fitz.Rect(0.0, 0.0, facts.width, diagnostic_height))
            page.draw_line(
                fitz.Point(0.0, original_height),
                fitz.Point(facts.width, original_height),
                color=(0.85, 0.10, 0.10),
                width=1.2,
                overlay=True,
            )

        for placement in placements:
            placement_font_file, placement_font_resource = _font_variant(
                font_file,
                font_resource,
                placement.font_weight,
            )
            bbox = fitz.Rect(placement.output_bbox)
            result = page.insert_textbox(
                bbox,
                placement.translated_text,
                fontname=placement_font_resource,
                fontfile=placement_font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                color=_color(placement.color_srgb),
                align=_textbox_alignment(placement.horizontal_policy),
                overlay=True,
            )
            if result < 0:
                raise RuntimeError(f"diagnostic_translation_render_failed:{placement.container_id}")
            receipts.append(
                {
                    "container_id": placement.container_id,
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                    "output_bbox": placement.output_bbox,
                    "layout_fit": placement.fit,
                    "insert_textbox_spare_height": result,
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_diagnostic_render")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_png = evidence_dir / "source.png"
    candidate_png = evidence_dir / "diagnostic_candidate.png"
    comparison_png = evidence_dir / "diagnostic_comparison.png"
    render_page(source_pdf, source_png, page_index=facts.page_index, zoom=1.5)
    render_page(candidate_pdf, candidate_png, page_index=facts.page_index, zoom=1.5)
    render_contact_sheet(source_pdf, candidate_pdf, comparison_png, page_index=facts.page_index, zoom=1.2)
    return {
        "diagnostic_candidate": True,
        "diagnostic_kind": diagnostic_kind,
        "source_pdf_sha256": source_hash,
        "candidate_pdf_sha256": sha256_file(candidate_pdf),
        "original_page_height": original_height,
        "diagnostic_page_height": round(diagnostic_height, 4),
        "page_extended": diagnostic_height > original_height + 0.01,
        "placement_count": len(placements),
        "insertion_receipts": receipts,
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "product_acceptance": False,
    }
