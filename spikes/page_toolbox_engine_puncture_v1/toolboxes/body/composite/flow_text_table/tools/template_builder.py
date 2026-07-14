from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
from statistics import median

from page_toolbox_puncture.contracts import PageFacts
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate, TextContainer
from toolboxes.body.flow_text.single.tools.template_builder import (
    build_p4_page_template,
    build_page_template,
)
from toolboxes.body.table.tools.models import TableTemplate
from toolboxes.body.table.tools.template_builder import TableCapabilityError, build_table_template

from . import TOOLBOX_KEY
from .models import (
    CompositePageTemplate,
    ContainerOwnership,
    FlowRegionTemplate,
    ObjectOwnership,
)
from .vector_table_builder import build_vector_table_detection, prefer_vector_detection


class CompositeCapabilityError(ValueError):
    pass


def build_composite_template(source_pdf: Path, facts: PageFacts) -> CompositePageTemplate:
    """Build one immutable ownership map, then delegate each region to its leaf template."""

    vector_detection = build_vector_table_detection(source_pdf, facts)
    try:
        detected_table = build_table_template(source_pdf, facts)
    except TableCapabilityError as exc:
        if vector_detection is None:
            raise CompositeCapabilityError(f"TABLE_CAPABILITY:{exc}") from exc
        detected_table = vector_detection.template
        table_cells = detected_table.cells
        table_regions = vector_detection.regions
    else:
        if vector_detection is not None and prefer_vector_detection(detected_table, vector_detection):
            detected_table = vector_detection.template
            table_cells = detected_table.cells
            table_regions = vector_detection.regions
        else:
            detected_cells = tuple(
                cell for cell in detected_table.cells
                if cell.table_id == detected_table.structure.table_id
            )
            table_cells, table_regions = _partition_table_cells(detected_table, detected_cells)
    if not table_cells or not any(cell.translatable for cell in table_cells):
        raise CompositeCapabilityError("TABLE_REGION_HAS_NO_TRANSLATABLE_CELL")
    table_regions = _expand_table_region_to_owned_cells(table_regions, table_cells)
    table_object_ids = _unique_object_ids(
        (object_id, cell.container_id)
        for cell in table_cells
        for object_id in cell.source_object_ids
    )
    table_template = replace(
        detected_table,
        cells=table_cells,
        protected_object_ids=tuple(
            sorted(
                object_id
                for cell in table_cells
                if not cell.translatable
                for object_id in cell.source_object_ids
            )
        ),
    )

    remaining_objects = tuple(
        item for item in facts.text_objects
        if item.object_id not in table_object_ids
    )
    if not remaining_objects:
        raise CompositeCapabilityError("FLOW_REGION_HAS_NO_NATIVE_TEXT")
    flow_facts = replace(
        facts,
        native_text_object_count=len(remaining_objects),
        text_objects=remaining_objects,
        text_objects_sha256=None,
    )
    flow_template = _restore_semantic_margins(
        build_p4_page_template(flow_facts),
        build_page_template(flow_facts),
    )
    flow_template = _mark_anchored_flow_containers(flow_template, facts)
    if not flow_template.containers:
        raise CompositeCapabilityError("FLOW_REGION_HAS_NO_TRANSLATABLE_CONTAINER")

    flow_regions = _split_flow_regions(flow_template, table_regions)
    flow_container_by_object: dict[str, str] = {}
    for region in flow_regions:
        for container in region.template.containers:
            for object_id in container.source_object_ids:
                if object_id in flow_container_by_object:
                    raise CompositeCapabilityError(f"DUPLICATE_FLOW_OBJECT_OWNER:{object_id}")
                flow_container_by_object[object_id] = container.container_id

    all_ids = {item.object_id for item in facts.text_objects}
    flow_ids = set(flow_container_by_object)
    if flow_ids & table_object_ids:
        raise CompositeCapabilityError("FLOW_TABLE_OBJECT_OWNERSHIP_OVERLAP")
    protected_ids = all_ids - flow_ids - table_object_ids
    if flow_ids | table_object_ids | protected_ids != all_ids:
        raise CompositeCapabilityError("OBJECT_OWNERSHIP_NOT_EXHAUSTIVE")

    table_container_by_object = {
        object_id: cell.container_id
        for cell in table_cells
        for object_id in cell.source_object_ids
    }
    ownerships = tuple(
        ObjectOwnership(
            item.object_id,
            "flow" if item.object_id in flow_ids else "table" if item.object_id in table_object_ids else "protected",
            flow_container_by_object.get(item.object_id) or table_container_by_object.get(item.object_id),
        )
        for item in facts.text_objects
    )
    container_ownerships = tuple(
        [
            ContainerOwnership(container.container_id, "flow", region.region_id)
            for region in flow_regions
            for container in region.template.containers
        ]
        + [
            ContainerOwnership(cell.container_id, "table", table_template.structure.table_id)
            for cell in table_template.translatable_cells
        ]
    )
    container_ids = [item.container_id for item in container_ownerships]
    if len(container_ids) != len(set(container_ids)):
        raise CompositeCapabilityError("DUPLICATE_CONTAINER_ID_ACROSS_OWNERS")

    return CompositePageTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        table_template,
        table_regions,
        flow_regions,
        ownerships,
        container_ownerships,
    )


