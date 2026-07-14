from __future__ import annotations

import re
from pathlib import Path

import fitz

from .models import SingleColumnTemplate, ToolboxDecision, ToolboxFinding
from .p4_models import P4LayoutPlan


def judge_p4_candidate(
    *,
    candidate_pdf: Path,
    template: SingleColumnTemplate,
    plan: P4LayoutPlan,
    upstream_findings: tuple[ToolboxFinding, ...] = (),
) -> ToolboxDecision:
    findings = list(upstream_findings)
    by_id = {item.container_id: item for item in template.containers}
    main = []
    for placement in plan.placements:
        source = by_id[placement.container_id]
        is_anchored = source.role in {"anchored", "anchored_grid", "image_anchored"}
        safe_left_heading_expansion = (
            source.role == "heading"
            and placement.horizontal_policy == "safe_heading_left_whitespace_expand"
            and placement.output_bbox[0] < source.source_bbox[0]
            and abs(placement.output_bbox[2] - source.source_bbox[2]) <= 0.01
        )
        safe_left_margin_expansion = (
            source.role == "margin"
            and placement.horizontal_policy == "safe_margin_left_whitespace_expand"
            and placement.output_bbox[0] < source.source_bbox[0]
        )
        if (
            abs(placement.output_bbox[0] - source.source_bbox[0]) > 0.01
            and not safe_left_heading_expansion
            and not safe_left_margin_expansion
        ):
            findings.append(ToolboxFinding("P4_HORIZONTAL_FLOW_ESCAPE", "HARD", "p4_quality_judge", placement.container_id, "文字容器左边界发生变化"))
        if placement.horizontal_policy == "normal_flow_width_invariant" and abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01:
            findings.append(ToolboxFinding("P4_HORIZONTAL_FLOW_ESCAPE", "HARD", "p4_quality_judge", placement.container_id, "普通正文未保持原文字框宽度"))
        if placement.horizontal_policy == "exceptional_short_line_expand" and source.role != "heading" and len(source.source_text) > 80:
            findings.append(ToolboxFinding("P4_ILLEGAL_WIDTH_EXPANSION", "HARD", "p4_quality_judge", placement.container_id, "普通正文非法使用短行横向扩展"))
        vertical_limit = template.height - 4.0 if is_anchored else plan.content_bottom
        if placement.output_bbox[3] > vertical_limit + 0.01 and source.role != "margin":
            findings.append(ToolboxFinding("P4_VERTICAL_PAGE_ESCAPE", "HARD", "p4_quality_judge", placement.container_id, "正文越过可用纵向底边"))
        minimum_font_size = 2.0 if is_anchored else 6.0
        if placement.font_size < minimum_font_size or placement.font_size + 0.01 < placement.source_font_size * 0.72:
            findings.append(ToolboxFinding("P4_FONT_TOO_SMALL", "HARD", "p4_quality_judge", placement.container_id, "字号低于 P4 可读下限"))
        if placement.line_height < 0.95:
            findings.append(ToolboxFinding("P4_LINE_HEIGHT_TOO_TIGHT", "HARD", "p4_quality_judge", placement.container_id, "行距低于 P4 下限"))
        if source.role not in {"margin", "anchored", "anchored_grid", "image_anchored"}:
            main.append(placement)

    for previous, current in zip(main, main[1:]):
        if current.output_bbox[1] + 0.01 < previous.output_bbox[1]:
            findings.append(ToolboxFinding("P4_FLOW_ORDER_CHANGED", "HARD", "p4_quality_judge", current.container_id, "纵向阅读顺序发生反转"))
        if current.output_bbox[1] < previous.output_bbox[3] - 0.1:
            findings.append(ToolboxFinding("P4_TEXT_OVERLAP", "HARD", "p4_quality_judge", current.container_id, "相邻目标文字容器发生重叠"))

    for index, left in enumerate(plan.placements):
        for right in plan.placements[index + 1:]:
            left_role = by_id[left.container_id].role
            right_role = by_id[right.container_id].role
            if not {left_role, right_role} & {"anchored", "anchored_grid", "image_anchored"}:
                continue
            if _intersects(left.output_bbox, right.output_bbox):
                findings.append(ToolboxFinding("P4_TEXT_OVERLAP", "HARD", "p4_quality_judge", right.container_id, "空间锚定文字容器与其他译文重叠"))

    with fitz.open(candidate_pdf) as document:
        candidate_text = document[0].get_text("text")
    expected_markers = [item.preserved_prefix for item in template.containers if item.preserved_prefix]
    for marker in set(expected_markers):
        if candidate_text.count(marker) < expected_markers.count(marker):
            findings.append(ToolboxFinding("LIST_MARKER_LOST", "HARD", "p4_quality_judge", None, "原生项目符号未完整保留"))
    normalized_candidate = _normalized(candidate_text)
    for container in template.containers:
        source = _normalized(container.source_text)
        if len(source) >= 24 and source in normalized_candidate:
            findings.append(ToolboxFinding("SOURCE_TEXT_RESIDUE", "HARD", "p4_quality_judge", container.container_id, "候选页仍保留完整长源文块"))
    hard = [finding for finding in findings if finding.severity == "HARD"]
    return ToolboxDecision(
        template.page_id,
        "PASS",
        "PASS" if not hard else "FAIL",
        "P4_PRODUCT_PASS" if not hard else "P4_PRODUCT_FAIL",
        tuple(findings),
    )


def _normalized(value: str) -> str:
    value = re.sub(r"[\uf0b7•●▪·]", "", value)
    return re.sub(r"\s+", "", value).casefold()


def _intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    tolerance: float = 0.1,
) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > tolerance
        and min(left[3], right[3]) - max(left[1], right[1]) > tolerance
    )
