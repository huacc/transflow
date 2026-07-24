from __future__ import annotations

import itertools
import re
import statistics
from dataclasses import dataclass, replace

from transflow.domain.common import content_sha256
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact

from .models import ChartTemplate, ChartTextContainer, ChartVisualRegion, Rect


class ChartCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Line:
    objects: tuple[KernelTextFact, ...]
    text: str
    bbox: Rect
    font_size: float
    color_srgb: int
    font_name: str


@dataclass(frozen=True)
class _Visual:
    object_id: str
    bbox: Rect
    kind: str


@dataclass(frozen=True)
class _LocalTableCell:
    allowed_bbox: Rect
    alignment: str
    role: str


def build_chart_template(facts: ExtractedPageFacts) -> ChartTemplate:
    """Adapt the frozen Spike ownership core to production mechanical facts."""

    width = facts.page.width_points
    height = facts.page.height_points
    visuals = tuple(
        [_Visual(item.object_id, item.bbox, "IMAGE") for item in facts.image_objects]
        + [_Visual(item.object_id, item.bbox, "DRAWING") for item in facts.drawing_objects]
    )
    if not visuals:
        raise ChartCapabilityError("CHART_VISUAL_NOT_FOUND")

    regions = _visual_regions(visuals, width, height)
    if not regions:
        raise ChartCapabilityError("CHART_REGION_NOT_FOUND")

    lines = _logical_lines(facts.text_spans)
    local_table_cells = _local_table_cells(lines, width, height)
    semantic_continuations = _semantic_numeric_continuations(lines)
    protected_lines = [
        line
        for line in lines
        if _line_key(line) not in semantic_continuations and _protected(line, width, height)
    ]
    editable_lines = [line for line in lines if line not in protected_lines]
    regular_lines = [line for line in editable_lines if _line_key(line) not in local_table_cells]
    table_groups = _table_line_groups(editable_lines, local_table_cells)
    editable_groups = _merge_overlapping_groups(
        [
            *_container_groups(
                regular_lines,
                visuals,
                width * height,
                all_lines=lines,
            ),
            *table_groups,
        ]
    )
    median_font = statistics.median(line.font_size for line in editable_lines) if editable_lines else 8.0
    visual_by_id = {item.object_id: item for item in visuals}
    page_area = width * height

    containers: list[ChartTextContainer] = []
    for reading_order, group in enumerate(editable_groups):
        source_bbox = _union([line.bbox for line in group])
        source_ids = tuple(item.object_id for line in group for item in line.objects)
        source_text = _joined_text(group)
        font_size = statistics.median(line.font_size for line in group)
        legend_row = _legend_data_row(group, lines, visuals, page_area)
        swatch = legend_row[1] if legend_row is not None else _legend_anchor(source_bbox, font_size, visuals, page_area)
        association = _association(source_bbox, regions)
        table_cell = _table_group_cell(group, local_table_cells)
        rotation = _rotation(source_bbox, source_text)
        page_role = _page_role(source_bbox, rotation, width, height)
        role = (
            table_cell.role
            if table_cell is not None
            else page_role
            or (
                "AXIS_OR_CATEGORY_LABEL"
                if rotation and len(re.findall(r"[A-Za-z\u3400-\u9fff]", source_text)) <= 48
                else _role(source_text, source_bbox, font_size, median_font, swatch, association)
            )
        )
        internal_visual = _internal_overlay_visual(
            source_bbox,
            visuals,
            association,
            role,
            font_size,
            page_area,
        )
        alignment = (
            table_cell.alignment
            if table_cell is not None
            else _alignment(group, source_bbox, association, visuals, page_area, width, swatch, role)
        )
        if internal_visual is not None and abs(_center_x(source_bbox) - _center_x(internal_visual.bbox)) <= max(
            font_size,
            (internal_visual.bbox[2] - internal_visual.bbox[0]) * 0.18,
        ):
            alignment = "CENTER"
        allowed_bbox = (
            table_cell.allowed_bbox
            if table_cell is not None
            else _allowed_bbox(
                group,
                source_bbox,
                lines,
                regions,
                visuals,
                association,
                swatch,
                role,
                alignment,
                width,
                height,
            )
        )
        if internal_visual is not None:
            gutter = min(0.5, font_size * 0.06)
            inner = (
                min(source_bbox[0], internal_visual.bbox[0] + gutter),
                min(source_bbox[1], internal_visual.bbox[1] + gutter),
                max(source_bbox[2], internal_visual.bbox[2] - gutter),
                max(source_bbox[3], internal_visual.bbox[3] - gutter),
            )
            allowed_bbox = (
                max(allowed_bbox[0], inner[0]),
                max(allowed_bbox[1], inner[1]),
                min(allowed_bbox[2], inner[2]),
                min(allowed_bbox[3], inner[3]),
            )
        if legend_row is not None:
            row_center = _center_y(source_bbox)
            row_half_height = font_size * 1.45 / 2.0
            allowed_bbox = (
                allowed_bbox[0],
                min(allowed_bbox[1], row_center - row_half_height),
                allowed_bbox[2],
                max(allowed_bbox[3], row_center + row_half_height),
            )
        overlapping_visual_ids = tuple(
            item.object_id
            for item in visuals
            if _area(item.bbox) < page_area * 0.80
            and _intersection_area(item.bbox, source_bbox) > 0.01
        )
        data_anchor_ids = (
            tuple(item.object_id for item in legend_row[0].objects)
            if legend_row is not None
            else ()
        )
        anchor_ids = tuple(
            dict.fromkeys((*data_anchor_ids, *((swatch.object_id,) if swatch else overlapping_visual_ids)))
        )
        if swatch:
            anchor_ids = tuple(dict.fromkeys((*data_anchor_ids, swatch.object_id, *overlapping_visual_ids)))
        elif not anchor_ids:
            anchor_ids = tuple(
                object_id
                for object_id in association.object_ids
                if object_id in visual_by_id
            )[:1]
        containers.append(
            ChartTextContainer(
                container_id=f"chart-text-{reading_order:03d}",
                role=role,
                association_id=association.region_id,
                source_object_ids=source_ids,
                semantic_object_id=_semantic_object_id(facts, group, source_text),
                source_text=source_text,
                source_bbox=_round_rect(source_bbox),
                allowed_bbox=_round_rect(allowed_bbox),
                anchor_object_ids=anchor_ids,
                anchor_relation=_anchor_relation(
                    source_bbox,
                    legend_row[0].bbox if legend_row is not None else swatch.bbox if swatch else association.bbox,
                ),
                reading_order=reading_order,
                required_literals=_required_literals(source_text),
                font_name=group[0].font_name,
                font_size=round(font_size, 4),
                color_srgb=statistics.mode(line.color_srgb for line in group),
                alignment=alignment,
                rotation=rotation,
            )
        )

    containers = _restore_minimum_textbox_heights(
        _disjoint_allowed_regions(containers),
        regions,
        height,
    )
    editable_ids = {object_id for container in containers for object_id in container.source_object_ids}
    protected_ids = tuple(
        item.object_id for item in facts.text_spans if item.object_id not in editable_ids
    )
    owned_ids = [object_id for container in containers for object_id in container.source_object_ids] + list(protected_ids)
    expected_ids = [item.object_id for item in facts.text_spans]
    if sorted(owned_ids) != sorted(expected_ids) or len(owned_ids) != len(set(owned_ids)):
        raise RuntimeError("CHART_TEXT_OWNERSHIP_NOT_TOTAL")

    locked_hash = facts.locked_objects_hash or content_sha256(
        {"images": facts.image_objects, "drawings": facts.drawing_objects}
    )
    structure_sha256 = content_sha256(
        {
            "toolbox_key": "body.chart",
            "page_identity": facts.page_identity,
            "visual_regions": regions,
            "containers": containers,
            "protected_object_ids": protected_ids,
            "locked_objects_sha256": locked_hash,
        }
    )
    return ChartTemplate(
        page_identity=facts.page_identity,
        width=width,
        height=height,
        visual_regions=regions,
        containers=tuple(containers),
        protected_object_ids=protected_ids,
        locked_objects_hash=locked_hash,
        structure_hash=structure_sha256,
    )


def _semantic_object_id(
    facts: ExtractedPageFacts,
    group: list[_Line],
    source_text: str,
) -> str:
    """Expose a stable primary identity; the unit retains every source span."""

    del facts, source_text
    source_objects = tuple(item for line in group for item in line.objects)
    if not source_objects:
        raise ChartCapabilityError("CHART_SEMANTIC_OBJECT_MISSING")
    return source_objects[0].object_id


