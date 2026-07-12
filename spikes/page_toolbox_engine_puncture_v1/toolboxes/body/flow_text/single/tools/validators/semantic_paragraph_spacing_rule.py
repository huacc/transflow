"""
tool_name: semantic_paragraph_spacing_rule
category: validators
input_contract: one adjacent body-to-body semantic-paragraph transition measured by paragraph_spacing_probe
output_contract: NOT_APPLICABLE, PASS, or one section_spacing_regression diagnosis with a source-derived target plan gap
failure_signals: negative measurement or candidate gap too small for the current shrink-only repair tool
fallback: retain the current candidate and ask a focused page-rhythm visual question
anti_overfit_statement: thresholds are ratios of current source gap and current typography; no fixed point threshold, sample id, page number, literal text, or fixed bbox is used
"""

from __future__ import annotations


def evaluate_semantic_paragraph_spacing(
    *,
    previous_container_id: str,
    next_container_id: str,
    previous_role: str,
    next_role: str,
    source_visible_gap_pt: float,
    candidate_visible_gap_pt: float,
    previous_candidate_bottom_inset_pt: float,
    next_candidate_top_inset_pt: float,
    source_typographic_scale_pt: float,
    candidate_typographic_scale_pt: float,
    relative_tolerance: float = 0.20,
    typographic_tolerance_ratio: float = 0.10,
) -> dict[str, object]:
    # 本规则只处理同一正文流里的 body -> body 语义段落；标题、编号项和章节切换另行裁决。
    if previous_role != "body" or next_role != "body":
        return {
            "previous_container_id": previous_container_id,
            "next_container_id": next_container_id,
            "rule_verdict": "NOT_APPLICABLE",
            "selected_failure_class": None,
            "repair_atom": None,
            "target_plan_gap_pt": None,
            "reason": "semantic_paragraph_spacing_rule_only_applies_to_adjacent_body_paragraphs",
        }
    values = (
        source_visible_gap_pt,
        candidate_visible_gap_pt,
        previous_candidate_bottom_inset_pt,
        next_candidate_top_inset_pt,
        source_typographic_scale_pt,
        candidate_typographic_scale_pt,
    )
    if any(value < 0 for value in values):
        raise ValueError("semantic_paragraph_spacing_measurement_must_be_nonnegative")
    # 容差由当前原文段距和当前字号共同推导，不使用固定 pt 阈值。
    tolerance = max(
        source_visible_gap_pt * relative_tolerance,
        max(source_typographic_scale_pt, candidate_typographic_scale_pt)
        * typographic_tolerance_ratio,
    )
    delta = candidate_visible_gap_pt - source_visible_gap_pt
    common = {
        "previous_container_id": previous_container_id,
        "next_container_id": next_container_id,
        "source_visible_gap_pt": round(source_visible_gap_pt, 4),
        "candidate_visible_gap_pt": round(candidate_visible_gap_pt, 4),
        "delta_pt": round(delta, 4),
        "tolerance_pt": round(tolerance, 4),
    }
    if abs(delta) <= tolerance:
        return {
            **common,
            "rule_verdict": "PASS",
            "selected_failure_class": None,
            "repair_atom": None,
            "target_plan_gap_pt": None,
        }

    # 文本框底部可能有渲染留白，先扣除当前候选的上下内缩，再反推计划层目标段距。
    target_plan_gap = max(
        0.0,
        source_visible_gap_pt
        - previous_candidate_bottom_inset_pt
        - next_candidate_top_inset_pt,
    )
    return {
        **common,
        "rule_verdict": "FAIL",
        "selected_failure_class": "section_spacing_regression",
        "repair_atom": "section_spacing_reflow",
        "target_plan_gap_pt": round(target_plan_gap, 4),
        "repair_direction": "shrink" if delta > 0 else "expand",
    }
