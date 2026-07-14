"""
tool_name: cross_column_container_split
category: repair executor
input_contract: one MultiColumnTemplate plus one rule-selected span and its source-object column groups
output_contract: a template where each source group has unique column ownership
failure_signals: target/container/source object missing, group does not map to a real column, duplicate ownership
fallback: reject the patch; caller keeps the previous template
anti_overfit_statement: container ids, column ids, text and geometry all come from the current rule evidence and PageFacts
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from toolboxes.body.flow_text.single.tools.models import TextContainer

from ..models import ColumnAssignment, MultiColumnTemplate


def apply_cross_column_container_split(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
    container_id: str,
    source_object_ids_by_column: dict[str, list[str]],
) -> tuple[MultiColumnTemplate, dict[str, object]]:
    """只拆一个误合并容器；其他容器、栏边界和既有归属不动。"""

    target = next((item for item in template.containers if item.container_id == container_id), None)
    if target is None:
        raise ValueError("cross_column_split_target_missing")
    assignment = {item.container_id: item.column_id for item in template.assignments}
    if assignment.get(container_id) != "span":
        raise ValueError("cross_column_split_target_is_not_span")
    source_by_id = {item.object_id: item for item in facts.text_objects}
    column_ids = {item.column_id for item in template.columns}
    claimed: list[str] = []
    replacements: list[tuple[str, TextContainer]] = []
    column_top = min(item.content_top for item in template.columns)
    for column_id, object_ids in source_object_ids_by_column.items():
        if column_id not in column_ids or not object_ids:
            raise ValueError("cross_column_split_invalid_column_group")
        if any(object_id not in source_by_id for object_id in object_ids):
            raise ValueError("cross_column_split_source_object_missing")
        claimed.extend(object_ids)
        objects = [source_by_id[object_id] for object_id in object_ids]
        bbox = _bbox(objects)
        # 位于各栏正文起点之前的成对片段是栏标题；样式仍完全来自当前源对象。
        role = "heading" if bbox[3] <= column_top + max(item.font_size for item in objects) else target.role
        replacements.append(
            (
                column_id,
                TextContainer(
                    container_id=f"{container_id}--{column_id}",
                    source_object_ids=tuple(object_ids),
                    source_text=_merge_text(objects),
                    reading_order=target.reading_order,
                    role=role,
                    source_bbox=tuple(round(value, 4) for value in bbox),
                    anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                    font_size=round(max(item.font_size for item in objects), 4),
                    color_srgb=max(objects, key=lambda item: (item.font_size, len(item.text))).color_srgb,
                    font_weight=_font_weight(objects),
                    preserved_prefix=None,
                ),
            )
        )
    if len(claimed) != len(set(claimed)) or set(claimed) != set(target.source_object_ids):
        raise ValueError("cross_column_split_source_ownership_mismatch")

    target_order = target.reading_order
    remaining = [item for item in template.containers if item.container_id != container_id]
    span_before = [
        item for item in remaining
        if assignment[item.container_id] == "span" and item.reading_order < target_order
    ]
    span_after = [
        item for item in remaining
        if assignment[item.container_id] == "span" and item.reading_order > target_order
    ]
    margin_values = [
        item for item in remaining if assignment[item.container_id] == "margin"
    ]
    fixed_values = [
        item for item in remaining if assignment[item.container_id] == "fixed"
    ]
    column_values: dict[str, list[TextContainer]] = {
        column.column_id: [
            item for item in remaining if assignment[item.container_id] == column.column_id
        ]
        for column in template.columns
    }
    for column_id, replacement in replacements:
        column_values[column_id].append(replacement)

    ordered: list[TextContainer] = sorted(span_before, key=_position)
    new_assignments: list[ColumnAssignment] = []
    for column in template.columns:
        values = sorted(column_values[column.column_id], key=_position)
        for column_order, item in enumerate(values):
            new_assignments.append(ColumnAssignment(item.container_id, column.column_id, column_order))
        ordered.extend(values)
    all_spans = sorted(span_before + span_after, key=_position)
    for span_order, item in enumerate(all_spans):
        new_assignments.append(ColumnAssignment(item.container_id, "span", span_order))
    ordered.extend(sorted(span_after, key=_position))
    for fixed_order, item in enumerate(sorted(fixed_values, key=_position)):
        new_assignments.append(ColumnAssignment(item.container_id, "fixed", fixed_order))
        ordered.append(item)
    for margin_order, item in enumerate(sorted(margin_values, key=_position)):
        new_assignments.append(ColumnAssignment(item.container_id, "margin", margin_order))
        ordered.append(item)

    ordered = [replace(item, reading_order=index) for index, item in enumerate(ordered)]
    assignment_by_id = {item.container_id: item for item in new_assignments}
    updated_columns = tuple(
        replace(
            column,
            content_top=round(
                min(column.content_top, *(item.source_bbox[1] for item in column_values[column.column_id])),
                4,
            ),
        )
        for column in template.columns
    )
    repaired = replace(
        template,
        columns=updated_columns,
        containers=tuple(ordered),
        assignments=tuple(assignment_by_id[item.container_id] for item in ordered),
        ambiguous_spanning_container_ids=tuple(
            item for item in template.ambiguous_spanning_container_ids if item != container_id
        ),
    )
    return repaired, {
        "operation_type": "split_container_by_source_column",
        "status": "applied",
        "removed_container_id": container_id,
        "created_container_ids": [item.container_id for _, item in replacements],
        "source_object_ids_by_column": source_object_ids_by_column,
        "hard_constraints": {
            "source_object_unique_ownership": True,
            "source_bboxes_unchanged": True,
            "unrelated_container_assignments_unchanged": True,
        },
    }


def _bbox(objects: list[TextObjectFact]) -> tuple[float, float, float, float]:
    return (
        min(item.bbox[0] for item in objects),
        min(item.bbox[1] for item in objects),
        max(item.bbox[2] for item in objects),
        max(item.bbox[3] for item in objects),
    )


def _merge_text(objects: list[TextObjectFact]) -> str:
    lines: dict[int, list[TextObjectFact]] = defaultdict(list)
    for item in objects:
        lines[item.line_index].append(item)
    return " ".join(
        "".join(item.text for item in sorted(values, key=lambda item: item.span_index)).strip()
        for _, values in sorted(lines.items())
        if values
    ).strip()


def _font_weight(objects: list[TextObjectFact]) -> str:
    strong_names = ("bold", "semibold", "demi", "medium")
    total = sum(max(1, len(item.text.strip())) for item in objects)
    strong = sum(
        max(1, len(item.text.strip()))
        for item in objects
        if any(token in item.font_name.casefold() for token in strong_names)
    )
    return "bold" if strong * 2 >= total else "regular"


def _position(container: TextContainer) -> tuple[float, float]:
    return container.source_bbox[1], container.source_bbox[0]