def _visual_regions(visuals: tuple[_Visual, ...], width: float, height: float) -> tuple[ChartVisualRegion, ...]:
    page_area = width * height
    result: list[ChartVisualRegion] = []
    page_backgrounds = [item for item in visuals if item.kind == "IMAGE" and _area(item.bbox) >= page_area * 0.80]
    for item in visuals:
        if item.kind != "IMAGE" or item in page_backgrounds:
            continue
        if _area(item.bbox) >= page_area * 0.004:
            result.append(
                ChartVisualRegion(
                    region_id=f"chart-region-{len(result):03d}",
                    kind="RASTER",
                    bbox=_round_rect(item.bbox),
                    object_ids=(item.object_id,),
                )
            )

    drawings = [item for item in visuals if item.kind == "DRAWING"]
    drawing_merge_gap = min(width, height) * 0.017
    groups: list[list[_Visual]] = []
    for drawing in sorted(drawings, key=lambda item: (item.bbox[1], item.bbox[0])):
        touching = [
            group
            for group in groups
            if _rect_gap(_union([item.bbox for item in group]), drawing.bbox) <= drawing_merge_gap
        ]
        if not touching:
            groups.append([drawing])
            continue
        target = touching[0]
        target.append(drawing)
        for extra in touching[1:]:
            target.extend(extra)
            groups.remove(extra)
    for group in groups:
        bbox = _union([item.bbox for item in group])
        if len(group) < 2 and _area(bbox) < page_area * 0.002:
            continue
        result.append(
            ChartVisualRegion(
                region_id=f"chart-region-{len(result):03d}",
                kind="VECTOR",
                bbox=_round_rect(bbox),
                object_ids=tuple(item.object_id for item in group),
            )
        )

    if not result and page_backgrounds:
        item = max(page_backgrounds, key=lambda value: _area(value.bbox))
        result.append(ChartVisualRegion("chart-region-000", "RASTER", _round_rect(item.bbox), (item.object_id,)))
    return tuple(sorted(result, key=lambda item: (item.bbox[1], item.bbox[0], item.region_id)))


def _logical_lines(objects: tuple[KernelTextFact, ...]) -> list[_Line]:
    canonical, aliases = _canonical_text_objects(objects)
    bands: list[list[KernelTextFact]] = []
    for item in sorted(canonical, key=lambda value: (_center_y(value.bbox), value.bbox[0])):
        target = next((band for band in reversed(bands) if any(_same_baseline(member, item) for member in band)), None)
        if target is None:
            bands.append([item])
        else:
            target.append(item)

    rows: list[list[KernelTextFact]] = []
    for band in bands:
        current: list[KernelTextFact] = []
        for item in sorted(band, key=lambda value: (value.bbox[0], value.bbox[1])):
            if current and not _same_row(current[-1], item):
                rows.append(current)
                current = []
            current.append(item)
        if current:
            rows.append(current)

    lines = []
    for row in rows:
        ordered = tuple(sorted(row, key=lambda value: value.bbox[0]))
        source_objects = tuple(alias for item in ordered for alias in aliases[item.object_id])
        lines.append(
            _Line(
                objects=source_objects,
                text=_join_fragments(ordered),
                bbox=_union([item.bbox for item in ordered]),
                font_size=statistics.median(item.font_size for item in ordered),
                color_srgb=statistics.mode(item.color_srgb for item in ordered),
                font_name=ordered[0].font_name,
            )
        )
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def _line_key(line: _Line) -> tuple[str, ...]:
    return tuple(sorted(item.object_id for item in line.objects))


def _local_table_cells(lines: list[_Line], page_width: float, page_height: float) -> dict[tuple[str, ...], _LocalTableCell]:
    bands = _row_bands(
        line
        for line in lines
        if page_height * 0.10 <= line.bbox[1] and line.bbox[3] <= page_height * 0.92
    )
    candidates = [
        band
        for band in bands
        if len(band) >= 3
        and sum(_numeric_like(line.text) for line in band) >= 2
        and any(re.search(r"[A-Za-z\u3400-\u9fff]", line.text) for line in band)
    ]
    runs: list[list[tuple[_Line, ...]]] = []
    for band in candidates:
        if runs and _stable_table_band(runs[-1][-1], band):
            runs[-1].append(band)
        else:
            runs.append([band])

    result: dict[tuple[str, ...], _LocalTableCell] = {}
    band_size_by_line = {
        _line_key(line): len(band)
        for band in bands
        for line in band
    }
    for run in (item for item in runs if len(item) >= 3):
        ordered_bands = [tuple(sorted(band, key=lambda line: line.bbox[0])) for band in run]
        column_count = len(ordered_bands[0])
        column_lefts = [
            min(band[index].bbox[0] for band in ordered_bands)
            for index in range(column_count)
        ]
        column_rights = [
            max(band[index].bbox[2] for band in ordered_bands)
            for index in range(column_count)
        ]
        left_boundaries = [
            column_lefts[index]
            if index == 0
            else (column_rights[index - 1] + column_lefts[index]) / 2.0
            for index in range(column_count)
        ]
        right_boundaries = [
            (column_rights[index] + column_lefts[index + 1]) / 2.0
            if index + 1 < column_count
            else min(page_width * 0.955, column_rights[index] + 24.0)
            for index in range(column_count)
        ]
        centers = [statistics.median(_center_y(line.bbox) for line in band) for band in ordered_bands]
        row_step = statistics.median(
            right - left for left, right in zip(centers, centers[1:])
        )
        numeric_columns = [
            all(_numeric_like(band[index].text) for band in ordered_bands)
            for index in range(column_count)
        ]
        for row_index, band in enumerate(ordered_bands):
            next_top = (
                min(line.bbox[1] for line in ordered_bands[row_index + 1]) - 0.8
                if row_index + 1 < len(ordered_bands)
                else None
            )
            for column_index, line in enumerate(band):
                if not re.search(r"[A-Za-z\u3400-\u9fff]", line.text):
                    continue
                bottom = next_top if next_top is not None else line.bbox[3] + max(1.0, row_step - (line.bbox[3] - line.bbox[1]) - 0.8)
                allowed = (
                    line.bbox[0],
                    line.bbox[1],
                    max(line.bbox[2] + 0.5, right_boundaries[column_index]),
                    max(line.bbox[3] + 0.5, bottom),
                )
                result[_line_key(line)] = _LocalTableCell(_round_rect(allowed), "LEFT", "TABLE_CELL")

        first_body_top = min(line.bbox[1] for line in ordered_bands[0])
        last_body_bottom = max(line.bbox[3] for line in ordered_bands[-1])
        table_left = left_boundaries[0]
        table_right = right_boundaries[-1]
        first_numeric_column = next((index for index, numeric in enumerate(numeric_columns) if numeric), column_count)
        section_lines = [
            line
            for line in lines
            if _line_key(line) not in result
            and band_size_by_line.get(_line_key(line)) == 1
            and _font_style(line.font_name)[0]
            and re.search(r"[A-Za-z\u3400-\u9fff]", line.text)
            and line.bbox[1] >= first_body_top - row_step * 1.5
            and line.bbox[3] <= last_body_bottom
            and _axis_overlap((line.bbox[0], line.bbox[2]), (table_left, table_right)) > 0.01
            and (
                first_numeric_column == column_count
                or _center_x(line.bbox) < left_boundaries[first_numeric_column]
            )
        ]
        body_tops = sorted(min(line.bbox[1] for line in band) for band in ordered_bands)
        for line in section_lines:
            next_top = next((top for top in body_tops if top > line.bbox[3]), None)
            bottom = line.bbox[3] + max(1.0, row_step - (line.bbox[3] - line.bbox[1]))
            if next_top is not None:
                bottom = min(bottom, next_top - 0.8)
            right = left_boundaries[first_numeric_column] - 0.8 if first_numeric_column < column_count else right_boundaries[0]
            allowed = (line.bbox[0], line.bbox[1], max(line.bbox[2], right), max(line.bbox[3], bottom))
            result[_line_key(line)] = _LocalTableCell(_round_rect(allowed), "LEFT", "TABLE_SECTION")

        header_gap = max(row_step * 3.5, statistics.median(line.font_size for line in ordered_bands[0]) * 4.5)
        body_font_size = statistics.median(line.font_size for band in ordered_bands for line in band)
        header_lines = [
            line
            for line in lines
            if _line_key(line) not in result
            and re.search(r"[A-Za-z\u3400-\u9fff]", line.text)
            and line.font_size <= body_font_size * 1.25
            and line.bbox[3] <= first_body_top
            and first_body_top - line.bbox[3] <= header_gap
            and _axis_overlap((line.bbox[0], line.bbox[2]), (table_left, table_right)) > 0.01
        ]
        header_keys = {_line_key(line) for line in header_lines}
        header_lines = [
            line
            for line in header_lines
            if not _continues_non_table_flow(line, lines, header_keys)
        ]
        for line in header_lines:
            column_index = min(
                range(column_count),
                key=lambda index: abs(_center_x(line.bbox) - (left_boundaries[index] + right_boundaries[index]) / 2.0),
            )
            horizontal_padding = max(1.0, line.font_size * 0.25)
            alignment = "RIGHT" if numeric_columns[column_index] else "LEFT"
            left = (
                left_boundaries[column_index] + horizontal_padding
                if alignment == "RIGHT"
                else line.bbox[0]
            )
            right = (
                column_rights[column_index]
                if column_index == column_count - 1
                else right_boundaries[column_index] - horizontal_padding
            )
            allowed = (
                min(line.bbox[0], left),
                line.bbox[1],
                max(line.bbox[2], right),
                max(line.bbox[3], first_body_top - 0.8),
            )
            result[_line_key(line)] = _LocalTableCell(_round_rect(allowed), alignment, "TABLE_HEADER")

        total_gap = max(row_step * 2.0, statistics.median(line.font_size for line in ordered_bands[-1]) * 2.5)
        total_lines = [
            line
            for line in lines
            if _line_key(line) not in result
            and _font_style(line.font_name)[0]
            and re.search(r"[A-Za-z\u3400-\u9fff]", line.text)
            and line.bbox[1] >= last_body_bottom
            and line.bbox[1] - last_body_bottom <= total_gap
            and _axis_overlap((line.bbox[0], line.bbox[2]), (table_left, table_right)) > 0.01
        ]
        for line in total_lines:
            column_index = min(
                range(column_count),
                key=lambda index: abs(_center_x(line.bbox) - (left_boundaries[index] + right_boundaries[index]) / 2.0),
            )
            right = right_boundaries[column_index]
            if column_index < first_numeric_column < column_count:
                right = left_boundaries[first_numeric_column] - 0.8
            allowed = (
                min(line.bbox[0], left_boundaries[column_index]),
                line.bbox[1],
                max(line.bbox[2], right),
                line.bbox[3] + max(1.0, row_step - (line.bbox[3] - line.bbox[1])),
            )
            alignment = "RIGHT" if numeric_columns[column_index] else "LEFT"
            result[_line_key(line)] = _LocalTableCell(_round_rect(allowed), alignment, "TABLE_TOTAL")
    for key, cell in _textual_table_cells(lines, page_width, page_height).items():
        result.setdefault(key, cell)
    for key, cell in _right_value_table_cells(
        lines,
        page_width,
        page_height,
    ).items():
        result.setdefault(key, cell)
    return result


