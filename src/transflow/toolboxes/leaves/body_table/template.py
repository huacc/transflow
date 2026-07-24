"""Build table ownership from production Kernel facts, never sample identity."""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import pairwise
from statistics import median

from transflow.domain.common import content_sha256
from transflow.domain.text_inventory import (
    InventoryDisposition,
    PageTextInventoryItem,
)
from transflow.pdf_kernel.facts import (
    ExtractedPageFacts,
    KernelTableFact,
    KernelTextFact,
    RectTuple,
)
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.leaves.body_table.models import (
    TableCell,
    TableStructure,
    TableTemplate,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

TOOLBOX_KEY = "body.table"
_NUMERIC = re.compile(r"^[\s\d,.:;()%+\-/$€£¥]+$")


def build_table_template(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> TableTemplate:
    """Adapt Kernel tables into cell-bound units and explicit page context."""

    inventory = {
        item.object_id: item
        for item in freeze_page_text_inventory(
            facts,
            target_language=policy.target_language,
        ).items
    }
    text_by_id = {item.object_id: item for item in facts.text_spans}
    structures: list[TableStructure] = []
    cells: list[TableCell] = []
    claimed: set[str] = set()

    for table_index, table in enumerate(
        sorted(facts.table_objects, key=lambda item: (item.bbox[1], item.bbox[0]))
    ):
        structure = _build_structure(
            table,
            table_index,
            text_by_id,
            facts.page.width_points,
        )
        structures.append(structure)
        table_cells, table_claimed = _build_table_cells(
            table,
            structure,
            text_by_id,
            inventory,
        )
        cells.extend(table_cells)
        claimed.update(table_claimed)

    cells.extend(
        _build_page_context(
            facts,
            inventory,
            claimed,
            start_order=len(cells),
        )
    )
    ordered = tuple(
        _with_reading_order(item, index)
        for index, item in enumerate(
            sorted(cells, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
        )
    )
    return TableTemplate(
        page_id=facts.page_identity,
        toolbox_key=TOOLBOX_KEY,
        width=facts.page.width_points,
        height=facts.page.height_points,
        structures=tuple(structures),
        cells=ordered,
        protected_object_ids=tuple(
            item.object_id
            for item in inventory.values()
            if item.disposition is InventoryDisposition.KEEP_SOURCE
        ),
    )


def _build_structure(
    table: KernelTableFact,
    table_index: int,
    text_by_id: dict[str, KernelTextFact],
    page_width: float,
) -> TableStructure:
    source_cells = tuple(bbox for bbox in table.cell_bboxes if _area(bbox) > 1.0)
    row_boundaries = _cluster_boundaries(
        tuple(value for bbox in source_cells for value in (bbox[1], bbox[3])),
        tolerance=5.0,
    )
    column_boundaries = list(
        _cluster_boundaries(
            tuple(value for bbox in source_cells for value in (bbox[0], bbox[2])),
            tolerance=max(1.0, page_width * 0.003),
        )
    )
    table_spans = tuple(
        text_by_id[object_id]
        for object_id in table.text_object_ids
        if object_id in text_by_id
    )
    inferred = _infer_internal_column_boundaries(
        tuple(column_boundaries),
        table_spans,
        page_width,
    )
    column_boundaries = sorted((*column_boundaries, *inferred))
    cell_bboxes = tuple(
        (
            round(left, 4),
            round(top, 4),
            round(right, 4),
            round(bottom, 4),
        )
        for top, bottom in pairwise(row_boundaries)
        for left, right in pairwise(column_boundaries)
        if right > left + 1.0 and bottom > top + 1.0
    )
    signature = content_sha256(
        {
            "bbox": table.bbox,
            "cell_bboxes": cell_bboxes,
            "kernel_table_id": table.object_id,
        }
    )
    return TableStructure(
        table_id=f"table-{table_index:02d}",
        bbox=table.bbox,
        cell_bboxes=cell_bboxes,
        direct_evidence=(
            "kernel.table",
            "kernel.cell_bboxes",
            *(
                ("repeated_protected_column_anchors",)
                if inferred
                else ()
            ),
        ),
        structure_sha256=signature,
    )


def _build_table_cells(
    table: KernelTableFact,
    structure: TableStructure,
    text_by_id: dict[str, KernelTextFact],
    inventory: dict[str, PageTextInventoryItem],
) -> tuple[list[TableCell], set[str]]:
    grouped: dict[int, list[KernelTextFact]] = defaultdict(list)
    unassigned: list[KernelTextFact] = []
    for object_id in table.text_object_ids:
        span = text_by_id.get(object_id)
        if span is None or not span.text.strip():
            continue
        candidates = [
            (index, bbox)
            for index, bbox in enumerate(structure.cell_bboxes)
            if _contains_point(bbox, _center(span.bbox), tolerance=0.75)
        ]
        if not candidates:
            unassigned.append(span)
            continue
        cell_index, _ = min(candidates, key=lambda item: (_area(item[1]), item[0]))
        grouped[cell_index].append(span)

    output = [
        _cell_from_spans(
            structure.table_id,
            f"{structure.table_id}-cell-{cell_index:04d}",
            spans,
            structure.cell_bboxes[cell_index],
            inventory,
            ownership_ambiguous=False,
        )
        for cell_index, spans in sorted(grouped.items())
    ]
    if unassigned:
        output.append(
            _cell_from_spans(
                structure.table_id,
                f"{structure.table_id}-unassigned",
                unassigned,
                structure.bbox,
                inventory,
                ownership_ambiguous=True,
            )
        )
    return output, {
        object_id
        for item in output
        for object_id in item.source_object_ids
    }


def _cluster_boundaries(
    values: tuple[float, ...],
    *,
    tolerance: float,
) -> tuple[float, ...]:
    clusters: list[list[float]] = []
    for value in sorted(set(values)):
        if clusters and value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return tuple(round(median(cluster), 4) for cluster in clusters)


def _infer_internal_column_boundaries(
    boundaries: tuple[float, ...],
    spans: tuple[KernelTextFact, ...],
    page_width: float,
) -> tuple[float, ...]:
    inferred: list[float] = []
    for left, right in pairwise(boundaries):
        width = right - left
        if width < page_width * 0.32:
            continue
        candidates = [
            item
            for item in spans
            if _NUMERIC.fullmatch(item.text.strip())
            and left + width * 0.72
            <= _center(item.bbox)[0]
            <= right
            and item.bbox[2] - item.bbox[0] <= width * 0.16
        ]
        if len(candidates) < 3:
            continue
        clusters: list[list[KernelTextFact]] = []
        tolerance = max(4.0, page_width * 0.025)
        for item in sorted(candidates, key=lambda row: _center(row.bbox)[0]):
            if (
                clusters
                and _center(item.bbox)[0]
                - _center(clusters[-1][-1].bbox)[0]
                <= tolerance
            ):
                clusters[-1].append(item)
            else:
                clusters.append([item])
        anchor_cluster = max(
            clusters,
            key=lambda group: (
                len(group),
                median(_center(item.bbox)[0] for item in group),
            ),
        )
        if len(anchor_cluster) < 3:
            continue
        font_scale = median(item.font_size for item in anchor_cluster)
        boundary = min(item.bbox[0] for item in anchor_cluster) - max(
            font_scale * 1.5,
            width * 0.04,
        )
        if left + width * 0.55 < boundary < right - max(8.0, font_scale):
            inferred.append(round(boundary, 4))
    return tuple(inferred)


def _build_page_context(
    facts: ExtractedPageFacts,
    inventory: dict[str, PageTextInventoryItem],
    claimed: set[str],
    *,
    start_order: int,
) -> list[TableCell]:
    grouped: dict[int, list[KernelTextFact]] = defaultdict(list)
    for span in facts.text_spans:
        if span.object_id not in claimed and span.text.strip():
            grouped[span.block_index].append(span)
    output: list[TableCell] = []
    for offset, (_, spans) in enumerate(
        sorted(
            grouped.items(),
            key=lambda item: (
                min(span.bbox[1] for span in item[1]),
                min(span.bbox[0] for span in item[1]),
                item[0],
            ),
        )
    ):
        source_bbox = _union(tuple(item.bbox for item in spans))
        hard_boundary = _context_boundary(source_bbox, facts)
        output.append(
            _cell_from_spans(
                "page-context",
                f"table-page-context-{offset:04d}",
                spans,
                hard_boundary,
                inventory,
                ownership_ambiguous=False,
                role=(
                    "margin"
                    if source_bbox[3]
                    <= facts.page.height_points * 0.08
                    or source_bbox[1]
                    >= facts.page.height_points * 0.92
                    else "context"
                ),
                reading_order=start_order + offset,
            )
        )
    return output

def _context_boundary(
    source_bbox: RectTuple,
    facts: ExtractedPageFacts,
) -> RectTuple:
    x0, y0, x1, y1 = source_bbox
    source_width = x1 - x0
    preferred_width = max(source_width, facts.page.width_points * 0.45)
    center = (x0 + x1) / 2.0
    left_gap = x0
    right_gap = facts.page.width_points - x1
    if abs(left_gap - right_gap) <= facts.page.width_points * 0.04:
        half = min(preferred_width / 2.0, center - 4.0, facts.page.width_points - 4.0 - center)
        return (round(center - half, 4), y0, round(center + half, 4), y1)
    if right_gap < left_gap * 0.65:
        return (round(max(4.0, x1 - preferred_width), 4), y0, x1, y1)
    return (x0, y0, round(min(facts.page.width_points - 4.0, x0 + preferred_width), 4), y1)


def _cell_from_spans(
    table_id: str,
    container_id: str,
    spans: list[KernelTextFact],
    hard_boundary: RectTuple,
    inventory: dict[str, PageTextInventoryItem],
    *,
    ownership_ambiguous: bool,
    role: str = "cell",
    reading_order: int = 0,
) -> TableCell:
    ordered = tuple(
        sorted(
            spans,
            key=lambda item: (
                item.block_index,
                item.line_index,
                item.span_index,
                item.bbox[0],
            ),
        )
    )
    object_ids = tuple(item.object_id for item in ordered)
    translation_ids = tuple(
        object_id
        for object_id in object_ids
        if inventory[object_id].disposition is InventoryDisposition.TRANSLATE
    )
    inline_keep_ids = tuple(
        object_id
        for object_id in object_ids
        if inventory[object_id].disposition is InventoryDisposition.KEEP_SOURCE
    )
    source_bbox = _union(tuple(item.bbox for item in ordered))
    representative = max(
        ordered,
        key=lambda item: (item.font_size, len(item.text), -item.span_index),
    )
    source_text = _merge_text(ordered)
    return TableCell(
        container_id=container_id,
        table_id=table_id,
        source_object_ids=object_ids,
        translation_object_ids=translation_ids,
        inline_keep_source_object_ids=inline_keep_ids,
        source_text=source_text,
        source_bbox=source_bbox,
        hard_legal_boundary=hard_boundary,
        reading_order=reading_order,
        role=role,
        font_size=max(item.font_size for item in ordered),
        color_srgb=representative.color_srgb,
        alignment=_alignment(source_bbox, hard_boundary, source_text),
        ownership_ambiguous=ownership_ambiguous,
    )


def _merge_text(spans: tuple[KernelTextFact, ...]) -> str:
    if len(spans) == 1:
        return spans[0].text
    output: list[str] = []
    previous: KernelTextFact | None = None
    for item in spans:
        text = item.text.strip()
        if not text:
            continue
        separator = ""
        if previous is not None:
            separator = "\n" if (
                item.block_index != previous.block_index
                or item.line_index != previous.line_index
            ) else " "
        output.append(separator + text)
        previous = item
    return "".join(output).strip()


def _alignment(
    source_bbox: RectTuple,
    boundary: RectTuple,
    source_text: str,
) -> str:
    left_gap = max(0.0, source_bbox[0] - boundary[0])
    right_gap = max(0.0, boundary[2] - source_bbox[2])
    tolerance = max(1.5, min(source_bbox[3] - source_bbox[1], 12.0) * 0.35)
    if _NUMERIC.fullmatch(source_text) or right_gap + tolerance < left_gap * 0.55:
        return "RIGHT"
    if abs(left_gap - right_gap) <= tolerance:
        return "CENTER"
    return "LEFT"


def _with_reading_order(cell: TableCell, reading_order: int) -> TableCell:
    return TableCell(
        container_id=cell.container_id,
        table_id=cell.table_id,
        source_object_ids=cell.source_object_ids,
        translation_object_ids=cell.translation_object_ids,
        inline_keep_source_object_ids=cell.inline_keep_source_object_ids,
        source_text=cell.source_text,
        source_bbox=cell.source_bbox,
        hard_legal_boundary=cell.hard_legal_boundary,
        reading_order=reading_order,
        role=cell.role,
        font_size=cell.font_size,
        color_srgb=cell.color_srgb,
        alignment=cell.alignment,
        ownership_ambiguous=cell.ownership_ambiguous,
    )


def _contains_point(
    rect: RectTuple,
    point: tuple[float, float],
    *,
    tolerance: float,
) -> bool:
    return (
        rect[0] - tolerance <= point[0] <= rect[2] + tolerance
        and rect[1] - tolerance <= point[1] <= rect[3] + tolerance
    )


def _center(rect: RectTuple) -> tuple[float, float]:
    return (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0


def _area(rect: RectTuple) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _union(rects: tuple[RectTuple, ...]) -> RectTuple:
    if not rects:
        raise ValueError("table text ownership requires at least one source rectangle")
    return (
        round(min(item[0] for item in rects), 4),
        round(min(item[1] for item in rects), 4),
        round(max(item[2] for item in rects), 4),
        round(max(item[3] for item in rects), 4),
    )
