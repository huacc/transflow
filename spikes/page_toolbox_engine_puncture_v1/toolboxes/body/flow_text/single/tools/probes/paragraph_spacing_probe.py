"""
tool_name: paragraph_spacing_probe
category: probes
input_contract: source/candidate PDF plus one adjacent semantic-paragraph transition and its source/output bboxes
output_contract: visible source/candidate gap and candidate textbox edge insets in PDF points
failure_signals: either clipped region contains no extractable text
fallback: route the transition to focused visual adjudication; do not emit a repair patch
anti_overfit_statement: all measurements come from current-run PDF geometry and runtime container bboxes
"""

from __future__ import annotations

from pathlib import Path

import fitz

from ..models import Rect


def probe_paragraph_transition(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    previous_source_bbox: Rect,
    next_source_bbox: Rect,
    previous_output_bbox: Rect,
    next_output_bbox: Rect,
    page_index: int = 0,
) -> dict[str, float]:
    # 段距使用“实际可见文字边缘”计算，不能只看文本框 bbox，否则会把框内余量误判为段距。
    with fitz.open(source_pdf) as source_document:
        source_page = source_document[page_index]
        previous_source_text_bbox = _visible_text_bbox(source_page, previous_source_bbox)
        next_source_text_bbox = _visible_text_bbox(source_page, next_source_bbox)
    with fitz.open(candidate_pdf) as candidate_document:
        candidate_page = candidate_document[page_index]
        previous_candidate_text_bbox = _visible_text_bbox(candidate_page, previous_output_bbox)
        next_candidate_text_bbox = _visible_text_bbox(candidate_page, next_output_bbox)

    source_gap = max(0.0, next_source_text_bbox[1] - previous_source_text_bbox[3])
    candidate_gap = max(0.0, next_candidate_text_bbox[1] - previous_candidate_text_bbox[3])
    # 候选框的上下内缩会交给规则层反推目标计划间距。
    return {
        "source_visible_gap_pt": round(source_gap, 4),
        "candidate_visible_gap_pt": round(candidate_gap, 4),
        "previous_candidate_bottom_inset_pt": round(
            max(0.0, previous_output_bbox[3] - previous_candidate_text_bbox[3]), 4
        ),
        "next_candidate_top_inset_pt": round(
            max(0.0, next_candidate_text_bbox[1] - next_output_bbox[1]), 4
        ),
    }


def _visible_text_bbox(page: fitz.Page, bbox: Rect) -> Rect:
    blocks = page.get_text("dict", clip=fitz.Rect(bbox)).get("blocks", [])
    line_bboxes = [
        tuple(float(value) for value in line["bbox"])
        for block in blocks
        if block.get("type") == 0
        for line in block.get("lines", [])
        if any(str(span.get("text") or "").strip() for span in line.get("spans", []))
    ]
    if not line_bboxes:
        raise ValueError("paragraph_spacing_region_has_no_text")
    return (
        min(item[0] for item in line_bboxes),
        min(item[1] for item in line_bboxes),
        max(item[2] for item in line_bboxes),
        max(item[3] for item in line_bboxes),
    )
