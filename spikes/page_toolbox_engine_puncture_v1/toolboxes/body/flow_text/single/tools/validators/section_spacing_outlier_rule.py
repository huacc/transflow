"""
tool_name: section_spacing_outlier_rule
category: validators
input_contract: one current-page P4LayoutPlan with source-derived gaps on adjacent placements
output_contract: PASS/NOT_APPLICABLE or one statistically isolated section_spacing_regression
failure_signals: insufficient comparable transitions or no isolated upper outlier
fallback: do not repair; route uncertain page rhythm to focused visual adjudication
anti_overfit_statement: the rule compares current-page gap-amplification distribution and current typography; it contains no sample id, text literal, page number, bbox, or fixed point threshold
"""

from __future__ import annotations

from statistics import quantiles

from ..p4_models import P4LayoutPlan


def evaluate_section_spacing_outlier(
    plan: P4LayoutPlan,
    *,
    outlier_iqr_ratio: float = 1.5,
    typographic_excess_ratio: float = 0.5,
) -> dict[str, object]:
    # 只比较当前页真实存在的相邻正文容器，不使用页码、样本文字或固定坐标。
    main = [item for item in plan.placements if item.role != "margin"]
    rows: list[dict[str, object]] = []
    for previous, current in zip(main, main[1:]):
        if current.source_gap <= 0:
            continue
        plan_gap = current.output_bbox[1] - previous.output_bbox[3]
        rows.append(
            {
                "previous_container_id": previous.container_id,
                "next_container_id": current.container_id,
                "previous_role": previous.role,
                "next_role": current.role,
                "source_gap_pt": current.source_gap,
                "candidate_plan_gap_pt": plan_gap,
                "gap_amplification_ratio": plan_gap / current.source_gap,
                "typographic_scale_pt": max(previous.font_size, current.font_size),
            }
        )
    # 转场太少时无法形成页内分布，宁可交给视觉裁决，也不凭单个绝对值下结论。
    if len(rows) < 4:
        return {
            "rule_verdict": "NOT_APPLICABLE",
            "selected_failure_class": None,
            "repair_atom": None,
            "reason": "section_spacing_distribution_requires_multiple_transitions",
        }

    # 用当前页“候选间距 / 原文间距”的四分位分布找孤立异常值。
    # 这样同一工具可适配字号、页面尺寸和版式密度不同的 PDF。
    ratios = [float(item["gap_amplification_ratio"]) for item in rows]
    q1, _, q3 = quantiles(ratios, n=4, method="inclusive")
    upper_fence = q3 + outlier_iqr_ratio * (q3 - q1)
    # 同时要求异常空白大于当前字号的一定比例，过滤 PDF 小数误差和抗锯齿漂移。
    outliers = [
        item
        for item in rows
        if float(item["gap_amplification_ratio"]) > upper_fence
        and float(item["candidate_plan_gap_pt"]) > float(item["source_gap_pt"])
        and float(item["candidate_plan_gap_pt"]) - float(item["source_gap_pt"])
        > float(item["typographic_scale_pt"]) * typographic_excess_ratio
    ]
    if not outliers:
        return {
            "rule_verdict": "PASS",
            "selected_failure_class": None,
            "repair_atom": None,
            "distribution": {
                "q1_ratio": round(q1, 4),
                "q3_ratio": round(q3, 4),
                "upper_fence_ratio": round(upper_fence, 4),
                "transition_count": len(rows),
            },
        }

    # 一轮只修最严重的一处；修完重新计算全页分布，避免多个修补相互影响。
    selected = max(outliers, key=lambda item: float(item["gap_amplification_ratio"]))
    return {
        "rule_verdict": "FAIL",
        "selected_failure_class": "section_spacing_regression",
        "repair_atom": "section_spacing_reflow",
        "previous_container_id": selected["previous_container_id"],
        "next_container_id": selected["next_container_id"],
        "source_gap_pt": round(float(selected["source_gap_pt"]), 4),
        "candidate_plan_gap_pt": round(float(selected["candidate_plan_gap_pt"]), 4),
        "gap_amplification_ratio": round(float(selected["gap_amplification_ratio"]), 4),
        "target_plan_gap_pt": round(float(selected["source_gap_pt"]), 4),
        "distribution": {
            "q1_ratio": round(q1, 4),
            "q3_ratio": round(q3, 4),
            "upper_fence_ratio": round(upper_fence, 4),
            "transition_count": len(rows),
        },
    }
