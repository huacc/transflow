from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from statistics import median

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.table.tools.models import TableCell, TableStructure, TableTemplate
from toolboxes.body.table.tools.template_builder import is_protected_text, protected_tokens


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class VectorTableDetection:
    template: TableTemplate
    regions: tuple[Rect, ...]
    grid_cell_count: int


@dataclass(frozen=True)
class _VectorRegion:
    bbox: Rect
    x_boundaries: tuple[float, ...]
    y_boundaries: tuple[float, ...]
    grid_cell_count: int
    cells: tuple[Rect, ...] = ()


@dataclass
class _TextGroup:
    region_index: int
    column_start: int
    column_end: int
    objects: list[TextObjectFact]
    structural_cell_index: int | None = None

    @property
    def bbox(self) -> Rect:
        return _union(item.bbox for item in self.objects)

    @property
    def text(self) -> str:
        lines: dict[tuple[int, int], list[TextObjectFact]] = {}
        for item in self.objects:
            lines.setdefault((item.block_index, item.line_index), []).append(item)
        return "\n".join(
            "".join(item.text for item in sorted(items, key=lambda value: (value.bbox[0], value.span_index))).strip()
            for _, items in sorted(
                lines.items(),
                key=lambda row: (
                    min(item.bbox[1] for item in row[1]),
                    min(item.bbox[0] for item in row[1]),
                ),
            )
        ).strip()


def build_vector_table_detection(source_pdf: Path, facts: PageFacts) -> VectorTableDetection | None:
    with fitz.open(source_pdf) as document:
        found = document[facts.page_index].find_tables().tables
        raw_regions = [_region_from_table(table, facts) for table in found]
    regions = tuple(
        _expand_region_to_owned_text(region, facts.text_objects)
        for region in raw_regions
        if region is not None
    )
    if not regions:
        return None

    groups = _text_groups(regions, facts.text_objects)
    if not groups:
        return None
    cells = _table_cells(regions, groups)
    if len([cell for cell in cells if cell.translatable]) < 2:
        return None

    x_boundaries = _merge_coordinates(
        value for region in regions for value in region.x_boundaries
    )
    y_boundaries = _merge_coordinates(
        value for region in regions for value in region.y_boundaries
    )
    bbox = _round_rect(
        (
            min(region.bbox[0] for region in regions),
            min(region.bbox[1] for region in regions),
            max(region.bbox[2] for region in regions),
            max(region.bbox[3] for region in regions),
        )
    )
    structure_payload = {
        "bbox": bbox,
        "column_boundaries": x_boundaries,
        "row_boundaries": y_boundaries,
        "locked_objects_sha256": facts.locked_objects_sha256,
        "direct_evidence": "vector_grid_cells",
    }
    structure = TableStructure(
        "table-00",
        bbox,
        x_boundaries,
        y_boundaries,
        ("semantic_text_anchors", "vector_grid_cells"),
        canonical_sha256(structure_payload),
    )
    ordered = tuple(
        TableCell(
            cell.container_id,
            cell.table_id,
            cell.row_index,
            cell.column_index,
            cell.row_span,
            cell.column_span,
            cell.source_object_ids,
            cell.source_text,
            cell.source_bbox,
            cell.cell_bbox,
            index,
            cell.role,
            cell.translatable,
            cell.protected_tokens,
            cell.font_size,
            cell.color_srgb,
            cell.font_weight,
            cell.alignment,
        )
        for index, cell in enumerate(sorted(cells, key=lambda value: (value.source_bbox[1], value.source_bbox[0])))
    )
    template = TableTemplate(
        facts.page_id,
        "body.table",
        facts.width,
        facts.height,
        structure,
        ordered,
        tuple(
            object_id
            for cell in ordered
            if not cell.translatable
            for object_id in cell.source_object_ids
        ),
    )
    return VectorTableDetection(
        template,
        tuple(region.bbox for region in regions),
        sum(region.grid_cell_count for region in regions),
    )