def _right_value_table_cells(
    lines: list[_Line],
    page_width: float,
    page_height: float,
) -> dict[tuple[str, ...], _LocalTableCell]:
    """Keep every label in a stable right-value table bound to its visual row."""

    bands = _row_bands(
        line
        for line in lines
        if page_height * 0.10 <= line.bbox[1] and line.bbox[3] <= page_height * 0.92
    )
    candidates = [
        band
        for band in bands
        if len(band) >= 2
        and any(_numeric_like(line.text) for line in band)
        and any(
            re.search(r"[A-Za-z\u3400-\u9fff]", line.text)
            for line in band
            if not _numeric_like(line.text)
        )
        and max(
            line.bbox[2]
            for line in band
            if _numeric_like(line.text)
        )
        >= page_width * 0.65
    ]
    runs: list[list[tuple[_Line, ...]]] = []
    for band in candidates:
        if runs and _stable_right_value_band(runs[-1][-1], band):
            runs[-1].append(band)
        else:
            runs.append([band])

    result: dict[tuple[str, ...], _LocalTableCell] = {}
    for run in (item for item in runs if len(item) >= 4):
        row_centers = [
            statistics.median(_center_y(line.bbox) for line in band)
            for band in run
        ]
        row_step = statistics.median(
            right - left
            for left, right in itertools.pairwise(row_centers)
        )
        for row_index, band in enumerate(run):
            ordered = tuple(sorted(band, key=lambda line: line.bbox[0]))
            textual = tuple(
                line for line in ordered if not _numeric_like(line.text)
            )
            numeric_left = min(
                line.bbox[0]
                for line in ordered
                if _numeric_like(line.text)
            )
            next_top = (
                min(line.bbox[1] for line in run[row_index + 1]) - 0.5
                if row_index + 1 < len(run)
                else max(line.bbox[3] for line in ordered)
                + max(0.5, row_step - max(
                    line.bbox[3] - line.bbox[1] for line in ordered
                ) - 0.5)
            )
            for column_index, line in enumerate(textual):
                right_obstacle = (
                    textual[column_index + 1].bbox[0]
                    if column_index + 1 < len(textual)
                    else numeric_left
                )
                gutter = max(0.5, line.font_size * 0.15)
                allowed = (
                    line.bbox[0],
                    line.bbox[1],
                    max(line.bbox[2] + 0.5, right_obstacle - gutter),
                    max(line.bbox[3] + 0.5, next_top),
                )
                result[_line_key(line)] = _LocalTableCell(
                    _round_rect(allowed),
                    "LEFT",
                    "TABLE_CELL",
                )
    return result


def _stable_right_value_band(
    previous: tuple[_Line, ...],
    candidate: tuple[_Line, ...],
) -> bool:
    previous_numeric = max(
        (line for line in previous if _numeric_like(line.text)),
        key=lambda line: line.bbox[2],
    )
    candidate_numeric = max(
        (line for line in candidate if _numeric_like(line.text)),
        key=lambda line: line.bbox[2],
    )
    font_size = statistics.median(
        line.font_size for line in (*previous, *candidate)
    )
    vertical_gap = (
        statistics.median(_center_y(line.bbox) for line in candidate)
        - statistics.median(_center_y(line.bbox) for line in previous)
    )
    return (
        font_size * 0.15 < vertical_gap <= font_size * 3.0
        and abs(previous_numeric.bbox[2] - candidate_numeric.bbox[2])
        <= font_size * 2.0
    )


def _textual_table_cells(
    lines: list[_Line],
    page_width: float,
    page_height: float,
) -> dict[tuple[str, ...], _LocalTableCell]:
    by_block: dict[int, list[_Line]] = {}
    for line in lines:
        block_indices = {item.block_index for item in line.objects}
        if len(block_indices) == 1:
            by_block.setdefault(next(iter(block_indices)), []).append(line)

    candidates: list[dict[str, object]] = []
    for block_index, block_lines in by_block.items():
        ordered = sorted(block_lines, key=lambda line: (line.bbox[0], line.bbox[1]))
        if len(ordered) < 2 or any(
            not re.search(r"[A-Za-z\u3400-\u9fff]", line.text)
            for line in ordered
        ):
            continue
        split_gap, split_index = max(
            (
                (ordered[index + 1].bbox[0] - ordered[index].bbox[2], index)
                for index in range(len(ordered) - 1)
            ),
            default=(-1.0, -1),
        )
        if split_gap < page_width * 0.08:
            continue
        left = tuple(ordered[: split_index + 1])
        right = tuple(ordered[split_index + 1 :])
        font_size = statistics.median(line.font_size for line in ordered)
        if (
            not left
            or not right
            or max(line.bbox[0] for line in left) - min(line.bbox[0] for line in left) > font_size * 1.5
            or max(line.bbox[0] for line in right) - min(line.bbox[0] for line in right) > font_size * 1.5
        ):
            continue
        top = min(line.bbox[1] for line in ordered)
        bottom = max(line.bbox[3] for line in ordered)
        if top < page_height * 0.10 or bottom > page_height * 0.92:
            continue
        candidates.append(
            {
                "block_index": block_index,
                "left": left,
                "right": right,
                "left_start": statistics.median(line.bbox[0] for line in left),
                "right_start": statistics.median(line.bbox[0] for line in right),
                "font_size": font_size,
                "top": top,
                "bottom": bottom,
                "header": all(_font_style(line.font_name)[0] for line in ordered),
            }
        )

    runs: list[list[dict[str, object]]] = []
    for row in sorted(candidates, key=lambda item: (float(item["top"]), float(item["left_start"]))):
        if runs and _stable_textual_table_row(runs[-1][-1], row):
            runs[-1].append(row)
        else:
            runs.append([row])

    result: dict[tuple[str, ...], _LocalTableCell] = {}
    for run in runs:
        body_rows = [row for row in run if not bool(row["header"])]
        if len(body_rows) < 3:
            continue
        left_start = statistics.median(float(row["left_start"]) for row in run)
        right_start = statistics.median(float(row["right_start"]) for row in run)
        left_right = max(
            line.bbox[2]
            for row in run
            for line in row["left"]
        )
        right_left = min(
            line.bbox[0]
            for row in run
            for line in row["right"]
        )
        column_boundary = (left_right + right_left) / 2.0
        right_boundary = min(
            page_width * 0.955,
            right_start + max(right_start - left_start, page_width * 0.20),
        )
        for index, row in enumerate(run):
            font_size = float(row["font_size"])
            row_top = float(row["top"])
            row_bottom = (
                float(run[index + 1]["top"]) - font_size * 0.05
                if index + 1 < len(run)
                else float(row["bottom"]) + font_size * 0.50
            )
            role = "TABLE_HEADER" if bool(row["header"]) else "TABLE_CELL"
            left_lines = row["left"]
            right_lines = row["right"]
            left_bbox = _round_rect(
                (
                    min(line.bbox[0] for line in left_lines),
                    row_top,
                    column_boundary - font_size * 0.05,
                    row_bottom,
                )
            )
            right_bbox = _round_rect(
                (
                    min(line.bbox[0] for line in right_lines),
                    row_top,
                    right_boundary,
                    row_bottom,
                )
            )
            for line in left_lines:
                result[_line_key(line)] = _LocalTableCell(left_bbox, "LEFT", role)
            for line in right_lines:
                result[_line_key(line)] = _LocalTableCell(right_bbox, "LEFT", role)
    return result


