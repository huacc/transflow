from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle
from shared_pdf_kernel.fonts import probe_font

from . import TOOLBOX_KEY
from .models import DiagramFinding, DiagramLayoutPlan, DiagramPlacement, DiagramTemplate, Rect
from .template_builder import is_coordinate_locked_container


_FONT_SIZE_STEP = 0.5
_LEADING_STEP = 0.3


def plan_diagram_layout(
    template: DiagramTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
    font_candidates: tuple[str, ...] = (),
) -> tuple[DiagramLayoutPlan, tuple[DiagramFinding, ...]]:
    expected = [item.container_id for item in template.containers]
    actual = [item.container_id for item in bundle.translations]
    if actual != expected:
        raise ValueError("DIAGRAM_TRANSLATION_ID_MISMATCH")
    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    font_pool = _font_pool(font_file, bold_path, font_candidates)
    coverage_text = "".join(translated.values())
    font_checks = {font: probe_font(Path(font), coverage_text) for font in font_pool}

    placements: list[DiagramPlacement] = []
    findings: list[DiagramFinding] = []
    probe_document = None
    probe_page = None
    coordinate_locked_ids = {
        container.container_id
        for container in template.containers
        if is_coordinate_locked_container(template, container)
    }
    if coordinate_locked_ids:
        probe_document = fitz.open()
        probe_page = probe_document.new_page(width=template.width, height=template.height)
    try:
        for container in template.containers:
            text = translated[container.container_id]
            preferred_font = bold_path if _is_bold(container.font_name) else font_file
            selected_font, font_check = _select_covering_font(text, preferred_font, font_pool, font_checks)
            resource = f"p14diagram{font_pool.index(selected_font)}"
            if not font_check.covers_text:
                placements.append(_unfit(container, text, selected_font, resource))
                findings.append(
                    _finding(
                        "FONT_GLYPH_MISSING",
                        "diagram_layout_planner",
                        container.node_id,
                        container.container_id,
                        "目标字体不能覆盖译文字形",
                        missing_codepoints=font_check.missing_codepoints,
                    )
                )
                continue

            placement = _fit_container(
                template,
                container,
                text,
                selected_font,
                resource,
                probe_page=probe_page if container.container_id in coordinate_locked_ids else None,
                coordinate_locked=container.container_id in coordinate_locked_ids,
                prior_placements=placements,
            )
            if not placement.fit:
                placement = _rebalance_overlapping_image_pair(
                    template,
                    container,
                    text,
                    selected_font,
                    resource,
                    translated,
                    placements,
                    placement,
                )
            placements.append(placement)
            if not placement.fit:
                code = "DIAGRAM_NODE_TEXT_UNFIT" if container.owner_kind == "node" else "DIAGRAM_LOCAL_TEXT_UNFIT"
                findings.append(
                    _finding(
                        code,
                        "diagram_layout_planner",
                        container.node_id,
                        container.container_id,
                        "译文在最低可读字号下仍无法装入所属节点或局部标签区域",
                        source_bbox=container.source_bbox,
                        allowed_bbox=container.allowed_bbox,
                        role=container.role,
                    )
                )
    finally:
        if probe_document is not None:
            probe_document.close()

    collisions = _placement_collisions(placements, coordinate_locked_ids)
    if collisions:
        findings.append(
            _finding(
                "DIAGRAM_TEXT_OWNER_COLLISION",
                "diagram_layout_planner",
                None,
                None,
                "不同文字 owner 的候选区域发生新增碰撞",
                collisions=collisions,
            )
        )
    return (
        DiagramLayoutPlan(template.page_id, TOOLBOX_KEY, template.topology_sha256, tuple(placements)),
        tuple(findings),
    )


