from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from statistics import median

from page_toolbox_puncture.contracts import PageFacts
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.chart.tools.models import ChartVisualRegion, Rect
from toolboxes.body.chart.tools.template_builder import ChartCapabilityError, build_chart_template
from toolboxes.body.table.tools.models import TableTemplate
from toolboxes.body.table.tools.template_builder import (
    TableCapabilityError,
    build_table_template,
    is_protected_text,
    protected_tokens,
)

from .models import ChartTableRegion, ChartTableTemplate, ChartTableTextContainer


TOOLBOX_KEY = "body.composite.chart_table"


class ChartTableCapabilityError(RuntimeError):
    pass


def build_chart_table_template(source_pdf: Path, facts: PageFacts) -> ChartTableTemplate:
    try:
        chart_template = build_chart_template(facts)
    except ChartCapabilityError as exc:
        raise ChartTableCapabilityError(f"CHART_CAPABILITY:{exc}") from exc

    table_seeds = tuple(
        item for item in chart_template.containers if item.role.startswith("TABLE_")
    )
    table_template = _build_structural_table(source_pdf, facts, table_seeds)
    table_object_ids = tuple(
        object_id for cell in table_template.cells for object_id in cell.source_object_ids
    )
    table_bbox = _union(
        (
            table_template.structure.bbox,
            *(cell.cell_bbox for cell in table_template.cells),
        )
    )
    table_visual_ids = tuple(
        item.object_id
        for item in (*facts.image_objects, *facts.drawing_objects)
        if _area(item.bbox) < facts.width * facts.height * 0.80 and _center_in(item.bbox, table_bbox)
    )
    table_regions = (
        ChartTableRegion(
            region_id="table-region-000",
            owner="table",
            bbox=table_bbox,
            object_ids=tuple(dict.fromkeys((*table_object_ids, *table_visual_ids))),
        ),
    )

    visual_by_id = {
        item.object_id: item.bbox for item in (*facts.image_objects, *facts.drawing_objects)
    }
    chart_regions: list[ChartVisualRegion] = []
    for region in chart_template.visual_regions:
        object_ids = tuple(
            item
            for item in region.object_ids
            if item not in table_visual_ids
            and item in visual_by_id
            and _area(visual_by_id[item]) < facts.width * facts.height * 0.80
        )
        if not object_ids:
            continue
        bboxes = tuple(visual_by_id[item] for item in object_ids if item in visual_by_id)
        if not bboxes:
            continue
        chart_regions.append(
            ChartVisualRegion(
                region_id=f"chart-region-{len(chart_regions):03d}",
                kind=region.kind,
                bbox=_union(bboxes),
                object_ids=object_ids,
            )
        )
    if not chart_regions:
        raise ChartTableCapabilityError("CHART_REGION_NOT_FOUND_OUTSIDE_TABLE")

    containers, protected_object_ids = _build_containers(
        chart_template.containers,
        table_template,
        facts,
        table_regions[0].region_id,
    )
    _validate_total_ownership(containers, protected_object_ids, facts)
    owners = {item.owner for item in containers}
    if "chart" not in owners:
        raise ChartTableCapabilityError("CHART_TEXT_OWNER_NOT_FOUND")
    if "table" not in owners:
        raise ChartTableCapabilityError("TABLE_TEXT_OWNER_NOT_FOUND")

    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "page_id": facts.page_id,
            "chart_regions": chart_regions,
            "table_regions": table_regions,
            "table_structure": table_template.structure,
            "containers": containers,
            "protected_object_ids": protected_object_ids,
            "locked_objects_sha256": chart_template.locked_objects_sha256,
        }
    )
    return ChartTableTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        chart_regions=tuple(chart_regions),
        table_regions=table_regions,
        table_template=table_template,
        containers=containers,
        protected_object_ids=protected_object_ids,
        locked_objects_sha256=chart_template.locked_objects_sha256,
        structure_sha256=structure_sha256,
    )


