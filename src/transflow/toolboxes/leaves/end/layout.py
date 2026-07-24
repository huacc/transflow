from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle

from .constants import TOOLBOX_KEY
from .models import EndFinding, EndLayoutPlan, EndPlacement, EndTemplate, Rect

_SCALES = (1.0, 0.92, 0.84, 0.76, 0.68, 0.62)
_LINE_HEIGHTS = (1.0, 0.94, 0.90)


def plan_end_layout(
    template: EndTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[EndLayoutPlan, tuple[EndFinding, ...]]:
    regions = template.translatable_regions
    expected = [region.region_id for region in regions]
    actual = [item.container_id for item in bundle.translations]
    if actual != expected:
        raise ValueError("END_TRANSLATION_ID_MISMATCH")

    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    findings: list[EndFinding] = []
    placements: list[EndPlacement] = []
    for region in regions:
        use_bold = _is_bold(region.font_name) and region.role not in {
            "contact",
            "contact_block",
            "disclaimer",
        }
        selected_font = bold_path if use_bold else font_file
        font_resource = "p10endb" if use_bold else "p10end"
        font_size, line_height, fit = _fit_text(
            page_width=template.width,
            page_height=template.height,
            bbox=region.allowed_bbox,
            text=translated[region.region_id],
            source_font_size=region.font_size,
            font_file=selected_font,
            font_resource=font_resource,
            alignment=region.alignment,
            role=region.role,
        )
        if not fit:
            findings.append(
                EndFinding(
                    code="END_TEXT_OVERFLOW",
                    severity="HARD",
                    owner="end_layout_planner",
                    region_id=region.region_id,
                    message="译文无法在结束页语义块的安全区域内完整装入",
                    evidence={
                        "role": region.role,
                        "source_bbox": region.source_bbox,
                        "allowed_bbox": region.allowed_bbox,
                        "source_font_size": region.font_size,
                        "minimum_font_size": round(
                            _minimum_font_size(region.font_size, region.role), 4
                        ),
                    },
                )
            )
        placements.append(
            EndPlacement(
                region_id=region.region_id,
                translated_text=translated[region.region_id],
                output_bbox=region.allowed_bbox,
                font_file=selected_font,
                font_resource=font_resource,
                font_size=round(font_size, 4),
                line_height=line_height,
                color_srgb=region.color_srgb,
                alignment=region.alignment,
                fit=fit,
            )
        )
    return EndLayoutPlan(
        template.page_id, TOOLBOX_KEY, template.structure_sha256, tuple(placements)
    ), tuple(findings)


def _fit_text(
    *,
    page_width: float,
    page_height: float,
    bbox: Rect,
    text: str,
    source_font_size: float,
    font_file: str,
    font_resource: str,
    alignment: str,
    role: str,
) -> tuple[float, float, bool]:
    minimum = _minimum_font_size(source_font_size, role)
    sizes: list[float] = []
    for scale in _SCALES:
        candidate = max(minimum, source_font_size * scale)
        if not sizes or abs(candidate - sizes[-1]) > 0.02:
            sizes.append(candidate)
    for line_height in _LINE_HEIGHTS:
        for font_size in sizes:
            with fitz.open() as document:
                page = document.new_page(width=page_width, height=page_height)
                spare_height = page.insert_textbox(
                    fitz.Rect(bbox),
                    text,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=font_size,
                    lineheight=line_height,
                    color=(0.0, 0.0, 0.0),
                    align=_fitz_alignment(alignment),
                )
            if spare_height >= 0:
                return font_size, line_height, True
    return minimum, _LINE_HEIGHTS[-1], False


def _minimum_font_size(source_font_size: float, role: str) -> float:
    floor = 5.5 if role in {"contact", "contact_block", "disclaimer"} else 6.0
    return max(floor, source_font_size * 0.62)


def _fitz_alignment(alignment: str) -> int:
    return {
        "left": fitz.TEXT_ALIGN_LEFT,
        "center": fitz.TEXT_ALIGN_CENTER,
        "right": fitz.TEXT_ALIGN_RIGHT,
    }[alignment]


def _is_bold(font_name: str) -> bool:
    lowered = font_name.casefold()
    return any(token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold"))


def contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def color(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)
