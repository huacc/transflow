"""
tool_name: trailing_postlude_ownership_rule
category: validators
input_contract: one repaired MultiColumnTemplate
output_contract: PASS or one trailing_single_flow_owned_by_column finding
failure_signals: after the shared multi-column band ends, smaller note text starts a trailing single flow but remains owned by one column
fallback: keep ownership unchanged and require focused adjudication
anti_overfit_statement: evidence uses only current-page relative font, vertical band and column ownership; no sample id, text literal, page number or fixed bbox is encoded
"""

from __future__ import annotations

from statistics import median

from ..models import MultiColumnTemplate


def evaluate_trailing_postlude_ownership(
    *,
    template: MultiColumnTemplate,
) -> dict[str, object]:
    """识别局部多栏结束后被误归入左栏或右栏的页尾注释流。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    by_column = {
        column.column_id: [
            item for item in template.containers
            if assignment[item.container_id] == column.column_id
        ]
        for column in template.columns
    }
    if any(not values for values in by_column.values()):
        return _pass()
    shared_bottom = min(max(item.source_bbox[3] for item in values) for values in by_column.values())
    shared_values = [
        item for values in by_column.values() for item in values
        if item.source_bbox[1] <= shared_bottom
    ]
    if not shared_values:
        return _pass()
    body_scale = median(item.font_size for item in shared_values)
    transition_candidates = [
        item for values in by_column.values() for item in values
        if item.source_bbox[1] > shared_bottom + item.font_size * 1.20
        and item.font_size <= body_scale * 0.90
    ]
    if not transition_candidates:
        return _pass()
    transition_y = min(item.source_bbox[1] for item in transition_candidates)
    promote = [
        item.container_id for values in by_column.values() for item in values
        if item.source_bbox[1] >= transition_y
    ]
    return {
        "rule_verdict": "FAIL",
        "selected_failure_class": "trailing_single_flow_owned_by_column",
        "repair_atom": "promote_trailing_column_items_to_postlude",
        "container_ids": promote,
        "evidence": {
            "shared_multi_column_bottom": round(shared_bottom, 4),
            "postlude_transition_y": round(transition_y, 4),
            "shared_body_font_scale": round(body_scale, 4),
            "smaller_note_transition_proved": True,
        },
    }


def _pass() -> dict[str, object]:
    return {
        "rule_verdict": "PASS",
        "selected_failure_class": None,
        "repair_atom": None,
    }
