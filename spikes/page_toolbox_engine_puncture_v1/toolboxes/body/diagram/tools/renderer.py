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

from .models import DiagramFinding, DiagramLayoutPlan, DiagramTemplate, Rect
from .template_builder import DiagramCapabilityError, diagram_geometry_sha256, is_coordinate_locked_container


def render_diagram_passthrough(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: DiagramTemplate,
    evidence_dir: Path,
) -> tuple[tuple[DiagramFinding, ...], dict[str, object]]:
    if template.mode != "passthrough" or template.containers:
        raise ValueError("diagram_passthrough_requires_empty_template")
    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, candidate_pdf)
    source_hash = sha256_file(source_pdf)
    candidate_hash = sha256_file(candidate_pdf)
    findings = () if source_hash == candidate_hash else (
        _finding(
            "DIAGRAM_PASSTHROUGH_BYTES_CHANGED",
            "diagram_pdf_renderer",
            None,
            None,
            "无可翻译原生文字的示意图没有保持字节级透传",
            source_sha256=source_hash,
            candidate_sha256=candidate_hash,
        ),
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_png = evidence_dir / "source.png"
    candidate_png = evidence_dir / "candidate.png"
    comparison_png = evidence_dir / "comparison.png"
    render_page(source_pdf, source_png, page_index=facts.page_index, zoom=2.0)
    render_page(candidate_pdf, candidate_png, page_index=facts.page_index, zoom=2.0)
    render_contact_sheet(source_pdf, candidate_pdf, comparison_png, page_index=facts.page_index, zoom=1.5)
    return findings, {
        "mode": "passthrough",
        "source_pdf_sha256": source_hash,
        "candidate_pdf_sha256": candidate_hash,
        "byte_identical": source_hash == candidate_hash,
        "diagram_geometry_sha256": template.diagram_geometry_sha256,
        "topology_sha256": template.topology_sha256,
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
    }


def render_diagram_candidate(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: DiagramTemplate,
    plan: DiagramLayoutPlan,
    evidence_dir: Path,
    allow_partial: bool = False,
) -> tuple[tuple[DiagramFinding, ...], dict[str, object]]:
    if template.mode != "translated":
        raise ValueError("diagram_candidate_requires_translated_template")
    planned_unfit = tuple(placement for placement in plan.placements if not placement.fit)
    if planned_unfit and not allow_partial:
        raise ValueError("cannot_render_unfit_diagram_plan")
    if plan.topology_sha256 != template.topology_sha256:
        raise ValueError("diagram_topology_signature_mismatch")
    if [item.container_id for item in plan.placements] != [item.container_id for item in template.containers]:
        raise ValueError("diagram_placement_order_mismatch")

    source_hash = sha256_file(source_pdf)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    container_by_id = {item.container_id: item for item in template.containers}
    node_by_id = {item.node_id: item for item in template.nodes}
    protected = tuple(source_by_id[object_id] for object_id in template.protected_object_ids)
    fitted = tuple(placement for placement in plan.placements if placement.fit)
    partition_protected_by_id = {
        item.object_id: item
        for item in (
            *protected,
            *(
                source_by_id[object_id]
                for placement in planned_unfit
                for object_id in container_by_id[placement.container_id].source_object_ids
            ),
        )
    }
    rendered_placements, safety_skipped, redaction_conflicts = _partition_safe_placements(
        fitted,
        container_by_id,
        source_by_id,
        tuple(partition_protected_by_id.values()),
    )
    if safety_skipped and not allow_partial:
        raise DiagramCapabilityError(
            f"DIAGRAM_SAFE_REDACTION_REGION_NOT_FOUND:{redaction_conflicts[0]['source_object_id']}"
        )
    skipped_ids = {item.container_id for item in safety_skipped}
    unrendered_placements = tuple(
        placement for placement in plan.placements if not placement.fit or placement.container_id in skipped_ids
    )
    rendered_plan = DiagramLayoutPlan(plan.page_id, plan.toolbox_key, plan.topology_sha256, rendered_placements)
    redacted_ids = {
        object_id
        for container in (container_by_id[item.container_id] for item in rendered_placements)
        for object_id in container.source_object_ids
    }
    redaction_protected_by_id = {
        item.object_id: item
        for item in (
            *protected,
            *(
                source_by_id[object_id]
                for placement in unrendered_placements
                for object_id in container_by_id[placement.container_id].source_object_ids
            ),
        )
    }
    redaction_protected = tuple(redaction_protected_by_id.values())
    receipts: list[dict[str, object]] = []

    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")
    if rendered_placements:
        with fitz.open(source_pdf) as document:
            page = document[facts.page_index]
            for object_id in sorted(redacted_ids):
                source_bbox = _safe_redaction_bbox(
                    source_by_id[object_id].bbox,
                    tuple(item.bbox for item in redaction_protected if item.object_id != object_id),
                )
                if source_bbox is None:
                    raise RuntimeError(f"DIAGRAM_SAFE_REDACTION_REGION_NOT_FOUND:{object_id}")
                page.add_redact_annot(fitz.Rect(source_bbox), fill=None)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            for placement in rendered_placements:
                container = container_by_id[placement.container_id]
                if placement.owner_id != container.owner_id or placement.node_id != container.node_id:
                    raise RuntimeError(f"DIAGRAM_LABEL_WRONG_OWNER:{placement.container_id}")
                if not _contains(container.allowed_bbox, placement.output_bbox):
                    raise RuntimeError(f"DIAGRAM_TEXT_OUTSIDE_ALLOWED_REGION:{placement.container_id}")
                coordinate_locked = is_coordinate_locked_container(template, container)
                source_frame_changed = any(
                    abs(left - right) > 0.01
                    for left, right in zip(container.source_bbox, placement.output_bbox)
                )
                if coordinate_locked and container.owner_kind != "node" and source_frame_changed:
                    raise RuntimeError(f"DIAGRAM_MAP_TEXT_COORDINATE_CHANGED:{placement.container_id}")
                if container.node_id and (not coordinate_locked or source_frame_changed):
                    node = node_by_id[container.node_id]
                    if not _contains(node.boundary_bbox, placement.output_bbox):
                        raise RuntimeError(f"DIAGRAM_NODE_TEXT_OUTSIDE_NODE:{placement.container_id}")
                spare = page.insert_textbox(
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
                if spare < 0:
                    raise RuntimeError(f"diagram_layout_probe_render_disagreement:{placement.container_id}")
                receipts.append(
                    {
                        "container_id": placement.container_id,
                        "owner_kind": placement.owner_kind,
                        "owner_id": placement.owner_id,
                        "node_id": placement.node_id,
                        "source_bbox": container.source_bbox,
                        "allowed_bbox": container.allowed_bbox,
                        "output_bbox": placement.output_bbox,
                        "glyph_bbox": placement.glyph_bbox,
                        "fit_profile": placement.fit_profile,
                        "font_size": placement.font_size,
                        "line_height": placement.line_height,
                        "insert_textbox_spare_height": round(float(spare), 4),
                        "translated_text_sha256": hashlib.sha256(placement.translated_text.encode("utf-8")).hexdigest(),
                    }
                )
            document.save(temporary, garbage=4, deflate=True)
        temporary.replace(candidate_pdf)
    else:
        shutil.copy2(source_pdf, candidate_pdf)
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("source_pdf_changed_during_diagram_render")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=facts.page_index, page_id=facts.page_id)
    candidate_diagram_geometry_sha256 = diagram_geometry_sha256(candidate_pdf, facts.page_index, candidate_facts)
    findings: list[DiagramFinding] = []
    if redaction_conflicts:
        findings.append(
            _finding(
                "DIAGRAM_SAFE_REDACTION_REGION_NOT_FOUND",
                "diagram_pdf_renderer",
                None,
                None,
                "部分文字与受保护文字重叠；保留这些容器的源文并继续渲染其他译文",
                conflicts=redaction_conflicts,
                skipped_container_ids=[item.container_id for item in safety_skipped],
            )
        )
    if candidate_diagram_geometry_sha256 != template.diagram_geometry_sha256:
        findings.append(
            _finding(
                "DIAGRAM_TOPOLOGY_CHANGED",
                "diagram_pdf_renderer",
                None,
                None,
                "节点、连线、箭头、图片或其他不可动图形发生变化",
                source_locked_objects_sha256=facts.locked_objects_sha256,
                candidate_locked_objects_sha256=candidate_facts.locked_objects_sha256,
                source_diagram_geometry_sha256=template.diagram_geometry_sha256,
                candidate_diagram_geometry_sha256=candidate_diagram_geometry_sha256,
                source_topology_sha256=template.topology_sha256,
            )
        )

    protected_missing = _missing_original_objects(protected, list(candidate_facts.text_objects))
    if protected_missing:
        findings.append(
            _finding(
                "DIAGRAM_PROTECTED_TEXT_CHANGED",
                "diagram_quality_judge",
                None,
                None,
                "数字、日期、代码、页码或其他保护文字缺失或移动",
                object_ids=protected_missing,
            )
        )

    source_remaining = []
    for placement in rendered_placements:
        container = container_by_id[placement.container_id]
        translated = placement.translated_text
        if _normalized(container.source_text) == _normalized(translated):
            continue
        sources = [source_by_id[object_id] for object_id in container.source_object_ids]
        if any(_same_original_text(source, candidate) for source in sources for candidate in candidate_facts.text_objects):
            source_remaining.append(container.container_id)
    if source_remaining:
        findings.append(
            _finding(
                "DIAGRAM_SOURCE_TEXT_REMAINED",
                "diagram_quality_judge",
                None,
                None,
                "已经翻译的原生文字仍残留在原 owner 中",
                container_ids=source_remaining,
            )
        )

    missing_translations = _missing_translated_text(candidate_pdf, facts.page_index, template, rendered_plan)
    if missing_translations:
        findings.append(
            _finding(
                "DIAGRAM_TRANSLATION_MISSING",
                "diagram_quality_judge",
                None,
                None,
                "译文没有完整出现在所属 owner 中",
                container_ids=missing_translations,
            )
        )

    connector_collisions = _new_connector_collisions(template, rendered_plan)
    if connector_collisions:
        findings.append(
            _finding(
                "DIAGRAM_CONNECTOR_TEXT_COLLISION",
                "diagram_quality_judge",
                None,
                None,
                "译文区域比源文字区域新增覆盖连线",
                collisions=connector_collisions,
            )
        )

    required_resources = {item.font_resource for item in rendered_placements}
    missing_fonts = missing_embedded_resources(candidate_pdf, required_resources, facts.page_index)
    if missing_fonts:
        findings.append(
            _finding(
                "FONT_NOT_EMBEDDED",
                "diagram_pdf_renderer",
                None,
                None,
                "目标字体资源未嵌入候选 PDF",
                resources=missing_fonts,
            )
        )

    allowed_regions = [item.source_bbox for item in template.containers] + [item.output_bbox for item in rendered_placements]
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, allowed_regions, page_index=facts.page_index)
    if diff_ratio > 0.012:
        findings.append(
            _finding(
                "DIAGRAM_OUTSIDE_ALLOWED_REGION_CHANGED",
                "diagram_pdf_renderer",
                None,
                None,
                "所有文字 owner 之外出现大范围渲染变化",
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
        "mode": "translated",
        "layout_strategy": template.layout_strategy,
        "source_pdf_sha256": source_hash,
        "candidate_pdf_sha256": sha256_file(candidate_pdf),
        "source_locked_objects_sha256": facts.locked_objects_sha256,
        "candidate_locked_objects_sha256": candidate_facts.locked_objects_sha256,
        "source_diagram_geometry_sha256": template.diagram_geometry_sha256,
        "candidate_diagram_geometry_sha256": candidate_diagram_geometry_sha256,
        "source_topology_sha256": template.topology_sha256,
        "candidate_topology_sha256": template.topology_sha256
        if candidate_diagram_geometry_sha256 == template.diagram_geometry_sha256
        else None,
        "node_count": len(template.nodes),
        "connector_count": len(template.connectors),
        "container_count": len(template.containers),
        "protected_object_count": len(template.protected_object_ids),
        "candidate_kind": "PARTIAL_REVIEW_CANDIDATE" if unrendered_placements else "FULL_TRANSLATION_CANDIDATE",
        "rendered_container_ids": [item.container_id for item in rendered_placements],
        "unrendered_container_ids": [item.container_id for item in unrendered_placements],
        "redaction_conflicts": redaction_conflicts,
        "outside_allowed_changed_pixel_ratio": diff_ratio,
        "embedded_font_resources": embedded_font_resources(candidate_pdf, facts.page_index),
        "source_png": str(source_png),
        "candidate_png": str(candidate_png),
        "comparison_png": str(comparison_png),
        "insertion_receipts": receipts,
    }
    return tuple(findings), evidence


def _partition_safe_placements(
    placements,
    container_by_id: dict[str, object],
    source_by_id: dict[str, TextObjectFact],
    protected: tuple[TextObjectFact, ...],
):
    renderable = list(placements)
    skipped = []
    conflicts = []
    protected_by_id = {item.object_id: item for item in protected}
    while renderable:
        next_renderable = []
        newly_skipped = []
        for placement in renderable:
            container = container_by_id[placement.container_id]
            placement_conflicts = []
            for source_object_id in container.source_object_ids:
                source = source_by_id[source_object_id]
                protected_objects = tuple(protected_by_id.values())
                if _safe_redaction_bbox(source.bbox, tuple(item.bbox for item in protected_objects)) is None:
                    for protected_object in protected_objects:
                        overlap = _intersection_area(source.bbox, protected_object.bbox)
                        if overlap <= 0.1:
                            continue
                        placement_conflicts.append(
                            {
                                "container_id": placement.container_id,
                                "source_object_id": source_object_id,
                                "protected_object_id": protected_object.object_id,
                                "intersection_area": round(overlap, 4),
                            }
                        )
            if placement_conflicts:
                newly_skipped.append(placement)
                conflicts.extend(placement_conflicts)
            else:
                next_renderable.append(placement)
        if not newly_skipped:
            break
        skipped.extend(newly_skipped)
        for placement in newly_skipped:
            container = container_by_id[placement.container_id]
            for source_object_id in container.source_object_ids:
                protected_by_id[source_object_id] = source_by_id[source_object_id]
        renderable = next_renderable
    return tuple(renderable), tuple(skipped), conflicts


def _safe_redaction_bbox(source_bbox: Rect, protected_bboxes: tuple[Rect, ...]) -> Rect | None:
    original_area = _area(source_bbox)
    safe = source_bbox
    for protected_bbox in protected_bboxes:
        if _intersection_area(safe, protected_bbox) <= 0.1:
            continue
        margin = 0.15
        candidates = (
            (safe[0], safe[1], safe[2], min(safe[3], protected_bbox[1] - margin)),
            (safe[0], max(safe[1], protected_bbox[3] + margin), safe[2], safe[3]),
        )
        viable = [item for item in candidates if _area(item) >= original_area * 0.80]
        if not viable:
            return None
        safe = max(viable, key=_area)
    return safe


def _missing_translated_text(pdf_path: Path, page_index: int, template: DiagramTemplate, plan: DiagramLayoutPlan) -> list[str]:
    missing = []
    container_by_id = {item.container_id: item for item in template.containers}
    with fitz.open(pdf_path) as document:
        page = document[page_index]
        for placement in plan.placements:
            container = container_by_id[placement.container_id]
            visible = page.get_textbox(fitz.Rect(container.allowed_bbox))
            if _normalized(placement.translated_text) not in _normalized(visible):
                missing.append(placement.container_id)
    return missing


def _new_connector_collisions(template: DiagramTemplate, plan: DiagramLayoutPlan) -> list[dict[str, object]]:
    containers = {item.container_id: item for item in template.containers}
    result = []
    for placement in plan.placements:
        container = containers[placement.container_id]
        if container.owner_kind == "node":
            continue
        source_hits = sum(_segment_hits_rect(item.start, item.end, container.source_bbox) for item in template.connectors)
        output_bbox = placement.glyph_bbox or placement.output_bbox
        output_hits = sum(_segment_hits_rect(item.start, item.end, output_bbox) for item in template.connectors)
        if output_hits > source_hits:
            result.append(
                {
                    "container_id": placement.container_id,
                    "source_connector_hits": source_hits,
                    "output_connector_hits": output_hits,
                }
            )
    return result


def _segment_hits_rect(start, end, rect: Rect) -> bool:
    expanded = fitz.Rect(rect[0] - 0.4, rect[1] - 0.4, rect[2] + 0.4, rect[3] + 0.4)
    result = expanded.intersects(fitz.Rect(min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]) + 0.01, max(start[1], end[1]) + 0.01))
    if not result:
        return False
    return bool(fitz.Rect(expanded).intersect(fitz.Rect(min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]) + 0.01, max(start[1], end[1]) + 0.01)))


def _missing_original_objects(source_objects: list[TextObjectFact], candidate_objects: list[TextObjectFact]) -> list[str]:
    return [source.object_id for source in source_objects if not any(_same_original_text(source, candidate) for candidate in candidate_objects)]


def _same_original_text(source: TextObjectFact, candidate: TextObjectFact) -> bool:
    return source.text == candidate.text and max(abs(source.bbox[index] - candidate.bbox[index]) for index in range(4)) <= 0.9


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _normalized(value: str) -> str:
    visual_equivalents = str.maketrans({"·": "•", "‧": "•", "∙": "•", "●": "•", "▪": "•", "◦": "•", "‣": "•"})
    return "".join(value.translate(visual_equivalents).split()).casefold()


def _finding(code, owner, node_id, container_id, message, **evidence):
    return DiagramFinding(code, "HARD", owner, node_id, container_id, message, dict(evidence))
