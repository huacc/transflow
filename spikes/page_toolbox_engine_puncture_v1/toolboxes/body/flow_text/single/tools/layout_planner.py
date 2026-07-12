from __future__ import annotations

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle

from . import TOOLBOX_KEY
from .models import LayoutPlacement, SingleColumnLayoutPlan, SingleColumnTemplate, ToolboxFinding


def plan_layout(
    template: SingleColumnTemplate,
    translations: PageTranslationBundle,
    *,
    font_file: str,
    font_resource: str = "p3cjk",
) -> tuple[SingleColumnLayoutPlan, tuple[ToolboxFinding, ...]]:
    translated_by_id = {item.container_id: item.translated_text for item in translations.translations}
    expected = [item.container_id for item in template.containers]
    if list(translated_by_id) != expected:
        raise ValueError("translation_ids_do_not_match_template_order")

    right_edge = min(template.width - 24.0, max(item.source_bbox[2] for item in template.containers if item.role != "margin"))
    placements: list[LayoutPlacement] = []
    findings: list[ToolboxFinding] = []
    for index, container in enumerate(template.containers):
        next_y = _next_distinct_y(template, index)
        x0, y0, source_x1, source_y1 = container.source_bbox
        x1 = source_x1 if container.role == "margin" else max(source_x1, right_edge)
        available_bottom = min(template.height - 18.0, next_y - 2.0) if next_y is not None else template.height - 18.0
        y1 = max(source_y1 + max(1.5, container.font_size * 0.3), available_bottom)
        if next_y is not None:
            y1 = min(y1, next_y - 2.0)
        output_bbox = (x0, y0, x1, max(y0 + 2.0, y1))
        translated_text = translated_by_id[container.container_id]
        font_size, line_height, fit = _fit_text(
            template.width,
            template.height,
            output_bbox,
            translated_text,
            container.font_size,
            font_file,
            font_resource,
            container.color_srgb,
        )
        if not fit:
            findings.append(
                ToolboxFinding(
                    "LAYOUT_TEXT_OVERFLOW",
                    "HARD",
                    "layout_planner",
                    container.container_id,
                    "译文在锚点固定和字号下限内无法装入可用区域",
                )
            )
        placements.append(
            LayoutPlacement(
                container.container_id,
                translated_text,
                tuple(round(value, 4) for value in output_bbox),
                container.anchor,
                round(font_size, 4),
                line_height,
                container.color_srgb,
                fit,
            )
        )
    return SingleColumnLayoutPlan(template.page_id, TOOLBOX_KEY, font_file, font_resource, tuple(placements)), tuple(findings)


def _next_distinct_y(template: SingleColumnTemplate, index: int) -> float | None:
    current_y = template.containers[index].source_bbox[1]
    for later in template.containers[index + 1 :]:
        if later.source_bbox[1] > current_y + 1.0:
            return later.source_bbox[1]
    return None


def _fit_text(
    page_width: float,
    page_height: float,
    bbox: tuple[float, float, float, float],
    text: str,
    source_font_size: float,
    font_file: str,
    font_resource: str,
    color_srgb: int,
) -> tuple[float, float, bool]:
    minimum = max(6.0, source_font_size * 0.72)
    sizes = []
    for scale in (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.72):
        value = max(minimum, source_font_size * scale)
        if not sizes or abs(value - sizes[-1]) > 0.01:
            sizes.append(value)
    for line_height in (1.15, 1.08, 1.0):
        for font_size in sizes:
            with fitz.open() as probe:
                page = probe.new_page(width=page_width, height=page_height)
                result = page.insert_textbox(
                    fitz.Rect(bbox),
                    text,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=font_size,
                    lineheight=line_height,
                    color=_color(color_srgb),
                )
            if result >= 0:
                return font_size, line_height, True
    return sizes[-1], 1.0, False


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)
