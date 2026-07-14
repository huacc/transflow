from __future__ import annotations

import hashlib
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts
from page_toolbox_puncture.sample_snapshot import sha256_file
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.fonts import embedded_font_resources, missing_embedded_resources
from shared_pdf_kernel.render import outside_region_diff_ratio, render_contact_sheet, render_page
from toolboxes.body.flow_text.single.tools.layout_planner import _color
from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant
from toolboxes.body.flow_text.single.tools.renderer import _textbox_alignment
from toolboxes.body.table.tools.layout_planner import _fitz_alignment
from toolboxes.body.table.tools.renderer import _missing_original_objects, _translated_text_rule_overlaps

from .layout_planner import validate_owner_boundaries
from .models import CompositeFinding, CompositeLayoutPlan, CompositePageTemplate, TableRegionTransform


def render_composite_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: CompositePageTemplate,
    plan: CompositeLayoutPlan,
    evidence_dir: Path,
) -> tuple[tuple[CompositeFinding, ...], dict[str, object]]:
    boundary_findings = validate_owner_boundaries(template, plan, facts)
    if boundary_findings:
        raise ValueError("cannot_render_cross_owner_layout")
    if any(not item.fit for flow_plan in plan.flow_plans for item in flow_plan.placements):
        raise ValueError("cannot_render_unfit_flow_plan")
    if any(not item.fit for item in plan.table_plan.placements):
        raise ValueError("cannot_render_unfit_table_plan")

    source_hash = sha256_file(source_pdf)
    table_reflowed = any(item.moved for item in plan.table_region_transforms)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    flow_container_by_id = {
        container.container_id: container
        for region in template.flow_regions
        for container in region.template.containers
    }
    planned_table_ids = {placement.container_id for placement in plan.table_plan.placements}
    table_cell_by_id = {
        cell.container_id: cell
        for cell in template.table_template.cells
        if cell.container_id in planned_table_ids
    }
    flow_text_by_id = {
        placement.container_id: placement.translated_text
        for flow_plan in plan.flow_plans
        for placement in flow_plan.placements
    }
    table_text_by_id = {
        placement.container_id: placement.translated_text
        for placement in plan.table_plan.placements
    }
    flow_redacted_ids = {
        object_id
        for container in flow_container_by_id.values()
        for object_id in container.source_object_ids
    }
    table_redacted_ids = {
        object_id
        for cell in table_cell_by_id.values()
        for object_id in cell.source_object_ids
    }
    redacted_ids = flow_redacted_ids | table_redacted_ids
    residue_flow_redacted_ids = {
        object_id
        for container in flow_container_by_id.values()
        if flow_text_by_id[container.container_id].strip() != container.source_text.strip()
        for object_id in container.source_object_ids
    }
    residue_table_redacted_ids = {
        object_id
        for cell in table_cell_by_id.values()
        if (
            cell.translatable
            and table_text_by_id[cell.container_id].strip() != cell.source_text.strip()
        )
        or _moved_repaint_requires_residue_check(cell, plan.table_region_transforms)
        for object_id in cell.source_object_ids
    }
    required_resources: set[str] = set()
    receipts: list[dict[str, object]] = []
    prefix_regions: list[tuple[float, float, float, float]] = []
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")

    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        background_drawings = _page_background_drawings(page) if table_reflowed else ()
        table_drawings = _owned_table_drawings(page, plan.table_region_transforms) if table_reflowed else ()
        for object_id in sorted(flow_redacted_ids):
            page.add_redact_annot(fitz.Rect(source_by_id[object_id].bbox), fill=None)
        for object_id in sorted(table_redacted_ids):
            rect = fitz.Rect(source_by_id[object_id].bbox)
            if table_reflowed:
                rect.x0 -= 0.5
                rect.y0 -= 0.5
                rect.x1 += 0.5
                rect.y1 += 0.5
            page.add_redact_annot(rect, fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        if table_reflowed:
            for drawing, _ in table_drawings:
                rect = fitz.Rect(drawing["rect"])
                rect.x0 -= 0.4
                rect.y0 -= 0.4
                rect.x1 += 0.4
                rect.y1 += 0.4
                page.add_redact_annot(rect, fill=None)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=fitz.PDF_REDACT_TEXT_NONE,
            )
            if background_drawings:
                _draw_transformed_table_graphics(
                    page,
                    tuple((drawing, None) for drawing in background_drawings),
                    overlay=False,
                )
            _draw_transformed_table_graphics(page, table_drawings)

        for flow_plan in plan.flow_plans:
            for placement in flow_plan.placements:
                font_file, font_resource = _font_variant(
                    flow_plan.font_file,
                    flow_plan.font_resource,
                    placement.font_weight,
                )
                required_resources.add(font_resource)
                alignment = _textbox_alignment(placement.horizontal_policy)
                result = page.insert_textbox(
                    fitz.Rect(placement.output_bbox),
                    placement.translated_text,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=placement.font_size,
                    lineheight=placement.line_height,
                    color=_color(placement.color_srgb),
                    align=alignment,
                    overlay=True,
                )
                if result < 0:
                    raise RuntimeError(f"flow_layout_probe_render_disagreement:{placement.container_id}")
                receipts.append(_receipt("flow", placement.container_id, placement.translated_text, placement.output_bbox, result))
                container = flow_container_by_id[placement.container_id]
                if container.preserved_prefix:
                    marker = next(
                        (
                            source_by_id[object_id]
                            for object_id in container.source_object_ids
                            if source_by_id[object_id].text.strip()
                            in {container.preserved_prefix, "\uf0b7", "•", "●", "▪"}
                        ),
                        None,
                    )
                    if marker is None:
                        raise RuntimeError(f"preserved_prefix_source_object_missing:{placement.container_id}")
                    marker_bbox = (
                        marker.bbox[0],
                        placement.output_bbox[1],
                        placement.output_bbox[0] - 2.0,
                        placement.output_bbox[3],
                    )
                    prefix_regions.extend((marker.bbox, marker_bbox))
                    marker_result = page.insert_textbox(
                        fitz.Rect(marker_bbox),
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

        for placement in plan.table_plan.placements:
            required_resources.add(placement.font_resource)
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
                raise RuntimeError(f"table_layout_probe_render_disagreement:{placement.container_id}")
            receipts.append(_receipt("table", placement.container_id, placement.translated_text, placement.output_bbox, result))
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    candidate_objects = list(candidate_facts.text_objects)
    findings: list[CompositeFinding] = []
    if not table_reflowed and candidate_facts.locked_objects_sha256 != facts.locked_objects_sha256:
        findings.append(
            _finding(
                "LOCKED_OBJECT_CHANGED",
                "pdf_renderer",
                None,
                "页框、图片、表格线条或填色对象发生变化",
                source=facts.locked_objects_sha256,
                candidate=candidate_facts.locked_objects_sha256,
            )
        )

    protected_ids = {
        item.object_id for item in template.ownerships if item.owner == "protected"
    }
    if not table_reflowed:
        protected_ids |= set(template.table_template.protected_object_ids)
    missing_protected = _missing_protected_objects(
        [source_by_id[object_id] for object_id in sorted(protected_ids)],
        candidate_objects,
    )
    if missing_protected:
        findings.append(
            _finding(
                "PROTECTED_OBJECT_CHANGED",
                "composite_quality_judge",
                None,
                "未授权翻译的原生文字对象缺失或移动",
                object_ids=missing_protected,
            )
        )

    original_remaining = _missing_original_objects(
        [source_by_id[object_id] for object_id in sorted(residue_flow_redacted_ids)],
        candidate_objects,
        invert=True,
    )
    table_original_remaining = _missing_original_objects(
        [source_by_id[object_id] for object_id in sorted(residue_table_redacted_ids)],
        candidate_objects,
        invert=True,
    )
    original_remaining.extend(
        object_id
        for object_id in table_original_remaining
        if not _has_authorized_table_text_at_bbox(
            source_by_id[object_id].text,
            source_by_id[object_id].bbox,
            plan.table_plan.placements,
        )
    )
    if original_remaining:
        findings.append(
            _finding(
                "SOURCE_TEXT_RESIDUE",
                "composite_quality_judge",
                None,
                "已翻译容器仍保留原文字形对象",
                object_ids=original_remaining,
            )
        )

    overlap_rows = []
    for transform in plan.table_region_transforms:
        placements = tuple(
            placement
            for placement in plan.table_plan.placements
            if _rect_center_in(placement.output_bbox, transform.target_bbox)
        )
        overlap_rows.extend(
            _translated_text_rule_overlaps(
                candidate_pdf,
                placements,
                transform.target_bbox,
                facts.page_index,
            )
        )
    table_overlaps = tuple(overlap_rows)
    if table_overlaps:
        findings.append(
            _finding(
                "TABLE_LINE_TEXT_OVERLAP",
                "table_quality_judge",
                None,
                "表格译文字形与保留表格线相交",
                overlaps=table_overlaps,
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

    allowed_regions = [source_by_id[object_id].bbox for object_id in redacted_ids]
    allowed_regions.extend(
        placement.output_bbox
        for flow_plan in plan.flow_plans
        for placement in flow_plan.placements
    )
    allowed_regions.extend(placement.output_bbox for placement in plan.table_plan.placements)
    allowed_regions.extend(prefix_regions)
    if table_reflowed:
        allowed_regions.extend(item.source_bbox for item in plan.table_region_transforms)
        allowed_regions.extend(item.target_bbox for item in plan.table_region_transforms)
    diff_ratio = outside_region_diff_ratio(
        source_pdf,
        candidate_pdf,
        allowed_regions,
        page_index=facts.page_index,
    )
    if diff_ratio > 0.01:
        findings.append(
            _finding(
                "OUTSIDE_ALLOWED_REGION_CHANGED",
                "pdf_renderer",
                None,
                "正文与表格允许区域之外出现大范围渲染变化",
                changed_pixel_ratio=diff_ratio,
            )
        )
    elif diff_ratio > 0.00001:
        findings.append(
            CompositeFinding(
                "OUTSIDE_ALLOWED_REGION_RENDER_DRIFT",
                "SOFT",
                "pdf_renderer",
                None,
                "锁定对象哈希一致，但允许区外存在轻微重写或抗锯齿漂移",
                {"changed_pixel_ratio": diff_ratio},
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
        "table_structure_sha256": template.table_template.structure.structure_sha256,
        "source_table_regions": template.table_regions,
        "target_table_regions": tuple(item.target_bbox for item in plan.table_region_transforms),
        "table_region_transforms": plan.table_region_transforms,
        "table_reflowed": table_reflowed,
        "table_owned_drawing_count": len(table_drawings) if table_reflowed else 0,
        "page_background_redraw_count": len(background_drawings) if table_reflowed else 0,
        "locked_object_policy": "non_table_objects_locked" if table_reflowed else "all_objects_locked",
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "table_line_text_overlap_count": len(table_overlaps),
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "ownership_counts": {
            owner: sum(item.owner == owner for item in template.ownerships)
            for owner in ("flow", "table", "protected")
        },
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _receipt(owner: str, container_id: str, text: str, bbox, spare_height: float) -> dict[str, object]:
    return {
        "owner": owner,
        "container_id": container_id,
        "translated_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "output_bbox": bbox,
        "insert_textbox_spare_height": round(float(spare_height), 4),
        "fit": True,
    }


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence) -> CompositeFinding:
    return CompositeFinding(code, "HARD", owner, container_id, message, evidence)


def _missing_protected_objects(source_objects, candidate_objects) -> list[str]:
    missing: list[str] = []
    for source in source_objects:
        width = source.bbox[2] - source.bbox[0]
        height = source.bbox[3] - source.bbox[1]
        tolerance = 1.5 if height > max(40.0, width * 3.0) else 0.75
        present = any(
            source.text == candidate.text
            and max(abs(source.bbox[index] - candidate.bbox[index]) for index in range(4)) <= tolerance
            for candidate in candidate_objects
        )
        if not present:
            missing.append(source.object_id)
    return missing


def _owned_table_drawings(page: fitz.Page, transforms: tuple[TableRegionTransform, ...]):
    rows = []
    for drawing in page.get_drawings():
        rect = tuple(float(value) for value in drawing["rect"])
        transform = next(
            (
                item
                for item in transforms
                if item.moved and _contains_rect(item.source_bbox, rect, tolerance=1.0)
            ),
            None,
        )
        if transform is not None:
            rows.append((drawing, transform))
    return tuple(rows)


def _page_background_drawings(page: fitz.Page):
    page_rect = tuple(float(value) for value in page.rect)
    return tuple(
        drawing
        for drawing in page.get_drawings()
        if drawing.get("fill") is not None
        and len(drawing.get("items", ())) == 1
        and drawing["items"][0][0] == "re"
        and _contains_rect(tuple(float(value) for value in drawing["rect"]), page_rect, tolerance=2.0)
        and int(drawing.get("seqno") or 0) <= 1
    )


def _draw_transformed_table_graphics(page: fitz.Page, rows, *, overlay: bool = True) -> None:
    ordered_rows = rows if overlay else tuple(reversed(rows))
    for drawing, transform in ordered_rows:
        shape = page.new_shape()
        for item in drawing.get("items", []):
            kind = item[0]
            if kind == "l":
                shape.draw_line(_point(item[1], transform), _point(item[2], transform))
            elif kind == "re":
                shape.draw_rect(fitz.Rect(_transform_rect(tuple(item[1]), transform)))
            elif kind == "c":
                shape.draw_bezier(*(_point(point, transform) for point in item[1:5]))
            elif kind == "qu":
                quad = item[1]
                shape.draw_quad(
                    fitz.Quad(
                        _point(quad.ul, transform),
                        _point(quad.ur, transform),
                        _point(quad.ll, transform),
                        _point(quad.lr, transform),
                    )
                )
            else:
                raise RuntimeError(f"UNSUPPORTED_TABLE_DRAWING_ITEM:{kind}")
        line_cap = drawing.get("lineCap") or 0
        if isinstance(line_cap, (tuple, list)):
            line_cap = max(line_cap)
        shape.finish(
            width=drawing.get("width") or 1.0,
            color=drawing.get("color"),
            fill=drawing.get("fill"),
            lineCap=int(line_cap),
            lineJoin=int(drawing.get("lineJoin") or 0),
            dashes=drawing.get("dashes"),
            even_odd=bool(drawing.get("even_odd")),
            closePath=bool(drawing.get("closePath")) if drawing.get("closePath") is not None else True,
            fill_opacity=float(drawing.get("fill_opacity") or 1.0),
            stroke_opacity=float(drawing.get("stroke_opacity") or 1.0),
        )
        shape.commit(overlay=overlay)


def _point(point, transform: TableRegionTransform) -> fitz.Point:
    return fitz.Point(
        float(point.x),
        float(point.y) if transform is None else _transform_y(float(point.y), transform),
    )


def _transform_rect(rect, transform: TableRegionTransform):
    if transform is None:
        return rect
    return (
        rect[0],
        _transform_y(rect[1], transform),
        rect[2],
        _transform_y(rect[3], transform),
    )


def _transform_y(value: float, transform: TableRegionTransform) -> float:
    source = transform.source_bbox
    target = transform.target_bbox
    scale = (target[3] - target[1]) / (source[3] - source[1])
    return target[1] + (value - source[1]) * scale


def _contains_rect(outer, inner, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _rect_center_in(rect, outer) -> bool:
    x = (rect[0] + rect[2]) / 2.0
    y = (rect[1] + rect[3]) / 2.0
    return outer[0] - 0.05 <= x <= outer[2] + 0.05 and outer[1] - 0.05 <= y <= outer[3] + 0.05


def _has_authorized_table_text_at_bbox(source_text: str, source_bbox, placements) -> bool:
    literal = "".join(source_text.split()).casefold()
    if not literal:
        return False
    return any(
        literal in "".join(placement.translated_text.split()).casefold()
        and _rect_center_in(source_bbox, placement.output_bbox)
        for placement in placements
    )


def _moved_repaint_requires_residue_check(cell, transforms, tolerance: float = 0.75) -> bool:
    transform = next(
        (
            item
            for item in transforms
            if item.moved and _rect_center_in(cell.source_bbox, item.source_bbox)
        ),
        None,
    )
    if transform is None:
        return False
    transformed = _transform_rect(cell.source_bbox, transform)
    return max(
        abs(transformed[index] - cell.source_bbox[index])
        for index in range(4)
    ) > tolerance
