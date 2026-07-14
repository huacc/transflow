"""
tool_name: rendered_semantic_spacing_rule
category: validators
input_contract: current candidate semantic-paragraph transition rows from semantic_paragraph_spacing_probe
output_contract: PASS or one worst rendered_text_overlap / semantic_paragraph_spacing_loss disease
failure_signals: visible glyph overlap or candidate relative paragraph rhythm materially below source
fallback: product FAIL; no automatic acceptance from non-overlapping plan bboxes
anti_overfit_statement: verdicts use current rendered glyph gaps and dimensionless source/candidate rhythm ratios only
"""

from __future__ import annotations


def evaluate_rendered_semantic_spacing(
    rows: tuple[dict[str, object], ...],
    *,
    relative_tolerance: float = 0.20,
    typographic_tolerance_ratio: float = 0.10,
    overlap_precision_ratio: float = 0.02,
    ignore_relative_spacing_columns: tuple[str, ...] = (),
) -> dict[str, object]:
    overlaps = [
        row for row in rows
        if float(row["candidate_visible_overlap_pt"])
        > float(row["candidate_typographic_scale_pt"]) * overlap_precision_ratio
    ]
    if overlaps:
        selected = max(overlaps, key=lambda row: float(row["candidate_visible_overlap_pt"]))
        return {
            **selected,
            "rule_verdict": "FAIL",
            "selected_failure_class": "rendered_text_overlap",
            "repair_atom": "rendered_semantic_spacing_reflow",
        }
    def tolerance(row: dict[str, object]) -> float:
        # 与 single 工具箱一致：容差取原文节奏的相对比例和当前字号比例中的较大者。
        return max(
            float(row["source_transition_ratio"]) * relative_tolerance,
            typographic_tolerance_ratio,
        )

    relative_rows = [
        row for row in rows
        if str(row.get("column_id") or "") not in ignore_relative_spacing_columns
    ]
    losses = [
        row for row in relative_rows
        if float(row["candidate_transition_ratio"])
        < float(row["source_transition_ratio"]) - tolerance(row)
    ]
    if losses:
        selected = max(
            losses,
            key=lambda row: float(row["source_transition_ratio"]) - float(row["candidate_transition_ratio"]),
        )
        return {
            **selected,
            "rule_verdict": "FAIL",
            "selected_failure_class": "semantic_paragraph_spacing_loss",
            "repair_atom": "rendered_semantic_spacing_reflow",
        }
    amplifications = [
        row for row in relative_rows
        if float(row["candidate_transition_ratio"])
        > float(row["source_transition_ratio"]) + tolerance(row)
        # 比率偏大但实际字形间只剩极小可见缝隙时，不得再向上压缩并制造重叠。
        and float(row["candidate_visible_gap_pt"])
        > float(row["candidate_typographic_scale_pt"]) * typographic_tolerance_ratio
    ]
    if amplifications:
        selected = max(
            amplifications,
            key=lambda row: float(row["candidate_transition_ratio"]) - float(row["source_transition_ratio"]),
        )
        return {
            **selected,
            "rule_verdict": "FAIL",
            "selected_failure_class": "semantic_paragraph_spacing_amplification",
            "repair_atom": "rendered_semantic_spacing_reflow",
        }
    return {
        "rule_verdict": "PASS",
        "selected_failure_class": None,
        "repair_atom": None,
        "transition_count": len(rows),
    }