def _build_structural_table(source_pdf, facts: PageFacts, table_seeds) -> TableTemplate:
    scoped_facts = facts
    if table_seeds:
        top = min(item.allowed_bbox[1] for item in table_seeds) - 2.0
        bottom = max(item.allowed_bbox[3] for item in table_seeds) + 2.0
        text_objects = tuple(
            item
            for item in facts.text_objects
            if top <= (item.bbox[1] + item.bbox[3]) / 2.0 <= bottom
        )
        scoped_facts = replace(
            facts,
            native_text_object_count=len(text_objects),
            text_objects=text_objects,
            text_objects_sha256=None,
        )
    try:
        detected = build_table_template(source_pdf, scoped_facts)
    except TableCapabilityError as exc:
        raise ChartTableCapabilityError(f"TABLE_REGION_NOT_FOUND:{exc}") from exc

    numeric_rows = {
        cell.row_index
        for cell in detected.cells
        if not cell.translatable and any(character.isdigit() for character in cell.source_text)
    }
    if len(numeric_rows) < 2:
        raise ChartTableCapabilityError("TABLE_REGION_NOT_FOUND:INSUFFICIENT_NUMERIC_ROWS")
    structured_band = _dominant_structured_row_band(detected) if not table_seeds else None
    if structured_band is not None:
        first_row, last_numeric_row = structured_band
    else:
        first_row = 0
        for row_index in range(min(numeric_rows)):
            row_cells = [
                cell
                for cell in detected.cells
                if cell.table_id == detected.structure.table_id and cell.row_index == row_index
            ]
            if (
                len(row_cells) == 1
                and row_cells[0].translatable
                and row_cells[0].column_span >= max(2, detected.structure.column_count - 1)
            ):
                first_row = row_index + 1
            else:
                break
        last_numeric_row = max(numeric_rows)
    cells = tuple(
        replace(
            cell,
            container_id=(
                f"{detected.structure.table_id}-r{cell.row_index - first_row:03d}-c{cell.column_index:02d}-s{cell.column_span:02d}"
                if cell.table_id == detected.structure.table_id
                else cell.container_id
            ),
            row_index=cell.row_index - first_row if cell.table_id == detected.structure.table_id else cell.row_index,
        )
        for cell in detected.cells
        if (
            first_row <= cell.row_index <= last_numeric_row
            if cell.table_id == detected.structure.table_id
            else structured_band is None
        )
    )
    row_boundaries = detected.structure.row_boundaries[first_row : last_numeric_row + 2]
    structure = replace(
        detected.structure,
        bbox=(
            detected.structure.bbox[0],
            row_boundaries[0],
            detected.structure.bbox[2],
            row_boundaries[-1],
        ),
        row_boundaries=row_boundaries,
        structure_sha256=canonical_sha256(
            {
                "table_id": detected.structure.table_id,
                "column_boundaries": detected.structure.column_boundaries,
                "row_boundaries": row_boundaries,
                "direct_evidence": detected.structure.direct_evidence,
            }
        ),
    )
    normalized = _refine_semantic_subcolumns(
        replace(detected, structure=structure, cells=cells),
        facts,
    )
    normalized = _merge_stacked_table_headers(normalized, facts)
    return _extend_trailing_structured_rows(normalized, facts)


def _dominant_structured_row_band(template: TableTemplate) -> tuple[int, int] | None:
    strong_rows: list[int] = []
    row_cells = {
        row_index: [
            cell
            for cell in template.cells
            if cell.table_id == template.structure.table_id and cell.row_index == row_index
        ]
        for row_index in range(template.structure.row_count)
    }
    for row_index, cells in row_cells.items():
        semantic = any(cell.translatable for cell in cells)
        numeric = any(
            not cell.translatable and any(character.isdigit() for character in cell.source_text)
            for cell in cells
        )
        if semantic and numeric:
            strong_rows.append(row_index)
    if len(strong_rows) < 2:
        return None
    clusters: list[list[int]] = []
    for row_index in strong_rows:
        if clusters and row_index - clusters[-1][-1] <= 2:
            clusters[-1].append(row_index)
        else:
            clusters.append([row_index])
    cluster = max(clusters, key=lambda rows: (len(rows), rows[-1] - rows[0]))
    if len(cluster) < 2:
        return None
    first, last = cluster[0], cluster[-1]
    expanded_headers = 0
    while first > 0 and row_cells[first - 1] and expanded_headers < 2:
        previous_bottom = max(cell.source_bbox[3] for cell in row_cells[first - 1])
        current_top = min(cell.source_bbox[1] for cell in row_cells[first])
        if current_top - previous_bottom > 24.0:
            break
        first -= 1
        expanded_headers += 1
    return first, last


