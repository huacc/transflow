from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .layout_planner import _fitz_alignment
from .models import CoverFinding, CoverLayoutPlan, CoverTemplate, Rect
from .template_builder import CoverCapabilityError


def render_cover_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: CoverTemplate,
    plan: CoverLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[CoverFinding, ...], dict[str, object]]:
    if template.visual_only:
        raise ValueError("visual_only_cover_must_use_passthrough")
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_cover_plan")
    if plan.structure_sha256 != template.structure_sha256:
        raise ValueError("cover_structure_signature_mismatch")

    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in template.containers}
    placed_container_ids = {placement.container_id for placement in plan.placements}
    redacted_ids = set(template.occluded_object_ids) | {
        object_id
        for container in template.containers
        if container.container_id in placed_container_ids
        for object_id in container.source_object_ids
    }
    runtime_protected_ids = tuple(
        (
            *template.protected_object_ids,
            *(
                object_id
                for container in template.containers
                if container.container_id not in placed_container_ids
                for object_id in container.source_object_ids
            ),
        )
    )
    required_resources = {placement.font_resource for placement in plan.placements if placement.render_text}
    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        glyph_bboxes = _raw_glyph_bboxes(page, source_by_id)
        protected_bboxes = [source_by_id[object_id].bbox for object_id in runtime_protected_ids]
        for object_id in sorted(redacted_ids):
            safe_rects = _safe_redaction_rects(
                source_by_id[object_id].bbox,
                protected_bboxes,
                glyph_bboxes.get(object_id, (source_by_id[object_id].bbox,)),
            )
            if not safe_rects:
                raise CoverCapabilityError(f"COVER_SAFE_REDACTION_REGION_NOT_FOUND:{object_id}")
            for safe_rect in safe_rects:
                page.add_redact_annot(fitz.Rect(safe_rect), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for placement in plan.placements:
            result = 0.0
            if placement.render_text:
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
            container = container_by_id[placement.container_id]
            receipts.append(
                {
                    "container_id": placement.container_id,
                    "role": container.role,
                    "hierarchy_level": container.hierarchy_level,
                    "anchor": container.anchor,
                    "source_bbox": container.source_bbox,
                    "allowed_bbox": container.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "render_text": placement.render_text,
                    "deduplicated_against_container_ids": placement.deduplicated_against_container_ids,
                    "font_size": placement.font_size,
                    "insert_textbox_spare_height": round(float(result), 4),
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    candidate_glyph_runs = _raw_glyph_runs(candidate_pdf, facts.page_index)
    findings: list[CoverFinding] = []
    allowed = [container_by_id[placement.container_id].source_bbox for placement in plan.placements] + [
        placement.output_bbox for placement in plan.placements
    ]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed, page_index=facts.page_index)
    locked_hash_match = candidate_facts.locked_objects_sha256 == facts.locked_objects_sha256
    if not locked_hash_match and diff_ratio > 0.001:
        findings.append(
            _finding(
                "COVER_LOCKED_OBJECT_CHANGED",
                "cover_pdf_renderer",
                None,
                "封面背景、图片、Logo、颜色块或矢量装饰发生变化",
                source=facts.locked_objects_sha256,
                candidate=candidate_facts.locked_objects_sha256,
            )
        )

    protected_sources = [source_by_id[object_id] for object_id in runtime_protected_ids]
    protected_missing = _missing_protected_objects(
        protected_sources,
        list(candidate_facts.text_objects),
        glyph_bboxes,
        candidate_glyph_runs,
    )
    if protected_missing:
        findings.append(
            _finding(
                "COVER_PROTECTED_TEXT_CHANGED",
                "cover_quality_judge",
                None,
                "受保护的数字、网址或非语义原生文字缺失或移位",
                object_ids=protected_missing,
            )
        )

    candidate_objects = list(candidate_facts.text_objects)
    translated_by_id = {placement.container_id: placement.translated_text for placement in plan.placements}
    source_remaining: list[str] = []
    target_missing: list[str] = []
    anchor_drift: list[dict[str, object]] = []
    for placement in plan.placements:
        container = container_by_id[placement.container_id]
        translated_text = translated_by_id[container.container_id]
        if container.translatable and _normalized(container.source_text) != _normalized(translated_text):
            source_objects = [source_by_id[object_id] for object_id in container.source_object_ids]
            if _missing_original_objects(source_objects, candidate_objects, invert=True):
                source_remaining.append(container.container_id)

        if not placement.render_text:
            continue

        rendered = _objects_for_translation(candidate_objects, placement.output_bbox, translated_text)
        rendered_text = _normalized("".join(item.text for item in sorted(rendered, key=lambda item: (item.bbox[1], item.bbox[0]))))
        if _normalized(translated_text) not in rendered_text:
            target_missing.append(container.container_id)
            continue
        bbox = _union([item.bbox for item in rendered])
        drift = _anchor_delta(container.anchor, container.source_bbox, bbox)
        if drift > max(2.0, facts.width * 0.006):
            anchor_drift.append(
                {
                    "container_id": container.container_id,
                    "anchor": container.anchor,
                    "drift": round(drift, 4),
                    "source_bbox": container.source_bbox,
                    "rendered_bbox": bbox,
                }
            )

    if source_remaining:
        findings.append(
            _finding(
                "COVER_SOURCE_TEXT_REMAINED",
                "cover_quality_judge",
                None,
                "已翻译的封面原生文字仍留在候选页中",
                container_ids=source_remaining,
            )
        )
    if target_missing:
        findings.append(
            _finding(
                "COVER_TRANSLATED_TEXT_MISSING",
                "cover_quality_judge",
                None,
                "候选页缺少完整译文",
                container_ids=target_missing,
            )
        )
    if anchor_drift:
        findings.append(
            _finding(
                "COVER_VISUAL_ANCHOR_DRIFT",
                "cover_quality_judge",
                None,
                "封面标题或身份文字偏离原视觉锚点",
                containers=anchor_drift,
            )
        )

    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(
            _finding(
                "FONT_NOT_EMBEDDED",
                "cover_pdf_renderer",
                None,
                "目标字体资源未嵌入候选 PDF",
                resources=missing_fonts,
            )
        )

    if diff_ratio > 0.001:
        findings.append(
            _finding(
                "COVER_OUTSIDE_ALLOWED_REGION_CHANGED",
                "cover_pdf_renderer",
                None,
                "封面文字安全区域之外出现渲染变化",
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
        "locked_object_hash_match": locked_hash_match,
        "locked_visual_preservation": diff_ratio <= 0.001,
        "cover_structure_sha256": template.structure_sha256,
        "translated_container_count": sum(
            container_by_id[placement.container_id].translatable for placement in plan.placements
        ),
        "literal_repaint_count": sum(
            not container_by_id[placement.container_id].translatable for placement in plan.placements
        ),
        "protected_object_count": len(runtime_protected_ids),
        "occluded_object_count": len(template.occluded_object_ids),
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _objects_for_translation(objects: list[TextObjectFact], bbox: Rect, text: str) -> list[TextObjectFact]:
    normalized = _normalized(text)
    return [
        item
        for item in objects
        if _intersection_area(item.bbox, bbox) > 0.01 and _normalized(item.text) in normalized
    ]


def _raw_glyph_bboxes(
    page: fitz.Page,
    source_by_id: dict[str, TextObjectFact],
) -> dict[str, tuple[Rect, ...]]:
    raw_spans: list[tuple[str, Rect, tuple[Rect, ...]]] = []
    blocks = page.get_text("rawdict").get("blocks", [])
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = "".join(str(character.get("c") or "") for character in span.get("chars", []))
                rectangles = tuple(
                    tuple(float(value) for value in character["bbox"])
                    for character in span.get("chars", [])
                    if str(character.get("c") or "").strip()
                    and character.get("bbox")
                    and float(character["bbox"][2]) - float(character["bbox"][0]) > 0.01
                    and float(character["bbox"][3]) - float(character["bbox"][1]) > 0.01
                )
                if rectangles:
                    raw_spans.append((text, tuple(float(value) for value in span["bbox"]), rectangles))  # type: ignore[arg-type]
    result: dict[str, tuple[Rect, ...]] = {}
    for object_id, source in source_by_id.items():
        matches = [
            rectangles
            for text, bbox, rectangles in raw_spans
            if text == source.text
            and max(abs(bbox[index] - source.bbox[index]) for index in range(4)) <= 0.75
        ]
        if matches:
            result[object_id] = matches[0]
    return result


def _raw_glyph_runs(pdf_path: Path, page_index: int) -> tuple[tuple[tuple[str, Rect], ...], ...]:
    with fitz.open(pdf_path) as document:
        page = document[page_index]
        return tuple(
            tuple(
                (str(character.get("c") or ""), tuple(float(value) for value in character["bbox"]))
                for character in span.get("chars", [])
                if str(character.get("c") or "").strip() and character.get("bbox")
            )
            for block in page.get_text("rawdict").get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("chars")
        )


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
        scored = [
            (
                {index for index in uncovered if _intersection_area(piece, glyphs[index]) > 0.001},
                piece,
            )
            for piece in pieces
        ]
        covered, best = max(scored, key=lambda item: (len(item[0]), _rect_area(item[1])), default=(set(), source))
        if not covered:
            return ()
        selected.append(best)
        uncovered -= covered
        pieces.remove(best)
    return tuple(selected)


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


def _rect_area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _missing_original_objects(
    source_objects: list[TextObjectFact],
    candidate_objects: list[TextObjectFact],
    *,
    invert: bool = False,
) -> list[str]:
    result: list[str] = []
    for source in source_objects:
        present = any(
            source.text == candidate.text
            and max(abs(source.bbox[index] - candidate.bbox[index]) for index in range(4)) <= 0.75
            for candidate in candidate_objects
        )
        if present == invert:
            result.append(source.object_id)
    return result


def _missing_protected_objects(
    source_objects: list[TextObjectFact],
    candidate_objects: list[TextObjectFact],
    source_glyphs: dict[str, tuple[Rect, ...]],
    candidate_runs: tuple[tuple[tuple[str, Rect], ...], ...],
) -> list[str]:
    missing: list[str] = []
    for source in source_objects:
        expected_text = "".join(character for character in source.text if not character.isspace())
        expected_boxes = source_glyphs.get(source.object_id, ())
        present = False
        if expected_text and len(expected_text) == len(expected_boxes):
            for run in candidate_runs:
                run_text = "".join(character for character, _ in run)
                start = run_text.find(expected_text)
                while start >= 0:
                    actual_boxes = tuple(bbox for _, bbox in run[start : start + len(expected_boxes)])
                    if all(
                        max(abs(expected[index] - actual[index]) for index in range(4)) <= 0.75
                        for expected, actual in zip(expected_boxes, actual_boxes, strict=True)
                    ):
                        present = True
                        break
                    start = run_text.find(expected_text, start + 1)
                if present:
                    break
        if not present and not _missing_original_objects([source], candidate_objects):
            present = True
        if not present:
            missing.append(source.object_id)
    return missing


def _anchor_delta(anchor: str, source: Rect, rendered: Rect) -> float:
    if anchor == "LEFT":
        return abs(source[0] - rendered[0])
    if anchor == "RIGHT":
        return abs(source[2] - rendered[2])
    return abs((source[0] + source[2]) / 2.0 - (rendered[0] + rendered[2]) / 2.0)


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _union(rectangles: list[Rect]) -> Rect:
    return (
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    )


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence: object) -> CoverFinding:
    return CoverFinding(code, "HARD", owner, container_id, message, dict(evidence))
