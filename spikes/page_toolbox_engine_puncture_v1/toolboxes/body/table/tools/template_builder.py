from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from statistics import median
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import Rect, TableCell, TableStructure, TableTemplate


class TableCapabilityError(RuntimeError):
    pass


_CURRENCY_LITERAL = r"(?<![A-Z])(?:HK\$|US\$|S\$|RMB|CNY|HKD|USD|EUR|GBP|JPY|AUD|CAD|MOP|NT\$)(?![A-Z])"
_CURRENCY = re.compile(rf"(?i){_CURRENCY_LITERAL}")
_IDENTIFIER_LITERAL = r"(?<![A-Za-z0-9])(?=[A-Za-z0-9.-]*[A-Za-z])(?=[A-Za-z0-9.-]*\d)[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)+(?![A-Za-z0-9])"
_FINANCIAL_AMOUNT = re.compile(
    r"(?ix)^(?:(?:HK|US|S|RMB|CNY|HKD|USD|EUR|GBP|JPY|AUD|CAD|MOP|NT)\s*\$?\s*)?"
    r"\(?[+\-\u2013\u2014\u2212]?\d[\d,]*(?:\.\d+)?%?\)?\s*"
    r"(?:cent(?:s)?|pence|penny|dollar(?:s)?|yuan|yen|euro(?:s)?)\.?$"
)
_PROTECTED_TOKEN = re.compile(
    rf"(?i){_CURRENCY_LITERAL}"
    rf"|{_IDENTIFIER_LITERAL}"
    r"|(?:\d{4}[-\u2013\u2014]\d{4})"
    r"|(?:(?<![A-Za-z0-9-])[+\-\u2013\u2014\u2212]?\d(?:[\d,]*\d)?(?:\.\d+)?%?(?![A-Za-z0-9-]))"
)
_LOCALIZABLE_ENUM_NUMBER = re.compile(
    r"(?i)\b(?:level|stage|phase|tier|grade|step)\s*(?P<number>\d{1,2})\b"
)
_END_PUNCTUATION = re.compile(r"[:;.!?。！？；：]\s*$")


@dataclass
class _Fragment:
    column_start: int
    column_end: int
    objects: list[TextObjectFact]

    @property
    def text(self) -> str:
        return "".join(item.text for item in self.objects).strip()

    @property
    def bbox(self) -> Rect:
        return _union(item.bbox for item in self.objects)

    @property
    def translatable(self) -> bool:
        return not all(is_protected_text(item.text) for item in self.objects)


@dataclass
class _Row:
    y0: float
    y1: float
    fragments: list[_Fragment]