def prefer_vector_detection(table: TableTemplate, detection: VectorTableDetection) -> bool:
    if detection.grid_cell_count < 20:
        return False
    p6_width = table.structure.bbox[2] - table.structure.bbox[0]
    if any(region[2] - region[0] < p6_width * 0.8 for region in detection.regions):
        return False
    vector_top = min(region[1] for region in detection.regions)
    vector_bottom = max(region[3] for region in detection.regions)
    font_scale = median(cell.font_size for cell in detection.template.cells)
    tolerance = max(12.0, font_scale * 2.0)
    if vector_top > table.structure.bbox[1] + tolerance:
        return False
    if vector_bottom < table.structure.bbox[3] - tolerance:
        return False
    return (
        vector_top < table.structure.bbox[1] - tolerance
        or vector_bottom > table.structure.bbox[3] + tolerance
    )


def _region_from_table(table, facts: PageFacts) -> _VectorRegion | None:
    bbox = tuple(float(value) for value in table.bbox)
    if table.col_count < 2 or bbox[2] - bbox[0] < facts.width * 0.35:
        return None
    cells = [
        tuple(float(value) for value in cell)
        for row in table.rows
        for cell in row.cells
        if cell is not None
    ]
    if len(cells) < 3:
        return None
    x_boundaries = _merge_coordinates(value for cell in cells for value in (cell[0], cell[2]))
    y_boundaries = _merge_coordinates(value for cell in cells for value in (cell[1], cell[3]))
    if len(x_boundaries) < 3 or len(y_boundaries) < 2:
        return None
    if table.row_count == 1:
        extended_bottom = _extended_body_bottom(bbox, x_boundaries, facts)
        if extended_bottom is None:
            return None
        bbox = (bbox[0], bbox[1], bbox[2], extended_bottom)
        y_boundaries = tuple(sorted({*y_boundaries, round(extended_bottom, 4)}))
    return _VectorRegion(
        _round_rect(bbox),
        x_boundaries,
        y_boundaries,
        len(cells),
        tuple(cells),
    )


def _expand_region_to_owned_text(
    region: _VectorRegion,
    objects: tuple[TextObjectFact, ...],
) -> _VectorRegion:
    owned = [
        item
        for item in objects
        if region.bbox[0] - 1.0 <= (item.bbox[0] + item.bbox[2]) / 2.0 <= region.bbox[2] + 1.0
        and region.bbox[1] - 1.0 <= (item.bbox[1] + item.bbox[3]) / 2.0 <= region.bbox[3] + 1.0
    ]
    if not owned:
        return region
    bbox = _round_rect(
        (
            min(region.bbox[0], min(item.bbox[0] for item in owned)),
            min(region.bbox[1], min(item.bbox[1] for item in owned)),
            max(region.bbox[2], max(item.bbox[2] for item in owned)),
            max(region.bbox[3], max(item.bbox[3] for item in owned)),
        )
    )
    x_boundaries = list(region.x_boundaries)
    x_boundaries[0] = min(x_boundaries[0], bbox[0])
    x_boundaries[-1] = max(x_boundaries[-1], bbox[2])
    y_boundaries = list(region.y_boundaries)
    y_boundaries[0] = min(y_boundaries[0], bbox[1])
    y_boundaries[-1] = max(y_boundaries[-1], bbox[3])
    return _VectorRegion(
        bbox,
        tuple(round(value, 4) for value in x_boundaries),
        tuple(round(value, 4) for value in y_boundaries),
        region.grid_cell_count,
        region.cells,
    )