def _font_pool(font_file: str, bold_font_file: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    for value in (font_file, bold_font_file, *candidates):
        normalized = str(Path(value))
        if Path(normalized).is_file() and normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("DIAGRAM_FONT_NOT_FOUND")
    return tuple(result)


def _select_covering_font(text: str, preferred: str, font_pool: tuple[str, ...], font_checks):
    preferred = str(Path(preferred))
    ordered = (preferred, *(font for font in font_pool if font != preferred))
    fallback = None
    for font in ordered:
        aggregate = font_checks[font]
        missing = set(aggregate.missing_codepoints)
        text_missing = tuple(
            dict.fromkeys(
                f"U+{ord(char):04X}"
                for char in text
                if not char.isspace() and f"U+{ord(char):04X}" in missing
            )
        )
        check = replace(aggregate, missing_codepoints=text_missing)
        fallback = fallback or (font, check)
        if check.covers_text:
            return font, check
    return fallback


def _rebalance_overlapping_image_pair(
    template,
    container,
    text: str,
    font_file: str,
    resource: str,
    translated: dict[str, str],
    placements: list[DiagramPlacement],
    unfit_placement: DiagramPlacement,
) -> DiagramPlacement:
    if container.role != "image_framed_label" or not placements:
        return unfit_placement
    prior_container = template.containers[len(placements) - 1]
    prior_placement = placements[-1]
    if (
        prior_container.role != "image_framed_label"
        or not prior_placement.fit
        or _intersection_area(prior_container.allowed_bbox, container.allowed_bbox) <= 0
    ):
        return unfit_placement

    minimum = _minimum_size(prior_container.role, prior_container.font_size)
    ceiling = prior_placement.font_size - _FONT_SIZE_STEP
    while ceiling >= minimum - 1e-6:
        rebalanced_prior = _fit_container(
            template,
            prior_container,
            translated[prior_container.container_id],
            prior_placement.font_file,
            prior_placement.font_resource,
            prior_placements=placements[:-1],
            font_ceiling=ceiling,
        )
        if not rebalanced_prior.fit:
            break
        rebalanced_current = _fit_container(
            template,
            container,
            text,
            font_file,
            resource,
            prior_placements=[*placements[:-1], rebalanced_prior],
        )
        if rebalanced_current.fit:
            placements[-1] = rebalanced_prior
            return rebalanced_current
        ceiling = rebalanced_prior.font_size - _FONT_SIZE_STEP
    return unfit_placement


def _fit_container(
    template,
    container,
    text: str,
    font_file: str,
    resource: str,
    *,
    probe_page: fitz.Page | None = None,
    coordinate_locked: bool = False,
    prior_placements: list[DiagramPlacement] | None = None,
    font_ceiling: float | None = None,
) -> DiagramPlacement:
    probe_document = None
    if probe_page is None:
        probe_document = fitz.open()
        probe_page = probe_document.new_page(width=template.width, height=template.height)
    minimum = container.font_size * 0.52 if coordinate_locked else _minimum_size(container.role, container.font_size)
    prefer_source_band = _connector_sensitive(container, template.connectors)
    fit_bboxes = _fit_bboxes(
        container,
        coordinate_locked=coordinate_locked,
        prior_placements=prior_placements or [],
        prefer_source_band=prefer_source_band,
    )
    try:
        bbox_first = (
            not coordinate_locked
            and len(fit_bboxes) > 1
            and (container.role == "title" or (container.role == "independent_paragraph" and prefer_source_band))
        )
        trials = _measured_trials(
            template,
            container,
            text,
            font_file,
            resource,
            fit_bboxes,
            minimum,
            coordinate_locked=coordinate_locked,
            bbox_first=bbox_first,
            font_ceiling=font_ceiling,
        )
        for size, line_height, fit_bbox in trials:
            spare, glyph_bbox = _probe(
                template.width,
                template.height,
                fit_bbox,
                text,
                size,
                line_height,
                font_file,
                resource,
                container.alignment,
                probe_page=probe_page,
                capture_glyph_bbox=not coordinate_locked,
            )
            if spare >= 0:
                output = fit_bbox if coordinate_locked else _tight_output_bbox(fit_bbox, spare, container.owner_kind)
                if glyph_bbox is not None and not coordinate_locked:
                    vertical_shift = output[1] - fit_bbox[1]
                    glyph_bbox = (
                        glyph_bbox[0],
                        glyph_bbox[1] + vertical_shift,
                        glyph_bbox[2],
                        glyph_bbox[3] + vertical_shift,
                    )
                return DiagramPlacement(
                    container.container_id,
                    container.owner_kind,
                    container.owner_id,
                    container.node_id,
                    text,
                    output,
                    font_file,
                    resource,
                    size,
                    line_height,
                    container.color_srgb,
                    container.alignment,
                    (
                        f"map-coordinate-locked-measured-{size:.2f}-{line_height:.4f}"
                        if coordinate_locked
                        else f"measured-{size:.2f}-{line_height:.4f}"
                    ),
                    True,
                    glyph_bbox,
                )
        return _unfit(container, text, font_file, resource)
    finally:
        if probe_document is not None:
            probe_document.close()


def _measured_trials(
    template,
    container,
    text: str,
    font_file: str,
    resource: str,
    fit_bboxes: tuple[Rect, ...],
    minimum: float,
    *,
    coordinate_locked: bool,
    bbox_first: bool,
    font_ceiling: float | None,
):
    sizes_by_bbox = {
        fit_bbox: _font_size_candidates(
            container.font_size,
            minimum,
            min(
                _maximum_size(
                    template,
                    container,
                    text,
                    font_file,
                    resource,
                    fit_bbox,
                    coordinate_locked=coordinate_locked,
                ),
                font_ceiling if font_ceiling is not None else float("inf"),
            ),
        )
        for fit_bbox in fit_bboxes
    }

    if bbox_first:
        for fit_bbox in fit_bboxes:
            for size in sizes_by_bbox[fit_bbox]:
                for line_height in _line_height_candidates(container, size, coordinate_locked):
                    yield size, line_height, fit_bbox
        return

    sizes = sorted({size for values in sizes_by_bbox.values() for size in values}, reverse=True)
    for size in sizes:
        for fit_bbox in fit_bboxes:
            if size not in sizes_by_bbox[fit_bbox]:
                continue
            for line_height in _line_height_candidates(container, size, coordinate_locked):
                yield size, line_height, fit_bbox


def _maximum_size(
    template,
    container,
    text: str,
    font_file: str,
    resource: str,
    fit_bbox: Rect,
    *,
    coordinate_locked: bool,
) -> float:
    source_size = container.font_size
    if coordinate_locked or container.owner_kind == "node" or _area(container.source_bbox) <= 0:
        return source_size
    source_spare, _ = _probe(
        template.width,
        template.height,
        container.source_bbox,
        text,
        source_size,
        1.0,
        font_file,
        resource,
        container.alignment,
        capture_glyph_bbox=False,
    )
    if source_spare < 0:
        return source_size
    area_ratio = max(1.0, _area(fit_bbox) / _area(container.source_bbox))
    return source_size * min(1.25, area_ratio ** 0.25)


def _font_size_candidates(source_size: float, minimum: float, maximum: float) -> tuple[float, ...]:
    rounded_minimum = round(minimum, 4)
    rounded_maximum = round(maximum, 4)
    values = {round(source_size, 4), rounded_minimum}
    size = source_size + _FONT_SIZE_STEP
    while size <= maximum + 1e-6:
        values.add(round(size, 4))
        size += _FONT_SIZE_STEP
    size = source_size - _FONT_SIZE_STEP
    while size >= minimum - 1e-6:
        values.add(round(size, 4))
        size -= _FONT_SIZE_STEP
    return tuple(
        sorted(
            (value for value in values if rounded_minimum - 1e-6 <= value <= rounded_maximum + 1e-6),
            reverse=True,
        )
    )


def _line_height_candidates(container, font_size: float, coordinate_locked: bool) -> tuple[float, ...]:
    if coordinate_locked or container.owner_kind == "node":
        return (1.0,)
    maximum_extra = font_size * (0.60 if _is_body_paragraph(container) else 0.20)
    steps = int(maximum_extra / _LEADING_STEP)
    return tuple(round((font_size + step * _LEADING_STEP) / font_size, 4) for step in range(steps, -1, -1))


def _fit_bboxes(
    container,
    *,
    coordinate_locked: bool = False,
    prior_placements: list[DiagramPlacement] | None = None,
    prefer_source_band: bool = False,
) -> tuple[Rect, ...]:
    if coordinate_locked:
        frames = [container.source_bbox]
        if container.owner_kind == "node" and container.allowed_bbox != container.source_bbox:
            frames.append(container.allowed_bbox)
        return tuple(frames)
    if container.owner_kind != "node":
        allowed = container.allowed_bbox
        if container.role in {"independent_paragraph", "title", "independent_label", "image_framed_label"}:
            separation = 4.0 if container.role == "independent_paragraph" else 1.0
            blockers = [
                placement.glyph_bbox or placement.output_bbox
                for placement in (prior_placements or [])
                if placement.fit
                and (placement.glyph_bbox or placement.output_bbox)[1] < container.source_bbox[1]
                and (placement.glyph_bbox or placement.output_bbox)[3] + separation > allowed[1]
                and _intersection_area(
                    (
                        allowed[0],
                        (placement.glyph_bbox or placement.output_bbox)[1],
                        allowed[2],
                        (placement.glyph_bbox or placement.output_bbox)[3],
                    ),
                    placement.glyph_bbox or placement.output_bbox,
                ) > 0
            ]
            if blockers:
                allowed = (allowed[0], max(allowed[1], max(item[3] for item in blockers) + separation), allowed[2], allowed[3])
                if allowed[3] <= allowed[1] + 0.5:
                    return ()
        if container.role == "independent_paragraph" and prefer_source_band:
            source_band = (
                allowed[0],
                max(allowed[1], container.source_bbox[1]),
                allowed[2],
                min(allowed[3], container.source_bbox[3]),
            )
            source_top = (
                allowed[0],
                max(allowed[1], container.source_bbox[1]),
                allowed[2],
                allowed[3],
            )
            frames = []
            for frame in (source_band, source_top, allowed):
                if frame[3] > frame[1] + 0.5 and frame not in frames:
                    frames.append(frame)
            return tuple(frames)
        if container.role == "title":
            single_bottom = min(
                allowed[3],
                allowed[1] + max(container.source_bbox[3] - container.source_bbox[1] + 3.0, container.font_size * 1.6),
            )
            single = (allowed[0], allowed[1], allowed[2], single_bottom)
            if single[3] < allowed[3] - 0.5:
                return (single, allowed)
        return (allowed,)
    source = container.source_bbox
    allowed = container.allowed_bbox
    center_x = (source[0] + source[2]) / 2.0
    center_y = (source[1] + source[3]) / 2.0
    if not (allowed[0] <= center_x <= allowed[2] and allowed[1] <= center_y <= allowed[3]):
        return ()
    maximum_width = 2.0 * min(center_x - allowed[0], allowed[2] - center_x)
    maximum_height = 2.0 * min(center_y - allowed[1], allowed[3] - center_y)
    centered = (
        round(center_x - maximum_width / 2.0, 4),
        round(center_y - maximum_height / 2.0, 4),
        round(center_x + maximum_width / 2.0, 4),
        round(center_y + maximum_height / 2.0, 4),
    )
    source_inside_allowed = (
        allowed[0] <= source[0]
        and allowed[1] <= source[1]
        and source[2] <= allowed[2]
        and source[3] <= allowed[3]
    )
    frames = (source, centered, allowed) if source_inside_allowed else (centered, allowed)
    return tuple(dict.fromkeys(frames))


def _is_body_paragraph(container) -> bool:
    source_height = container.source_bbox[3] - container.source_bbox[1]
    return (
        container.role == "independent_paragraph"
        and (source_height >= container.font_size * 2.2 or len(container.source_text) >= 80)
    )


def _tight_output_bbox(bbox: Rect, spare: float, owner_kind: str) -> Rect:
    if spare <= 1.0:
        return bbox
    retained_slack = 1.0
    removable = spare - retained_slack
    if owner_kind == "node":
        top = bbox[1] + removable / 2.0
        bottom = bbox[3] - removable / 2.0
    else:
        top = bbox[1]
        bottom = bbox[3] - removable
    return (bbox[0], top, bbox[2], bottom)


def _probe(
    width: float,
    height: float,
    bbox: Rect,
    text: str,
    size: float,
    line_height: float,
    font_file: str,
    resource: str,
    alignment: str,
    *,
    probe_page: fitz.Page | None = None,
    capture_glyph_bbox: bool = True,
) -> tuple[float, Rect | None]:
    document = None
    page = probe_page
    if page is None:
        document = fitz.open()
        page = document.new_page(width=width, height=height)
    try:
        result = page.insert_textbox(
            fitz.Rect(bbox),
            text,
            fontname=resource,
            fontfile=font_file,
            fontsize=size,
            lineheight=line_height,
            align=_fitz_alignment(alignment),
            overlay=True,
        )
        if result < 0 or not capture_glyph_bbox:
            return float(result), None
        glyph_rects = [
            fitz.Rect(span["bbox"])
            for block in page.get_text("dict")["blocks"]
            if block.get("type") == 0
            for line in block.get("lines", ())
            for span in line.get("spans", ())
            if span.get("text")
        ]
        glyph_bbox = None
        if glyph_rects:
            union = glyph_rects[0]
            for rect in glyph_rects[1:]:
                union |= rect
            glyph_bbox = tuple(float(value) for value in union)
        return float(result), glyph_bbox
    finally:
        if document is not None:
            document.close()


def _minimum_size(role: str, source_size: float) -> float:
    if role == "node_text":
        return source_size * 0.50
    if role == "connector_label":
        return source_size * 0.68
    if role == "title":
        return source_size * 0.70
    if role == "image_framed_label":
        return source_size * 0.50
    if role == "map_anchor_label":
        return source_size * 0.52
    if role == "independent_paragraph":
        return source_size * 0.68
    return source_size * 0.68


def _unfit(container, text: str, font_file: str, resource: str) -> DiagramPlacement:
    return DiagramPlacement(
        container.container_id,
        container.owner_kind,
        container.owner_id,
        container.node_id,
        text,
        container.allowed_bbox,
        font_file,
        resource,
        _minimum_size(container.role, container.font_size),
        1.0,
        container.color_srgb,
        container.alignment,
        "unfit",
        False,
    )


def _placement_collisions(
    placements: list[DiagramPlacement],
    coordinate_locked_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    result = []
    coordinate_locked_ids = coordinate_locked_ids or set()
    fit = [item for item in placements if item.fit]
    for index, left in enumerate(fit):
        for right in fit[index + 1 :]:
            if left.owner_id == right.owner_id:
                continue
            if left.container_id in coordinate_locked_ids and right.container_id in coordinate_locked_ids:
                continue
            left_bbox = left.glyph_bbox or left.output_bbox
            right_bbox = right.glyph_bbox or right.output_bbox
            overlap = _intersection_area(left_bbox, right_bbox)
            if overlap > min(_area(left_bbox), _area(right_bbox)) * 0.08:
                result.append({"left": left.container_id, "right": right.container_id})
    return result


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _connector_sensitive(container, connectors) -> bool:
    if container.role != "independent_paragraph":
        return False
    source = container.source_bbox
    allowed = container.allowed_bbox
    source_height = source[3] - source[1]
    rect = (
        allowed[0],
        max(allowed[1], source[1] - source_height),
        allowed[2],
        min(allowed[3], source[3] + source_height),
    )
    tolerance = 0.4
    return any(
        not (
            max(connector.start[0], connector.end[0]) < rect[0] - tolerance
            or min(connector.start[0], connector.end[0]) > rect[2] + tolerance
            or max(connector.start[1], connector.end[1]) < rect[1] - tolerance
            or min(connector.start[1], connector.end[1]) > rect[3] + tolerance
        )
        for connector in connectors
    )


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _is_bold(font_name: str) -> bool:
    return any(token in font_name.casefold() for token in ("bold", "black", "heavy", "semibold", "demi"))


def _finding(code, owner, node_id, container_id, message, **evidence):
    return DiagramFinding(code, "HARD", owner, node_id, container_id, message, dict(evidence))