def _refine_semantic_subcolumns(template: TableTemplate, facts: PageFacts) -> TableTemplate:
    fact_by_id = {item.object_id: item for item in facts.text_objects}
    candidates: list[float] = []
    for cell in template.cells:
        if cell.table_id != template.structure.table_id or not cell.translatable:
            continue
        objects = sorted(
            (fact_by_id[object_id] for object_id in cell.source_object_ids),
            key=lambda item: (item.bbox[0], item.bbox[1]),
        )
        for left, right in zip(objects, objects[1:]):
            gap = right.bbox[0] - left.bbox[2]
            if gap >= max(24.0, median((left.font_size, right.font_size)) * 5.0):
                candidates.append((left.bbox[2] + right.bbox[0]) / 2.0)
    clusters: list[list[float]] = []
    for value in sorted(candidates):
        if clusters and value - median(clusters[-1]) <= 8.0:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    additions = tuple(
        round(median(cluster), 4)
        for cluster in clusters
        if len(cluster) >= 3
        and all(abs(median(cluster) - boundary) > 12.0 for boundary in template.structure.column_boundaries)
    )
    if not additions:
        return template

    old_boundaries = template.structure.column_boundaries
    new_boundaries = tuple(sorted((*old_boundaries, *additions)))
    cells = []
    for cell in template.cells:
        if cell.table_id != template.structure.table_id:
            cells.append(cell)
            continue
        old_left = old_boundaries[cell.column_index]
        old_right = old_boundaries[cell.column_index + cell.column_span]
        start = new_boundaries.index(old_left)
        end = new_boundaries.index(old_right)
        objects = tuple(fact_by_id[object_id] for object_id in cell.source_object_ids)
        by_column: dict[int, list] = {}
        for item in objects:
            center = (item.bbox[0] + item.bbox[2]) / 2.0
            column = next(
                index
                for index, (left, right) in enumerate(zip(new_boundaries, new_boundaries[1:]))
                if left - 0.1 <= center <= right + 0.1
            )
            by_column.setdefault(column, []).append(item)
        split_groups = {
            column: group for column, group in by_column.items() if start <= column < end
        }
        if len(split_groups) <= 1:
            cells.append(
                replace(
                    cell,
                    container_id=f"{template.structure.table_id}-r{cell.row_index:03d}-c{start:02d}-s{end - start:02d}",
                    column_index=start,
                    column_span=end - start,
                    cell_bbox=(new_boundaries[start], cell.cell_bbox[1], new_boundaries[end], cell.cell_bbox[3]),
                )
            )
            continue
        for column, group in sorted(split_groups.items()):
            source_bbox = _union(tuple(item.bbox for item in group))
            source_text = _merge_objects(group)
            cells.append(
                replace(
                    cell,
                    container_id=f"{template.structure.table_id}-r{cell.row_index:03d}-c{column:02d}-s01",
                    column_index=column,
                    column_span=1,
                    source_object_ids=tuple(item.object_id for item in group),
                    source_text=source_text,
                    source_bbox=source_bbox,
                    cell_bbox=(new_boundaries[column], cell.cell_bbox[1], new_boundaries[column + 1], cell.cell_bbox[3]),
                    protected_tokens=protected_tokens(source_text),
                    font_size=round(max(item.font_size for item in group), 4),
                    color_srgb=max(group, key=lambda item: (item.font_size, len(item.text))).color_srgb,
                    alignment="left",
                )
            )
    cells.sort(key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    cells = [replace(item, reading_order=index) for index, item in enumerate(cells)]
    structure = replace(
        template.structure,
        column_boundaries=new_boundaries,
        structure_sha256=canonical_sha256(
            {
                "table_id": template.structure.table_id,
                "column_boundaries": new_boundaries,
                "row_boundaries": template.structure.row_boundaries,
                "direct_evidence": (*template.structure.direct_evidence, "repeated_semantic_subcolumns"),
            }
        ),
        direct_evidence=tuple(sorted((*template.structure.direct_evidence, "repeated_semantic_subcolumns"))),
    )
    return replace(template, structure=structure, cells=tuple(cells))


def _merge_objects(objects) -> str:
    lines: list[list] = []
    for item in sorted(objects, key=lambda value: (value.bbox[1], value.bbox[0])):
        if lines and abs(lines[-1][0].bbox[1] - item.bbox[1]) <= max(lines[-1][0].font_size, item.font_size) * 0.45:
            lines[-1].append(item)
        else:
            lines.append([item])
    return "\n".join("".join(item.text for item in line).strip() for line in lines)


def _merge_stacked_table_headers(template: TableTemplate, facts: PageFacts) -> TableTemplate:
    auxiliary = [cell for cell in template.cells if cell.table_id == "page-aux" and cell.role == "page_heading"]
    headers = [
        cell
        for cell in template.cells
        if cell.table_id == template.structure.table_id and cell.row_index == 0 and cell.translatable
    ]
    if not auxiliary or not headers:
        return template
    fact_by_id = {item.object_id: item for item in facts.text_objects}
    replacements = {}
    consumed = set()
    boundaries = template.structure.column_boundaries
    for aux in auxiliary:
        center = (aux.source_bbox[0] + aux.source_bbox[2]) / 2.0
        column = _column_for_center(center, boundaries)
        candidates = [
            cell
            for cell in headers
            if cell.column_index <= column < cell.column_index + cell.column_span
            and 0.0 <= cell.source_bbox[1] - aux.source_bbox[3] <= max(18.0, cell.font_size * 2.5)
        ]
        if not candidates:
            continue
        header = min(candidates, key=lambda cell: cell.source_bbox[1] - aux.source_bbox[3])
        objects = [
            *(fact_by_id[object_id] for object_id in aux.source_object_ids),
            *(fact_by_id[object_id] for object_id in header.source_object_ids),
        ]
        source_text = _merge_objects(objects)
        replacements[header.container_id] = replace(
            header,
            source_object_ids=tuple(item.object_id for item in objects),
            source_text=source_text,
            source_bbox=_union(tuple(item.bbox for item in objects)),
            cell_bbox=(
                boundaries[header.column_index],
                min(aux.source_bbox[1], header.cell_bbox[1]),
                boundaries[header.column_index + header.column_span],
                header.cell_bbox[3],
            ),
            protected_tokens=protected_tokens(source_text),
            font_size=round(max(item.font_size for item in objects), 4),
        )
        consumed.add(aux.container_id)
    if not replacements:
        return template
    cells = [
        replacements.get(cell.container_id, cell)
        for cell in template.cells
        if cell.container_id not in consumed
    ]
    cells.sort(key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    cells = tuple(replace(item, reading_order=index) for index, item in enumerate(cells))
    return replace(template, cells=cells)


def _extend_trailing_structured_rows(template: TableTemplate, facts: PageFacts) -> TableTemplate:
    main_cells = tuple(
        cell for cell in template.cells if cell.table_id == template.structure.table_id
    )
    if not main_cells:
        return template
    owned_ids = {
        object_id for cell in template.cells for object_id in cell.source_object_ids
    }
    last_source_bottom = max(cell.source_bbox[3] for cell in main_cells)
    bbox = template.structure.bbox
    trailing = [
        item
        for item in facts.text_objects
        if item.object_id not in owned_ids
        and item.bbox[1] > last_source_bottom + 0.5
        and item.bbox[3] <= bbox[3] + 0.5
        and item.bbox[0] < bbox[2]
        and item.bbox[2] > bbox[0]
    ]
    rows: list[list] = []
    for item in sorted(trailing, key=lambda value: (value.bbox[1], value.bbox[0])):
        if rows and abs(rows[-1][0].bbox[1] - item.bbox[1]) <= max(rows[-1][0].font_size, item.font_size) * 0.45:
            rows[-1].append(item)
        else:
            rows.append([item])
    boundaries = template.structure.column_boundaries
    structured_rows = []
    for row in rows:
        columns = {
            _column_for_center((item.bbox[0] + item.bbox[2]) / 2.0, boundaries)
            for item in row
        }
        numeric_count = sum(any(character.isdigit() for character in item.text) for item in row)
        if len(columns) >= 2 and numeric_count >= 2:
            structured_rows.append(row)
    if not structured_rows:
        return template

    existing_last_boundary = template.structure.row_boundaries[-1]
    row_boundaries = list(template.structure.row_boundaries[:-1])
    previous_bottom = max(cell.source_bbox[3] for cell in main_cells if cell.row_index == max(item.row_index for item in main_cells))
    for row in structured_rows:
        row_boundaries.append(round((previous_bottom + min(item.bbox[1] for item in row)) / 2.0, 4))
        previous_bottom = max(item.bbox[3] for item in row)
    row_boundaries.append(existing_last_boundary)

    cells = [
        replace(
            cell,
            cell_bbox=(
                cell.cell_bbox[0],
                row_boundaries[cell.row_index],
                cell.cell_bbox[2],
                row_boundaries[cell.row_index + cell.row_span],
            ),
        )
        if cell.table_id == template.structure.table_id
        else cell
        for cell in template.cells
    ]
    first_row_index = max(cell.row_index for cell in main_cells) + 1
    for offset, row in enumerate(structured_rows):
        row_index = first_row_index + offset
        by_column: dict[int, list] = {}
        for item in row:
            column = _column_for_center((item.bbox[0] + item.bbox[2]) / 2.0, boundaries)
            by_column.setdefault(column, []).append(item)
        for column, group in sorted(by_column.items()):
            source_text = _merge_objects(group)
            translatable = not all(is_protected_text(item.text) for item in group)
            representative = max(group, key=lambda item: (item.font_size, len(item.text)))
            cells.append(
                replace(
                    main_cells[0],
                    container_id=f"{template.structure.table_id}-r{row_index:03d}-c{column:02d}-s01",
                    row_index=row_index,
                    column_index=column,
                    row_span=1,
                    column_span=1,
                    source_object_ids=tuple(item.object_id for item in group),
                    source_text=source_text,
                    source_bbox=_union(tuple(item.bbox for item in group)),
                    cell_bbox=(boundaries[column], row_boundaries[row_index], boundaries[column + 1], row_boundaries[row_index + 1]),
                    role="table_body",
                    translatable=translatable,
                    protected_tokens=protected_tokens(source_text) if translatable else (),
                    font_size=round(max(item.font_size for item in group), 4),
                    color_srgb=representative.color_srgb,
                    font_weight="bold" if "bold" in representative.font_name.lower() else "regular",
                    alignment="left" if column == 0 else "right",
                )
            )
    cells.sort(key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    cells = [replace(item, reading_order=index) for index, item in enumerate(cells)]
    evidence = tuple(sorted((*template.structure.direct_evidence, "trailing_structured_rows")))
    structure = replace(
        template.structure,
        row_boundaries=tuple(row_boundaries),
        direct_evidence=evidence,
        structure_sha256=canonical_sha256(
            {
                "table_id": template.structure.table_id,
                "column_boundaries": boundaries,
                "row_boundaries": row_boundaries,
                "direct_evidence": evidence,
            }
        ),
    )
    protected_object_ids = tuple(
        object_id for cell in cells if not cell.translatable for object_id in cell.source_object_ids
    )
    return replace(
        template,
        structure=structure,
        cells=tuple(cells),
        protected_object_ids=protected_object_ids,
    )


def _column_for_center(value: float, boundaries: tuple[float, ...]) -> int:
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        if left - 0.1 <= value <= right + 0.1:
            return index
    return 0 if value < boundaries[0] else len(boundaries) - 2


def _build_containers(chart_containers, table_template, facts: PageFacts, table_region_id: str):
    table_object_ids = {
        object_id for cell in table_template.cells for object_id in cell.source_object_ids
    }
    fact_by_id = {item.object_id: item for item in facts.text_objects}
    containers: list[ChartTableTextContainer] = []
    for container in chart_containers:
        remaining_ids = tuple(
            object_id for object_id in container.source_object_ids if object_id not in table_object_ids
        )
        if not remaining_ids:
            continue
        sliced = container if remaining_ids == container.source_object_ids else _slice_chart_container(
            container,
            remaining_ids,
            fact_by_id,
        )
        first_table_top = min(cell.source_bbox[1] for cell in table_template.cells if cell.table_id == table_template.structure.table_id)
        near_table_heading = (
            sliced.source_bbox[3] <= first_table_top
            and sliced.source_bbox[3] >= table_template.structure.bbox[1] - max(24.0, sliced.font_size * 2.0)
        )
        owner = (
            "shared"
            if sliced.role in {"PAGE_HEADER", "PAGE_FOOTER"} or near_table_heading
            else "chart"
        )
        containers.append(ChartTableTextContainer.from_chart(sliced, owner))

    for cell in table_template.translatable_cells:
        objects = tuple(fact_by_id[object_id] for object_id in cell.source_object_ids)
        representative = max(objects, key=lambda item: (item.font_size, len(item.text.strip())))
        containers.append(
            ChartTableTextContainer(
                container_id=f"p16-{cell.container_id}",
                owner="table",
                role=_table_role(cell.role),
                association_id=table_region_id,
                source_object_ids=cell.source_object_ids,
                source_text=cell.source_text,
                source_bbox=cell.source_bbox,
                allowed_bbox=cell.cell_bbox,
                anchor_object_ids=(),
                anchor_relation="OVERLAY",
                reading_order=cell.reading_order,
                required_literals=cell.protected_tokens,
                font_name=representative.font_name,
                font_size=cell.font_size,
                color_srgb=cell.color_srgb,
                alignment=cell.alignment.upper(),
            )
        )

    containers.sort(key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    containers = [replace(item, reading_order=index) for index, item in enumerate(containers)]
    editable_ids = {
        object_id for container in containers for object_id in container.source_object_ids
    }
    protected_object_ids = tuple(
        item.object_id for item in facts.text_objects if item.object_id not in editable_ids
    )
    return tuple(containers), protected_object_ids


def _slice_chart_container(container, object_ids, fact_by_id):
    objects = tuple(fact_by_id[object_id] for object_id in object_ids)
    ordered = sorted(objects, key=lambda item: (item.block_index, item.line_index, item.span_index))
    source_bbox = _union(tuple(item.bbox for item in ordered))
    return replace(
        container,
        source_object_ids=tuple(item.object_id for item in ordered),
        source_text=" ".join(item.text.strip() for item in ordered if item.text.strip()),
        source_bbox=source_bbox,
        allowed_bbox=source_bbox,
        anchor_object_ids=(),
        required_literals=(),
    )


def _table_role(role: str) -> str:
    if role == "table_header":
        return "TABLE_HEADER"
    if role in {"merged_section_header", "table_section"}:
        return "TABLE_SECTION"
    if role == "table_total":
        return "TABLE_TOTAL"
    return "TABLE_CELL"


def _validate_total_ownership(containers, protected_object_ids, facts: PageFacts) -> None:
    owned = [object_id for item in containers for object_id in item.source_object_ids]
    owned.extend(protected_object_ids)
    expected = [item.object_id for item in facts.text_objects]
    if sorted(owned) != sorted(expected) or len(owned) != len(set(owned)):
        raise ChartTableCapabilityError("COMPOSITE_TEXT_OWNERSHIP_NOT_TOTAL")


def _center_in(inner: Rect, outer: Rect) -> bool:
    x = (inner[0] + inner[2]) / 2.0
    y = (inner[1] + inner[3]) / 2.0
    return outer[0] - 1.0 <= x <= outer[2] + 1.0 and outer[1] - 1.0 <= y <= outer[3] + 1.0


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _union(rects: tuple[Rect, ...]) -> Rect:
    return (
        round(min(item[0] for item in rects), 4),
        round(min(item[1] for item in rects), 4),
        round(max(item[2] for item in rects), 4),
        round(max(item[3] for item in rects), 4),
    )