def _stable_textual_table_row(left: dict[str, object], right: dict[str, object]) -> bool:
    font_size = statistics.median((float(left["font_size"]), float(right["font_size"])))
    vertical_gap = float(right["top"]) - float(left["bottom"])
    return (
        0.0 <= vertical_gap <= font_size * 3.0
        and abs(float(left["left_start"]) - float(right["left_start"])) <= font_size * 1.5
        and abs(float(left["right_start"]) - float(right["right_start"])) <= font_size * 1.5
    )


def _table_line_groups(
    lines: list[_Line],
    cells: dict[tuple[str, ...], _LocalTableCell],
) -> list[list[_Line]]:
    groups: list[list[_Line]] = []
    for line in (item for item in lines if _line_key(item) in cells):
        cell = cells[_line_key(line)]
        target = next(
            (
                group
                for group in reversed(groups)
                if (
                    cell.role == "TABLE_CELL"
                    and cell.allowed_bbox == cells[_line_key(group[-1])].allowed_bbox
                )
                or (
                    cell.role == "TABLE_HEADER"
                    and cells[_line_key(group[-1])].role == "TABLE_HEADER"
                    and _axis_overlap(
                        (cell.allowed_bbox[0], cell.allowed_bbox[2]),
                        (cells[_line_key(group[-1])].allowed_bbox[0], cells[_line_key(group[-1])].allowed_bbox[2]),
                    )
                    >= min(
                        cell.allowed_bbox[2] - cell.allowed_bbox[0],
                        cells[_line_key(group[-1])].allowed_bbox[2] - cells[_line_key(group[-1])].allowed_bbox[0],
                    )
                    * 0.80
                    and 0.0 <= line.bbox[1] - group[-1].bbox[3] <= max(line.font_size, group[-1].font_size) * 0.40
                )
            ),
            None,
        )
        if target is None:
            groups.append([line])
        else:
            target.append(line)
    return groups


def _table_group_cell(
    group: list[_Line],
    cells: dict[tuple[str, ...], _LocalTableCell],
) -> _LocalTableCell | None:
    group_cells = [cells.get(_line_key(line)) for line in group]
    if not group_cells or any(cell is None for cell in group_cells):
        return None
    concrete = [cell for cell in group_cells if cell is not None]
    if len({cell.role for cell in concrete}) != 1 or len({cell.alignment for cell in concrete}) != 1:
        return None
    return _LocalTableCell(
        _round_rect(_union([cell.allowed_bbox for cell in concrete])),
        concrete[0].alignment,
        concrete[0].role,
    )


def _row_bands(lines) -> list[tuple[_Line, ...]]:
    bands: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (_center_y(item.bbox), item.bbox[0])):
        center = _center_y(line.bbox)
        target = next(
            (
                band
                for band in reversed(bands)
                if abs(center - statistics.median(_center_y(item.bbox) for item in band))
                <= line.font_size * 0.30
            ),
            None,
        )
        if target is None:
            bands.append([line])
        else:
            target.append(line)
    return [tuple(sorted(band, key=lambda item: item.bbox[0])) for band in bands]


def _stable_table_band(previous: tuple[_Line, ...], candidate: tuple[_Line, ...]) -> bool:
    if len(previous) != len(candidate):
        return False
    previous = tuple(sorted(previous, key=lambda line: line.bbox[0]))
    candidate = tuple(sorted(candidate, key=lambda line: line.bbox[0]))
    font_size = statistics.median([line.font_size for line in (*previous, *candidate)])
    vertical_gap = statistics.median(_center_y(line.bbox) for line in candidate) - statistics.median(
        _center_y(line.bbox) for line in previous
    )
    return (
        font_size * 0.15 < vertical_gap <= font_size * 6.0
        and all(abs(left.bbox[0] - right.bbox[0]) <= font_size * 1.5 for left, right in zip(previous, candidate))
    )


def _continues_non_table_flow(
    line: _Line,
    lines: list[_Line],
    table_header_keys: set[tuple[str, ...]],
) -> bool:
    """Reject a table-header claim that continues a paragraph from above."""

    current = line
    visited: set[tuple[str, ...]] = set()
    while True:
        current_key = _line_key(current)
        if current_key in visited:
            return False
        visited.add(current_key)
        predecessor = max(
            (
                candidate
                for candidate in lines
                if candidate.bbox[1] < current.bbox[1]
                and _stable_left_flow_step(candidate, current)
            ),
            key=lambda candidate: candidate.bbox[3],
            default=None,
        )
        if predecessor is None:
            return False
        if _line_key(predecessor) not in table_header_keys:
            return True
        current = predecessor


def _stable_left_flow_step(previous: _Line, candidate: _Line) -> bool:
    font_size = min(previous.font_size, candidate.font_size)
    size_ratio = abs(previous.font_size - candidate.font_size) / max(
        previous.font_size,
        candidate.font_size,
        0.1,
    )
    gap = candidate.bbox[1] - previous.bbox[3]
    return (
        previous.color_srgb == candidate.color_srgb
        and _font_style(previous.font_name) == _font_style(candidate.font_name)
        and size_ratio <= 0.15
        and abs(previous.bbox[0] - candidate.bbox[0]) <= max(4.0, font_size * 0.8)
        and -font_size <= gap <= max(3.0, font_size * 0.9)
        and _scripts_compatible(previous.text, candidate.text)
        and not _numeric_like(previous.text)
        and not _numeric_like(candidate.text)
    )


def _numeric_like(text: str) -> bool:
    value = text.strip()
    return bool(re.search(r"\d", value)) and not re.search(r"[A-Za-z\u3400-\u9fff]", value)


def _canonical_text_objects(objects: tuple[KernelTextFact, ...]):
    grouped: dict[tuple[object, ...], list[KernelTextFact]] = {}
    for item in objects:
        key = (item.text, tuple(round(value, 3) for value in item.bbox))
        grouped.setdefault(key, []).append(item)
    representatives = tuple(items[-1] for items in grouped.values())
    aliases = {items[-1].object_id: tuple(items) for items in grouped.values()}
    return representatives, aliases


def _same_baseline(left: KernelTextFact, right: KernelTextFact) -> bool:
    return abs(_center_y(left.bbox) - _center_y(right.bbox)) <= min(left.font_size, right.font_size) * 0.50


def _same_row(left: KernelTextFact, right: KernelTextFact) -> bool:
    gap = right.bbox[0] - left.bbox[2]
    if (
        _vertical_semantic_object(left) and _numeric_like(right.text)
    ) or (
        _vertical_semantic_object(right) and _numeric_like(left.text)
    ):
        return False
    font_size = max(left.font_size, right.font_size)
    independent_cells = (
        gap > font_size
        and min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0]) >= font_size * 3.0
    )
    if independent_cells:
        return False
    multiplication_tail = right.text.strip() in {"x", "×"} and gap <= max(left.font_size, right.font_size)
    style_bridge = (
        left.color_srgb == right.color_srgb
        and _font_style(left.font_name) == _font_style(right.font_name)
        and abs(left.font_size - right.font_size) / max(left.font_size, right.font_size, 0.1) <= 0.15
    )
    return (
        _same_baseline(left, right)
        and gap >= -max(left.font_size, right.font_size)
        and gap <= max(left.font_size, right.font_size) * 1.35
        and (
            _script(left.text) == _script(right.text)
            or _neutral(left.text)
            or _neutral(right.text)
            or multiplication_tail
            or style_bridge
        )
    )


def _container_groups(
    lines: list[_Line],
    visuals: tuple[_Visual, ...],
    page_area: float,
    *,
    all_lines: list[_Line] | None = None,
) -> list[list[_Line]]:
    context = all_lines if all_lines is not None else lines
    groups: list[list[_Line]] = []
    for line in lines:
        target = next(
            (
                group
                for group in reversed(groups)
                if _can_join(group[-1], line, visuals, page_area, all_lines=context)
            ),
            None,
        )
        if target is None:
            groups.append([line])
        else:
            target.append(line)
    return groups


