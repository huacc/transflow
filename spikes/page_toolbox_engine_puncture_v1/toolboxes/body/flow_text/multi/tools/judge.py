from __future__ import annotations

import re
from pathlib import Path

import fitz

from toolboxes.body.flow_text.single.tools.models import ToolboxDecision, ToolboxFinding

from .models import MultiColumnLayoutPlan, MultiColumnTemplate
from .layout_planner import _mixed_flow_column_bottom_limits
from .validators.structural_anchor_zone_rule import evaluate_structural_anchor_zones


def judge_multi_candidate(
    *,
    candidate_pdf: Path,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    upstream_findings: tuple[ToolboxFinding, ...] = (),
    rendered_spacing_decision: dict[str, object] | None = None,
) -> ToolboxDecision:
    findings = list(upstream_findings)
    by_id = {item.container_id: item for item in template.containers}
    assignment = {item.container_id: item.column_id for item in template.assignments}
    band = {item.column_id: item for item in template.columns}
    placement_by_id = {item.container_id: item for item in plan.placements}
    dynamic_column_limits = _mixed_flow_column_bottom_limits(template=template, plan=plan)

    for placement in plan.placements:
        source = by_id[placement.container_id]
        column_id = assignment.get(placement.container_id)
        safe_left_heading_expansion = placement.horizontal_policy == "safe_heading_left_whitespace_expand"
        safe_fixed_left_expansion = placement.horizontal_policy == "locked_visual_overlay_safe_left_expand"
        safe_margin_expansion = placement.horizontal_policy in {
            "safe_margin_right_whitespace_expand",
            "safe_margin_row_horizontal_reflow",
        }
        safe_margin_row_reflow = placement.horizontal_policy == "safe_margin_row_horizontal_reflow"
        safe_paired_column_width = (
            placement.horizontal_policy == "paired_row_column_width"
            and column_id in band
            and abs(placement.output_bbox[2] - band[column_id].right) <= 0.01
        )
        if abs(placement.output_bbox[0] - source.source_bbox[0]) > 0.01 and not safe_left_heading_expansion and not safe_fixed_left_expansion and not safe_margin_row_reflow:
            findings.append(ToolboxFinding("P5_COLUMN_LEFT_ANCHOR_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "文字框左侧锚点发生变化"))
        width_changed = abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01
        safe_heading_expansion = placement.horizontal_policy == "safe_heading_whitespace_expand"
        safe_flow_expansion = placement.horizontal_policy == "safe_flow_whitespace_expand"
        if width_changed and not safe_heading_expansion and not safe_flow_expansion and not safe_margin_expansion and not safe_paired_column_width:
            findings.append(ToolboxFinding("P5_COLUMN_WIDTH_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "非标题文字框宽度发生变化"))
        if safe_heading_expansion and source.role != "heading":
            findings.append(ToolboxFinding("P5_ILLEGAL_HEADING_WIDTH_EXPANSION", "HARD", "p5_quality_judge", placement.container_id, "普通栏内正文非法使用标题安全扩展"))
        if safe_flow_expansion:
            if column_id == "span":
                safe_flow_right = max(item.right for item in template.columns)
            elif column_id in band:
                safe_flow_right = band[column_id].right
            else:
                safe_flow_right = source.source_bbox[2]
                findings.append(ToolboxFinding("P5_ILLEGAL_FLOW_WIDTH_EXPANSION", "HARD", "p5_quality_judge", placement.container_id, "普通流文本扩宽只能发生在单栏文本区或所属栏内"))
            if placement.output_bbox[2] > safe_flow_right + 0.01:
                findings.append(ToolboxFinding("P5_FLOW_SAFE_WIDTH_ESCAPE", "HARD", "p5_quality_judge", placement.container_id, "普通流文本写出了单栏文本区或所属栏的安全右边界"))
        if safe_left_heading_expansion and (
            column_id != "span"
            or source.role != "heading"
            or abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01
        ):
            findings.append(ToolboxFinding("P5_ILLEGAL_LEFT_HEADING_EXPANSION", "HARD", "p5_quality_judge", placement.container_id, "仅页级右锚定标题允许向左利用安全空白"))
        if column_id is None:
            findings.append(ToolboxFinding("P5_COLUMN_OWNERSHIP_MISSING", "HARD", "p5_quality_judge", placement.container_id, "文字容器缺少栏归属"))
        elif column_id == "margin":
            if abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01 and not safe_margin_expansion:
                findings.append(ToolboxFinding("P5_MARGIN_WIDTH_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "页边文字横向边界发生变化"))
            if placement.output_bbox[0] < 0.0 or placement.output_bbox[2] > template.width:
                findings.append(ToolboxFinding("P5_MARGIN_HORIZONTAL_ESCAPE", "HARD", "p5_quality_judge", placement.container_id, "页边文字写出页面横向边界"))
            if abs(placement.output_bbox[3] - source.source_bbox[3]) > 0.01:
                findings.append(ToolboxFinding("P5_MARGIN_BOTTOM_ANCHOR_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "页边文字底部锚点发生变化"))
            if placement.output_bbox[1] < 0.0 or placement.output_bbox[3] > template.height:
                findings.append(ToolboxFinding("P5_MARGIN_VERTICAL_ESCAPE", "HARD", "p5_quality_judge", placement.container_id, "页边文字写出页面边界"))
        elif column_id == "fixed":
            if safe_fixed_left_expansion and (
                placement.output_bbox[0] >= source.source_bbox[0] - 0.01
                or abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01
            ):
                findings.append(ToolboxFinding("P5_ILLEGAL_FIXED_OVERLAY_EXPANSION", "HARD", "p5_quality_judge", placement.container_id, "局部锁定图说明文字未保持原右锚点安全向左扩展"))
            if abs(placement.output_bbox[2] - source.source_bbox[2]) > 0.01:
                findings.append(ToolboxFinding("P5_FIXED_OVERLAY_WIDTH_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "局部锁定图说明文字横向边界发生变化"))
            if abs(placement.output_bbox[3] - source.source_bbox[3]) > 0.01:
                findings.append(ToolboxFinding("P5_FIXED_OVERLAY_BOTTOM_ANCHOR_CHANGED", "HARD", "p5_quality_judge", placement.container_id, "局部锁定图说明文字底部锚点发生变化"))
            if not placement.fit or placement.output_bbox[1] < 0.0:
                findings.append(ToolboxFinding("P5_FIXED_OVERLAY_VERTICAL_CONFLICT", "HARD", "p5_quality_judge", placement.container_id, "局部锁定图说明文字无法在原位置安全回填"))
        elif column_id != "span":
            column = band[column_id]
            if placement.output_bbox[0] < column.left - 0.01 or placement.output_bbox[2] > column.right + 0.01:
                findings.append(ToolboxFinding("P5_CROSS_COLUMN_WRITE", "HARD", "p5_quality_judge", placement.container_id, "译文写出所属栏水平边界"))
            column_bottom_limit = dynamic_column_limits.get(placement.container_id, column.content_bottom)
            if placement.output_bbox[3] > column_bottom_limit + 0.01:
                findings.append(ToolboxFinding("P5_COLUMN_VERTICAL_ESCAPE", "HARD", "p5_quality_judge", placement.container_id, "译文写出所属栏纵向边界"))
        elif safe_heading_expansion and placement.output_bbox[2] > max(item.right for item in template.columns) + 0.01:
            findings.append(ToolboxFinding("P5_SPANNING_SAFE_WIDTH_ESCAPE", "HARD", "p5_quality_judge", placement.container_id, "页级标题越过经证明的安全右边界"))
        minimum_font_size = 5.0 if column_id == "margin" else 6.0
        if placement.font_size < minimum_font_size or placement.font_size + 0.01 < placement.source_font_size * 0.72:
            findings.append(ToolboxFinding("P5_FONT_TOO_SMALL", "HARD", "p5_quality_judge", placement.container_id, "字号低于 P5 可读下限"))

    for column in template.columns:
        ids = [item.container_id for item in template.assignments if item.column_id == column.column_id]
        placements = [placement_by_id[item] for item in ids]
        for previous, current in zip(placements, placements[1:]):
            if current.output_bbox[1] + 0.01 < previous.output_bbox[1]:
                findings.append(ToolboxFinding("P5_COLUMN_ORDER_CHANGED", "HARD", "p5_quality_judge", current.container_id, "列内阅读顺序反转"))
            if current.output_bbox[1] < previous.output_bbox[3] - 0.1:
                findings.append(ToolboxFinding("P5_COLUMN_TEXT_OVERLAP", "HARD", "p5_quality_judge", current.container_id, "同栏相邻文字容器发生重叠"))

    # 页内范式区段按阅读顺序相邻但不能互相覆盖；尤其防止页首 span 修复误带动页尾 span。
    content_bands = [item for item in plan.flow_bands if item.mode in {"single", "multi"}]
    for previous_band, current_band in zip(content_bands, content_bands[1:]):
        previous_bottom = max(placement_by_id[item].output_bbox[3] for item in previous_band.container_ids)
        current_top = min(placement_by_id[item].output_bbox[1] for item in current_band.container_ids)
        if current_top < previous_bottom - 0.1:
            findings.append(
                ToolboxFinding(
                    "P5_FLOW_BAND_TEXT_OVERLAP",
                    "HARD",
                    "p5_quality_judge",
                    current_band.container_ids[0] if current_band.container_ids else None,
                    "相邻页内排版区段的文字 bbox 发生覆盖",
                )
            )

    if template.ambiguous_spanning_container_ids:
        findings.append(ToolboxFinding("P5_AMBIGUOUS_SPANNING_BODY", "HARD", "p5_template_builder", None, "存在非顶部跨栏正文，需要细粒度裁决"))

    for anchor_finding in evaluate_structural_anchor_zones(template=template, plan=plan):
        findings.append(
            ToolboxFinding(
                "P5_STRUCTURAL_ANCHOR_ZONE_CROSSING",
                "HARD",
                "p5_structural_anchor_zone_rule",
                str(anchor_finding["container_id"]),
                "译文改变了原文结构分隔线所属语义带或穿越不可动结构锚点",
            )
        )

    if rendered_spacing_decision and rendered_spacing_decision.get("rule_verdict") == "FAIL":
        failure_class = str(rendered_spacing_decision.get("selected_failure_class"))
        if failure_class == "rendered_text_overlap":
            code = "P5_RENDERED_TEXT_OVERLAP"
            message = "相邻语义段落的实际字形发生重叠"
        elif failure_class == "semantic_paragraph_spacing_amplification":
            code = "P5_SEMANTIC_PARAGRAPH_SPACING_AMPLIFICATION"
            message = "候选同语义组间距明显大于原文相对字号节奏"
        else:
            code = "P5_SEMANTIC_PARAGRAPH_SPACING_LOSS"
            message = "候选语义段距明显小于原文相对行节奏"
        findings.append(
            ToolboxFinding(
                code,
                "HARD",
                "p5_rendered_semantic_spacing_rule",
                str(rendered_spacing_decision.get("next_container_id") or "") or None,
                message,
            )
        )

    with fitz.open(candidate_pdf) as document:
        candidate_text = document[0].get_text("text")
    normalized_candidate = _normalized(candidate_text)
    for container in template.containers:
        source = _normalized(container.source_text)
        if len(source) >= 24 and source in normalized_candidate:
            findings.append(ToolboxFinding("SOURCE_TEXT_RESIDUE", "HARD", "p5_quality_judge", container.container_id, "候选页仍保留完整长源文块"))
    expected_markers = [item.preserved_prefix for item in template.containers if item.preserved_prefix]
    for marker in set(expected_markers):
        if candidate_text.count(marker) < expected_markers.count(marker):
            findings.append(ToolboxFinding("LIST_MARKER_LOST", "HARD", "p5_quality_judge", None, "原生项目符号未完整保留"))

    hard = [item for item in findings if item.severity == "HARD"]
    return ToolboxDecision(template.page_id, "PASS", "PASS" if not hard else "FAIL", "P5_PRODUCT_PASS" if not hard else "P5_PRODUCT_FAIL", tuple(findings))


def _normalized(value: str) -> str:
    value = re.sub(r"[\uf0b7•●▪·]", "", value)
    return re.sub(r"\s+", "", value).casefold()
