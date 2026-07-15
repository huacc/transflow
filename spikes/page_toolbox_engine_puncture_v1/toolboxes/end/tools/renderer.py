from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .layout_planner import color, contains
from .models import EndFinding, EndLayoutPlan, EndTemplate, Rect
from .template_builder import EndCapabilityError


def render_end_passthrough(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: EndTemplate,
    evidence_dir: Path,
) -> tuple[tuple[EndFinding, ...], dict[str, object]]:
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, candidate_pdf)
    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    source_hash = sha256_file(source_pdf)
    candidate_hash = sha256_file(candidate_pdf)
    findings: list[EndFinding] = []
    if source_hash != candidate_hash:
        findings.append(_finding("END_PASSTHROUGH_BYTES_CHANGED", "end_pdf_renderer", None, "无可翻译原生文字的结束页未保持字节等价"))
    if candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(_finding("END_LOCKED_OBJECT_CHANGED", "end_pdf_renderer", None, "透传页的页框、图片或矢量对象发生变化"))
    if candidate_facts.text_objects_sha256 != facts.text_objects_sha256:
        findings.append(_finding("END_PROTECTED_TEXT_CHANGED", "end_quality_judge", None, "透传页的原生文字发生变化"))
    evidence = _render_evidence(source_pdf, candidate_pdf, evidence_dir, facts.page_index)
    evidence.update(
        {
            "mode": "passthrough",
            "source_pdf_sha256": source_hash,
            "candidate_pdf_sha256": candidate_hash,
            "byte_equivalent": source_hash == candidate_hash,
            "source_locked_objects_sha256": facts.locked_objects_sha256,
            "candidate_locked_objects_sha256": candidate_facts.locked_objects_sha256,
            "structure_sha256": template.structure_sha256,
            "protected_object_count": len(template.protected_object_ids),
        }
    )
    return tuple(findings), evidence


