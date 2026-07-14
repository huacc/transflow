"""
tool_name: semantic_paragraph_fragmentation_rule
category: validators
input_contract: one ownership-stable MultiColumnTemplate
output_contract: PASS or one adjacent_same_paragraph_fragments finding
failure_signals: adjacent same-owner body/list fragments have matching style, line rhythm and no semantic terminator
fallback: keep fragments separate and let focused adjudication inspect uncertain boundaries
anti_overfit_statement: the rule uses current-page role, owner, typography and relative line geometry only; no sample id, literal text, page number or fixed bbox is encoded
"""

from __future__ import annotations

import re
from statistics import median

from toolboxes.body.flow_text.single.tools.models import TextContainer

from ..models import MultiColumnTemplate


def evaluate_semantic_paragraph_fragmentation(
    *,
    template: MultiColumnTemplate,
    owner_line_gap_limits: dict[str, float] | None = None,
) -> dict[str, object]:
    """一次只找出一对被 PDF 按源行拆开的同语义段落片段。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    paired_rows = _has_paired_row_evidence(template)
    column_by_id = {item.column_id: item for item in template.columns}
    owners = ["span", *(item.column_id for item in template.columns)]
    for owner in owners:
        values = [
            item for item in template.containers
            if assignment[item.container_id] == owner
        ]
        owner_line_gap_limit = (
            owner_line_gap_limits[owner]
            if owner_line_gap_limits is not None and owner in owner_line_gap_limits
            else _owner_line_gap_limit(values)
        )
        for previous, current in zip(values, values[1:]):
            column = column_by_id.get(owner)
            if paired_rows and column is not None and _starts_aligned_paired_row(
                current=current,
                owner=owner,
                template=template,
                assignment=assignment,
                column_by_id=column_by_id,
            ):
                continue
            if _same_semantic_paragraph(
                previous,
                current,
                column_left=column.left if column is not None else None,
                paired_rows=paired_rows,
                line_gap_limit_ratio=owner_line_gap_limit,
                column_right=column.right if column is not None else None,
            ):
                return {
                    "rule_verdict": "FAIL",
                    "selected_failure_class": "adjacent_same_paragraph_fragments",
                    "repair_atom": "merge_adjacent_same_owner_fragments",
                    "owner": owner,
                    "previous_container_id": previous.container_id,
                    "current_container_id": current.container_id,
                    "evidence": {
                        "relative_vertical_gap": round(
                            (current.source_bbox[1] - previous.source_bbox[3])
                            / max(previous.font_size, current.font_size, 0.01),
                            4,
                        ),
                        "relative_indent": round(
                            (current.source_bbox[0] - previous.source_bbox[0])
                            / max(previous.font_size, current.font_size, 0.01),
                            4,
                        ),
                        "same_color": previous.color_srgb == current.color_srgb,
                        "terminal_punctuation_absent": True,
                        "owner_line_gap_limit_ratio": round(owner_line_gap_limit, 4),
                    },
                }
    return {
        "rule_verdict": "PASS",
        "selected_failure_class": None,
        "repair_atom": None,
    }


def derive_owner_line_gap_limits(template: MultiColumnTemplate) -> dict[str, float]:
    """在开始合并前冻结各局部流的源行节奏，避免容器变少后阈值失真。"""

    assignment = {item.container_id: item.column_id for item in template.assignments}
    owners = ["span", *(item.column_id for item in template.columns)]
    return {
        owner: _owner_line_gap_limit(
            [item for item in template.containers if assignment[item.container_id] == owner]
        )
        for owner in owners
    }


def _same_semantic_paragraph(
    previous: TextContainer,
    current: TextContainer,
    *,
    column_left: float | None,
    paired_rows: bool,
    line_gap_limit_ratio: float,
    column_right: float | None = None,
) -> bool:
    if previous.role not in {"body", "list"} or current.role not in {"body", "heading", "list"}:
        return False
    if current.preserved_prefix:
        return False
    scale = max(previous.font_size, current.font_size, 0.01)
    if current.role == "list":
        if column_right is None:
            return False
        column_width = max(column_right - (column_left or previous.source_bbox[0]), scale)
        remaining_width = max(0.0, column_right - previous.source_bbox[2])
        if remaining_width > max(scale * 1.50, column_width * 0.08):
            return False
    # 成对行范式中，普通短单元回到栏左锚点表示新行；但前一源行已排到栏右边界时，
    # 这是 PDF 按视觉行拆分的正文续行，应先恢复自然段再重新判断页面范式。
    if paired_rows and column_left is not None and current.source_bbox[0] <= column_left + scale * 0.40:
        if column_right is None:
            return False
        column_width = max(column_right - column_left, scale)
        remaining_width = max(0.0, column_right - previous.source_bbox[2])
        if remaining_width > max(scale * 1.50, column_width * 0.08):
            return False
    if previous.color_srgb != current.color_srgb:
        return False
    if abs(previous.font_size - current.font_size) / scale > 0.05:
        return False
    indent_ratio = (current.source_bbox[0] - previous.source_bbox[0]) / scale
    if indent_ratio < -0.40 or indent_ratio > 4.0:
        return False
    gap_ratio = (current.source_bbox[1] - previous.source_bbox[3]) / scale
    if not (-0.05 <= gap_ratio <= line_gap_limit_ratio):
        return False
    return not bool(re.search(r"[。！？.!?:：；;]\s*$", previous.source_text))


def _starts_aligned_paired_row(
    *,
    current: TextContainer,
    owner: str,
    template: MultiColumnTemplate,
    assignment: dict[str, str],
    column_by_id: dict[str, object],
) -> bool:
    column = column_by_id[owner]
    if current.source_bbox[0] > column.left + current.font_size * 0.40:
        return False
    for peer in template.containers:
        peer_owner = assignment[peer.container_id]
        peer_column = column_by_id.get(peer_owner)
        if peer_owner == owner or peer_column is None:
            continue
        if peer.source_bbox[0] > peer_column.left + peer.font_size * 0.40:
            continue
        if abs(current.source_bbox[1] - peer.source_bbox[1]) <= max(current.font_size, peer.font_size) * 0.45:
            return True
    return False


def _owner_line_gap_limit(values: list[TextContainer]) -> float:
    """从当前局部流的常规相邻行推导行距上限，避免把某一页的点值写进规则。"""

    ratios: list[float] = []
    for previous, current in zip(values, values[1:]):
        scale = max(previous.font_size, current.font_size, 0.01)
        if previous.color_srgb != current.color_srgb or previous.font_weight != current.font_weight:
            continue
        if abs(previous.font_size - current.font_size) / scale > 0.05:
            continue
        if abs(current.source_bbox[0] - previous.source_bbox[0]) / scale > 0.40:
            continue
        ratio = (current.source_bbox[1] - previous.source_bbox[3]) / scale
        if ratio >= -0.05:
            ratios.append(ratio)
    if len(ratios) < 2:
        return 0.75
    ratios.sort()
    # 取当前页相邻行分布的较低三分位，避开段间距；仅保留通用相对上限。
    typical = ratios[(len(ratios) - 1) // 3]
    return min(1.35, max(0.75, typical * 1.20))


def _has_paired_row_evidence(template: MultiColumnTemplate) -> bool:
    """重复的跨栏行首对齐且单元较短，才视为键值/清单式成对多栏。"""

    if len(template.columns) != 2:
        return False
    assignment = {item.container_id: item.column_id for item in template.assignments}
    roots: list[list[TextContainer]] = []
    for column in template.columns:
        values = [
            item for item in template.containers
            if assignment[item.container_id] == column.column_id
            and item.source_bbox[0] <= column.left + item.font_size * 0.40
        ]
        roots.append(values)
    if min(len(values) for values in roots) < 4:
        return False
    aligned = sum(
        1
        for left in roots[0]
        if any(
            abs(left.source_bbox[1] - right.source_bbox[1])
            <= max(left.font_size, right.font_size) * 0.45
            for right in roots[1]
        )
    )
    shorter = min(len(values) for values in roots)
    height_ratios = [
        (item.source_bbox[3] - item.source_bbox[1]) / max(item.font_size, 0.01)
        for values in roots
        for item in values
    ]
    height_ratios.sort()
    median_height_ratio = height_ratios[len(height_ratios) // 2]
    short_cell_evidence = min(
        median(
            (item.source_bbox[2] - item.source_bbox[0])
            / max(column.right - column.left, item.font_size)
            for item in values
        )
        for column, values in zip(template.columns, roots)
    )
    return (
        aligned >= 3
        and aligned / shorter >= 0.60
        and median_height_ratio <= 4.5
        and short_cell_evidence <= 0.95
    )