def _can_join(
    previous: _Line,
    candidate: _Line,
    visuals: tuple[_Visual, ...],
    page_area: float,
    *,
    all_lines: list[_Line] | None = None,
) -> bool:
    previous_swatch = _legend_anchor(previous.bbox, previous.font_size, visuals, page_area)
    candidate_swatch = _legend_anchor(candidate.bbox, candidate.font_size, visuals, page_area)
    if previous_swatch is not None and candidate_swatch is not None and previous_swatch.object_id != candidate_swatch.object_id:
        return False
    context = all_lines if all_lines is not None else [previous, candidate]
    previous_row = _legend_data_anchor(previous, context, visuals, page_area)
    candidate_row = _legend_data_anchor(candidate, context, visuals, page_area)
    if (
        previous_row is not None
        and candidate_row is not None
        and _line_key(previous_row[0]) != _line_key(candidate_row[0])
    ):
        return False
    gap = candidate.bbox[1] - previous.bbox[3]
    size_ratio = abs(candidate.font_size - previous.font_size) / max(candidate.font_size, previous.font_size, 0.1)
    anchor_delta = min(
        abs(candidate.bbox[0] - previous.bbox[0]),
        abs(candidate.bbox[2] - previous.bbox[2]),
        abs(_center_x(candidate.bbox) - _center_x(previous.bbox)),
    )
    numeric_unit_pair = _numeric_unit_pair(previous, candidate)
    return (
        previous.color_srgb == candidate.color_srgb
        and (
            _scripts_compatible(previous.text, candidate.text)
            or _body_numeric_tail(previous, candidate)
            or numeric_unit_pair
        )
        and _font_style(previous.font_name) == _font_style(candidate.font_name)
        and size_ratio <= 0.15
        and anchor_delta <= max(4.0, min(previous.font_size, candidate.font_size) * 0.8)
        and -min(previous.font_size, candidate.font_size) <= gap <= max(3.0, min(previous.font_size, candidate.font_size) * 0.9)
    )


def _merge_overlapping_groups(groups: list[list[_Line]]) -> list[list[_Line]]:
    merged = [list(group) for group in groups]
    changed = True
    while changed:
        changed = False
        for left_index in range(len(merged)):
            left_bbox = _union([line.bbox for line in merged[left_index]])
            match = next(
                (
                    right_index
                    for right_index in range(left_index + 1, len(merged))
                    if _intersection_area(left_bbox, _union([line.bbox for line in merged[right_index]])) > 0.05
                ),
                None,
            )
            if match is None:
                continue
            merged[left_index].extend(merged.pop(match))
            merged[left_index].sort(key=lambda line: (line.bbox[1], line.bbox[0]))
            changed = True
            break
    return sorted(merged, key=lambda group: (_union([line.bbox for line in group])[1], _union([line.bbox for line in group])[0]))


def _disjoint_allowed_regions(containers: list[ChartTextContainer]) -> list[ChartTextContainer]:
    result = list(containers)
    for left_index in range(len(result)):
        for right_index in range(left_index + 1, len(result)):
            left = result[left_index]
            right = result[right_index]
            if _intersection_area(left.allowed_bbox, right.allowed_bbox) <= 0.05:
                continue
            left_bbox = left.source_bbox
            right_bbox = right.source_bbox
            horizontal = _horizontal_order(left_bbox, right_bbox)
            vertical = _vertical_order(left_bbox, right_bbox)
            if horizontal is None and vertical is None:
                raise ChartCapabilityError(
                    f"CHART_OVERLAPPING_TEXT_OWNERS:{left.container_id}:{right.container_id}"
                )
            horizontal_gap = (
                max(0.0, right_bbox[0] - left_bbox[2], left_bbox[0] - right_bbox[2])
                if horizontal is not None
                else -1.0
            )
            vertical_gap = (
                max(0.0, right_bbox[1] - left_bbox[3], left_bbox[1] - right_bbox[3])
                if vertical is not None
                else -1.0
            )
            centered_category_row = (
                vertical is not None
                and left.role == right.role == "AXIS_OR_CATEGORY_LABEL"
                and left.alignment != right.alignment
                and "CENTER" in {left.alignment, right.alignment}
            )
            if horizontal is not None and horizontal_gap >= vertical_gap and not centered_category_row:
                first_index, second_index = (
                    (left_index, right_index) if horizontal == (0, 1) else (right_index, left_index)
                )
                first, second = result[first_index], result[second_index]
                boundary = (first.source_bbox[2] + second.source_bbox[0]) / 2.0
                result[first_index] = replace(
                    first,
                    allowed_bbox=_round_rect((first.allowed_bbox[0], first.allowed_bbox[1], min(first.allowed_bbox[2], boundary), first.allowed_bbox[3])),
                )
                result[second_index] = replace(
                    second,
                    allowed_bbox=_round_rect((max(second.allowed_bbox[0], boundary), second.allowed_bbox[1], second.allowed_bbox[2], second.allowed_bbox[3])),
                )
            else:
                assert vertical is not None
                first_index, second_index = (
                    (left_index, right_index) if vertical == (0, 1) else (right_index, left_index)
                )
                first, second = result[first_index], result[second_index]
                boundary = (first.source_bbox[3] + second.source_bbox[1]) / 2.0
                result[first_index] = replace(
                    first,
                    allowed_bbox=_round_rect((first.allowed_bbox[0], first.allowed_bbox[1], first.allowed_bbox[2], min(first.allowed_bbox[3], boundary))),
                )
                result[second_index] = replace(
                    second,
                    allowed_bbox=_round_rect((second.allowed_bbox[0], max(second.allowed_bbox[1], boundary), second.allowed_bbox[2], second.allowed_bbox[3])),
                )
    unresolved = [
        (left.container_id, right.container_id)
        for index, left in enumerate(result)
        for right in result[index + 1 :]
        if _intersection_area(left.allowed_bbox, right.allowed_bbox) > 0.05
    ]
    if unresolved:
        raise ChartCapabilityError(f"CHART_TEXT_SLOT_PARTITION_FAILED:{unresolved[0][0]}:{unresolved[0][1]}")
    return result


def _restore_minimum_textbox_heights(
    containers: list[ChartTextContainer],
    regions: tuple[ChartVisualRegion, ...],
    page_height: float,
) -> list[ChartTextContainer]:
    """Recover a writable label height only from currently unclaimed whitespace."""

    eligible_roles = {"AXIS_OR_CATEGORY_LABEL", "LEGEND_LABEL"}
    result = list(containers)
    for index, container in enumerate(result):
        if container.rotation or container.role not in eligible_roles:
            continue
        current = container.allowed_bbox
        minimum_height = container.font_size * 1.40
        if current[3] - current[1] >= minimum_height:
            continue
        desired_bottom = min(page_height * 0.94, current[1] + minimum_height)
        candidate = (current[0], current[1], current[2], desired_bottom)
        if candidate[3] <= current[3]:
            continue
        if any(
            other_index != index
            and _intersection_area(candidate, other.allowed_bbox) > 0.05
            for other_index, other in enumerate(result)
        ):
            continue
        if any(
            _intersection_area(candidate, region.bbox)
            > _intersection_area(current, region.bbox) + 0.05
            for region in regions
        ):
            continue
        result[index] = replace(
            container,
            allowed_bbox=_round_rect(candidate),
        )
    return result


def _horizontal_order(left: Rect, right: Rect) -> tuple[int, int] | None:
    if left[2] <= right[0]:
        return (0, 1)
    if right[2] <= left[0]:
        return (1, 0)
    return None


def _vertical_order(left: Rect, right: Rect) -> tuple[int, int] | None:
    if left[3] <= right[1]:
        return (0, 1)
    if right[3] <= left[1]:
        return (1, 0)
    return None


def _protected(line: _Line, page_width: float, page_height: float) -> bool:
    text = line.text.strip()
    if not re.search(r"[A-Za-z\u3400-\u9fff]", text):
        return True
    if re.fullmatch(r"(?:https?://|www\.)\S+", text, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:FY|Y)?\d{4}[A-Z]?", text, flags=re.IGNORECASE):
        return True
    notation = text.strip("()[]")
    if re.fullmatch(r"[A-Z][A-Z0-9+./'’$%-]{1,15}", notation) and (
        notation != text or bool(re.search(r"[0-9+./'’$%-]", notation))
    ):
        return True
    numeric_suffix = re.fullmatch(r"[-+]?\d+(?:[.,]\d+)*(\s*)([A-Za-z%]+)", text)
    if numeric_suffix:
        whitespace, suffix = numeric_suffix.groups()
        if not whitespace or suffix == "%" or suffix.isupper() or len(suffix) <= 3:
            return True
    return False


def _semantic_numeric_continuations(lines: list[_Line]) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for line in lines:
        if _numeric_like(line.text) and any(
            _numeric_unit_pair(line, other)
            for other in lines
            if other is not line
        ):
            result.add(_line_key(line))
    for previous, current in zip(lines, lines[1:]):
        current_text = current.text.strip()
        if not re.fullmatch(r"[\d\s%.,。()（）+\-]+", current_text):
            continue
        if not re.search(r"[=×xX*/÷╱]", previous.text) and not _body_numeric_tail(previous, current):
            continue
        gap = current.bbox[1] - previous.bbox[3]
        size_ratio = abs(current.font_size - previous.font_size) / max(current.font_size, previous.font_size, 0.1)
        if (
            previous.color_srgb == current.color_srgb
            and _font_style(previous.font_name) == _font_style(current.font_name)
            and size_ratio <= 0.15
            and 0.0 <= gap <= max(previous.font_size, current.font_size) * 1.2
            and abs(current.bbox[0] - previous.bbox[0]) <= max(previous.font_size, current.font_size) * 1.5
        ):
            result.add(_line_key(current))
    return result