def _restore_semantic_margins(
    filtered: SingleColumnTemplate,
    unfiltered: SingleColumnTemplate,
) -> SingleColumnTemplate:
    owned_object_ids = {
        object_id
        for container in filtered.containers
        for object_id in container.source_object_ids
    }
    additions = [
        container
        for container in unfiltered.containers
        if container.role == "margin"
        and not owned_object_ids.intersection(container.source_object_ids)
        and _is_semantic_margin_text(container.source_text)
    ]
    ordered = sorted(
        [*filtered.containers, *additions],
        key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.reading_order),
    )
    return replace(
        filtered,
        containers=tuple(
            replace(container, reading_order=index)
            for index, container in enumerate(ordered)
        ),
    )


def _is_semantic_margin_text(text: str) -> bool:
    value = text.strip()
    compact = re.sub(r"\s+", "", value)
    if not value or re.fullmatch(r"\d+(?:[/.-]\d+)*", compact):
        return False
    if re.fullmatch(r"(?:(?:page|p\.?))?\d+(?:of\d+)?", compact, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"(?:第)?\d+页(?:/?共?\d+页)?", compact):
        return False
    if re.fullmatch(r"(?:https?://|www\.)\S+", value, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"\S+@\S+\.\S+", value):
        return False
    return any(character.isalpha() for character in value)


