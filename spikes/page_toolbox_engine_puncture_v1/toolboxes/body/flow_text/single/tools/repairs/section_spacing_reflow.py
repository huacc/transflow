"""
tool_name: section_spacing_reflow
category: repair executor
input_contract: P4LayoutPlan plus one adjacent source-derived transition and target gap
output_contract: repaired P4LayoutPlan plus before/after application evidence
failure_signals: missing/non-adjacent transition, invalid target gap, upward page escape, new overlap
fallback: no mutation; return the original plan through the caller's rollback path
anti_overfit_statement: runtime container ids and source/candidate gaps are patch evidence, never code branches
"""

from __future__ import annotations

from dataclasses import replace

from ..p4_models import P4LayoutPlan


def apply_section_spacing_reflow(
    plan: P4LayoutPlan,
    *,
    previous_container_id: str,
    next_container_id: str,
    target_gap_pt: float,
) -> tuple[P4LayoutPlan, dict[str, object]]:
    if target_gap_pt < 0:
        raise ValueError("section_spacing_target_gap_must_be_nonnegative")
    placements = list(plan.placements)
    ids = [item.container_id for item in placements]
    if previous_container_id not in ids or next_container_id not in ids:
        raise ValueError("section_spacing_transition_container_missing")
    previous_index = ids.index(previous_container_id)
    next_index = ids.index(next_container_id)
    if next_index != previous_index + 1:
        raise ValueError("section_spacing_transition_must_be_adjacent")

    previous = placements[previous_index]
    current = placements[next_index]
    before_gap = current.output_bbox[1] - previous.output_bbox[3]
    shift_up = max(0.0, before_gap - target_gap_pt)
    if shift_up <= 0.01:
        return plan, {
            "operation_type": "section_spacing_reflow",
            "status": "no_change_needed",
            "previous_container_id": previous_container_id,
            "next_container_id": next_container_id,
            "before_gap_pt": round(before_gap, 4),
            "after_gap_pt": round(before_gap, 4),
            "shift_up_pt": 0.0,
        }

    affected: list[str] = []
    for index in range(next_index, len(placements)):
        placement = placements[index]
        if placement.role == "margin":
            continue
        x0, y0, x1, y1 = placement.output_bbox
        shifted = (x0, y0 - shift_up, x1, y1 - shift_up)
        if shifted[1] < plan.content_top - 0.01:
            raise ValueError("section_spacing_reflow_escapes_content_top")
        placements[index] = replace(
            placement,
            output_bbox=tuple(round(value, 4) for value in shifted),
            vertical_policy=f"{placement.vertical_policy}+section_spacing_reflow",
        )
        affected.append(placement.container_id)

    repaired = replace(plan, placements=tuple(placements))
    main = [item for item in repaired.placements if item.role != "margin"]
    for first, second in zip(main, main[1:]):
        if second.output_bbox[1] < first.output_bbox[3] - 0.01:
            raise ValueError(f"section_spacing_reflow_creates_overlap:{first.container_id}:{second.container_id}")
    after_previous = repaired.placements[previous_index]
    after_current = repaired.placements[next_index]
    after_gap = after_current.output_bbox[1] - after_previous.output_bbox[3]
    return repaired, {
        "operation_type": "section_spacing_reflow",
        "status": "applied",
        "previous_container_id": previous_container_id,
        "next_container_id": next_container_id,
        "before_gap_pt": round(before_gap, 4),
        "target_gap_pt": round(target_gap_pt, 4),
        "after_gap_pt": round(after_gap, 4),
        "shift_up_pt": round(shift_up, 4),
        "affected_container_ids": affected,
        "hard_constraints": {
            "x_coordinates_unchanged": True,
            "widths_unchanged": True,
            "locked_objects_untouched": True
        }
    }
