"""
tool_name: tolerant_multi_route_rule
category: routing validator
input_contract: one MultiColumnTemplate produced from the current page
output_contract: ACCEPT_STANDARD, ACCEPT_TOLERANT, or REQUIRE_FINE_GRAINED_ADJUDICATION
failure_signals: unsupported column count, empty column, unclear gutter, or ambiguous mid-flow span
fallback: stop before translation and request a focused second-stage routing adjudication
anti_overfit_statement: routing uses current template topology and relative geometry only; no sample id, text literal, page number or fixed bbox is encoded
"""

from __future__ import annotations

from ..models import MultiColumnTemplate


def evaluate_tolerant_multi_route(template: MultiColumnTemplate) -> dict[str, object]:
    """把上游分类当作路由先验，再用页内结构证据决定标准接收、容错接收或二次裁决。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    column_ids = {item.column_id for item in template.columns}
    empty_columns = [
        column_id for column_id in column_ids
        if not any(owner == column_id for owner in assignment.values())
    ]
    unclear_gutters = [
        f"{left.column_id}:{right.column_id}"
        for left, right in zip(template.columns, template.columns[1:])
        if left.right >= right.left
    ]
    hard_reasons: list[str] = []
    if len(template.columns) not in {2, 3}:
        hard_reasons.append("unsupported_column_count")
    if empty_columns:
        hard_reasons.append("empty_detected_column")
    if unclear_gutters:
        hard_reasons.append("unclear_column_gutter")
    if template.ambiguous_spanning_container_ids:
        hard_reasons.append("ambiguous_mid_flow_span")
    if hard_reasons:
        return {
            "route_verdict": "REQUIRE_FINE_GRAINED_ADJUDICATION",
            "upstream_route": "body.flow_text.multi",
            "matched_tolerance_modes": [],
            "hard_reasons": hard_reasons,
            "evidence": {
                "empty_column_ids": empty_columns,
                "unclear_gutters": unclear_gutters,
                "ambiguous_spanning_container_ids": list(template.ambiguous_spanning_container_ids),
            },
        }

    structure_top = min(item.content_top for item in template.columns)
    column_values = [
        item for item in template.containers
        if assignment[item.container_id] in column_ids
    ]
    source_floor = max(item.source_bbox[3] for item in column_values)
    spans = [item for item in template.containers if assignment[item.container_id] == "span"]
    modes: list[str] = []
    if any(item.source_bbox[1] <= structure_top + template.height * 0.04 for item in spans):
        modes.append("page_prelude_then_multi")
    if any(item.source_bbox[1] >= source_floor - item.font_size * 2.0 for item in spans):
        modes.append("multi_then_page_postlude")
    if any(owner == "fixed" for owner in assignment.values()):
        modes.append("multi_with_locked_visual_overlay")
    widths = [item.right - item.left for item in template.columns]
    if min(widths) / max(widths) < 0.45:
        modes.append("label_column_with_content_column")
    if any(item.content_bottom < template.height * 0.70 for item in template.columns):
        modes.append("multi_with_preserved_lower_visual_region")
    return {
        "route_verdict": "ACCEPT_TOLERANT" if modes else "ACCEPT_STANDARD",
        "upstream_route": "body.flow_text.multi",
        "matched_tolerance_modes": modes or ["standard_two_or_three_column_flow"],
        "hard_reasons": [],
        "evidence": {
            "column_count": len(template.columns),
            "span_count": len(spans),
            "fixed_overlay_count": sum(owner == "fixed" for owner in assignment.values()),
            "gutter_clear": True,
            "column_ownership_complete": len(assignment) == len(template.containers),
        },
    }
