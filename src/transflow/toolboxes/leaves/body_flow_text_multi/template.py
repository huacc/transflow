"""Build source-derived column bands without sample or file identity checks."""

from __future__ import annotations

import re
from dataclasses import replace
from itertools import pairwise

from transflow.domain.text_inventory import (
    InventoryDisposition,
    PageTextInventoryItem,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.leaves.body_flow_text_multi.models import (
    ColumnAssignment,
    ColumnBand,
    MultiColumnTemplate,
    MultiTextContainer,
)
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    SingleTextContainer,
)
from transflow.toolboxes.leaves.body_flow_text_single.template import build_containers
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

TOOLBOX_KEY = "body.flow_text.multi"
SENTENCE_END = re.compile(r"[。！？.!?:：；;]\s*$")


def build_multi_column_template(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> MultiColumnTemplate:
    """Lift the Spike column-band model onto production Kernel facts."""

    inventory = {
        item.object_id: item
        for item in freeze_page_text_inventory(
            facts,
            target_language=policy.target_language,
        ).items
    }
    source = tuple(
        _adapt(item, inventory) for item in build_containers(facts, policy)
    )
    body = tuple(item for item in source if item.role != "margin")
    evidence = [
        item
        for item in body
        if item.role in {"body", "list"}
        and len(item.source_text) >= 20
        and _width(item) <= facts.page.width_points * 0.52
    ]
    clusters = _cluster_column_starts(evidence, facts.page.width_points)
    if len(clusters) not in {2, 3}:
        evidence = [
            item
            for item in body
            if len(item.source_text) >= 3
            and _width(item) <= facts.page.width_points * 0.52
        ]
        clusters = _cluster_column_starts(evidence, facts.page.width_points)
    if len(clusters) not in {2, 3}:
        return _unsupported_template(facts, source, body)

    clusters = _active_column_groups(clusters)
    columns = _build_column_bands(clusters, facts)
    anchors = {
        column.column_id: _weighted_anchor(clusters[index])
        for index, column in enumerate(columns)
    }
    assigned: dict[str, list[MultiTextContainer]] = {
        column.column_id: [] for column in columns
    }
    spans: list[MultiTextContainer] = []
    margins: list[MultiTextContainer] = []
    ambiguous: list[str] = []
    for container in source:
        if container.role == "margin":
            margins.append(container)
            continue
        column_id = _assign_column(
            container,
            columns,
            anchors,
            facts.page.width_points,
        )
        if column_id == "span":
            spans.append(container)
            if not _has_direct_spanning_geometry(
                container,
                columns,
                facts.page.width_points,
            ):
                ambiguous.append(container.container_id)
            continue
        assigned[column_id].append(container)

    if any(not values for values in assigned.values()):
        return _unsupported_template(facts, source, body)

    merged_by_column = {
        column_id: _merge_flow_containers(tuple(sorted(values, key=_position)))
        for column_id, values in assigned.items()
    }
    columns = _reconcile_columns(columns, merged_by_column, facts)
    if not columns:
        return _unsupported_template(facts, source, body)
    first_column_top = min(column.content_top for column in columns)
    top_spans = sorted(
        (
            item
            for item in spans
            if item.source_bbox[1]
            <= first_column_top + facts.page.height_points * 0.04
        ),
        key=_position,
    )
    late_spans = sorted(
        (item for item in spans if item not in top_spans),
        key=_position,
    )

    ordered: list[MultiTextContainer] = list(top_spans)
    assignments: list[ColumnAssignment] = []
    for column in columns:
        values = merged_by_column[column.column_id]
        for index, container in enumerate(values):
            assignments.append(
                ColumnAssignment(container.container_id, column.column_id, index)
            )
        ordered.extend(values)
    for index, container in enumerate((*top_spans, *late_spans)):
        assignments.append(ColumnAssignment(container.container_id, "span", index))
    ordered.extend(late_spans)
    for index, container in enumerate(sorted(margins, key=_position)):
        assignments.append(ColumnAssignment(container.container_id, "margin", index))
        ordered.append(container)

    ordered = [
        replace(container, reading_order=index)
        for index, container in enumerate(ordered)
    ]
    assignment_by_id = {item.container_id: item for item in assignments}
    return MultiColumnTemplate(
        facts.page_identity,
        TOOLBOX_KEY,
        facts.page.width_points,
        facts.page.height_points,
        columns,
        tuple(ordered),
        tuple(assignment_by_id[item.container_id] for item in ordered),
        tuple(sorted(ambiguous)),
    )


def _adapt(
    container: SingleTextContainer,
    inventory: dict[str, PageTextInventoryItem],
) -> MultiTextContainer:
    translation_object_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory[object_id].disposition is InventoryDisposition.TRANSLATE
    )
    inline_keep_source_object_ids = tuple(
        object_id
        for object_id in container.source_object_ids
        if inventory[object_id].disposition is InventoryDisposition.KEEP_SOURCE
    )
    return MultiTextContainer(
        container_id=container.container_id,
        source_object_ids=container.source_object_ids,
        translation_object_ids=translation_object_ids,
        inline_keep_source_object_ids=inline_keep_source_object_ids,
        source_rects=container.source_rects,
        source_text=container.source_text,
        reading_order=container.reading_order,
        role=container.role,
        source_bbox=container.source_bbox,
        font_size=container.font_size,
        color_srgb=container.color_srgb,
        preferred_line_height=container.preferred_line_height,
        preserved_prefix=container.preserved_prefix,
    )