def _numeric_unit_pair(left: _Line, right: _Line) -> bool:
    if _numeric_like(left.text) == _numeric_like(right.text):
        return False
    numeric, unit = (left, right) if _numeric_like(left.text) else (right, left)
    semantic_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", unit.text))
    if not 1 <= semantic_count <= 12:
        return False
    numeric_blocks = {getattr(item, "block_index", None) for item in numeric.objects}
    unit_blocks = {getattr(item, "block_index", None) for item in unit.objects}
    if len(numeric_blocks) != 1 or numeric_blocks != unit_blocks or None in numeric_blocks:
        return False
    font_size = max(numeric.font_size, unit.font_size)
    if unit.bbox[1] < numeric.bbox[1]:
        return False
    vertical_gap = max(
        0.0,
        numeric.bbox[1] - unit.bbox[3],
        unit.bbox[1] - numeric.bbox[3],
    )
    return (
        abs(numeric.bbox[0] - unit.bbox[0]) <= font_size
        and vertical_gap <= font_size * 0.60
    )


def _vertical_semantic_object(item: KernelTextFact) -> bool:
    width = item.bbox[2] - item.bbox[0]
    height = item.bbox[3] - item.bbox[1]
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", item.text)) and height >= width * 2.2


def _body_numeric_tail(previous: _Line, current: _Line) -> bool:
    current_text = current.text.strip()
    if not re.fullmatch(r"[\d\s%.,。()（）+\-]+", current_text) or not re.search(r"\d", current_text):
        return False
    semantic_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", previous.text))
    if semantic_count < 20 or previous.text.rstrip().endswith(("。", "！", "？", ".", "!", "?", "；", ";")):
        return False
    gap = current.bbox[1] - previous.bbox[3]
    size_ratio = abs(current.font_size - previous.font_size) / max(current.font_size, previous.font_size, 0.1)
    return (
        previous.color_srgb == current.color_srgb
        and _font_style(previous.font_name) == _font_style(current.font_name)
        and size_ratio <= 0.15
        and 0.0 <= gap <= max(previous.font_size, current.font_size) * 1.2
        and abs(current.bbox[0] - previous.bbox[0]) <= max(previous.font_size, current.font_size) * 1.5
        and previous.bbox[2] - previous.bbox[0] >= max(current.font_size * 20.0, (current.bbox[2] - current.bbox[0]) * 3.0)
    )


def _legend_anchor(source: Rect, font_size: float, visuals: tuple[_Visual, ...], page_area: float) -> _Visual | None:
    candidates = []
    page_scale = page_area**0.5
    for item in visuals:
        width = item.bbox[2] - item.bbox[0]
        height = item.bbox[3] - item.bbox[1]
        if _area(item.bbox) <= page_area * 0.0000002 or _area(item.bbox) >= page_area * 0.01:
            continue
        swatch_limit = max(page_scale * 0.0255, font_size * 2.0)
        if width > swatch_limit or height > swatch_limit:
            continue
        vertical_overlap = _axis_overlap((source[1], source[3]), (item.bbox[1], item.bbox[3]))
        if vertical_overlap <= 0 and abs(_center_y(source) - _center_y(item.bbox)) > font_size:
            continue
        if _rect_gap(source, item.bbox) <= max(page_scale * 0.0113, font_size * 3.0):
            candidates.append(item)
    return min(candidates, key=lambda item: _rect_gap(source, item.bbox)) if candidates else None


def _legend_data_anchor(
    line: _Line,
    all_lines: list[_Line],
    visuals: tuple[_Visual, ...],
    page_area: float,
) -> tuple[_Line, _Visual] | None:
    if not re.search(r"[A-Za-z\u3400-\u9fff]", line.text):
        return None
    candidates: list[tuple[_Line, _Visual]] = []
    for value in all_lines:
        if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)*%", value.text.strip()):
            continue
        swatch = _legend_anchor(value.bbox, value.font_size, visuals, page_area)
        if swatch is None or value.bbox[2] > line.bbox[0] + line.font_size:
            continue
        horizontal_gap = max(0.0, line.bbox[0] - value.bbox[2])
        vertical_delta = abs(_center_y(line.bbox) - _center_y(value.bbox))
        if horizontal_gap <= max(line.font_size * 4.0, value.font_size * 3.0) and vertical_delta <= max(
            line.font_size,
            value.font_size,
        ) * 0.90:
            candidates.append((value, swatch))
    return min(
        candidates,
        key=lambda item: (
            abs(_center_y(line.bbox) - _center_y(item[0].bbox)),
            max(0.0, line.bbox[0] - item[0].bbox[2]),
        ),
    ) if candidates else None


def _legend_data_row(
    group: list[_Line],
    all_lines: list[_Line],
    visuals: tuple[_Visual, ...],
    page_area: float,
) -> tuple[_Line, _Visual] | None:
    anchors = [_legend_data_anchor(line, all_lines, visuals, page_area) for line in group]
    if not anchors or any(item is None for item in anchors):
        return None
    resolved = [item for item in anchors if item is not None]
    if len({_line_key(item[0]) for item in resolved}) != 1:
        return None
    return resolved[0]


def _association(source: Rect, regions: tuple[ChartVisualRegion, ...]) -> ChartVisualRegion:
    return min(regions, key=lambda region: (_rect_gap(source, region.bbox), _area(region.bbox)))


def _internal_overlay_visual(
    source: Rect,
    visuals: tuple[_Visual, ...],
    association: ChartVisualRegion,
    role: str,
    font_size: float,
    page_area: float,
) -> _Visual | None:
    if role != "AXIS_OR_CATEGORY_LABEL":
        return None
    tolerance = max(0.5, font_size * 0.12)
    source_width = source[2] - source[0]
    source_height = source[3] - source[1]
    candidates: list[_Visual] = []
    for visual in visuals:
        width = visual.bbox[2] - visual.bbox[0]
        height = visual.bbox[3] - visual.bbox[1]
        if (
            _area(visual.bbox) <= 0.5
            or _area(visual.bbox) >= min(page_area * 0.12, _area(association.bbox) * 0.50)
            or min(width, height) < font_size * 1.25
            or source[0] < visual.bbox[0] - tolerance
            or source[1] < visual.bbox[1] - tolerance
            or source[2] > visual.bbox[2] + tolerance
            or source[3] > visual.bbox[3] + tolerance
            or (
                source_width < width * 0.08
                and source_height < height * 0.08
            )
        ):
            continue
        candidates.append(visual)
    return min(candidates, key=lambda item: _area(item.bbox), default=None)


def _role(text: str, bbox: Rect, font_size: float, median_font: float, swatch, association) -> str:
    if _note_label(text):
        return "ANNOTATION"
    if swatch is not None:
        return "LEGEND_LABEL"
    semantic_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", text))
    if semantic_count >= 80 or (
        semantic_count >= 40 and bbox[3] - bbox[1] >= font_size * 2.2
    ):
        return "ANNOTATION"
    if font_size >= median_font * 1.45 or (text.isupper() and font_size >= median_font * 1.15 and len(text) >= 5):
        return "TITLE"
    if len(text) <= 48 and _rect_gap(bbox, association.bbox) <= font_size * 4.0:
        return "AXIS_OR_CATEGORY_LABEL"
    return "ANNOTATION"


def _page_role(source: Rect, rotation: int, page_width: float, page_height: float) -> str | None:
    if source[1] <= page_height * 0.08:
        return "PAGE_HEADER"
    if source[3] >= page_height * 0.90:
        return "PAGE_FOOTER"
    if rotation and source[0] >= page_width * 0.90 and source[1] >= page_height * 0.70:
        return "PAGE_FOOTER"
    return None


def _rotation(source: Rect, text: str) -> int:
    width = source[2] - source[0]
    height = source[3] - source[1]
    semantic_count = len(re.findall(r"[A-Za-z\u3400-\u9fff]", text))
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    stacked_cjk = cjk_count >= 2 and height >= width * 1.8
    return 90 if stacked_cjk or (semantic_count >= 4 and height >= width * 2.2) else 0