def is_protected_text(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if _FINANCIAL_AMOUNT.fullmatch(value):
        return True
    without_currency = _CURRENCY.sub("", value)
    if any(character.isalpha() or "\u3400" <= character <= "\u9fff" for character in without_currency):
        return False
    allowed = re.sub(r"[\s\d,.'\u2019%()\[\]{}+\-\u2013\u2014\u2212/:*]", "", without_currency)
    return not allowed and (any(character.isdigit() for character in value) or bool(_CURRENCY.search(value)) or value in {"-", "–", "—"})


def protected_tokens(text: str) -> tuple[str, ...]:
    localizable_spans = {
        match.span("number")
        for match in _LOCALIZABLE_ENUM_NUMBER.finditer(text)
    }
    return tuple(
        dict.fromkeys(
            match.group(0)
            for match in _PROTECTED_TOKEN.finditer(text)
            if match.span() not in localizable_spans
        )
    )


def is_currency_literal(value: str) -> bool:
    return bool(_CURRENCY.fullmatch(value.strip()))


def build_table_template(source_pdf: Path, facts: PageFacts) -> TableTemplate:
    if not facts.text_objects:
        raise TableCapabilityError("table_page_has_no_native_text")
    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        numeric_clusters = _numeric_columns(facts)
        if not numeric_clusters:
            raise TableCapabilityError("TABLE_DIRECT_EVIDENCE_MISSING:repeated_numeric_columns")

        numeric_objects = [item for cluster in numeric_clusters for item in cluster]
        numeric_top = min(item.bbox[1] for item in numeric_objects)
        numeric_bottom = max(item.bbox[3] for item in numeric_objects)
        first_numeric_x0 = min(item.bbox[0] for item in numeric_clusters[0])
        relevant_text = [
            item
            for item in facts.text_objects
            if numeric_top - 60.0 <= item.bbox[1] <= numeric_bottom + 20.0
            and item.bbox[0] < numeric_clusters[-1][-1].bbox[2] + 20.0
        ]
        label_right_candidates = [
            item.bbox[2]
            for item in relevant_text
            if not is_protected_text(item.text) and item.bbox[2] < first_numeric_x0 - 1.0
        ]
        table_left = min((item.bbox[0] for item in relevant_text), default=0.0)
        table_right = max(item.bbox[2] for item in numeric_clusters[-1])
        raw_boundaries = [table_left]
        label_right = max(label_right_candidates, default=table_left)
        raw_boundaries.append((label_right + first_numeric_x0) / 2.0)
        for previous, current in zip(numeric_clusters, numeric_clusters[1:]):
            previous_right = median(item.bbox[2] for item in previous)
            current_left = min(item.bbox[0] for item in current)
            raw_boundaries.append((previous_right + current_left) / 2.0)
        raw_boundaries.append(table_right)

        drawing_x, drawing_y, horizontal_rules, drawing_modes = _drawing_evidence(
            page, numeric_top, numeric_bottom, table_left, table_right
        )
        if len(numeric_clusters) == 1 and len(horizontal_rules) < 2:
            raise TableCapabilityError("TABLE_DIRECT_EVIDENCE_MISSING:single_numeric_column_rules")
        boundaries = _drawing_column_boundaries(
            drawing_x,
            table_left,
            table_right,
            relevant_text,
        ) or _snap_boundaries(raw_boundaries, drawing_x)
        if any(right - left < 4.0 for left, right in zip(boundaries, boundaries[1:])):
            raise TableCapabilityError("TABLE_DIRECT_EVIDENCE_MISSING:degenerate_columns")
        table_top = min([numeric_top - 3.0] + [value for value in drawing_y if numeric_top - 80.0 <= value <= numeric_top + 8.0])
        table_bottom = max([numeric_bottom + 3.0] + [value for value in drawing_y if numeric_bottom - 8.0 <= value <= numeric_bottom + 40.0])
        lines = _page_lines(facts)
        merged_header_lines = [
            line
            for line in lines
            if table_top - 35.0 <= line.y1 < table_top
            and line.fragments[0].bbox[0] >= boundaries[1] - 5.0
            and line.fragments[0].bbox[2] <= boundaries[-1] + 5.0
        ]
        if merged_header_lines:
            table_top = min(table_top, min(line.y0 for line in merged_header_lines))
        table_bbox = _round_rect((boundaries[0], table_top, boundaries[-1], table_bottom))

        table_rows = _logical_rows(
            [line for line in lines if _inside_vertical(line, table_top, table_bottom) and _overlaps_x(line, boundaries[0], boundaries[-1])],
            boundaries,
            numeric_top,
        )
        if len(table_rows) < 2:
            raise TableCapabilityError("TABLE_DIRECT_EVIDENCE_MISSING:stable_rows")
        row_boundaries = _row_boundaries(table_rows, table_top, table_bottom, horizontal_rules)
        structure_payload = {
            "bbox": table_bbox,
            "column_boundaries": boundaries,
            "row_boundaries": row_boundaries,
            "locked_objects_sha256": facts.locked_objects_sha256,
        }
        numeric_evidence = (
            "repeated_numeric_right_edges"
            if len(numeric_clusters) > 1
            else "single_numeric_column_with_rules"
        )
        structure = TableStructure(
            table_id="table-00",
            bbox=table_bbox,
            column_boundaries=tuple(boundaries),
            row_boundaries=tuple(row_boundaries),
            direct_evidence=tuple(sorted({numeric_evidence, *drawing_modes})),
            structure_sha256=canonical_sha256(structure_payload),
        )

        reading_order = 0
        top_lines = [line for line in lines if facts.height * 0.04 <= line.y0 and line.y0 < table_top - 1.0]
        footer_top = _page_footer_top(lines, facts.width, facts.height)
        bottom_lines = [line for line in lines if table_bottom + 1.0 < line.y0 and line.y1 < footer_top]
        footer_lines = [line for line in lines if line.y0 >= footer_top]
        top_cells = _auxiliary_cells(top_lines, "page_heading", facts.width, table_top, reading_order)
        reading_order += len(top_cells)

        table_cells: list[TableCell] = []
        for row_index, row_span, fragment in _cell_fragments(table_rows, numeric_top):
            row = table_rows[row_index]
            row_top, row_bottom = row_boundaries[row_index], row_boundaries[row_index + row_span]
            only_text = len(row.fragments) == 1 and row.fragments[0].translatable
            column_start = fragment.column_start
            column_end = fragment.column_end
            bbox = fragment.bbox
            visible_objects = _deduplicate_overlaid_objects(fragment.objects)
            representative = max(visible_objects, key=lambda item: (item.font_size, len(item.text.strip())))
            translatable = fragment.translatable
            role = "table_header" if bbox[3] <= numeric_top + 0.5 else _cell_role(row_index, fragment, only_text)
            source_text = _merge_fragment_text(fragment.objects)
            table_cells.append(
                TableCell(
                    container_id=f"table-00-r{row_index:03d}-c{column_start:02d}-s{column_end - column_start + 1:02d}",
                    table_id="table-00",
                    row_index=row_index,
                    column_index=column_start,
                    row_span=row_span,
                    column_span=column_end - column_start + 1,
                    source_object_ids=tuple(item.object_id for item in fragment.objects),
                    source_text=source_text,
                    source_bbox=_round_rect(bbox),
                    cell_bbox=_round_rect((boundaries[column_start], row_top, boundaries[column_end + 1], row_bottom)),
                    reading_order=reading_order,
                    role=role,
                    translatable=translatable,
                    protected_tokens=protected_tokens(source_text) if translatable else (),
                    font_size=round(max(item.font_size for item in visible_objects), 4),
                    color_srgb=representative.color_srgb,
                    font_weight=_font_weight(visible_objects),
                    alignment=_alignment(bbox, (boundaries[column_start], row_top, boundaries[column_end + 1], row_bottom), column_start),
                )
            )
            reading_order += 1
        table_cells = _clip_overlapping_cell_write_areas(table_cells)
        bottom_cells = _auxiliary_cells(bottom_lines, "table_footnote", facts.width, facts.height * 0.92, reading_order)
        reading_order += len(bottom_cells)
        footer_cells = _auxiliary_cells(footer_lines, "page_footer", facts.width, facts.height, reading_order)
        cells = [*top_cells, *table_cells, *bottom_cells, *footer_cells]
        ordered = tuple(replace(cell, reading_order=index) for index, cell in enumerate(cells))
        protected_ids = tuple(
            object_id
            for cell in ordered
            if not cell.translatable
            for object_id in cell.source_object_ids
        )
        return TableTemplate(facts.page_id, TOOLBOX_KEY, facts.width, facts.height, structure, ordered, protected_ids)


def _clip_overlapping_cell_write_areas(cells: list[TableCell]) -> list[TableCell]:
    output = []
    for cell in cells:
        if not cell.translatable:
            output.append(cell)
            continue
        following = [
            other
            for other in cells
            if other.row_index == cell.row_index
            and other.container_id != cell.container_id
            and other.source_bbox[0] > cell.source_bbox[0] + 0.5
            and other.source_bbox[0] < cell.cell_bbox[2] - 0.5
        ]
        if not following:
            output.append(cell)
            continue
        next_left = min(other.source_bbox[0] for other in following)
        clearance = max(0.8, cell.font_size * 0.10)
        right = max(cell.source_bbox[2] + 0.5, next_left - clearance)
        if right < cell.cell_bbox[2] - 0.05:
            cell = replace(
                cell,
                cell_bbox=(cell.cell_bbox[0], cell.cell_bbox[1], round(right, 4), cell.cell_bbox[3]),
            )
        output.append(cell)
    return output


def _numeric_columns(facts: PageFacts) -> list[list[TextObjectFact]]:
    candidates = [
        item
        for item in facts.text_objects
        if is_protected_text(item.text)
        and item.bbox[2] > facts.width * 0.25
        and item.bbox[1] >= facts.height * 0.10
        and item.bbox[3] <= facts.height * 0.92
    ]
    right_aligned = _stable_numeric_clusters(candidates, 2)
    left_aligned = _stable_numeric_clusters(candidates, 0)
    selected = max(
        (right_aligned, left_aligned),
        key=lambda clusters: (sum(len(cluster) for cluster in clusters), -len(clusters)),
    )
    return sorted(
        selected,
        key=lambda cluster: median((item.bbox[0] + item.bbox[2]) / 2.0 for item in cluster),
    )


def _stable_numeric_clusters(
    candidates: list[TextObjectFact],
    coordinate_index: int,
) -> list[list[TextObjectFact]]:
    clusters: list[list[TextObjectFact]] = []
    for item in sorted(candidates, key=lambda value: value.bbox[coordinate_index]):
        if clusters:
            center = sum(value.bbox[coordinate_index] for value in clusters[-1]) / len(clusters[-1])
            if abs(item.bbox[coordinate_index] - center) <= 5.0:
                clusters[-1].append(item)
                continue
        clusters.append([item])
    return [
        sorted(cluster, key=lambda item: (item.bbox[1], item.bbox[0]))
        for cluster in clusters
        if len(cluster) >= 2 and len({round(item.bbox[1], 1) for item in cluster}) >= 2
    ]


def _drawing_evidence(
    page: fitz.Page,
    numeric_top: float,
    numeric_bottom: float,
    table_left: float,
    table_right: float,
) -> tuple[list[tuple[float, int]], list[float], list[float], set[str]]:
    x_values: list[float] = []
    y_values: list[float] = []
    horizontal_rules: list[float] = []
    modes: set[str] = set()
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            rect: fitz.Rect | None = None
            if item[0] == "l":
                first, second = item[1], item[2]
                rect = fitz.Rect(min(first.x, second.x), min(first.y, second.y), max(first.x, second.x), max(first.y, second.y))
                if abs(first.y - second.y) <= 1.0:
                    modes.add("horizontal_rules")
                    horizontal_rules.append(round((first.y + second.y) / 2.0, 4))
                if abs(first.x - second.x) <= 1.0:
                    modes.add("vertical_rules")
            elif item[0] == "re":
                rect = fitz.Rect(item[1])
                modes.add("filled_or_bordered_rectangles")
                table_width = max(table_right - table_left, 1.0)
                table_overlap = max(0.0, min(rect.x1, table_right) - max(rect.x0, table_left))
                fill_band = drawing.get("fill") is not None and table_overlap >= table_width * 0.60
                if drawing.get("color") is not None or fill_band:
                    horizontal_rules.extend((round(rect.y0, 4), round(rect.y1, 4)))
                elif rect.height <= 1.0 and rect.width > 20.0:
                    horizontal_rules.append(round((rect.y0 + rect.y1) / 2.0, 4))
            if rect is None or rect.y1 < numeric_top - 80.0 or rect.y0 > numeric_bottom + 40.0:
                continue
            if rect.x1 < table_left - 15.0 or rect.x0 > table_right + 15.0:
                continue
            x_values.extend((round(rect.x0, 2), round(rect.x1, 2)))
            y_values.extend((round(rect.y0, 2), round(rect.y1, 2)))
    counts: dict[float, int] = defaultdict(int)
    for value in x_values:
        counts[value] += 1
    return sorted(counts.items()), y_values, horizontal_rules, modes


def _snap_boundaries(raw: list[float], drawing_x: list[tuple[float, int]]) -> list[float]:
    snapped: list[float] = []
    for value in raw:
        candidates = [(coordinate, count) for coordinate, count in drawing_x if abs(coordinate - value) <= 12.0]
        if candidates:
            coordinate, _ = min(candidates, key=lambda item: (-item[1], abs(item[0] - value)))
            value = coordinate
        if not snapped or value > snapped[-1] + 0.5:
            snapped.append(round(value, 4))
    return snapped


def _drawing_column_boundaries(
    drawing_x: list[tuple[float, int]],
    table_left: float,
    table_right: float,
    relevant_text: list[TextObjectFact],
) -> list[float]:
    if not relevant_text:
        return []
    text_right = max(item.bbox[2] for item in relevant_text)
    font_size = median(item.font_size for item in relevant_text)
    right_limit = text_right + max(12.0, font_size * 3.0)
    boundaries = [
        coordinate
        for coordinate, count in drawing_x
        if count >= 3
        and table_left - 12.0 <= coordinate <= right_limit
    ]
    if len(boundaries) < 3:
        return []
    if boundaries[0] > table_left + 12.0 or boundaries[-1] < table_right - 0.5:
        return []
    if any(right - left < 4.0 for left, right in zip(boundaries, boundaries[1:])):
        return []
    return [round(value, 4) for value in boundaries]


def _page_lines(facts: PageFacts) -> list[_Row]:
    groups = _visual_line_groups(list(facts.text_objects))
    return [
        _Row(
            min(item.bbox[1] for item in objects),
            max(item.bbox[3] for item in objects),
            [_Fragment(0, 0, sorted(objects, key=lambda item: (item.bbox[0], item.span_index)))],
        )
        for objects in groups
    ]


def _logical_rows(lines: list[_Row], boundaries: list[float], numeric_top: float) -> list[_Row]:
    split_rows: list[_Row] = []
    for line in lines:
        fragments: dict[tuple[int, int], list[TextObjectFact]] = defaultdict(list)
        for item in line.fragments[0].objects:
            start = _column_for_x(item.bbox[0] + 0.2, boundaries)
            end = _column_for_x(item.bbox[2] - 0.2, boundaries)
            if _is_single_stacked_header_column(item, start, end, boundaries, numeric_top):
                start = end
            fragments[(start, max(start, end))].append(item)
        row_fragments = [
            _Fragment(start, end, sorted(objects, key=lambda item: item.bbox[0]))
            for (start, end), objects in sorted(fragments.items())
        ]
        split_rows.append(
            _Row(
                line.y0,
                line.y1,
                _merge_inline_fragments(row_fragments),
            )
        )
    merged: list[_Row] = []
    for row in split_rows:
        if merged and _can_merge_rows(merged[-1], row):
            merged[-1] = _merge_rows(merged[-1], row)
        else:
            merged.append(row)
    return merged


def _is_single_stacked_header_column(
    item: TextObjectFact,
    start: int,
    end: int,
    boundaries: list[float],
    numeric_top: float,
) -> bool:
    if item.bbox[3] > numeric_top + 0.5 or end - start != 1:
        return False
    end_width = boundaries[end + 1] - boundaries[end]
    right_edge_tolerance = max(1.0, item.font_size * 0.6)
    width_tolerance = max(1.0, item.font_size * 0.85)
    return (
        abs(boundaries[end + 1] - item.bbox[2]) <= right_edge_tolerance
        and item.bbox[2] - item.bbox[0] <= end_width + width_tolerance
    )


def _cell_fragments(rows: list[_Row], numeric_top: float) -> list[tuple[int, int, _Fragment]]:
    consumed: set[tuple[int, int]] = set()
    output: list[tuple[int, int, _Fragment]] = []
    for row_index, row in enumerate(rows):
        for fragment_index, fragment in enumerate(row.fragments):
            if (row_index, fragment_index) in consumed:
                continue
            chain = [(row_index, fragment_index, fragment)]
            if fragment.translatable and fragment.bbox[3] <= numeric_top + 0.5:
                next_row_index = row_index + 1
                while next_row_index < len(rows) and rows[next_row_index].y0 <= numeric_top + 0.5:
                    matches = [
                        (index, candidate)
                        for index, candidate in enumerate(rows[next_row_index].fragments)
                        if candidate.translatable
                        and candidate.column_start == fragment.column_start
                        and candidate.column_end == fragment.column_end
                    ]
                    if len(matches) != 1:
                        break
                    next_fragment_index, next_fragment = matches[0]
                    if not _is_stacked_header_continuation(chain[-1][2], next_fragment):
                        break
                    chain.append((next_row_index, next_fragment_index, next_fragment))
                    next_row_index += 1
            for consumed_row, consumed_fragment, _ in chain:
                consumed.add((consumed_row, consumed_fragment))
            objects = [item for _, _, candidate in chain for item in candidate.objects]
            output.append(
                (
                    row_index,
                    len(chain),
                    _Fragment(fragment.column_start, fragment.column_end, objects),
                )
            )
    return output


def _merge_inline_fragments(fragments: list[_Fragment]) -> list[_Fragment]:
    output: list[_Fragment] = []
    for fragment in sorted(fragments, key=lambda item: item.bbox[0]):
        if output:
            previous = output[-1]
            previous_lines = {(item.block_index, item.line_index) for item in previous.objects}
            current_lines = {(item.block_index, item.line_index) for item in fragment.objects}
            gap = fragment.bbox[0] - previous.bbox[2]
            font_size = min(
                median(item.font_size for item in previous.objects),
                median(item.font_size for item in fragment.objects),
            )
            if previous_lines & current_lines and -0.5 <= gap <= max(0.8, font_size * 0.25):
                output[-1] = _Fragment(
                    previous.column_start,
                    max(previous.column_end, fragment.column_end),
                    sorted(previous.objects + fragment.objects, key=lambda item: item.bbox[0]),
                )
                continue
        output.append(fragment)
    return output


def _is_stacked_header_continuation(left: _Fragment, right: _Fragment) -> bool:
    left_font = median(item.font_size for item in left.objects)
    right_font = median(item.font_size for item in right.objects)
    font_size = min(left_font, right_font)
    gap = right.bbox[1] - left.bbox[3]
    horizontal_overlap = min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0])
    left_aligned = abs(left.bbox[0] - right.bbox[0]) <= max(3.0, font_size * 1.5)
    return (
        -font_size * 1.5 <= gap <= max(4.0, font_size * 0.8)
        and (horizontal_overlap > 0.5 or left_aligned)
    )