def _extended_body_bottom(
    bbox: Rect,
    x_boundaries: tuple[float, ...],
    facts: PageFacts,
) -> float | None:
    candidates: list[TextObjectFact] = []
    for item in sorted(facts.text_objects, key=lambda value: (value.bbox[1], value.bbox[0])):
        if item.bbox[1] < bbox[3] - 0.5 or item.bbox[3] > facts.height * 0.92:
            continue
        column = _column_for_x(item.bbox[0] + 0.2, x_boundaries)
        if item.bbox[0] < bbox[0] - 2.0 or item.bbox[2] > x_boundaries[column + 1] + max(3.0, item.font_size * 0.7):
            continue
        candidates.append(item)
    if not candidates:
        return None

    accepted: list[TextObjectFact] = []
    current_bottom = bbox[3]
    typical_font = median(item.font_size for item in candidates)
    maximum_gap = max(24.0, typical_font * 3.0)
    for item in candidates:
        if item.bbox[1] - current_bottom > maximum_gap:
            break
        accepted.append(item)
        current_bottom = max(current_bottom, item.bbox[3])
    if len(accepted) < 2:
        return None
    return round(min(facts.height * 0.92, current_bottom + typical_font * 0.8), 4)


def _text_groups(
    regions: tuple[_VectorRegion, ...],
    objects: tuple[TextObjectFact, ...],
) -> list[_TextGroup]:
    buckets: dict[tuple[int, int, int | None], list[TextObjectFact]] = {}
    for item in objects:
        region_index = next(
            (
                index
                for index, region in enumerate(regions)
                if region.bbox[0] - 1.0 <= (item.bbox[0] + item.bbox[2]) / 2.0 <= region.bbox[2] + 1.0
                and region.bbox[1] - 1.0 <= (item.bbox[1] + item.bbox[3]) / 2.0 <= region.bbox[3] + 1.0
            ),
            None,
        )
        if region_index is None:
            continue
        structural_cell_index = _structural_cell_index(item, regions[region_index])
        buckets.setdefault((region_index, item.block_index, structural_cell_index), []).append(item)

    groups: list[_TextGroup] = []
    for (region_index, _, structural_cell_index), items in buckets.items():
        boundaries = regions[region_index].x_boundaries
        for sequence in _semantic_block_groups(items):
            bbox = _union(item.bbox for item in sequence)
            start, end = _column_span_for_bbox(
                bbox,
                boundaries,
                max(item.font_size for item in sequence),
            )
            groups.append(
                _TextGroup(
                    region_index,
                    start,
                    max(start, end),
                    sequence,
                    structural_cell_index,
                )
            )
    groups = _split_semantic_rows_with_numeric_peers(groups)
    return _merge_continuations(groups, regions)


def _split_semantic_rows_with_numeric_peers(groups: list[_TextGroup]) -> list[_TextGroup]:
    output: list[_TextGroup] = []
    for group in groups:
        lines = _source_lines(group.objects)
        if len(lines) < 2 or all(is_protected_text(item.text) for item in group.objects):
            output.append(group)
            continue
        protected_peers = [
            item
            for peer in groups
            if peer is not group
            and peer.region_index == group.region_index
            and (peer.column_end < group.column_start or peer.column_start > group.column_end)
            for item in peer.objects
            if _is_row_amount(item.text)
        ]
        matched_lines = 0
        for line in lines:
            line_center = (_union(item.bbox for item in line)[1] + _union(item.bbox for item in line)[3]) / 2.0
            line_font = max(item.font_size for item in line)
            if any(
                abs((item.bbox[1] + item.bbox[3]) / 2.0 - line_center)
                <= max(line_font, item.font_size) * 0.75
                for item in protected_peers
            ):
                matched_lines += 1
        if matched_lines < 2:
            output.append(group)
            continue
        output.extend(
            _TextGroup(
                group.region_index,
                group.column_start,
                group.column_end,
                line,
                group.structural_cell_index,
            )
            for line in lines
        )
    return output


def _is_row_amount(text: str) -> bool:
    value = text.strip()
    if not is_protected_text(value):
        return False
    return bool(
        re.search(r"[,()%]", value)
        or re.search(r"\d\.\d", value)
        or re.match(r"^[+\-–—−]", value)
        or re.search(r"(?i)(?:HK|US|RMB|CNY|HKD|USD|EUR|GBP|JPY|AUD|CAD|MOP|NT)\s*\$", value)
    )