def _alignment(
    group: list[_Line],
    source: Rect,
    association: ChartVisualRegion,
    visuals: tuple[_Visual, ...],
    page_area: float,
    page_width: float,
    swatch: _Visual | None,
    role: str,
) -> str:
    if role == "TABLE_CELL":
        return "LEFT"
    if role == "ANNOTATION" and _note_label(_joined_text(group)):
        return "LEFT"
    if role in {"PAGE_HEADER", "PAGE_FOOTER"}:
        if source[3] - source[1] >= max(page_width * 0.04, (source[2] - source[0]) * 2.2):
            return "LEFT"
        return "RIGHT" if _center_x(source) >= page_width / 2.0 else "LEFT"
    if swatch is not None:
        return "LEFT" if _center_x(swatch.bbox) < _center_x(source) else "RIGHT"
    edge_tolerance = statistics.median(line.font_size for line in group)
    association_width = association.bbox[2] - association.bbox[0]
    source_width = source[2] - source[0]
    relation = _anchor_relation(source, association.bbox)
    if (
        role == "TITLE"
        and relation in {"ABOVE", "BELOW", "OVERLAY"}
        and _shares_owner_center_axis(
            group,
            source,
            association.bbox,
            edge_tolerance,
        )
    ):
        return "CENTER"
    below_visual = any(
        _area(item.bbox) < page_area * 0.80
        and item.bbox[3] - item.bbox[1] >= (source[3] - source[1]) * 2.0
        and item.bbox[3] <= source[1] + edge_tolerance
        and source[1] - item.bbox[3] <= edge_tolerance * 2.0
        and _axis_overlap((source[0], source[2]), (item.bbox[0], item.bbox[2]))
        >= min(source_width, item.bbox[2] - item.bbox[0]) * 0.25
        for item in visuals
    )
    if (
        role == "AXIS_OR_CATEGORY_LABEL"
        and (relation == "BELOW" or below_visual)
        and association.bbox[0] <= _center_x(source) <= association.bbox[2]
        and source_width <= association_width * 0.25
    ):
        return "CENTER"
    adjacent_visuals = [
        item
        for item in visuals
        if _area(item.bbox) < page_area * 0.80
        and _axis_overlap((source[1], source[3]), (item.bbox[1], item.bbox[3]))
        >= min(source[3] - source[1], item.bbox[3] - item.bbox[1]) * 0.25
        and _rect_gap(source, item.bbox) <= edge_tolerance * 4.0
    ]
    if adjacent_visuals:
        nearest = min(adjacent_visuals, key=lambda item: (_rect_gap(source, item.bbox), _area(item.bbox)))
        if source[2] <= nearest.bbox[0] + edge_tolerance:
            return "RIGHT"
        if source[0] >= nearest.bbox[2] - edge_tolerance:
            return "LEFT"
    if len(group) >= 2:
        left_edges = [line.bbox[0] for line in group]
        if max(left_edges) - min(left_edges) <= statistics.median(line.font_size for line in group) * 0.8:
            return "LEFT"
    underlaid = any(
        _area(item.bbox) < page_area * 0.80
        and _intersection_area(source, item.bbox) > 0.01
        and _area(item.bbox) >= _area(association.bbox) * 0.80
        for item in visuals
    )
    near_horizontal_edge = (
        _center_x(source) <= association.bbox[0] + edge_tolerance
        or _center_x(source) >= association.bbox[2] - edge_tolerance
    )
    if (
        role == "AXIS_OR_CATEGORY_LABEL"
        and (not underlaid or near_horizontal_edge)
        and association_width >= source_width * 2.5
        and _intersection_area(source, association.bbox) > 0.01
    ):
        relative_x = (_center_x(source) - association.bbox[0]) / max(association_width, 0.1)
        association_height = association.bbox[3] - association.bbox[1]
        relative_y = (_center_y(source) - association.bbox[1]) / max(association_height, 0.1)
        if near_horizontal_edge or 0.22 <= relative_y <= 0.78:
            if relative_x <= 0.35:
                return "RIGHT"
            if relative_x >= 0.65:
                return "LEFT"
    vertically_related = _axis_overlap((source[1], source[3]), (association.bbox[1], association.bbox[3])) > 0.01
    close_to_association = (
        _rect_gap(source, association.bbox)
        <= statistics.median(line.font_size for line in group) * 4.0
    )
    if vertically_related or close_to_association:
        if relation == "LEFT_OF" or source[2] <= association.bbox[0] + edge_tolerance:
            return "RIGHT"
        if relation == "RIGHT_OF" or source[0] >= association.bbox[2] - edge_tolerance:
            return "LEFT"
    if (
        relation == "OVERLAY"
        and association_width <= max(page_width * 0.12, source_width * 2.2)
        and source[0] >= association.bbox[0] - edge_tolerance
        and source[2] <= association.bbox[2] + edge_tolerance
    ):
        return "CENTER"
    if role in {"TITLE", "AXIS_OR_CATEGORY_LABEL", "ANNOTATION"}:
        return "LEFT"
    if abs(_center_x(source) - page_width / 2.0) <= page_width * 0.08:
        return "CENTER"
    return "RIGHT" if source[0] >= page_width * 0.55 else "LEFT"


def _shares_owner_center_axis(
    group: list[_Line],
    source: Rect,
    owner: Rect,
    edge_tolerance: float,
) -> bool:
    owner_width = owner[2] - owner[0]
    source_width = source[2] - source[0]
    if source_width > owner_width * 1.35:
        return False
    left_gap = source[0] - owner[0]
    right_gap = owner[2] - source[2]
    axis_tolerance = max(edge_tolerance * 1.5, owner_width * 0.08)
    if abs(left_gap - right_gap) > axis_tolerance * 2.0:
        return False
    source_center = _center_x(source)
    line_tolerance = max(edge_tolerance, source_width * 0.08)
    return all(
        abs(_center_x(line.bbox) - source_center) <= line_tolerance
        for line in group
    )


def _note_label(text: str) -> bool:
    return bool(re.fullmatch(r"notes?[:：]?", text.strip(), re.IGNORECASE))