def _can_merge_rows(left: _Row, right: _Row) -> bool:
    if any(not fragment.translatable for fragment in left.fragments):
        return False
    left_labels = [fragment for fragment in left.fragments if fragment.translatable and fragment.column_start == 0]
    right_labels = [fragment for fragment in right.fragments if fragment.translatable and fragment.column_start == 0]
    left_text_columns = {fragment.column_start for fragment in left.fragments if fragment.translatable}
    right_text_columns = {fragment.column_start for fragment in right.fragments if fragment.translatable}
    if left_text_columns != {0} or 0 not in right_text_columns:
        return False
    gap = right.y0 - left.y1
    left_objects = [item for fragment in left_labels for item in fragment.objects]
    right_objects = [item for fragment in right_labels for item in fragment.objects]
    if gap < -0.5 or gap > max(4.0, min(max(item.font_size for item in left_objects), max(item.font_size for item in right_objects)) * 0.6):
        return False
    if _font_weight(left_objects) != _font_weight(right_objects):
        return False
    left_text = " ".join(fragment.text for fragment in left.fragments if fragment.translatable).strip()
    if not left_text or _END_PUNCTUATION.search(left_text):
        return False
    left_x = min(item.bbox[0] for item in left_objects if not is_protected_text(item.text))
    right_x = min(item.bbox[0] for item in right_objects if not is_protected_text(item.text))
    return abs(left_x - right_x) <= 35.0


