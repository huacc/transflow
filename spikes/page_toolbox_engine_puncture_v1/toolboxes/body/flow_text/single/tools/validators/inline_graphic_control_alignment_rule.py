"""
tool_name: inline_graphic_control_alignment_rule
category: validators
input_contract: one current-run inline-control probe group
output_contract: PASS, NOT_APPLICABLE, or icon_label_misalignment with image_overlay_text_relayout
failure_signals: ambiguous current drawing position or insufficient container movement
fallback: do not modify graphics; route the group to focused visual adjudication
anti_overfit_statement: the decision uses current control counts and movement normalized by current control size; no label literal, sample id, bbox, or fixed point threshold is used
"""

from __future__ import annotations


def evaluate_inline_graphic_control_alignment(
    probe: dict[str, object],
    *,
    minimum_normalized_shift: float = 0.5,
) -> dict[str, object]:
    count = int(probe["control_count"])
    source_hits = int(probe["source_position_hit_count"])
    target_hits = int(probe["target_position_hit_count"])
    common = {
        "container_id": probe["container_id"],
        "source_position_hit_count": source_hits,
        "target_position_hit_count": target_hits,
        "control_count": count,
    }
    # 控件已经位于目标位置时直接 PASS，保证规则幂等。
    if target_hits == count:
        return {**common, "rule_verdict": "PASS", "selected_failure_class": None, "repair_atom": None}
    # 旧位置仍存在且文字已移动超过控件自身尺度，才允许确定性修补。
    if source_hits == count and float(probe["normalized_container_shift"]) > minimum_normalized_shift:
        return {
            **common,
            "rule_verdict": "FAIL",
            "selected_failure_class": "icon_label_misalignment",
            "repair_atom": "image_overlay_text_relayout",
        }
    return {
        **common,
        "rule_verdict": "NOT_APPLICABLE",
        "selected_failure_class": None,
        "repair_atom": None,
        "reason": "inline_control_position_is_ambiguous",
    }