def _allowed_bbox(
    group: list[_Line],
    source: Rect,
    all_lines: list[_Line],
    regions: tuple[ChartVisualRegion, ...],
    visuals: tuple[_Visual, ...],
    association: ChartVisualRegion,
    swatch: _Visual | None,
    role: str,
    alignment: str,
    page_width: float,
    page_height: float,
) -> Rect:
    group_ids = {item.object_id for line in group for item in line.objects}
    others = [line for line in all_lines if not group_ids.intersection(item.object_id for item in line.objects)]
    page_left = max(12.0, page_width * 0.025)
    page_right = min(page_width - 12.0, page_width * 0.955)
    visual_gutter = 0.5
    left = source[0]
    right = page_right
    if alignment == "RIGHT":
        left = page_left
        right = source[2]
    elif alignment == "CENTER":
        local_visuals = [
            item
            for item in visuals
            if _area(item.bbox) < page_width * page_height * 0.80
            and _intersection_area(source, item.bbox) > 0.01
        ]
        local_visual = min(local_visuals, key=lambda item: _area(item.bbox)) if local_visuals else None
        if local_visual is not None:
            left = max(page_left, local_visual.bbox[0] + visual_gutter)
            right = min(page_right, local_visual.bbox[2] - visual_gutter)
        else:
            left = max(page_left, association.bbox[0] + 1.5)
            right = min(page_right, association.bbox[2] - 1.5)
    label_lane = role == "AXIS_OR_CATEGORY_LABEL" or (
        role == "ANNOTATION" and _intersection_area(source, association.bbox) > 0.01
    )
    adjacent_row_top: float | None = None
    blocked_left = False
    blocked_right = False
    has_local_overlay = any(
        _intersection_area(source, item.bbox) > 0.01
        and _area(item.bbox) < _area(association.bbox) * 0.80
        for item in visuals
    )
    for other in others:
        overlap = _axis_overlap((source[1], source[3]), (other.bbox[1], other.bbox[3]))
        if overlap < min(source[3] - source[1], other.bbox[3] - other.bbox[1]) * 0.25:
            continue
        if alignment == "RIGHT" and other.bbox[2] <= source[0]:
            left = max(left, (other.bbox[2] + source[0]) / 2.0)
        elif alignment != "RIGHT" and other.bbox[0] >= source[2]:
            right = min(right, (source[2] + other.bbox[0]) / 2.0)

    if swatch is not None and _center_x(swatch.bbox) >= _center_x(source):
        right = source[2]
    if alignment == "LEFT":
        for region in regions:
            if _intersection_area(source, region.bbox) > 0.01 or region.bbox[0] < source[2]:
                continue
            vertical = _axis_overlap((source[1], source[3]), (region.bbox[1], region.bbox[3]))
            if vertical >= min(source[3] - source[1], region.bbox[3] - region.bbox[1]) * 0.25:
                right = min(right, region.bbox[0] - 2.0)
    if label_lane:
        row_gap_limit = page_width * 0.15
        for visual in visuals:
            if (
                visual is swatch
                or _area(visual.bbox) <= 0.5
                or _area(visual.bbox) >= page_width * page_height * 0.80
                or _intersection_area(source, visual.bbox) > 0.01
            ):
                continue
            vertical = _axis_overlap((source[1], source[3]), (visual.bbox[1], visual.bbox[3]))
            if vertical < min(source[3] - source[1], visual.bbox[3] - visual.bbox[1]) * 0.25:
                continue
            if visual.bbox[0] >= source[2]:
                gap = visual.bbox[0] - source[2]
                if alignment == "LEFT":
                    right = min(right, visual.bbox[0] - visual_gutter)
            elif visual.bbox[2] <= source[0]:
                gap = source[0] - visual.bbox[2]
                if alignment == "RIGHT":
                    left = max(left, visual.bbox[2] + visual_gutter)
            else:
                continue
            if gap <= row_gap_limit:
                blocked_right = blocked_right or (
                    alignment == "LEFT"
                    and not has_local_overlay
                    and visual.bbox[0] >= source[2]
                )
                blocked_left = blocked_left or (
                    alignment == "RIGHT"
                    and not has_local_overlay
                    and visual.bbox[2] <= source[0]
                )
                candidate_top = visual.bbox[1] + 1.0
                adjacent_row_top = (
                    candidate_top
                    if adjacent_row_top is None
                    else min(adjacent_row_top, candidate_top)
                )
        if blocked_right:
            left = page_left
            for other in others:
                vertical = _axis_overlap((source[1], source[3]), (other.bbox[1], other.bbox[3]))
                if vertical <= 0.01 or other.bbox[2] > source[0]:
                    continue
                left = max(left, (other.bbox[2] + source[0]) / 2.0)
            for visual in visuals:
                vertical = _axis_overlap((source[1], source[3]), (visual.bbox[1], visual.bbox[3]))
                if vertical <= 0.01 or visual.bbox[2] > source[0]:
                    continue
                left = max(left, visual.bbox[2] + visual_gutter)
        if blocked_left:
            right = page_right
            for other in others:
                vertical = _axis_overlap((source[1], source[3]), (other.bbox[1], other.bbox[3]))
                if vertical <= 0.01 or other.bbox[0] < source[2]:
                    continue
                right = min(right, (source[2] + other.bbox[0]) / 2.0)
            for visual in visuals:
                vertical = _axis_overlap((source[1], source[3]), (visual.bbox[1], visual.bbox[3]))
                if vertical <= 0.01 or visual.bbox[0] < source[2]:
                    continue
                right = min(right, visual.bbox[0] - visual_gutter)
    right = max(right, source[2] + (0.5 if alignment != "RIGHT" else 0.0))
    left = min(left, source[0] - (0.5 if alignment == "RIGHT" else 0.0))

    top = source[1]
    if label_lane:
        top_limit = page_height * 0.03
        lane_width = max(0.1, right - left)
        for other in others:
            if other.bbox[1] >= source[1]:
                continue
            horizontal = _axis_overlap((left, right), (other.bbox[0], other.bbox[2]))
            if horizontal >= min(lane_width, other.bbox[2] - other.bbox[0]) * 0.15:
                top_limit = max(top_limit, other.bbox[3] + 0.5)
        for region in regions:
            if region.bbox[3] > source[1] or _intersection_area(source, region.bbox) > 0.01:
                continue
            horizontal = _axis_overlap((left, right), (region.bbox[0], region.bbox[2]))
            if horizontal >= min(lane_width, region.bbox[2] - region.bbox[0]) * 0.15:
                top_limit = max(top_limit, region.bbox[3] + 2.0)
        for visual in visuals:
            if (
                visual is swatch
                or _area(visual.bbox) >= page_width * page_height * 0.80
                or visual.bbox[3] > source[1] + 0.5
                or _intersection_area(source, visual.bbox) > 0.01
            ):
                continue
            horizontal = _axis_overlap((left, right), (visual.bbox[0], visual.bbox[2]))
            if horizontal >= min(lane_width, visual.bbox[2] - visual.bbox[0]) * 0.15:
                top_limit = max(top_limit, visual.bbox[3] + 0.5)
        top = max(top_limit, adjacent_row_top if adjacent_row_top is not None else top_limit)

    bottom_limit = page_height * 0.94
    if alignment == "CENTER" and _intersection_area(source, association.bbox) > 0.01:
        bottom_limit = min(bottom_limit, association.bbox[3] - 1.0)
    lane_width = max(0.1, right - left)
    for other in others:
        if other.bbox[1] < source[3] - 0.5:
            continue
        horizontal = _axis_overlap((left, right), (other.bbox[0], other.bbox[2]))
        if horizontal >= min(lane_width, other.bbox[2] - other.bbox[0]) * 0.15:
            bottom_limit = min(bottom_limit, other.bbox[1] - 0.5)
    for region in regions:
        if region.bbox[1] < source[3] or _intersection_area(source, region.bbox) > 0.01:
            continue
        horizontal = _axis_overlap((left, right), (region.bbox[0], region.bbox[2]))
        if horizontal >= min(lane_width, region.bbox[2] - region.bbox[0]) * 0.15:
            bottom_limit = min(bottom_limit, region.bbox[1] - 2.0)
    for visual in visuals:
        if visual is swatch or _area(visual.bbox) >= page_width * page_height * 0.80:
            continue
        if visual.bbox[1] < source[3] - 0.5 or _intersection_area(source, visual.bbox) > 0.01:
            continue
        horizontal = _axis_overlap((left, right), (visual.bbox[0], visual.bbox[2]))
        if horizontal >= min(lane_width, visual.bbox[2] - visual.bbox[0]) * 0.15:
            bottom_limit = min(bottom_limit, visual.bbox[1] - 0.5)
    # The extracted glyph bbox is an anchor, not a translated-text cage.  The
    # safe lane can use all whitespace up to the next text/visual obstacle.
    bottom = max(source[3], bottom_limit)
    if label_lane and bottom - top < statistics.median(line.font_size for line in group) * 1.05:
        top = source[1]
    return (left, top if label_lane else source[1], right, bottom)


def _required_literals(text: str) -> tuple[str, ...]:
    literals = re.findall(
        r"(?:https?://\S+|www\.\S+|"
        r"\b(?![A-Z]{1,4}\d{1,3},\d{3}(?:\D|$))"
        r"[A-Z]{1,4}\d+[A-Z0-9.-]*\b|"
        r"(?<![\d'\u2018\u2019])(?:"
        r"\d+(?:,\d{3})+(?:\.\d+)?%?|"
        r"\d+(?:[.:/-]\d+)+%?|"
        r"\d+(?:\.\d+)?%?))",
        text,
    )
    normalized: list[str] = []
    for literal in literals:
        currency_amount = re.fullmatch(
            r"[A-Z]{1,4}(\d+(?:,\d{3})*(?:\.\d+)?)",
            literal,
        )
        if currency_amount is not None and any(
            marker in currency_amount.group(1) for marker in (",", ".")
        ):
            literal = currency_amount.group(1)
        normalized.append(literal)
    return tuple(dict.fromkeys(normalized))


def _joined_text(lines: list[_Line]) -> str:
    result = ""
    for line in lines:
        text = line.text.strip()
        if not result:
            result = text
        elif _contains_cjk(result) and _contains_cjk(text):
            result += text
        else:
            result += " " + text
    return result


def _join_fragments(objects: tuple[KernelTextFact, ...]) -> str:
    result = ""
    for item in objects:
        text = item.text.strip()
        if not result:
            result = text
        elif _contains_cjk(result) and _contains_cjk(text):
            result += text
        else:
            result += " " + text
    return result


def _anchor_relation(source: Rect, anchor: Rect) -> str:
    if _intersection_area(source, anchor) > 0.01:
        return "OVERLAY"
    if source[2] <= anchor[0]:
        return "LEFT_OF"
    if source[0] >= anchor[2]:
        return "RIGHT_OF"
    return "ABOVE" if source[3] <= anchor[1] else "BELOW"


def _script(text: str) -> str:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_cjk == has_latin:
        return "MIXED"
    return "CJK" if has_cjk else "LATIN"


def _scripts_compatible(left: str, right: str) -> bool:
    left_script = _script(left)
    right_script = _script(right)
    if left_script == right_script:
        return True
    return (
        left_script == "MIXED"
        and bool(re.search(r"[\u3400-\u9fff]", left)) == bool(re.search(r"[\u3400-\u9fff]", right))
    ) or (
        right_script == "MIXED"
        and bool(re.search(r"[\u3400-\u9fff]", left)) == bool(re.search(r"[\u3400-\u9fff]", right))
    )


def _neutral(text: str) -> bool:
    return not re.search(r"[A-Za-z\u3400-\u9fff]", text)


def _font_style(name: str) -> tuple[bool, bool]:
    value = name.casefold()
    tokens = {token for token in re.split(r"[^a-z0-9]+", value) if token}
    bold = any(
        token in value for token in ("bold", "black", "heavy", "semibold")
    ) or bool(tokens & {"bd", "demi", "sb"})
    italic = any(token in value for token in ("italic", "oblique")) or bool(
        tokens & {"it", "ital", "obl"}
    )
    return bold, italic


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]


def _union(rects: list[Rect]) -> Rect:
    return (min(item[0] for item in rects), min(item[1] for item in rects), max(item[2] for item in rects), max(item[3] for item in rects))


def _center_x(rect: Rect) -> float:
    return (rect[0] + rect[2]) / 2.0


def _center_y(rect: Rect) -> float:
    return (rect[1] + rect[3]) / 2.0


def _axis_overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _intersection_area(left: Rect, right: Rect) -> float:
    return _axis_overlap((left[0], left[2]), (right[0], right[2])) * _axis_overlap((left[1], left[3]), (right[1], right[3]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rect_gap(left: Rect, right: Rect) -> float:
    dx = max(0.0, left[0] - right[2], right[0] - left[2])
    dy = max(0.0, left[1] - right[3], right[1] - left[3])
    return (dx * dx + dy * dy) ** 0.5