def _unsupported_template(
    facts: ExtractedPageFacts,
    source: tuple[MultiTextContainer, ...],
    body: tuple[MultiTextContainer, ...],
) -> MultiColumnTemplate:
    assignments = tuple(
        ColumnAssignment(item.container_id, "unsupported", index)
        for index, item in enumerate(source)
    )
    return MultiColumnTemplate(
        facts.page_identity,
        TOOLBOX_KEY,
        facts.page.width_points,
        facts.page.height_points,
        (),
        source,
        assignments,
        tuple(item.container_id for item in body),
    )


def _cluster_column_starts(
    containers: list[MultiTextContainer],
    page_width: float,
) -> list[list[MultiTextContainer]]:
    threshold = max(18.0, page_width * 0.075)
    clusters: list[list[MultiTextContainer]] = []
    for container in sorted(containers, key=lambda item: item.source_bbox[0]):
        target = min(
            clusters,
            key=lambda group: abs(container.source_bbox[0] - _weighted_anchor(group)),
            default=None,
        )
        if (
            target is None
            or abs(container.source_bbox[0] - _weighted_anchor(target)) > threshold
        ):
            clusters.append([container])
        else:
            target.append(container)
    total_weight = sum(_cluster_weight(group) for group in clusters)
    material = [
        group
        for group in clusters
        if _cluster_weight(group) >= total_weight * 0.03
    ]
    if len(material) > 3:
        material = sorted(material, key=_cluster_weight, reverse=True)[:3]
    return sorted(material, key=_weighted_anchor)


def _active_column_groups(
    clusters: list[list[MultiTextContainer]],
) -> list[list[MultiTextContainer]]:
    """Discard page prelude evidence before two columns become jointly active."""

    aligned_tops: list[float] = []
    for left_index, left_group in enumerate(clusters):
        for right_group in clusters[left_index + 1 :]:
            for left in left_group:
                for right in right_group:
                    vertical_gap = max(
                        0.0,
                        max(left.source_bbox[1], right.source_bbox[1])
                        - min(left.source_bbox[3], right.source_bbox[3]),
                    )
                    if vertical_gap <= max(left.font_size, right.font_size) * 2.0:
                        aligned_tops.append(
                            min(left.source_bbox[1], right.source_bbox[1])
                        )
    if not aligned_tops:
        return clusters
    active_top = min(aligned_tops)
    active = [
        [
            item
            for item in group
            if item.source_bbox[3] >= active_top - item.font_size * 2.0
        ]
        for group in clusters
    ]
    active = [group for group in active if group]
    return active if len(active) in {2, 3} else clusters


def _build_column_bands(
    clusters: list[list[MultiTextContainer]],
    facts: ExtractedPageFacts,
) -> tuple[ColumnBand, ...]:
    anchors = [_weighted_anchor(group) for group in clusters]
    output: list[ColumnBand] = []
    for index, group in enumerate(clusters):
        left = min(item.source_bbox[0] for item in group)
        right_values = sorted(item.source_bbox[2] for item in group)
        if index + 1 < len(clusters):
            bounded = [value for value in right_values if value < anchors[index + 1]]
            usable = bounded or [(anchors[index] + anchors[index + 1]) / 2.0]
        else:
            usable = right_values
        right = usable[min(len(usable) - 1, round((len(usable) - 1) * 0.90))]
        top = min(item.source_bbox[1] for item in group)
        bottom = _external_content_bottom(left, right, top, facts)
        output.append(
            ColumnBand(
                f"column-{index + 1}",
                index,
                round(max(4.0, left), 4),
                round(min(facts.page.width_points - 4.0, right), 4),
                round(top, 4),
                round(bottom, 4),
            )
        )
    return tuple(output)


def _external_content_bottom(
    left: float,
    right: float,
    top: float,
    facts: ExtractedPageFacts,
) -> float:
    guards = [
        item.bbox[1]
        for item in facts.text_spans
        if item.bbox[1] >= facts.page.height_points * 0.90
    ]
    locked_bboxes = tuple(item.bbox for item in facts.image_objects) + tuple(
        item.bbox for item in facts.drawing_objects
    )
    for x0, y0, x1, y1 in locked_bboxes:
        area_ratio = ((x1 - x0) * (y1 - y0)) / max(
            facts.page.width_points * facts.page.height_points,
            1.0,
        )
        horizontal_overlap = max(0.0, min(right, x1) - max(left, x0))
        if (
            area_ratio < 0.45
            and y0 > top
            and horizontal_overlap >= (right - left) * 0.30
        ):
            guards.append(y0)
    return min(guards) - 4.0 if guards else facts.page.height_points - 20.0


