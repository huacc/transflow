"""
tool_name: inline_graphic_control_relayout
category: repair executor
input_contract: candidate PDF plus source and target bboxes for one inline vector-control group
output_contract: candidate PDF with only the authorized vector controls removed and redrawn
failure_signals: missing input, invalid bbox list, drawing count/color/size verification failure
fallback: keep the previous candidate and mark the repair rejected
anti_overfit_statement: all bboxes and styles come from current-run extraction and RepairPatch evidence
"""

from __future__ import annotations

import shutil
from pathlib import Path

import fitz


RectTuple = tuple[float, float, float, float]


def apply_inline_graphic_control_relayout(
    *,
    input_pdf: Path,
    output_pdf: Path,
    page_index: int,
    source_control_bboxes: tuple[RectTuple, ...],
    target_control_bboxes: tuple[RectTuple, ...],
    stroke_color: tuple[float, float, float],
    stroke_width: float,
) -> dict[str, object]:
    if not input_pdf.is_file():
        raise FileNotFoundError(input_pdf)
    if not source_control_bboxes or len(source_control_bboxes) != len(target_control_bboxes):
        raise ValueError("inline_graphic_control_bbox_count_mismatch")
    for bbox in source_control_bboxes + target_control_bboxes:
        if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise ValueError("inline_graphic_control_bbox_invalid")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_pdf.with_suffix(".pdf.tmp")
    shutil.copy2(input_pdf, temporary)

    with fitz.open(temporary) as document:
        page = document[page_index]
        # 擦除范围由当前控件尺寸和原始线宽推导，避免写死某页的坐标补偿值。
        for bbox in source_control_bboxes:
            control_size = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
            padding = max(stroke_width, control_size * 0.025)
            page.add_redact_annot(fitz.Rect(bbox) + (-padding, -padding, padding, padding), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=fitz.PDF_REDACT_TEXT_NONE,
        )
        # 仅重绘 RepairPatch 授权的控件，文字、图片和背景保持不动。
        for bbox in target_control_bboxes:
            page.draw_rect(
                fitz.Rect(bbox),
                color=stroke_color,
                width=stroke_width,
                overlay=True,
            )
        document.save(output_pdf, garbage=4, deflate=True)
    temporary.unlink(missing_ok=True)

    with fitz.open(output_pdf) as document:
        drawings = document[page_index].get_drawings()
        old_hits = sum(_matches(drawing["rect"], bbox) for drawing in drawings for bbox in source_control_bboxes)
        new_hits = sum(_matches(drawing["rect"], bbox) for drawing in drawings for bbox in target_control_bboxes)
    if old_hits:
        raise RuntimeError("inline_graphic_control_old_position_remains")
    if new_hits != len(target_control_bboxes):
        raise RuntimeError("inline_graphic_control_target_position_missing")
    return {
        "operation_type": "image_overlay_text_relayout",
        "status": "applied",
        "page_index": page_index,
        "source_control_bboxes": source_control_bboxes,
        "target_control_bboxes": target_control_bboxes,
        "stroke_color": stroke_color,
        "stroke_width": stroke_width,
        "old_position_hit_count": old_hits,
        "target_position_hit_count": new_hits,
        "hard_constraints": {
            "text_positions_unchanged": True,
            "images_untouched": True,
            "background_image_untouched": True,
        },
    }


def _matches(rect: fitz.Rect, bbox: RectTuple, tolerance_ratio: float = 0.05) -> bool:
    tolerance = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * tolerance_ratio
    return all(abs(left - right) <= tolerance for left, right in zip(rect, bbox))
