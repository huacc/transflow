"""
tool_name: inline_graphic_control_probe
category: probes
input_contract: current source/candidate PDF, page facts, page template, and current layout plan
output_contract: runtime inline-control groups with source/target bboxes, style, and current-position hit counts
failure_signals: ambiguous drawing style, missing checked-control symbol mapping, or no inline control group
fallback: return no actionable group and route image/graphic alignment to focused visual adjudication
anti_overfit_statement: controls are detected from current-page square geometry, typography ratios, Unicode symbol category, and container movement; no literal label, sample id, page number, bbox, or fixed point value is used
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts

from ..models import Rect, SingleColumnTemplate
from ..p4_models import P4LayoutPlan


def probe_inline_graphic_controls(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    facts: PageFacts,
    template: SingleColumnTemplate,
    plan: P4LayoutPlan,
) -> tuple[dict[str, object], ...]:
    placements = {item.container_id: item for item in plan.placements}
    source_controls = [item.bbox for item in facts.drawing_objects]
    if not source_controls:
        return ()

    groups: list[dict[str, object]] = []
    with fitz.open(source_pdf) as source_document, fitz.open(candidate_pdf) as candidate_document:
        source_page = source_document[facts.page_index]
        candidate_page = candidate_document[facts.page_index]
        source_drawings = source_page.get_drawings()
        candidate_drawings = candidate_page.get_drawings()
        for container in template.containers:
            if container.role == "margin" or container.container_id not in placements:
                continue
            # 先用当前字号、方形比例和与文字容器的邻近关系识别行内控件，不识别具体标签文字。
            controls = [bbox for bbox in source_controls if _is_inline_control(bbox, container.source_bbox, container.font_size)]
            if not controls:
                continue
            styles = [_drawing_style(source_drawings, bbox) for bbox in controls]
            if any(style is None for style in styles) or any(style != styles[0] for style in styles[1:]):
                continue
            placement = placements[container.container_id]
            dx = placement.output_bbox[0] - container.source_bbox[0]
            dy = placement.output_bbox[1] - container.source_bbox[1]
            # 空控件保持相对文字锚点；文字容器移动多少，控件就同步移动多少。
            translated = [_translate_bbox(bbox, dx, dy) for bbox in controls]

            checked_indexes = [
                index
                for index, bbox in enumerate(controls)
                if _symbol_chars(source_page, bbox)
            ]
            candidate_symbols = _symbol_chars(candidate_page, placement.output_bbox)
            if len(candidate_symbols) < len(checked_indexes):
                continue
            # 带勾控件以候选中的 Unicode 符号中心为准，避免中英文词长变化导致横向漂移。
            for checked_index, symbol in zip(checked_indexes, candidate_symbols):
                translated[checked_index] = _center_bbox(controls[checked_index], symbol["bbox"])

            # 同时检查旧位置和目标位置，使重复执行能够直接返回 PASS，而不是反复修补。
            source_hits = sum(
                _matches_relative(drawing["rect"], bbox)
                for drawing in candidate_drawings
                for bbox in controls
            )
            target_hits = sum(
                _matches_relative(drawing["rect"], bbox)
                for drawing in candidate_drawings
                for bbox in translated
            )
            control_scale = max(_size(bbox) for bbox in controls)
            groups.append(
                {
                    "container_id": container.container_id,
                    "source_control_bboxes": controls,
                    "target_control_bboxes": translated,
                    "stroke_color": styles[0][0],
                    "stroke_width": styles[0][1],
                    "source_position_hit_count": source_hits,
                    "target_position_hit_count": target_hits,
                    "control_count": len(controls),
                    "normalized_container_shift": max(abs(dx), abs(dy)) / control_scale,
                    "checked_control_count": len(checked_indexes),
                    "candidate_symbol_count": len(candidate_symbols),
                }
            )
    return tuple(groups)


def _is_inline_control(bbox: Rect, text_bbox: Rect, font_size: float) -> bool:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width <= 0 or height <= 0 or font_size <= 0:
        return False
    aspect = width / height
    scale = max(width, height) / font_size
    box_center_y = (bbox[1] + bbox[3]) / 2
    text_center_y = (text_bbox[1] + text_bbox[3]) / 2
    vertical_near = abs(box_center_y - text_center_y) <= max(height, text_bbox[3] - text_bbox[1]) * 0.75
    horizontal_near = bbox[2] >= text_bbox[0] - max(width, height) * 2 and bbox[0] <= text_bbox[2] + max(width, height) * 2
    return 0.75 <= aspect <= 4 / 3 and 0.5 <= scale <= 1.5 and vertical_near and horizontal_near


def _drawing_style(drawings: list[dict[str, object]], bbox: Rect) -> tuple[tuple[float, float, float], float] | None:
    for drawing in drawings:
        if _matches_relative(drawing["rect"], bbox):
            color = drawing.get("color")
            width = drawing.get("width")
            if color is None or width is None:
                return None
            return tuple(float(value) for value in color), float(width)
    return None


def _symbol_chars(page: fitz.Page, bbox: Rect) -> list[dict[str, object]]:
    symbols: list[dict[str, object]] = []
    raw = page.get_text("rawdict", clip=fitz.Rect(bbox))
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    value = str(char.get("c") or "")
                    if value and unicodedata.category(value).startswith("S"):
                        symbols.append({"char": value, "bbox": tuple(float(item) for item in char["bbox"])})
    return sorted(symbols, key=lambda item: (item["bbox"][0], item["bbox"][1]))


def _translate_bbox(bbox: Rect, dx: float, dy: float) -> Rect:
    return tuple(round(value, 4) for value in (bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy))


def _center_bbox(source_bbox: Rect, target_content_bbox: Rect) -> Rect:
    width = source_bbox[2] - source_bbox[0]
    height = source_bbox[3] - source_bbox[1]
    center_x = (target_content_bbox[0] + target_content_bbox[2]) / 2
    center_y = (target_content_bbox[1] + target_content_bbox[3]) / 2
    return tuple(round(value, 4) for value in (center_x - width / 2, center_y - height / 2, center_x + width / 2, center_y + height / 2))


def _matches_relative(rect: fitz.Rect, bbox: Rect, tolerance_ratio: float = 0.05) -> bool:
    tolerance = _size(bbox) * tolerance_ratio
    return all(abs(left - right) <= tolerance for left, right in zip(rect, bbox))


def _size(bbox: Rect) -> float:
    return max(bbox[2] - bbox[0], bbox[3] - bbox[1])
