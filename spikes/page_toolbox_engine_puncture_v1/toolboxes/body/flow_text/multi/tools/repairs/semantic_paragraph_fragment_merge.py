"""
tool_name: semantic_paragraph_fragment_merge
category: repair executor
input_contract: one MultiColumnTemplate and one rule-selected adjacent container pair
output_contract: one merged source-language semantic container with unchanged owner and union source bbox
failure_signals: target missing, owners differ, or containers are not adjacent in template order
fallback: reject the patch and keep the previous template
anti_overfit_statement: pair ids, text, style and geometry all come from current-page rule evidence
"""

from __future__ import annotations

from dataclasses import replace

from ..models import ColumnAssignment, MultiColumnTemplate


def apply_semantic_paragraph_fragment_merge(
    *,
    template: MultiColumnTemplate,
    previous_container_id: str,
    current_container_id: str,
) -> tuple[MultiColumnTemplate, dict[str, object]]:
    """只合并一对同语义续行；其他容器、栏归属和横向几何不动。"""

    by_id = {item.container_id: item for item in template.containers}
    previous = by_id.get(previous_container_id)
    current = by_id.get(current_container_id)
    if previous is None or current is None:
        raise ValueError("semantic_fragment_merge_target_missing")
    owner = {item.container_id: item.column_id for item in template.assignments}
    if owner[previous_container_id] != owner[current_container_id]:
        raise ValueError("semantic_fragment_merge_owner_mismatch")
    positions = {item.container_id: index for index, item in enumerate(template.containers)}
    previous_position = positions[previous_container_id]
    current_position = positions[current_container_id]
    if current_position <= previous_position or any(
        owner[item.container_id] == owner[previous_container_id]
        for item in template.containers[previous_position + 1:current_position]
    ):
        raise ValueError("semantic_fragment_merge_targets_not_adjacent")

    merged_text = _join_source_text(previous.source_text, current.source_text)
    merged = replace(
        previous,
        source_object_ids=previous.source_object_ids + current.source_object_ids,
        source_text=merged_text,
        source_bbox=(
            min(previous.source_bbox[0], current.source_bbox[0]),
            min(previous.source_bbox[1], current.source_bbox[1]),
            max(previous.source_bbox[2], current.source_bbox[2]),
            max(previous.source_bbox[3], current.source_bbox[3]),
        ),
        anchor=(
            min(previous.source_bbox[0], current.source_bbox[0]),
            min(previous.source_bbox[1], current.source_bbox[1]),
        ),
    )
    containers = []
    for item in template.containers:
        if item.container_id == previous_container_id:
            containers.append(merged)
        elif item.container_id != current_container_id:
            containers.append(item)
    containers = [replace(item, reading_order=index) for index, item in enumerate(containers)]
    owner.pop(current_container_id)
    owner_order: dict[str, int] = {}
    assignments: list[ColumnAssignment] = []
    for item in containers:
        item_owner = owner[item.container_id]
        assignments.append(ColumnAssignment(item.container_id, item_owner, owner_order.get(item_owner, 0)))
        owner_order[item_owner] = owner_order.get(item_owner, 0) + 1
    repaired = replace(
        template,
        containers=tuple(containers),
        assignments=tuple(assignments),
        ambiguous_spanning_container_ids=tuple(
            item for item in template.ambiguous_spanning_container_ids
            if item != current_container_id
        ),
    )
    return repaired, {
        "operation_type": "merge_adjacent_same_owner_fragments",
        "status": "applied",
        "kept_container_id": previous_container_id,
        "removed_container_id": current_container_id,
        "owner": owner[previous_container_id],
        "source_object_count": len(merged.source_object_ids),
        "hard_constraints": {
            "owner_unchanged": True,
            "source_object_unique_ownership": True,
            "horizontal_bbox_is_source_union": True,
        },
    }


def _join_source_text(previous: str, current: str) -> str:
    left = previous.rstrip()
    right = current.lstrip()
    if left.endswith("-") and right[:1].islower():
        return left[:-1] + right
    if left.endswith(("-", "‐", "‑", "‒", "–", "—", "/")):
        return left + right
    separator = "" if _is_han(left[-1:]) and _is_han(right[:1]) else " "
    return left + separator + right


def _is_han(value: str) -> bool:
    return bool(value and "\u3400" <= value <= "\u9fff")
