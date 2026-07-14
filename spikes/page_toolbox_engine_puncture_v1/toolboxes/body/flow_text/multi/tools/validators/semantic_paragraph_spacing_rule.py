"""
tool_name: semantic_paragraph_spacing_rule
category: validators
input_contract: two adjacent current-column containers plus source line rhythm and candidate typography facts
output_contract: NOT_APPLICABLE or one source-rhythm-derived target plan gap
failure_signals: source has no repeatable line rhythm or either container has no source line
fallback: retain the profile-scaled source bbox gap and leave visual uncertainty for the rendered candidate gate
anti_overfit_statement: the target is a ratio of current source line positions mapped through the current candidate font metrics; no sample id, text literal, page number, bbox literal, or fixed point gap is used
"""

from __future__ import annotations

from toolboxes.body.flow_text.single.tools.models import TextContainer


def evaluate_semantic_paragraph_spacing_target(
    *,
    previous: TextContainer,
    current: TextContainer,
    previous_source_line_tops: tuple[float, ...],
    current_source_line_tops: tuple[float, ...],
    source_line_step: float | None,
    previous_output_height: float,
    previous_candidate_line_count: int,
    previous_candidate_line_step: float,
    current_candidate_line_step: float,
) -> dict[str, object]:
    if previous.role != "body" or current.role != "body":
        return {
            "rule_verdict": "NOT_APPLICABLE",
            "selected_failure_class": None,
            "repair_atom": None,
            "reason": "semantic_spacing_only_applies_to_adjacent_body_paragraphs",
        }
    if source_line_step is None or not previous_source_line_tops or not current_source_line_tops:
        return {
            "rule_verdict": "NOT_APPLICABLE",
            "selected_failure_class": None,
            "repair_atom": None,
            "reason": "source_line_rhythm_evidence_is_insufficient",
        }

    source_transition = current_source_line_tops[0] - previous_source_line_tops[-1]
    if source_transition <= 0:
        return {
            "rule_verdict": "NOT_APPLICABLE",
            "selected_failure_class": None,
            "repair_atom": None,
            "reason": "source_paragraph_order_is_not_monotonic",
        }
    source_transition_ratio = source_transition / source_line_step
    candidate_line_step = (previous_candidate_line_step + current_candidate_line_step) / 2.0
    previous_last_line_top = max(0, previous_candidate_line_count - 1) * previous_candidate_line_step
    target_plan_gap = max(
        0.0,
        previous_last_line_top
        + source_transition_ratio * candidate_line_step
        - previous_output_height,
    )
    return {
        "rule_verdict": "APPLY",
        "selected_failure_class": "semantic_paragraph_spacing_loss",
        "repair_atom": "semantic_source_rhythm_reflow",
        "previous_container_id": previous.container_id,
        "next_container_id": current.container_id,
        "source_line_step_pt": round(source_line_step, 4),
        "source_transition_pt": round(source_transition, 4),
        "source_transition_ratio": round(source_transition_ratio, 4),
        "candidate_line_step_pt": round(candidate_line_step, 4),
        "target_plan_gap_pt": round(target_plan_gap, 4),
    }