def _merge_rows(left: _Row, right: _Row) -> _Row:
    fragments: dict[tuple[int, int], list[TextObjectFact]] = defaultdict(list)
    for fragment in (*left.fragments, *right.fragments):
        fragments[(fragment.column_start, fragment.column_end)].extend(fragment.objects)
    return _Row(
        left.y0,
        right.y1,
        [_Fragment(start, end, objects) for (start, end), objects in sorted(fragments.items())],
    )


def _row_boundaries(rows: list[_Row], top: float, bottom: float, drawing_y: list[float]) -> list[float]:
    values = [round(top, 4)]
    for left, right in zip(rows, rows[1:]):
        midpoint = (left.y1 + right.y0) / 2.0
        direct_boundaries = {
            round(value, 4)
            for value in drawing_y
            if left.y1 + 0.1 <= value <= right.y0 - 0.1
        }
        boundary = min(direct_boundaries, key=lambda value: abs(value - midpoint)) if direct_boundaries else midpoint
        values.append(round(boundary, 4))
    values.append(round(bottom, 4))
    return values


def _page_footer_top(lines: list[_Row], page_width: float, page_height: float) -> float:
    candidates: list[float] = []
    for line in lines:
        if line.y0 < page_height * 0.90:
            continue
        objects = _deduplicate_overlaid_objects(line.fragments[0].objects)
        if len(objects) < 2:
            continue
        line_bbox = _union(item.bbox for item in objects)
        if line_bbox[2] - line_bbox[0] > page_width * 0.55:
            continue
        ordered = sorted(objects, key=lambda item: item.bbox[0])
        largest_gap = max((right.bbox[0] - left.bbox[2] for left, right in zip(ordered, ordered[1:])), default=0.0)
        font_scale = median(item.font_size for item in objects)
        if largest_gap >= max(4.0, font_scale):
            candidates.append(line.y0)
    if candidates:
        return min(candidates) - 1.0
    return page_height * 0.92


