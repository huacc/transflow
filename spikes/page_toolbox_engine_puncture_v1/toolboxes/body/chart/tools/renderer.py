from __future__ import annotations

import hashlib
import re
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import canonical_sha256, extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .models import ChartFinding, ChartLayoutPlan, ChartTemplate, Rect
from .template_builder import ChartCapabilityError


def render_chart_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: ChartTemplate,
    plan: ChartLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[ChartFinding, ...], dict[str, object]]:
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_chart_plan")
    if plan.structure_sha256 != template.structure_sha256:
        raise ValueError("chart_structure_signature_mismatch")

    placement_ids = [item.container_id for item in plan.placements]
    expected = [item.container_id for item in template.containers if item.container_id in set(placement_ids)]
    if placement_ids != expected or len(placement_ids) != len(set(placement_ids)):
        raise ValueError("chart_placement_order_mismatch")

    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in template.containers}
    runtime_protected_ids = tuple(
        dict.fromkeys(
            (*template.protected_object_ids, *(
                object_id
                for container in template.containers
                if container.container_id not in set(placement_ids)
                for object_id in container.source_object_ids
            ))
        )
    )
    protected = [source_by_id[object_id] for object_id in runtime_protected_ids]
    redacted_ids = {
        object_id
        for container in template.containers
        if container.container_id in set(placement_ids)
        for object_id in container.source_object_ids
    }

    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")
    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        glyph_bboxes = _raw_glyph_bboxes(page, source_by_id)
        protected_bboxes = [item.bbox for item in protected]
        for object_id in sorted(redacted_ids):
            source_bbox = source_by_id[object_id].bbox
            safe_rects = _safe_redaction_rects(
                source_bbox,
                protected_bboxes,
                glyph_bboxes.get(object_id, (source_bbox,)),
            )
            if not safe_rects:
                raise ChartCapabilityError(f"CHART_SAFE_REDACTION_REGION_NOT_FOUND:{object_id}")
            for safe_rect in safe_rects:
                page.add_redact_annot(fitz.Rect(safe_rect), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for placement in plan.placements:
            container = container_by_id[placement.container_id]
            if not _contains(container.allowed_bbox, placement.output_bbox):
                raise RuntimeError(f"CHART_WRITE_OUTSIDE_ALLOWED_REGION:{placement.container_id}")
            spare = page.insert_textbox(
                fitz.Rect(placement.output_bbox),
                placement.translated_text,
                fontname=placement.font_resource,
                fontfile=placement.font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                color=_color(placement.color_srgb),
                align=_fitz_alignment(placement.alignment),
                rotate=placement.rotation,
                overlay=True,
            )
            if spare < 0:
                raise RuntimeError(f"chart_layout_probe_render_disagreement:{placement.container_id}")
            receipts.append(
                {
                    "container_id": placement.container_id,
                    "role": container.role,
                    "association_id": container.association_id,
                    "anchor_object_ids": container.anchor_object_ids,
                    "anchor_relation": container.anchor_relation,
                    "source_bbox": container.source_bbox,
                    "allowed_bbox": container.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "profile": placement.profile,
                    "font_size": placement.font_size,
                    "minimum_font_size": placement.minimum_font_size,
                    "rotation": placement.rotation,
                    "insert_textbox_spare_height": round(float(spare), 4),
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_chart_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    findings: list[ChartFinding] = []
    source_visual_signature = _locked_visual_signature(facts)
    candidate_visual_signature = _locked_visual_signature(candidate_facts)
    if source_visual_signature != candidate_visual_signature:
        findings.append(
            _finding(
                "CHART_DATA_VISUAL_CHANGED",
                "chart_pdf_renderer",
                None,
                None,
                "图表图片、柱线点扇区、坐标轴、色块、颜色或边界发生变化",
                source=source_visual_signature,
                candidate=candidate_visual_signature,
            )
        )

    protected_missing = _missing_original_objects(protected, list(candidate_facts.text_objects))
    if protected_missing:
        findings.append(
            _finding(
                "CHART_PROTECTED_TEXT_CHANGED",
                "chart_quality_judge",
                None,
                None,
                "数值、刻度、数据标签、页眉页脚或其他受保护原生文字缺失或移动",
                object_ids=protected_missing,
            )
        )

    visual_objects = tuple(
        [(item.object_id, item.bbox, "IMAGE") for item in facts.image_objects]
        + [(item.object_id, item.bbox, "DRAWING") for item in facts.drawing_objects]
    )
    page_area = facts.width * facts.height
    for placement in plan.placements:
        container = container_by_id[placement.container_id]
        rendered = _slot_text_objects(
            candidate_facts.text_objects,
            placement.output_bbox,
            protected,
            color_srgb=placement.color_srgb,
            font_size=placement.font_size,
            rotation=placement.rotation,
        )
        actual_text = _normalized("".join(item.text for item in rendered))
        expected_text = _normalized(placement.translated_text)
        if actual_text != expected_text:
            findings.append(
                _finding(
                    "CHART_TRANSLATION_MISSING",
                    "chart_quality_judge",
                    container.association_id,
                    container.container_id,
                    "图表文字槽位实际字形与计划译文不一致",
                    expected_sha256=hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
                    actual_sha256=hashlib.sha256(actual_text.encode("utf-8")).hexdigest(),
                )
            )
        source_residue = _slot_text_objects(
            candidate_facts.text_objects,
            container.source_bbox,
            protected,
            rotation=placement.rotation,
        )
        residue_text = _normalized("".join(item.text for item in source_residue))
        if expected_text != _normalized(container.source_text) and _normalized(container.source_text) in residue_text:
            findings.append(
                _finding(
                    "CHART_SOURCE_RESIDUE",
                    "chart_quality_judge",
                    container.association_id,
                    container.container_id,
                    "已替换的原生图表文字仍残留在候选页",
                )
            )
        if not rendered:
            continue
        glyph_bbox = _union([item.bbox for item in rendered])
        if not _contains(placement.output_bbox, glyph_bbox, tolerance=0.75):
            findings.append(
                _finding(
                    "CHART_GLYPH_OUTSIDE_SLOT",
                    "chart_quality_judge",
                    container.association_id,
                    container.container_id,
                    "候选实际字形越出图表文字安全区域",
                    glyph_bbox=glyph_bbox,
                    allowed_bbox=placement.output_bbox,
                )
            )
        source_visual_ids = {
            object_id
            for object_id, bbox, _ in visual_objects
            if _intersection_area(container.source_bbox, bbox) > 0.01
        } | set(container.anchor_object_ids)
        for object_id, bbox, kind in visual_objects:
            if object_id in source_visual_ids or _area(bbox) >= page_area * 0.80:
                continue
            if _intersection_area(glyph_bbox, bbox) <= 0.05:
                continue
            code = "CHART_IMAGE_TEXT_OVERLAID" if kind == "IMAGE" else "CHART_TEXT_GRAPHIC_COLLISION"
            findings.append(
                _finding(
                    code,
                    "chart_quality_judge",
                    container.association_id,
                    container.container_id,
                    "译文字形进入了源文字未占用的图表图片或矢量构件",
                    visual_object_id=object_id,
                    glyph_bbox=glyph_bbox,
                    visual_bbox=bbox,
                )
            )
            break
        if container.role == "LEGEND_LABEL" and container.anchor_object_ids:
            anchor = next((bbox for object_id, bbox, _ in visual_objects if object_id == container.anchor_object_ids[0]), None)
            if anchor is not None and _anchor_relation(glyph_bbox, anchor) != container.anchor_relation:
                findings.append(
                    _finding(
                        "CHART_LEGEND_ASSOCIATION_CHANGED",
                        "chart_quality_judge",
                        container.association_id,
                        container.container_id,
                        "图例译文跨过色块或改变了原有对应方向",
                        source_relation=container.anchor_relation,
                        candidate_relation=_anchor_relation(glyph_bbox, anchor),
                    )
                )

    required_resources = {item.font_resource for item in plan.placements}
    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(
            _finding(
                "FONT_NOT_EMBEDDED",
                "chart_pdf_renderer",
                None,
                None,
                "目标字体资源未嵌入候选 PDF",
                resources=missing_fonts,
            )
        )

    allowed_regions = [container_by_id[item.container_id].source_bbox for item in plan.placements] + [item.output_bbox for item in plan.placements]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed_regions, page_index=facts.page_index)
    if diff_ratio > 0.012:
        findings.append(
            _finding(
                "CHART_OUTSIDE_TEXT_REGION_CHANGED",
                "chart_pdf_renderer",
                None,
                None,
                "图表文字安全区域之外出现大范围渲染变化",
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
        "render_backend": "PyMuPDF (Poppler unavailable in current environment)",
        "source_pdf_sha256": source_hash,
        "candidate_pdf_sha256": sha256_file(candidate_pdf),
        "source_locked_visual_signature": source_visual_signature,
        "candidate_locked_visual_signature": candidate_visual_signature,
        "structure_sha256": template.structure_sha256,
        "visual_region_count": len(template.visual_regions),
        "container_count": len(template.containers),
        "protected_object_count": len(runtime_protected_ids),
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _locked_visual_signature(facts: PageFacts) -> str:
    return canonical_sha256(
        {
            "geometry_sha256": facts.geometry_sha256,
            "images": sorted((item.bbox, item.width, item.height, item.content_sha256) for item in facts.image_objects),
            "drawings": sorted((item.bbox, item.content_sha256) for item in facts.drawing_objects),
        }
    )


def _raw_glyph_bboxes(page: fitz.Page, source_by_id: dict[str, TextObjectFact]) -> dict[str, tuple[Rect, ...]]:
    raw_spans: list[tuple[str, Rect, tuple[Rect, ...]]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = "".join(str(character.get("c") or "") for character in span.get("chars", []))
                boxes = tuple(
                    tuple(float(value) for value in character["bbox"])
                    for character in span.get("chars", [])
                    if str(character.get("c") or "").strip()
                    and character.get("bbox")
                    and float(character["bbox"][2]) - float(character["bbox"][0]) > 0.01
                    and float(character["bbox"][3]) - float(character["bbox"][1]) > 0.01
                )
                if boxes:
                    raw_spans.append((text, tuple(float(value) for value in span["bbox"]), boxes))
    result: dict[str, tuple[Rect, ...]] = {}
    for object_id, source in source_by_id.items():
        matches = [boxes for text, bbox, boxes in raw_spans if text == source.text and _rect_delta(bbox, source.bbox) <= 0.75]
        if matches:
            result[object_id] = matches[0]
    return result


def _safe_redaction_rects(source: Rect, protected: list[Rect], glyphs: tuple[Rect, ...]) -> tuple[Rect, ...]:
    pieces = [source]
    for item in protected:
        cut = (item[0] - 0.05, item[1] - 0.05, item[2] + 0.05, item[3] + 0.05)
        pieces = [piece for candidate in pieces for piece in _subtract_rect(candidate, cut)]
        if not pieces:
            return ()
    uncovered = set(range(len(glyphs)))
    selected: list[Rect] = []
    while uncovered:
        scored = [({index for index in uncovered if _intersection_area(piece, glyphs[index]) > 0.001}, piece) for piece in pieces]
        covered, best = max(scored, key=lambda item: (len(item[0]), _area(item[1])), default=(set(), source))
        if not covered:
            return ()
        selected.append(best)
        uncovered -= covered
        pieces.remove(best)
    return tuple(selected)


def _subtract_rect(source: Rect, cut: Rect) -> list[Rect]:
    x0, y0 = max(source[0], cut[0]), max(source[1], cut[1])
    x1, y1 = min(source[2], cut[2]), min(source[3], cut[3])
    if x0 >= x1 or y0 >= y1:
        return [source]
    pieces = [
        (source[0], source[1], source[2], y0),
        (source[0], y1, source[2], source[3]),
        (source[0], y0, x0, y1),
        (x1, y0, source[2], y1),
    ]
    return [item for item in pieces if item[2] - item[0] > 0.05 and item[3] - item[1] > 0.05]


def _slot_text_objects(
    objects,
    slot: Rect,
    protected,
    *,
    color_srgb: int | None = None,
    font_size: float | None = None,
    rotation: int = 0,
):
    result = []
    for item in objects:
        if any(item.text == source.text and _rect_delta(item.bbox, source.bbox) <= 0.75 for source in protected):
            continue
        if color_srgb is not None and item.color_srgb != color_srgb:
            continue
        if font_size is not None and abs(item.font_size - font_size) > 0.25:
            continue
        if _intersection_area(item.bbox, slot) / max(0.001, _area(item.bbox)) >= 0.5:
            result.append(item)
    key = (lambda item: (item.bbox[0], item.bbox[1])) if rotation in {90, 270} else (lambda item: (item.bbox[1], item.bbox[0]))
    return sorted(result, key=key)


def _missing_original_objects(source_objects, candidate_objects):
    return [source.object_id for source in source_objects if not any(_same_protected_text(source, candidate) for candidate in candidate_objects)]


def _same_protected_text(source, candidate) -> bool:
    return (
        source.text == candidate.text
        and source.font_name == candidate.font_name
        and source.color_srgb == candidate.color_srgb
        and max(abs(source.bbox[index] - candidate.bbox[index]) for index in (0, 1, 3)) <= 0.75
        and abs(source.bbox[2] - candidate.bbox[2]) <= max(2.0, source.font_size * 0.25)
    )


def _anchor_relation(source: Rect, anchor: Rect) -> str:
    if _intersection_area(source, anchor) > 0.01:
        return "OVERLAY"
    if source[2] <= anchor[0]:
        return "LEFT_OF"
    if source[0] >= anchor[2]:
        return "RIGHT_OF"
    return "ABOVE" if source[3] <= anchor[1] else "BELOW"


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("∙", "•")).casefold()


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance


def _rect_delta(left: Rect, right: Rect) -> float:
    return max(abs(left[index] - right[index]) for index in range(4))


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _union(rects: list[Rect]) -> Rect:
    return (min(item[0] for item in rects), min(item[1] for item in rects), max(item[2] for item in rects), max(item[3] for item in rects))


def _finding(code, owner, association_id, container_id, message, **evidence):
    return ChartFinding(code, "HARD", owner, association_id, container_id, message, dict(evidence))
