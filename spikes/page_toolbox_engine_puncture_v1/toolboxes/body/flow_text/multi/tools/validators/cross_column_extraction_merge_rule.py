"""
tool_name: cross_column_extraction_merge_rule
category: validators
input_contract: current PageFacts and one initial MultiColumnTemplate
output_contract: PASS or one cross_column_extraction_merge finding
failure_signals: one span container is made only of source objects that each belong to one distinct column
fallback: leave the container unchanged and route ambiguous geometry to focused adjudication
anti_overfit_statement: the rule uses current-page object/column overlap only; no sample id, text literal, page number, bbox, or fixed absolute coordinate is encoded
"""

from __future__ import annotations

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact

from ..models import ColumnBand, MultiColumnTemplate


def evaluate_cross_column_extraction_merge(
    *,
    facts: PageFacts,
    template: MultiColumnTemplate,
) -> dict[str, object]:
    """找出一个被 PDF 提取层误合并、但视觉上分别属于不同栏的容器。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    source_by_id = {item.object_id: item for item in facts.text_objects}
    for container in sorted(template.containers, key=lambda item: item.reading_order):
        if assignment.get(container.container_id) != "span" or len(container.source_object_ids) < 2:
            continue
        groups: dict[str, list[str]] = {}
        valid = True
        for object_id in container.source_object_ids:
            source_object = source_by_id.get(object_id)
            column_id = _unique_column(source_object, template.columns) if source_object else None
            if column_id is None:
                valid = False
                break
            groups.setdefault(column_id, []).append(object_id)
        if valid and len(groups) >= 2 and _groups_are_visually_separate(
            groups=groups,
            source_by_id=source_by_id,
            columns=template.columns,
        ):
            return {
                "rule_verdict": "FAIL",
                "selected_failure_class": "cross_column_extraction_merge",
                "repair_atom": "split_container_by_source_column",
                "container_id": container.container_id,
                "source_object_ids_by_column": groups,
                "evidence": {
                    "source_object_count": len(container.source_object_ids),
                    "distinct_column_count": len(groups),
                    "all_source_objects_have_unique_column_ownership": True,
                },
            }
    return {
        "rule_verdict": "PASS",
        "selected_failure_class": None,
        "repair_atom": None,
    }


def _unique_column(
    source_object: TextObjectFact,
    columns: tuple[ColumnBand, ...],
) -> str | None:
    x0, _, x1, _ = source_object.bbox
    width = max(x1 - x0, 0.01)
    owners = [
        column.column_id
        for column in columns
        if max(0.0, min(x1, column.right) - max(x0, column.left)) / width >= 0.80
    ]
    return owners[0] if len(owners) == 1 else None


def _groups_are_visually_separate(
    *,
    groups: dict[str, list[str]],
    source_by_id: dict[str, TextObjectFact],
    columns: tuple[ColumnBand, ...],
) -> bool:
    ordered = [column for column in columns if column.column_id in groups]
    for left, right in zip(ordered, ordered[1:]):
        left_objects = [source_by_id[object_id] for object_id in groups[left.column_id]]
        right_objects = [source_by_id[object_id] for object_id in groups[right.column_id]]
        left_lines = {(item.block_index, item.line_index) for item in left_objects}
        right_lines = {(item.block_index, item.line_index) for item in right_objects}
        if not left_lines & right_lines:
            continue
        visual_gap = min(item.bbox[0] for item in right_objects) - max(item.bbox[2] for item in left_objects)
        font_scale = max(item.font_size for item in (*left_objects, *right_objects))
        source_gutter = max(0.0, right.left - left.right)
        if visual_gap < max(font_scale * 0.50, source_gutter * 0.50):
            return False
    return True