def _auxiliary_cells(lines: list[_Row], role: str, page_width: float, limit: float, start_order: int) -> list[TableCell]:
    output: list[TableCell] = []
    useful = list(lines) if role == "page_footer" else [
        line for line in lines if not all(is_protected_text(item.text) for item in line.fragments[0].objects)
    ]
    for index, line in enumerate(useful):
        fragments = _split_auxiliary_objects(line.fragments[0].objects)
        fragment_bboxes = [_union(item.bbox for item in objects) for objects in fragments]
        next_y = useful[index + 1].y0 - 1.0 if index + 1 < len(useful) else limit - 1.0
        for fragment_index, (objects, bbox) in enumerate(zip(fragments, fragment_bboxes)):
            visible_objects = _deduplicate_overlaid_objects(objects)
            representative = max(visible_objects, key=lambda item: (item.font_size, len(item.text.strip())))
            safe_bottom = max(bbox[3] + 1.0, next_y)
            if fragment_index + 1 < len(fragment_bboxes):
                next_left = fragment_bboxes[fragment_index + 1][0]
                safe_right = max(bbox[2] + 0.5, (bbox[2] + next_left) / 2.0)
            else:
                safe_right = max(bbox[2], page_width - 24.0)
            translatable = not all(is_protected_text(item.text) for item in visible_objects)
            source_text = _merge_fragment_text(objects)
            suffix = f"-f{fragment_index:02d}" if len(fragments) > 1 else ""
            output.append(
                TableCell(
                    container_id=f"aux-{role}-{index:03d}{suffix}",
                    table_id="page-aux",
                    row_index=index,
                    column_index=fragment_index,
                    row_span=1,
                    column_span=1,
                    source_object_ids=tuple(item.object_id for item in objects),
                    source_text=source_text,
                    source_bbox=_round_rect(bbox),
                    cell_bbox=_round_rect((bbox[0], bbox[1], safe_right, safe_bottom)),
                    reading_order=start_order + len(output),
                    role=role,
                    translatable=translatable,
                    protected_tokens=protected_tokens(source_text) if translatable else (),
                    font_size=round(max(item.font_size for item in visible_objects), 4),
                    color_srgb=representative.color_srgb,
                    font_weight=_font_weight(visible_objects),
                    alignment="left",
                )
            )
    return output