def _mark_anchored_flow_containers(
    template: SingleColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate:
    body_fonts = [
        item.font_size
        for item in template.containers
        if item.role in {"body", "list"} and len(item.source_text) > 24
    ]
    reference_fonts = body_fonts or [
        item.font_size for item in template.containers if item.role != "margin"
    ]
    if not reference_fonts:
        return template
    reference_font = median(reference_fonts)
    page_area = facts.width * facts.height
    candidate_annotation_images = tuple(
        item.bbox
        for item in facts.image_objects
        if _rect_area(item.bbox) < page_area * 0.25
    )
    annotation_images = (
        candidate_annotation_images
        if sum(_rect_area(bbox) for bbox in candidate_annotation_images) < page_area * 0.50
        else ()
    )

    containers = []
    for container in template.containers:
        center = (
            (container.source_bbox[0] + container.source_bbox[2]) / 2.0,
            (container.source_bbox[1] + container.source_bbox[3]) / 2.0,
        )
        tiny_annotation = (
            container.role != "margin"
            and reference_font >= 4.0
            and container.font_size <= reference_font * 0.55
        )
        image_annotation = (
            container.role == "heading"
            and any(_point_in_rect(center, bbox) for bbox in annotation_images)
        )
        if image_annotation:
            containers.append(replace(container, role="image_anchored"))
        elif tiny_annotation:
            containers.append(replace(container, role="anchored"))
        else:
            containers.append(container)
    eligible = [
        container
        for container in containers
        if container.role not in {"margin", "anchored", "image_anchored"}
    ]
    spatial_grid_ids: set[str] = set()
    for index, container in enumerate(eligible):
        for other in eligible[index + 1:]:
            if _is_spatial_row_peer(container, other):
                spatial_grid_ids.update((container.container_id, other.container_id))
    anchored = tuple(
        replace(container, role="anchored_grid")
        if container.container_id in spatial_grid_ids
        else container
        for container in containers
    )
    return replace(template, containers=anchored)


def _rect_area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _point_in_rect(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def _is_spatial_row_peer(left: TextContainer, right: TextContainer) -> bool:
    left_height = left.source_bbox[3] - left.source_bbox[1]
    right_height = right.source_bbox[3] - right.source_bbox[1]
    vertical_overlap = min(left.source_bbox[3], right.source_bbox[3]) - max(
        left.source_bbox[1], right.source_bbox[1]
    )
    horizontal_gap = max(
        right.source_bbox[0] - left.source_bbox[2],
        left.source_bbox[0] - right.source_bbox[2],
    )
    return (
        vertical_overlap > max(0.5, min(left_height, right_height) * 0.30)
        and horizontal_gap > max(2.0, min(left.font_size, right.font_size) * 0.40)
    )


def _split_flow_regions(
    template: SingleColumnTemplate,
    table_regions: tuple[tuple[float, float, float, float], ...],
) -> tuple[FlowRegionTemplate, ...]:
    bands: list[tuple[str, tuple[float, float, float, float]]] = []
    first_top = table_regions[0][1]
    bands.append(("before_table", (0.0, 0.0, template.width, first_top)))
    for previous, current in zip(table_regions, table_regions[1:]):
        bands.append(("between_tables", (0.0, previous[3], template.width, current[1])))
    bands.append(("after_table", (0.0, table_regions[-1][3], template.width, template.height)))
    groups: dict[int, list[TextContainer]] = {index: [] for index in range(len(bands))}
    for container in template.containers:
        boundary_tolerance = max(0.5, min(2.0, container.font_size * 0.15))
        matching = [
            index
            for index, (_, allowed_bbox) in enumerate(bands)
            if container.source_bbox[1] >= allowed_bbox[1] - boundary_tolerance
            and container.source_bbox[3] <= allowed_bbox[3] + boundary_tolerance
        ]
        if len(matching) != 1:
            raise CompositeCapabilityError(f"FLOW_TABLE_SOURCE_REGION_OVERLAP:{container.container_id}")
        groups[matching[0]].append(container)

    regions: list[FlowRegionTemplate] = []
    for index, (relation, allowed_bbox) in enumerate(bands):
        containers = groups[index]
        if not containers:
            continue
        ordered = tuple(replace(container, reading_order=index) for index, container in enumerate(containers))
        region_id = f"flow-{relation}-{index:03d}"
        regions.append(
            FlowRegionTemplate(
                region_id,
                relation,
                tuple(round(value, 4) for value in allowed_bbox),
                replace(template, containers=ordered),
            )
        )
    if not regions:
        raise CompositeCapabilityError("FLOW_REGION_NOT_SEPARABLE_FROM_TABLE")
    return tuple(regions)


def _partition_table_cells(
    template: TableTemplate,
    cells,
) -> tuple[tuple, tuple[tuple[float, float, float, float], ...]]:
    """Split one broad P6 structure when a long non-tabular row gap separates real tables."""

    supported_rows = sorted(
        {
            cell.row_index
            for cell in cells
            if not cell.translatable and cell.column_index > 0
        }
    )
    if not supported_rows:
        raise CompositeCapabilityError("TABLE_REGION_HAS_NO_NUMERIC_COLUMN_EVIDENCE")
    clusters: list[list[int]] = [[supported_rows[0]]]
    for row_index in supported_rows[1:]:
        if row_index - clusters[-1][-1] > 3:
            clusters.append([row_index])
        else:
            clusters[-1].append(row_index)
    if len(clusters) == 1:
        return tuple(cells), (template.structure.bbox,)

    cells_by_row: dict[int, list] = {}
    for cell in cells:
        cells_by_row.setdefault(cell.row_index, []).append(cell)
    ranges = _expanded_table_ranges(
        clusters,
        cells_by_row,
        template.structure.row_count,
    )

    kept_rows = {
        row_index
        for start, end in ranges
        for row_index in range(start, end + 1)
    }
    kept_cells = tuple(cell for cell in cells if cell.row_index in kept_rows)
    x0, _, x1, _ = template.structure.bbox
    row_boundaries = template.structure.row_boundaries
    regions = tuple(
        (
            x0,
            row_boundaries[start],
            x1,
            row_boundaries[end + 1],
        )
        for start, end in ranges
    )
    return kept_cells, regions


def _expand_table_region_to_owned_cells(table_regions, table_cells):
    expanded = []
    for region in table_regions:
        owned = [
            cell
            for cell in table_cells
            if region[1] - 0.75
            <= (cell.source_bbox[1] + cell.source_bbox[3]) / 2.0
            <= region[3] + 0.75
        ]
        if not owned:
            expanded.append(region)
            continue
        expanded.append(
            (
                min(region[0], min(cell.source_bbox[0] for cell in owned)),
                min(region[1], min(cell.source_bbox[1] for cell in owned)),
                max(region[2], max(cell.source_bbox[2] for cell in owned)),
                max(region[3], max(cell.source_bbox[3] for cell in owned)),
            )
        )
    return tuple(expanded)


def _expanded_table_ranges(
    clusters: list[list[int]],
    cells_by_row: dict[int, list],
    row_count: int,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for cluster in clusters:
        start, end = cluster[0], cluster[-1]
        changed = True
        while changed:
            changed = False
            spanning_start = min(
                (
                    row_index
                    for row_index, row_cells in cells_by_row.items()
                    if row_index < start
                    and any(row_index + cell.row_span - 1 >= start for cell in row_cells)
                ),
                default=start,
            )
            if spanning_start < start:
                start = spanning_start
                changed = True
            if start > 0 and _is_structural_table_row(cells_by_row.get(start - 1, [])):
                start -= 1
                changed = True
            elif (
                start > 1
                and _is_table_label_row(cells_by_row.get(start - 1, []))
                and _is_structural_table_row(cells_by_row.get(start - 2, []))
            ):
                start -= 2
                changed = True
            if end + 1 < row_count and _is_structural_table_row(cells_by_row.get(end + 1, [])):
                end += 1
                changed = True
            spanning_end = max(
                (
                    cell.row_index + cell.row_span - 1
                    for row_index in range(start, end + 1)
                    for cell in cells_by_row.get(row_index, [])
                ),
                default=end,
            )
            if spanning_end > end:
                end = min(row_count - 1, spanning_end)
                changed = True

        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _is_structural_table_row(cells) -> bool:
    if not cells:
        return False
    if any(cell.role == "table_header" for cell in cells):
        return True
    column_starts = {cell.column_index for cell in cells}
    return len(column_starts) >= 2 or all(cell.column_index > 0 for cell in cells)


def _is_table_label_row(cells) -> bool:
    if len(cells) != 1 or not getattr(cells[0], "translatable", False):
        return False
    text = "".join(getattr(cells[0], "source_text", "").split())
    return 0 < len(text) <= 16


def _unique_object_ids(rows) -> set[str]:
    owners: dict[str, str] = {}
    for object_id, container_id in rows:
        previous = owners.get(object_id)
        if previous is not None and previous != container_id:
            raise CompositeCapabilityError(f"DUPLICATE_TABLE_OBJECT_OWNER:{object_id}")
        owners[object_id] = container_id
    return set(owners)
