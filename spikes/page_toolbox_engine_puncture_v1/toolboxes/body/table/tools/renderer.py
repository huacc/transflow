from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page

from .layout_planner import _contains, _fitz_alignment
from .models import TableFinding, TableLayoutPlan, TablePlacement, TableTemplate


def render_table_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: TableTemplate,
    plan: TableLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[TableFinding, ...], dict[str, object]]:
    if any(not placement.fit for placement in plan.placements):
        raise ValueError("cannot_render_unfit_table_plan")
    if plan.structure_sha256 != template.structure.structure_sha256:
        raise ValueError("table_structure_signature_mismatch")
    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    cell_by_id = {cell.container_id: cell for cell in template.translatable_cells}
    redacted_ids = {
        object_id
        for cell in template.translatable_cells
        for object_id in cell.source_object_ids
    }
    required_resources = {placement.font_resource for placement in plan.placements}
    receipts: list[dict[str, object]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        for object_id in sorted(redacted_ids):
            page.add_redact_annot(fitz.Rect(source_by_id[object_id].bbox), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        for placement in plan.placements:
            if not _contains(placement.allowed_bbox, placement.output_bbox):
                raise RuntimeError(f"CROSS_CELL_WRITE:{placement.container_id}")
            contained_in_structural_cell = _contains(placement.cell_bbox, placement.output_bbox)
            if not contained_in_structural_cell:
                raise RuntimeError(f"CROSS_CELL_WRITE:{placement.container_id}")
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
                    "cell_bbox": placement.cell_bbox,
                    "allowed_bbox": placement.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                    "insert_textbox_spare_height": round(float(result), 4),
                    "font_size": placement.font_size,
                    "line_height": placement.line_height,
                    "alignment": placement.alignment,
                    "contained_in_cell": contained_in_structural_cell,
                    "within_baseline_safe_bbox": True,
                    "new_cross_cell_excursion": False,
                }
            )
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    findings: list[TableFinding] = []
    table_container_ids = {
        cell.container_id
        for cell in template.translatable_cells
        if cell.table_id == template.structure.table_id
    }
    line_text_overlaps = _translated_text_rule_overlaps(
        candidate_pdf,
        tuple(placement for placement in plan.placements if placement.container_id in table_container_ids),
        template.structure.bbox,
        facts.page_index,
    )
    if line_text_overlaps:
        findings.append(
            _finding(
                "TABLE_LINE_TEXT_OVERLAP",
                "table_judge",
                None,
                "translated glyph bounds intersect retained table rules",
                overlaps=line_text_overlaps,
            )
        )
    if candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(
            _finding(
                "TABLE_LOCKED_OBJECT_CHANGED",
                "pdf_renderer",
                None,
                "页框、图片、表格线条或填色对象发生变化",
                source=facts.locked_objects_sha256,
                candidate=candidate_facts.locked_objects_sha256,
            )
        )
    protected_missing = _missing_original_objects(
        [source_by_id[object_id] for object_id in template.protected_object_ids],
        list(candidate_facts.text_objects),
    )
    if protected_missing:
        findings.append(
            _finding(
                "PROTECTED_CELL_CHANGED",
                "table_judge",
                None,
                "纯数字、金额、日期或币种保护格在候选中缺失或移动",
                object_ids=protected_missing,
            )
        )
    translated_by_id = {placement.container_id: placement.translated_text for placement in plan.placements}
    original_remaining: list[str] = []
    for container_id, cell in cell_by_id.items():
        if _normalized(cell.source_text) == _normalized(translated_by_id[container_id]):
            continue
        source_objects = [source_by_id[object_id] for object_id in cell.source_object_ids]
        remaining = _missing_original_objects(source_objects, list(candidate_facts.text_objects), invert=True)
        if remaining:
            original_remaining.append(container_id)
    if original_remaining:
        findings.append(
            _finding(
                "ORIGINAL_CELL_TEXT_REMAINED",
                "table_judge",
                None,
                "已翻译文字格仍保留原文字形对象",
                container_ids=original_remaining,
            )
        )
    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(
            _finding(
                "FONT_NOT_EMBEDDED",
                "pdf_renderer",
                None,
                "目标字体资源未嵌入候选 PDF",
                resources=missing_fonts,
            )
        )
    allowed = [cell.source_bbox for cell in template.translatable_cells] + [placement.output_bbox for placement in plan.placements]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed, page_index=facts.page_index)
    if diff_ratio > 0.01:
        findings.append(
            _finding(
                "OUTSIDE_ALLOWED_REGION_CHANGED",
                "pdf_renderer",
                None,
                "允许文字格之外出现大范围渲染变化",
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
        "table_structure_sha256": template.structure.structure_sha256,
        "table_bbox": template.structure.bbox,
        "column_boundaries": template.structure.column_boundaries,
        "row_boundaries": template.structure.row_boundaries,
        "protected_object_count": len(template.protected_object_ids),
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "table_line_text_overlap_count": len(line_text_overlaps),
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _translated_text_rule_overlaps(
    candidate_pdf: Path,
    placements: tuple[TablePlacement, ...],
    table_bbox: tuple[float, float, float, float],
    page_index: int,
) -> tuple[dict[str, object], ...]:
    table_rect = fitz.Rect(table_bbox)
    overlaps: list[dict[str, object]] = []
    with fitz.open(candidate_pdf) as document:
        page = document[page_index]
        rules = _table_rule_segments(page, table_rect)
        spans = [
            (str(span["text"]), fitz.Rect(span["bbox"]))
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text", "")).strip()
        ]
        for placement in placements:
            translated = _normalized(placement.translated_text)
            placement_rect = fitz.Rect(placement.output_bbox)
            for span_text, span_bbox in spans:
                normalized_span = _normalized(span_text)
                if not normalized_span or normalized_span not in translated or not span_bbox.intersects(placement_rect):
                    continue
                for orientation, coordinate, start, end in rules:
                    if _is_text_underline(span_bbox, orientation, coordinate, start, end):
                        continue
                    if orientation == "horizontal":
                        intersects = (
                            min(span_bbox.x1, end) > max(span_bbox.x0, start)
                            and span_bbox.y0 + 0.3 < coordinate < span_bbox.y1 - 0.3
                        )
                    else:
                        intersects = (
                            min(span_bbox.y1, end) > max(span_bbox.y0, start)
                            and span_bbox.x0 + 0.3 < coordinate < span_bbox.x1 - 0.3
                        )
                    if intersects:
                        overlaps.append(
                            {
                                "container_id": placement.container_id,
                                "orientation": orientation,
                                "rule_coordinate": round(float(coordinate), 4),
                                "glyph_bbox": tuple(round(float(value), 4) for value in span_bbox),
                                "glyph_text": span_text,
                            }
                        )
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for item in overlaps:
        key = (
            item["container_id"], item["orientation"], item["rule_coordinate"],
            item["glyph_bbox"], item["glyph_text"],
        )
        unique[key] = item
    return tuple(unique.values())


def _is_text_underline(
    span_bbox: fitz.Rect,
    orientation: str,
    coordinate: float,
    start: float,
    end: float,
) -> bool:
    if orientation != "horizontal" or span_bbox.height <= 0.0:
        return False
    if coordinate < span_bbox.y0 + span_bbox.height * 0.75:
        return False
    overlap = min(span_bbox.x1, end) - max(span_bbox.x0, start)
    if overlap < span_bbox.width * 0.80:
        return False
    side_slack = max(2.0, span_bbox.height * 0.75)
    return end - start <= span_bbox.width + side_slack * 2.0


def _table_rule_segments(page: fitz.Page, table_bbox: fitz.Rect) -> tuple[tuple[str, float, float, float], ...]:
    rules: list[tuple[str, float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l":
                first, second = item[1], item[2]
                if drawing.get("color") is None:
                    continue
                if abs(first.y - second.y) <= 0.4 and abs(first.x - second.x) > 10.0:
                    coordinate = (first.y + second.y) / 2.0
                    start, end = sorted((first.x, second.x))
                    if table_bbox.y0 - 0.5 <= coordinate <= table_bbox.y1 + 0.5:
                        rules.append(("horizontal", coordinate, max(start, table_bbox.x0), min(end, table_bbox.x1)))
                elif abs(first.x - second.x) <= 0.4 and abs(first.y - second.y) > 10.0:
                    coordinate = (first.x + second.x) / 2.0
                    start, end = sorted((first.y, second.y))
                    if table_bbox.x0 - 0.5 <= coordinate <= table_bbox.x1 + 0.5:
                        rules.append(("vertical", coordinate, max(start, table_bbox.y0), min(end, table_bbox.y1)))
            elif item[0] == "re":
                rect = fitz.Rect(item[1])
                if drawing.get("color") is not None:
                    if rect.width > 10.0:
                        rules.extend(
                            (
                                ("horizontal", rect.y0, max(rect.x0, table_bbox.x0), min(rect.x1, table_bbox.x1)),
                                ("horizontal", rect.y1, max(rect.x0, table_bbox.x0), min(rect.x1, table_bbox.x1)),
                            )
                        )
                    if rect.height > 10.0:
                        rules.extend(
                            (
                                ("vertical", rect.x0, max(rect.y0, table_bbox.y0), min(rect.y1, table_bbox.y1)),
                                ("vertical", rect.x1, max(rect.y0, table_bbox.y0), min(rect.y1, table_bbox.y1)),
                            )
                        )
                elif rect.height <= 1.0 and rect.width > 10.0:
                    rules.append(("horizontal", (rect.y0 + rect.y1) / 2.0, max(rect.x0, table_bbox.x0), min(rect.x1, table_bbox.x1)))
                elif rect.width <= 1.0 and rect.height > 10.0:
                    rules.append(("vertical", (rect.x0 + rect.x1) / 2.0, max(rect.y0, table_bbox.y0), min(rect.y1, table_bbox.y1)))
    return tuple(rule for rule in rules if rule[3] > rule[2])


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


def _normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence: object) -> TableFinding:
    return TableFinding(code, "HARD", owner, container_id, message, dict(evidence))
