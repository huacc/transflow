"""
tool_name: trailing_postlude_reassignment
category: repair executor
input_contract: one MultiColumnTemplate and current-page container ids selected by the postlude ownership rule
output_contract: the same template with selected trailing items assigned to span
failure_signals: target missing or target is not currently column-owned
fallback: reject the patch and keep the previous template
anti_overfit_statement: selected ids and transition geometry come exclusively from the current rule decision
"""

from __future__ import annotations

from dataclasses import replace

from toolboxes.body.flow_text.single.tools.models import TextContainer

from ..models import ColumnAssignment, MultiColumnTemplate


def apply_trailing_postlude_reassignment(
    *,
    template: MultiColumnTemplate,
    container_ids: list[str],
) -> tuple[MultiColumnTemplate, dict[str, object]]:
    """只改变已证明属于页尾单流的栏归属，不修改文字、样式或坐标。"""

    selected = set(container_ids)
    assignment = {item.container_id: item.column_id for item in template.assignments}
    column_ids = {item.column_id for item in template.columns}
    if not selected or any(item not in assignment for item in selected):
        raise ValueError("trailing_postlude_target_missing")
    if any(assignment[item] not in column_ids for item in selected):
        raise ValueError("trailing_postlude_target_is_not_column_owned")

    spans = [
        item for item in template.containers
        if assignment[item.container_id] == "span" or item.container_id in selected
    ]
    structure_top = min(item.content_top for item in template.columns)
    top_spans = sorted(
        [item for item in spans if item.source_bbox[1] <= structure_top + template.height * 0.04],
        key=_position,
    )
    late_spans = sorted([item for item in spans if item not in top_spans], key=_position)
    fixed = sorted(
        [item for item in template.containers if assignment[item.container_id] == "fixed"],
        key=_position,
    )
    margins = sorted(
        [item for item in template.containers if assignment[item.container_id] == "margin"],
        key=_position,
    )

    ordered: list[TextContainer] = list(top_spans)
    assignments: list[ColumnAssignment] = []
    for column in template.columns:
        values = sorted(
            [
                item for item in template.containers
                if assignment[item.container_id] == column.column_id
                and item.container_id not in selected
            ],
            key=_position,
        )
        for index, item in enumerate(values):
            assignments.append(ColumnAssignment(item.container_id, column.column_id, index))
        ordered.extend(values)
    for index, item in enumerate(top_spans + late_spans):
        assignments.append(ColumnAssignment(item.container_id, "span", index))
    ordered.extend(late_spans)
    for index, item in enumerate(fixed):
        assignments.append(ColumnAssignment(item.container_id, "fixed", index))
        ordered.append(item)
    for index, item in enumerate(margins):
        assignments.append(ColumnAssignment(item.container_id, "margin", index))
        ordered.append(item)

    ordered = [replace(item, reading_order=index) for index, item in enumerate(ordered)]
    assignment_by_id = {item.container_id: item for item in assignments}
    repaired = replace(
        template,
        containers=tuple(ordered),
        assignments=tuple(assignment_by_id[item.container_id] for item in ordered),
        ambiguous_spanning_container_ids=tuple(
            item for item in template.ambiguous_spanning_container_ids if item not in selected
        ),
    )
    return repaired, {
        "operation_type": "promote_trailing_column_items_to_postlude",
        "status": "applied",
        "container_ids": sorted(selected),
        "hard_constraints": {
            "source_text_unchanged": True,
            "source_bboxes_unchanged": True,
            "horizontal_geometry_unchanged": True,
        },
    }


def _position(container: TextContainer) -> tuple[float, float]:
    return container.source_bbox[1], container.source_bbox[0]