def _split_auxiliary_objects(objects: list[TextObjectFact]) -> list[list[TextObjectFact]]:
    output: list[list[TextObjectFact]] = []
    for item in sorted(objects, key=lambda value: (value.bbox[0], value.span_index)):
        if output:
            previous = output[-1]
            previous_bbox = _union(value.bbox for value in previous)
            overlaid = max(abs(item.bbox[index] - previous_bbox[index]) for index in range(4)) <= 0.75
            same_kind = is_protected_text(item.text) == all(is_protected_text(value.text) for value in previous)
            font_scale = min(item.font_size, median(value.font_size for value in previous))
            adjacent = item.bbox[0] - previous_bbox[2] <= max(1.0, font_scale * 0.25)
            if overlaid or (same_kind and adjacent):
                previous.append(item)
                continue
        output.append([item])
    return output


def _cell_role(row_index: int, fragment: _Fragment, only_text: bool) -> str:
    text = fragment.text
    if row_index <= 1:
        return "table_header"
    if only_text and (_font_weight(fragment.objects) == "bold" or _is_uppercase_heading(text)):
        return "merged_section_header"
    return "table_body"


def _is_uppercase_heading(text: str) -> bool:
    letters = [character for character in text if character.isascii() and character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


def _font_weight(objects: list[TextObjectFact]) -> str:
    total = sum(max(1, len(item.text.strip())) for item in objects)
    bold = sum(max(1, len(item.text.strip())) for item in objects if "bold" in item.font_name.casefold())
    return "bold" if bold * 2 >= total else "regular"


def _alignment(source_bbox: Rect, cell_bbox: Rect, column_index: int) -> str:
    if column_index == 0:
        return "left"
    source_center = (source_bbox[0] + source_bbox[2]) / 2.0
    cell_center = (cell_bbox[0] + cell_bbox[2]) / 2.0
    if abs(source_center - cell_center) <= (cell_bbox[2] - cell_bbox[0]) * 0.18:
        return "center"
    if cell_bbox[2] - source_bbox[2] <= max(3.0, (cell_bbox[2] - cell_bbox[0]) * 0.10):
        return "right"
    return "left"


def _column_for_x(value: float, boundaries: list[float]) -> int:
    for index in range(len(boundaries) - 1):
        if value < boundaries[index + 1] or index == len(boundaries) - 2:
            return index
    return len(boundaries) - 2


def _merge_fragment_text(objects: list[TextObjectFact]) -> str:
    lines = ["".join(item.text for item in _deduplicate_overlaid_objects(items)).strip() for items in _visual_line_groups(objects)]
    return "\n".join(line for line in lines if line).strip()


def _deduplicate_overlaid_objects(objects: list[TextObjectFact]) -> list[TextObjectFact]:
    output: list[TextObjectFact] = []
    for item in objects:
        duplicate_index = next(
            (
                index
                for index, previous in enumerate(output)
                if item.text.strip() == previous.text.strip()
                and max(abs(item.bbox[coordinate] - previous.bbox[coordinate]) for coordinate in range(4)) <= 0.75
            ),
            None,
        )
        if duplicate_index is None:
            output.append(item)
        else:
            # PDF content painted later is visually on top of an overlaid duplicate.
            output[duplicate_index] = item
    return output


def _visual_line_groups(objects: list[TextObjectFact]) -> list[list[TextObjectFact]]:
    groups: list[list[TextObjectFact]] = []
    for item in sorted(objects, key=lambda value: ((value.bbox[1] + value.bbox[3]) / 2.0, value.bbox[0])):
        center = (item.bbox[1] + item.bbox[3]) / 2.0
        if groups:
            previous_center = median((value.bbox[1] + value.bbox[3]) / 2.0 for value in groups[-1])
            tolerance = max(1.5, min(item.font_size, median(value.font_size for value in groups[-1])) * 0.28)
            if abs(center - previous_center) <= tolerance:
                groups[-1].append(item)
                groups[-1].sort(key=lambda value: (value.bbox[0], value.span_index))
                continue
        groups.append([item])
    return groups


def _inside_vertical(row: _Row, top: float, bottom: float) -> bool:
    center = (row.y0 + row.y1) / 2.0
    return top - 0.5 <= center <= bottom + 0.5


def _overlaps_x(row: _Row, left: float, right: float) -> bool:
    bbox = row.fragments[0].bbox
    return bbox[2] >= left - 1.0 and bbox[0] <= right + 1.0


def _union(rectangles) -> Rect:
    values = list(rectangles)
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)