def _source_lines(objects: list[TextObjectFact]) -> list[list[TextObjectFact]]:
    lines: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        lines.setdefault((item.block_index, item.line_index), []).append(item)
    return [
        sorted(items, key=lambda item: (item.bbox[0], item.span_index))
        for _, items in sorted(
            lines.items(),
            key=lambda row: (
                min(item.bbox[1] for item in row[1]),
                min(item.bbox[0] for item in row[1]),
            ),
        )
    ]


def _semantic_block_groups(items: list[TextObjectFact]) -> list[list[TextObjectFact]]:
    line_runs: list[list[TextObjectFact]] = []
    lines: dict[int, list[TextObjectFact]] = {}
    for item in items:
        lines.setdefault(item.line_index, []).append(item)
    for line_items in lines.values():
        runs: list[list[TextObjectFact]] = []
        for item in sorted(line_items, key=lambda value: (value.bbox[0], value.span_index)):
            if runs:
                previous_right = max(value.bbox[2] for value in runs[-1])
                font_scale = min(item.font_size, median(value.font_size for value in runs[-1]))
                if item.bbox[0] - previous_right <= max(2.0, font_scale):
                    runs[-1].append(item)
                    continue
            runs.append([item])
        line_runs.extend(runs)

    output: list[list[TextObjectFact]] = []
    for run in sorted(line_runs, key=lambda row: (_union(item.bbox for item in row)[1], _union(item.bbox for item in row)[0])):
        matches = [group for group in output if _line_run_continues(group, run)]
        if matches:
            max(matches, key=lambda group: _union(item.bbox for item in group)[3]).extend(run)
        else:
            output.append(list(run))
    return output


def _line_run_continues(
    group: list[TextObjectFact],
    run: list[TextObjectFact],
) -> bool:
    group_bbox = _union(item.bbox for item in group)
    run_bbox = _union(item.bbox for item in run)
    font_scale = max(
        median(item.font_size for item in group),
        median(item.font_size for item in run),
    )
    vertical_gap = run_bbox[1] - group_bbox[3]
    if vertical_gap < -font_scale or vertical_gap > max(4.0, font_scale * 1.2):
        return False
    overlap = min(group_bbox[2], run_bbox[2]) - max(group_bbox[0], run_bbox[0])
    overlap_ratio = overlap / max(1.0, min(group_bbox[2] - group_bbox[0], run_bbox[2] - run_bbox[0]))
    left_aligned = abs(group_bbox[0] - run_bbox[0]) <= max(3.0, font_scale * 1.5)
    return overlap_ratio >= 0.25 or left_aligned


def _merge_continuations(
    groups: list[_TextGroup],
    regions: tuple[_VectorRegion, ...],
) -> list[_TextGroup]:
    output: list[_TextGroup] = []
    for group in sorted(groups, key=lambda value: (value.region_index, value.column_start, value.bbox[1], value.bbox[0])):
        if output:
            previous = output[-1]
            same_column = (
                previous.region_index == group.region_index
                and previous.column_start == group.column_start
                and previous.column_end == group.column_end
                and previous.structural_cell_index == group.structural_cell_index
            )
            font_scale = max(
                max(item.font_size for item in previous.objects),
                max(item.font_size for item in group.objects),
            )
            column_left = regions[group.region_index].x_boundaries[group.column_start]
            is_indented = group.bbox[0] - column_left >= max(3.0, font_scale * 0.8)
            close = group.bbox[1] - previous.bbox[3] <= max(3.0, font_scale * 0.8)
            if same_column and close and is_indented and _starts_with_bullet(previous.text) and not _starts_with_bullet(group.text):
                previous.objects.extend(group.objects)
                continue
        output.append(group)
    return output