def render_end_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: EndTemplate,
    plan: EndLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[EndFinding, ...], dict[str, object]]:
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_end_plan")
    if plan.structure_sha256 != template.structure_sha256:
        raise ValueError("end_structure_signature_mismatch")
    regions = template.translatable_regions
    if [placement.region_id for placement in plan.placements] != [region.region_id for region in regions]:
        raise ValueError("end_placement_order_mismatch")

    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    region_by_id = {region.region_id: region for region in regions}
    protected_sources = [source_by_id[object_id] for object_id in template.protected_object_ids]
    protected_bboxes = [item.bbox for item in protected_sources]
    required_resources = {placement.font_resource for placement in plan.placements}
    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        for region in regions:
            for object_id in region.source_object_ids:
                safe_rect = _safe_redaction_rect(source_by_id[object_id].bbox, protected_bboxes)
                if safe_rect is None:
                    raise EndCapabilityError(f"END_SAFE_REDACTION_REGION_NOT_FOUND:{object_id}")
                page.add_redact_annot(fitz.Rect(safe_rect), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for placement in plan.placements:
            region = region_by_id[placement.region_id]
            if not contains(region.allowed_bbox, placement.output_bbox):
                raise RuntimeError(f"END_WRITE_OUTSIDE_ALLOWED_BAND:{placement.region_id}")
            spare_height = page.insert_textbox(
                fitz.Rect(placement.output_bbox),
                placement.translated_text,
                fontname=placement.font_resource,
                fontfile=placement.font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                color=color(placement.color_srgb),
                align={"left": fitz.TEXT_ALIGN_LEFT, "center": fitz.TEXT_ALIGN_CENTER, "right": fitz.TEXT_ALIGN_RIGHT}[placement.alignment],
                overlay=True,
            )
            if spare_height < 0:
                raise RuntimeError(f"layout_probe_render_disagreement:{placement.region_id}")
            receipts.append(
                {
                    "region_id": placement.region_id,
                    "role": region.role,
                    "alignment": placement.alignment,
                    "source_bbox": region.source_bbox,
                    "allowed_bbox": region.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "font_size": placement.font_size,
                    "line_height": placement.line_height,
                    "insert_textbox_spare_height": round(float(spare_height), 4),
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    findings: list[EndFinding] = []
    if candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(_finding("END_LOCKED_OBJECT_CHANGED", "end_pdf_renderer", None, "页框、背景、Logo、二维码或装饰图形发生变化"))

    missing_protected = [item.object_id for item in protected_sources if not _object_preserved(item, candidate_facts.text_objects)]
    if missing_protected:
        findings.append(
            _finding(
                "END_PROTECTED_TEXT_CHANGED",
                "end_quality_judge",
                None,
                "品牌标识、链接或已是目标语言的联系信息发生变化",
                object_ids=missing_protected,
            )
        )

    source_remaining: list[str] = []
    translated_missing: list[str] = []
    literal_missing: dict[str, list[str]] = {}
    protected_overlaps: list[dict[str, object]] = []
    for placement in plan.placements:
        region = region_by_id[placement.region_id]
        if any(_object_preserved(source_by_id[object_id], candidate_facts.text_objects) for object_id in region.source_object_ids):
            source_remaining.append(region.region_id)
        local_text = "".join(
            item.text
            for item in candidate_facts.text_objects
            if _intersection_area(item.bbox, placement.output_bbox) > 0.01
        )
        if _normalized(placement.translated_text) not in _normalized(local_text):
            translated_missing.append(region.region_id)
        missing = [literal for literal in region.required_literals if literal not in local_text]
        if missing:
            literal_missing[region.region_id] = missing
        for protected in protected_sources:
            if _intersection_area(placement.output_bbox, protected.bbox) > 0.01:
                protected_overlaps.append(
                    {
                        "region_id": region.region_id,
                        "protected_object_id": protected.object_id,
                        "intersection_area": round(_intersection_area(placement.output_bbox, protected.bbox), 4),
                    }
                )
    if source_remaining:
        findings.append(_finding("END_SOURCE_TEXT_REMAINED", "end_quality_judge", None, "已翻译的原生文字仍在原锚点残留", region_ids=source_remaining))
    if translated_missing:
        findings.append(_finding("END_TRANSLATED_TEXT_MISSING", "end_quality_judge", None, "译文未在对应结束页语义块中完整出现", region_ids=translated_missing))
    if literal_missing:
        findings.append(_finding("END_REQUIRED_LITERAL_MISSING", "end_quality_judge", None, "候选页未保留联系数字、链接或认证标识", missing=literal_missing))
    if protected_overlaps:
        findings.append(_finding("END_PROTECTED_ANCHOR_OVERLAP", "end_quality_judge", None, "译文安全区域侵入受保护文字锚点", overlaps=protected_overlaps))

    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(_finding("FONT_NOT_EMBEDDED", "end_pdf_renderer", None, "目标字体资源未嵌入候选 PDF", resources=missing_fonts))

    allowed = [region.source_bbox for region in regions] + [placement.output_bbox for placement in plan.placements]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed, page_index=facts.page_index)
    if diff_ratio > 0.01:
        findings.append(
            _finding(
                "END_OUTSIDE_ALLOWED_REGION_CHANGED",
                "end_pdf_renderer",
                None,
                "结束页文字允许区域之外出现大范围渲染变化",
                changed_pixel_ratio=diff_ratio,
            )
        )

    evidence = _render_evidence(source_pdf, candidate_pdf, evidence_dir, facts.page_index)
    evidence.update(
        {
            "mode": "translated",
            "source_pdf_sha256": source_hash,
            "candidate_pdf_sha256": sha256_file(candidate_pdf),
            "source_locked_objects_sha256": facts.locked_objects_sha256,
            "candidate_locked_objects_sha256": candidate_facts.locked_objects_sha256,
            "structure_sha256": template.structure_sha256,
            "translated_region_count": len(regions),
            "protected_object_count": len(template.protected_object_ids),
            "outside_allowed_changed_pixel_ratio": diff_ratio,
            "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
            "insertion_receipts": receipts,
        }
    )
    return tuple(findings), evidence


def _render_evidence(source_pdf: Path, candidate_pdf: Path, evidence_dir: Path, page_index: int) -> dict[str, object]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_png = evidence_dir / "source.png"
    candidate_png = evidence_dir / "candidate.png"
    comparison_png = evidence_dir / "comparison.png"
    render_page(source_pdf, source_png, page_index=page_index, zoom=2.0)
    render_page(candidate_pdf, candidate_png, page_index=page_index, zoom=2.0)
    render_contact_sheet(source_pdf, candidate_pdf, comparison_png, page_index=page_index, zoom=1.5)
    return {
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
    }


def _object_preserved(source: TextObjectFact, candidates: tuple[TextObjectFact, ...]) -> bool:
    return any(
        source.text == candidate.text
        and max(abs(source.bbox[index] - candidate.bbox[index]) for index in range(4)) <= 0.75
        for candidate in candidates
    )


def _safe_redaction_rect(source: Rect, protected: list[Rect]) -> Rect | None:
    candidates = [source]
    for protected_rect in protected:
        cut = (
            protected_rect[0] - 0.05,
            protected_rect[1] - 0.05,
            protected_rect[2] + 0.05,
            protected_rect[3] + 0.05,
        )
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


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def _finding(code: str, owner: str, region_id: str | None, message: str, **evidence: object) -> EndFinding:
    return EndFinding(code, "HARD", owner, region_id, message, dict(evidence))
