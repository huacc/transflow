from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .models import AnchoredBlocksTemplate, AnchoredFinding, AnchoredLayoutPlan, Rect
from .template_builder import AnchoredBlocksCapabilityError


def render_anchored_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: AnchoredBlocksTemplate,
    plan: AnchoredLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[AnchoredFinding, ...], dict[str, object]]:
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_anchored_blocks_plan")
    if plan.structure_sha256 != template.structure_sha256:
        raise ValueError("anchored_blocks_structure_signature_mismatch")
    if [item.container_id for item in plan.placements] != [item.container_id for item in template.containers]:
        raise ValueError("anchored_blocks_placement_order_mismatch")

    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in template.containers}
    protected_bboxes = [source_by_id[object_id].bbox for object_id in template.protected_object_ids]
    redacted_ids = {
        object_id
        for container in template.containers
        for object_id in container.source_object_ids
    }
    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")
    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        for object_id in sorted(redacted_ids):
            safe_rect = _safe_redaction_rect(source_by_id[object_id].bbox, protected_bboxes)
            if safe_rect is None:
                raise AnchoredBlocksCapabilityError(f"ANCHORED_BLOCKS_SAFE_REDACTION_REGION_NOT_FOUND:{object_id}")
            page.add_redact_annot(fitz.Rect(safe_rect), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for placement in plan.placements:
            container = container_by_id[placement.container_id]
            if placement.block_owner_id != container.block_owner_id:
                raise RuntimeError(f"ANCHORED_BLOCK_WRONG_OWNER:{placement.container_id}")
            if not _contains(container.slot_bbox, placement.output_bbox):
                raise RuntimeError(f"ANCHORED_BLOCK_WRITE_OUTSIDE_CELL:{placement.container_id}")
            if not _contains(container.allowed_bbox, placement.output_bbox):
                raise RuntimeError(f"ANCHORED_BLOCK_WRITE_OUTSIDE_ALLOWED_REGION:{placement.container_id}")
            result = page.insert_textbox(
                fitz.Rect(placement.output_bbox),
                placement.translated_text,
                fontname=placement.font_resource,
                fontfile=placement.font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                color=_color(placement.color_srgb),
                align=_fitz_alignment(placement.alignment),
                overlay=True,
            )
            if result < 0:
                raise RuntimeError(f"layout_probe_render_disagreement:{placement.container_id}")
            receipts.append(
                {
                    "container_id": placement.container_id,
                    "block_owner_id": placement.block_owner_id,
                    "source_bbox": container.source_bbox,
                    "slot_bbox": container.slot_bbox,
                    "allowed_bbox": container.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "profile": placement.profile,
                    "font_size": placement.font_size,
                    "line_height": placement.line_height,
                    "insert_textbox_spare_height": round(float(result), 4),
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    findings: list[AnchoredFinding] = []
    if candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_LOCKED_OBJECT_CHANGED",
                "anchored_blocks_pdf_renderer",
                None,
                None,
                "页面图片、颜色、矢量背景或块边框发生变化",
                source=facts.locked_objects_sha256,
                candidate=candidate_facts.locked_objects_sha256,
            )
        )

    protected_sources = [source_by_id[object_id] for object_id in template.protected_object_ids]
    protected_missing = _missing_original_objects(protected_sources, list(candidate_facts.text_objects))
    if protected_missing:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_PROTECTED_TEXT_CHANGED",
                "anchored_blocks_quality_judge",
                None,
                None,
                "大数字、百分比、日期、代码或其他受保护文字缺失或移动",
                object_ids=protected_missing,
            )
        )

    translated = {item.container_id: item.translated_text for item in plan.placements}
    original_remaining = []
    for container in template.containers:
        if _normalized(container.source_text) == _normalized(translated[container.container_id]):
            continue
        sources = [source_by_id[object_id] for object_id in container.source_object_ids]
        if _missing_original_objects(sources, list(candidate_facts.text_objects), invert=True):
            original_remaining.append(container.container_id)
    if original_remaining:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_SOURCE_TEXT_REMAINED",
                "anchored_blocks_quality_judge",
                None,
                None,
                "已经翻译的原生文字仍保留原文字形",
                container_ids=original_remaining,
            )
        )

    required_resources = {item.font_resource for item in plan.placements}
    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(
            _finding(
                "FONT_NOT_EMBEDDED",
                "anchored_blocks_pdf_renderer",
                None,
                None,
                "目标字体资源未嵌入候选 PDF",
                resources=missing_fonts,
            )
        )

    allowed = [container.source_bbox for container in template.containers] + [item.output_bbox for item in plan.placements]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed, page_index=facts.page_index)
    if diff_ratio > 0.012:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_OUTSIDE_ALLOWED_REGION_CHANGED",
                "anchored_blocks_pdf_renderer",
                None,
                None,
                "块内允许区域之外出现大范围渲染变化",
                changed_pixel_ratio=diff_ratio,
            )
        )

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
        "structure_sha256": template.structure_sha256,
        "block_owner_count": len(template.block_owners),
        "container_count": len(template.containers),
        "protected_object_count": len(template.protected_object_ids),
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _missing_original_objects(
    source_objects: list[TextObjectFact],
    candidate_objects: list[TextObjectFact],
    *,
    invert: bool = False,
) -> list[str]:
    result = []
    for source in source_objects:
        present = any(
            source.text == candidate.text
            and max(abs(source.bbox[index] - candidate.bbox[index]) for index in range(4)) <= 0.75
            for candidate in candidate_objects
        )
        if present == invert:
            result.append(source.object_id)
    return result


def _safe_redaction_rect(source: Rect, protected: list[Rect]) -> Rect | None:
    candidates = [source]
    for item in protected:
        cut = (item[0] - 1.0, item[1] - 1.0, item[2] + 1.0, item[3] + 1.0)
        candidates = [piece for candidate in candidates for piece in _subtract_rect(candidate, cut)]
        if not candidates:
            return None
    return max(candidates, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]), default=None)


def _subtract_rect(source: Rect, cut: Rect) -> list[Rect]:
    x0 = max(source[0], cut[0])
    y0 = max(source[1], cut[1])
    x1 = min(source[2], cut[2])
    y1 = min(source[3], cut[3])
    if x0 >= x1 or y0 >= y1:
        return [source]
    pieces = [
        (source[0], source[1], source[2], y0),
        (source[0], y1, source[2], source[3]),
        (source[0], y0, x0, y1),
        (x1, y0, source[2], y1),
    ]
    return [piece for piece in pieces if piece[2] - piece[0] > 0.05 and piece[3] - piece[1] > 0.05]


def _contains(outer: Rect, inner: Rect) -> bool:
    return outer[0] - 0.01 <= inner[0] and outer[1] - 0.01 <= inner[1] and inner[2] <= outer[2] + 0.01 and inner[3] <= outer[3] + 0.01


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def _finding(code, owner, block_owner_id, container_id, message, **evidence):
    return AnchoredFinding(code, "HARD", owner, block_owner_id, container_id, message, dict(evidence))