def _table_cells(
    regions: tuple[_VectorRegion, ...],
    groups: list[_TextGroup],
) -> list[TableCell]:
    global_x = _merge_coordinates(value for region in regions for value in region.x_boundaries)
    global_y = _merge_coordinates(value for region in regions for value in region.y_boundaries)
    cells: list[TableCell] = []
    ordered = sorted(groups, key=lambda value: (value.region_index, value.column_start, value.bbox[1]))
    for group_index, group in enumerate(sorted(groups, key=lambda value: (value.bbox[1], value.bbox[0]))):
        region = regions[group.region_index]
        peers = [
            item
            for item in ordered
            if item.region_index == group.region_index and item.column_start == group.column_start
        ]
        position = peers.index(group)
        if group.structural_cell_index is not None:
            structural_cell = region.cells[group.structural_cell_index]
            top = max(
                structural_cell[1],
                _boundary_between(
                    peers[position - 1].bbox[3] if position else structural_cell[1],
                    group.bbox[1],
                    region.y_boundaries,
                    structural_cell[1],
                ),
            )
            bottom = min(
                structural_cell[3],
                _boundary_between(
                    group.bbox[3],
                    peers[position + 1].bbox[1] if position + 1 < len(peers) else structural_cell[3],
                    region.y_boundaries,
                    structural_cell[3],
                ),
            )
            left = region.x_boundaries[group.column_start]
            right = region.x_boundaries[group.column_end + 1]
        elif len(region.y_boundaries) > 3:
            top, bottom = _enclosing_row_boundaries(group.bbox, region.y_boundaries)
            left = region.x_boundaries[group.column_start]
            right = region.x_boundaries[group.column_end + 1]
        else:
            top = _boundary_between(
                peers[position - 1].bbox[3] if position else region.bbox[1],
                group.bbox[1],
                region.y_boundaries,
                region.bbox[1],
            )
            bottom = _boundary_between(
                group.bbox[3],
                peers[position + 1].bbox[1] if position + 1 < len(peers) else region.bbox[3],
                region.y_boundaries,
                region.bbox[3],
            )
            left = region.x_boundaries[group.column_start]
            right = region.x_boundaries[group.column_end + 1]
        source_bbox = _round_rect(group.bbox)
        cell_bbox = _round_rect(
            (
                left,
                min(top, source_bbox[1]),
                right,
                max(bottom, source_bbox[3]),
            )
        )
        text = group.text
        visible = sorted(group.objects, key=lambda item: (item.font_size, len(item.text.strip())), reverse=True)
        representative = visible[0]
        translatable = not all(is_protected_text(item.text) for item in group.objects)
        row_index = _coordinate_index(cell_bbox[1], global_y)
        row_end = _coordinate_index(cell_bbox[3], global_y)
        column_index = _coordinate_index(cell_bbox[0], global_x)
        column_end = _coordinate_index(cell_bbox[2], global_x)
        first_row_bottom = region.y_boundaries[1]
        cells.append(
            TableCell(
                f"vector-table-{group_index:04d}",
                "table-00",
                row_index,
                column_index,
                max(1, row_end - row_index),
                max(1, column_end - column_index),
                tuple(item.object_id for item in group.objects),
                text,
                source_bbox,
                cell_bbox,
                group_index,
                "table_header" if source_bbox[1] < first_row_bottom + 0.5 else "table_body",
                translatable,
                protected_tokens(text) if translatable else (),
                round(max(item.font_size for item in group.objects), 4),
                representative.color_srgb,
                _font_weight(group.objects),
                _alignment(source_bbox, cell_bbox, group.column_start),
            )
        )
    return cells


def _boundary_between(
    left: float,
    right: float,
    boundaries: tuple[float, ...],
    fallback: float,
) -> float:
    if right <= left:
        return fallback
    midpoint = (left + right) / 2.0
    candidates = [value for value in boundaries if left - 0.2 <= value <= right + 0.2]
    return min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint


def _structural_cell_index(
    item: TextObjectFact,
    region: _VectorRegion,
) -> int | None:
    center_x = (item.bbox[0] + item.bbox[2]) / 2.0
    center_y = (item.bbox[1] + item.bbox[3]) / 2.0
    candidates = [
        (index, cell)
        for index, cell in enumerate(region.cells)
        if cell[0] - 0.5 <= center_x <= cell[2] + 0.5
        and cell[1] - 0.5 <= center_y <= cell[3] + 0.5
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (row[1][2] - row[1][0]) * (row[1][3] - row[1][1]),
    )[0]


def _enclosing_row_boundaries(
    bbox: Rect,
    boundaries: tuple[float, ...],
) -> tuple[float, float]:
    tolerance = 0.5
    top_candidates = [value for value in boundaries if value <= bbox[1] + tolerance]
    bottom_candidates = [value for value in boundaries if value >= bbox[3] - tolerance]
    top = max(top_candidates) if top_candidates else boundaries[0]
    bottom = min(bottom_candidates) if bottom_candidates else boundaries[-1]
    if bottom <= top:
        return boundaries[0], boundaries[-1]
    return top, bottom


def _column_span_for_bbox(
    bbox: Rect,
    boundaries: tuple[float, ...],
    font_size: float,
) -> tuple[int, int]:
    start = _column_for_x(bbox[0] + 0.2, boundaries)
    end = _column_for_x(bbox[2] - 0.2, boundaries)
    tolerance = max(0.6, min(2.0, font_size * 0.40))

    if start + 1 < len(boundaries) - 1:
        boundary = boundaries[start + 1]
        if 0.0 <= boundary - bbox[0] <= tolerance and bbox[2] > boundary + tolerance:
            start += 1
    if end > start:
        boundary = boundaries[end]
        if 0.0 <= bbox[2] - boundary <= tolerance and bbox[0] < boundary - tolerance:
            end -= 1
    return start, max(start, end)


def _column_for_x(value: float, boundaries: tuple[float, ...]) -> int:
    for index in range(len(boundaries) - 1):
        if value < boundaries[index + 1] or index == len(boundaries) - 2:
            return index
    return len(boundaries) - 2


def _coordinate_index(value: float, boundaries: tuple[float, ...]) -> int:
    return min(range(len(boundaries)), key=lambda index: abs(boundaries[index] - value))


def _merge_coordinates(values, tolerance: float = 1.0) -> tuple[float, ...]:
    output: list[float] = []
    for value in sorted(float(item) for item in values):
        if output and value - output[-1] <= tolerance:
            output[-1] = (output[-1] + value) / 2.0
        else:
            output.append(value)
    return tuple(round(value, 4) for value in output)


def _starts_with_bullet(text: str) -> bool:
    return text.lstrip().startswith(("\u2022", "\u25c6", "\u25c7", "\u25aa", "\u25e6", "\uf0b2", "-"))


def _font_weight(objects: list[TextObjectFact]) -> str:
    total = sum(max(1, len(item.text.strip())) for item in objects)
    bold = sum(
        max(1, len(item.text.strip()))
        for item in objects
        if "bold" in item.font_name.casefold()
    )
    return "bold" if bold * 2 >= total else "regular"


def _alignment(source_bbox: Rect, cell_bbox: Rect, column_index: int) -> str:
    if column_index == 0:
        return "left"
    source_center = (source_bbox[0] + source_bbox[2]) / 2.0
    cell_center = (cell_bbox[0] + cell_bbox[2]) / 2.0
    if abs(source_center - cell_center) <= (cell_bbox[2] - cell_bbox[0]) * 0.18:
        return "center"
    if cell_bbox[2] - source_bbox[2] <= 3.0:
        return "right"
    return "left"


def _union(rects) -> Rect:
    rows = list(rects)
    return (
        min(rect[0] for rect in rows),
        min(rect[1] for rect in rows),
        max(rect[2] for rect in rows),
        max(rect[3] for rect in rows),
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(value, 4) for value in rect)
