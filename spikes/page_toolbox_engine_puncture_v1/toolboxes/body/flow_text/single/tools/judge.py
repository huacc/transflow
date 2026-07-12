from __future__ import annotations

import re
from pathlib import Path

import fitz

from .models import SingleColumnLayoutPlan, SingleColumnTemplate, ToolboxDecision, ToolboxFinding


def judge_candidate(
    *,
    candidate_pdf: Path,
    template: SingleColumnTemplate,
    plan: SingleColumnLayoutPlan,
    upstream_findings: tuple[ToolboxFinding, ...] = (),
) -> ToolboxDecision:
    findings = list(upstream_findings)
    by_id = {item.container_id: item for item in template.containers}
    for placement in plan.placements:
        source = by_id[placement.container_id]
        if placement.anchor != source.anchor or placement.output_bbox[:2] != source.anchor:
            findings.append(ToolboxFinding("ANCHOR_CHANGED", "HARD", "quality_judge", placement.container_id, "容器左上角锚点发生变化"))

    ordered = [item.container_id for item in sorted(template.containers, key=lambda row: row.reading_order)]
    if ordered != [item.container_id for item in plan.placements]:
        findings.append(ToolboxFinding("READING_ORDER_CHANGED", "HARD", "quality_judge", None, "布局计划改变了阅读顺序"))

    with fitz.open(candidate_pdf) as document:
        candidate_text = document[0].get_text("text")
    expected_markers = [item.preserved_prefix for item in template.containers if item.preserved_prefix]
    for marker in set(expected_markers):
        if candidate_text.count(marker) < expected_markers.count(marker):
            findings.append(ToolboxFinding("LIST_MARKER_LOST", "HARD", "quality_judge", None, "列表容器的原生项目符号未保留"))
    normalized_candidate = _normalized(candidate_text)
    for container in template.containers:
        source = _normalized(container.source_text)
        if len(source) >= 24 and source in normalized_candidate:
            findings.append(ToolboxFinding("SOURCE_TEXT_RESIDUE", "HARD", "quality_judge", container.container_id, "候选页仍保留完整长源文块"))
    for placement in plan.placements:
        translated = _normalized(placement.translated_text)
        if len(translated) >= 4 and translated not in normalized_candidate:
            findings.append(ToolboxFinding("TRANSLATION_NOT_RENDERED", "HARD", "quality_judge", placement.container_id, "按容器 ID 返回的译文未在候选页中完整出现"))

    hard = [finding for finding in findings if finding.severity == "HARD"]
    return ToolboxDecision(
        template.page_id,
        "PASS",
        "PASS" if not hard else "FAIL",
        "P3_PRODUCT_PASS" if not hard else "P3_PRODUCT_FAIL",
        tuple(findings),
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()
