from __future__ import annotations

from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle

from . import TOOLBOX_KEY
from .models import ContentsFinding, ContentsLayoutPlan, ContentsPlacement, ContentsTemplate, Rect


_SCALES = (1.0, 0.92, 0.85, 0.78, 0.70, 0.62)
_LINE_HEIGHTS = (1.0, 0.95, 0.9)


def plan_contents_layout(
    template: ContentsTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[ContentsLayoutPlan, tuple[ContentsFinding, ...]]:
    expected = [container.container_id for container in template.containers]
    actual = [item.container_id for item in bundle.translations]
    if actual != expected:
        raise ValueError("CONTENTS_TRANSLATION_ID_MISMATCH")
    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    findings: list[ContentsFinding] = []
    placements: list[ContentsPlacement] = []

    for container in template.containers:
        text = translated[container.container_id]
        use_bold = container.role in {"title", "group_heading"} or _is_bold(container.font_name)
        selected_font = bold_path if use_bold else font_file
        resource = "p8contentsb" if use_bold else "p8contents"
        font_size, line_height, fit = _fit_text(
            template.width,
            template.height,
            container.allowed_bbox,
            text,
            container.font_size,
            selected_font,
            resource,
            container.role,
        )
        if not fit:
            findings.append(
                _finding(
                    "CONTENTS_TEXT_OVERFLOW",
                    "contents_layout_planner",
                    container.container_id,
                    "译文在固定条目锚点和相邻行边界内无法完整装入",
                    role=container.role,
                    source_bbox=container.source_bbox,
                    allowed_bbox=container.allowed_bbox,
                    source_font_size=container.font_size,
                    minimum_font_size=round(_minimum_font_size(container.font_size, container.role), 4),
                )
            )
        placements.append(
            ContentsPlacement(
                container_id=container.container_id,
                translated_text=text,
                output_bbox=container.allowed_bbox,
                font_file=selected_font,
                font_resource=resource,
                font_size=round(font_size, 4),
                line_height=line_height,
                color_srgb=container.color_srgb,
                fit=fit,
            )
        )
    return ContentsLayoutPlan(template.page_id, TOOLBOX_KEY, template.structure_sha256, tuple(placements)), tuple(findings)


def _fit_text(
    page_width: float,
    page_height: float,
    bbox: Rect,
    text: str,
    source_font_size: float,
    font_file: str,
    font_resource: str,
    role: str,
) -> tuple[float, float, bool]:
    minimum = _minimum_font_size(source_font_size, role)
    sizes: list[float] = []
    for scale in _SCALES:
        value = max(minimum, source_font_size * scale)
        if not sizes or abs(value - sizes[-1]) > 0.02:
            sizes.append(value)
    for line_height in _LINE_HEIGHTS:
        for font_size in sizes:
            with fitz.open() as document:
                page = document.new_page(width=page_width, height=page_height)
                result = page.insert_textbox(
                    fitz.Rect(bbox),
                    text,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=font_size,
                    lineheight=line_height,
                    align=fitz.TEXT_ALIGN_LEFT,
                )
            if result >= 0:
                return font_size, line_height, True
    return minimum, _LINE_HEIGHTS[-1], False


def _minimum_font_size(source_font_size: float, role: str) -> float:
    floor = 6.0 if role == "title" else 5.0
    scale = 0.70 if role == "title" else 0.62
    return max(floor, source_font_size * scale)


def _is_bold(font_name: str) -> bool:
    lowered = font_name.casefold()
    return any(token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold"))


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence: object) -> ContentsFinding:
    return ContentsFinding(code, "HARD", owner, container_id, message, dict(evidence))