def _assign_column(
    container: MultiTextContainer,
    columns: tuple[ColumnBand, ...],
    anchor_by_id: dict[str, float],
    page_width: float,
) -> str:
    x0, _, x1, _ = container.source_bbox
    width = x1 - x0
    if any(
        x0 < previous.right and x1 > current.left
        for previous, current in pairwise(columns)
    ):
        return "span"
    overlaps = [
        (
            column.column_id,
            max(0.0, min(x1, column.right) - max(x0, column.left)),
        )
        for column in columns
    ]
    material = [item for item in overlaps if item[1] >= max(4.0, width * 0.22)]
    if width >= page_width * 0.60 or len(material) >= 2:
        return "span"
    return min(
        columns,
        key=lambda column: abs(x0 - anchor_by_id[column.column_id]),
    ).column_id


def _has_direct_spanning_geometry(
    container: MultiTextContainer,
    columns: tuple[ColumnBand, ...],
    page_width: float,
) -> bool:
    x0, _, x1, _ = container.source_bbox
    return (x1 - x0) >= page_width * 0.60 or any(
        x0 < previous.right and x1 > current.left
        for previous, current in pairwise(columns)
    )


def _reconcile_columns(
    columns: tuple[ColumnBand, ...],
    assigned: dict[str, tuple[MultiTextContainer, ...]],
    facts: ExtractedPageFacts,
) -> tuple[ColumnBand, ...]:
    updated: list[ColumnBand] = []
    for column in columns:
        values = assigned[column.column_id]
        updated.append(
            replace(
                column,
                left=round(min(item.source_bbox[0] for item in values), 4),
                right=round(max(item.source_bbox[2] for item in values), 4),
                content_top=round(min(item.source_bbox[1] for item in values), 4),
                content_bottom=round(
                    max(
                        max(item.source_bbox[3] for item in values),
                        _external_content_bottom(
                            min(item.source_bbox[0] for item in values),
                            max(item.source_bbox[2] for item in values),
                            min(item.source_bbox[1] for item in values),
                            facts,
                        ),
                    ),
                    4,
                ),
            )
        )
    if any(
        previous.right >= current.left
        for previous, current in pairwise(updated)
    ):
        return ()
    return tuple(updated)


def _merge_flow_containers(
    containers: tuple[MultiTextContainer, ...],
) -> tuple[MultiTextContainer, ...]:
    merged: list[MultiTextContainer] = []
    for container in containers:
        if merged and _can_merge_flow_lines(merged[-1], container):
            previous = merged[-1]
            left = previous.source_text.rstrip()
            right = container.source_text.lstrip()
            if left.endswith("-") and right[:1].islower():
                text = left[:-1] + right
            else:
                separator = "" if _is_han(left[-1:]) and _is_han(right[:1]) else " "
                text = left + separator + right
            merged[-1] = replace(
                previous,
                source_object_ids=(
                    previous.source_object_ids + container.source_object_ids
                ),
                translation_object_ids=(
                    previous.translation_object_ids
                    + container.translation_object_ids
                ),
                inline_keep_source_object_ids=(
                    previous.inline_keep_source_object_ids
                    + container.inline_keep_source_object_ids
                ),
                source_rects=previous.source_rects + container.source_rects,
                source_text=text,
                source_bbox=(
                    min(previous.source_bbox[0], container.source_bbox[0]),
                    min(previous.source_bbox[1], container.source_bbox[1]),
                    max(previous.source_bbox[2], container.source_bbox[2]),
                    max(previous.source_bbox[3], container.source_bbox[3]),
                ),
            )
        else:
            merged.append(container)
    return tuple(
        replace(container, reading_order=index)
        for index, container in enumerate(merged)
    )


def _can_merge_flow_lines(
    previous: MultiTextContainer,
    current: MultiTextContainer,
) -> bool:
    if previous.role not in {"body", "list"} or current.role not in {
        "body",
        "heading",
    }:
        return False
    if previous.role == "list" and len(previous.source_text) < 40:
        return False
    if (
        previous.color_srgb != current.color_srgb
        or abs(previous.font_size - current.font_size) > 0.35
    ):
        return False
    indent = current.source_bbox[0] - previous.source_bbox[0]
    if (
        indent < -max(6.0, previous.font_size * 0.75)
        or indent > max(30.0, previous.font_size * 4.0)
    ):
        return False
    gap = current.source_bbox[1] - previous.source_bbox[3]
    if not (-0.5 <= gap <= max(3.0, previous.font_size * 0.75)):
        return False
    return not bool(SENTENCE_END.search(previous.source_text))


def _cluster_weight(group: list[MultiTextContainer]) -> int:
    return sum(max(1, len(item.source_text)) for item in group)


def _weighted_anchor(group: list[MultiTextContainer]) -> float:
    weight = _cluster_weight(group)
    return sum(
        item.source_bbox[0] * max(1, len(item.source_text))
        for item in group
    ) / max(weight, 1)


def _width(container: MultiTextContainer) -> float:
    return container.source_bbox[2] - container.source_bbox[0]


def _position(container: MultiTextContainer) -> tuple[float, float]:
    return container.source_bbox[1], container.source_bbox[0]


def _is_han(value: str) -> bool:
    return bool(value and "\u3400" <= value <= "\u9fff")
