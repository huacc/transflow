from __future__ import annotations

import re
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle

from . import TOOLBOX_KEY
from .models import TableFinding, TableLayoutPlan, TablePlacement, TableTemplate


_SCALES = (1.0, 0.92, 0.85, 0.78, 0.70, 0.62, 0.55)
_LINE_HEIGHTS = (1.05, 1.0, 0.95)
_CHINESE_DATE = re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_ENGLISH_MONTHS = (
    r"jan(?:uary)?",
    r"feb(?:ruary)?",
    r"mar(?:ch)?",
    r"apr(?:il)?",
    r"may",
    r"jun(?:e)?",
    r"jul(?:y)?",
    r"aug(?:ust)?",
    r"sep(?:t(?:ember)?)?",
    r"oct(?:ober)?",
    r"nov(?:ember)?",
    r"dec(?:ember)?",
)


def plan_table_layout(
    template: TableTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[TableLayoutPlan, tuple[TableFinding, ...]]:
    expected = [cell.container_id for cell in template.translatable_cells]
    actual = [item.container_id for item in bundle.translations]
    if actual != expected:
        raise ValueError("CELL_TRANSLATION_ID_MISMATCH")
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    findings: list[TableFinding] = []
    placements: list[TablePlacement] = []
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file

    for cell in template.translatable_cells:
        text = translated[cell.container_id].strip()
        missing = _missing_protected_tokens(cell.source_text, text, cell.protected_tokens)
        if missing:
            findings.append(
                _finding(
                    "PROTECTED_TOKEN_MISSING",
                    "translation_validator",
                    cell.container_id,
                    "混合文字格中的数字、金额、日期或币种未原样保留",
                    missing_tokens=missing,
                )
            )
        is_table_cell = cell.table_id == "table-00"
        horizontal_clearance = max(0.8, cell.font_size * 0.10) if is_table_cell else 0.0
        vertical_clearance = max(1.5, cell.font_size * 0.25) if is_table_cell else 0.5
        x0 = max(cell.source_bbox[0], cell.cell_bbox[0] + horizontal_clearance)
        x1 = cell.cell_bbox[2] - horizontal_clearance
        safe_y0 = cell.cell_bbox[1] + vertical_clearance
        y0 = max(cell.source_bbox[1], safe_y0)
        y1 = cell.cell_bbox[3] - vertical_clearance
        primary_bbox = (x0, y0, x1, y1)
        expanded_bbox = (x0, safe_y0, x1, y1)
        allowed_bbox = (
            cell.cell_bbox[0],
            min(cell.cell_bbox[1], cell.source_bbox[1]),
            cell.cell_bbox[2],
            max(cell.cell_bbox[3], cell.source_bbox[3]),
        )
        candidates = [primary_bbox]
        if is_table_cell and expanded_bbox != primary_bbox:
            candidates.append(expanded_bbox)
        valid_candidates = [
            bbox
            for bbox in candidates
            if _contains(allowed_bbox, bbox) and bbox[2] > bbox[0] + 1.0 and bbox[3] > bbox[1] + 1.0
        ]
        if not valid_candidates:
            output_bbox = primary_bbox
            findings.append(
                _finding(
                    "CROSS_CELL_WRITE",
                    "layout_planner",
                    cell.container_id,
                    "固定左上角后的可写区域超出结构格与原始字形的联合边界",
                    cell_bbox=cell.cell_bbox,
                    source_bbox=cell.source_bbox,
                    allowed_bbox=allowed_bbox,
                    output_bbox=output_bbox,
                )
            )
            placements.append(
                TablePlacement(
                    cell.container_id,
                    text,
                    cell.cell_bbox,
                    allowed_bbox,
                    output_bbox,
                    (x0, y0),
                    bold_path if cell.font_weight == "bold" else font_file,
                    "p6tableb" if cell.font_weight == "bold" else "p6table",
                    cell.font_size,
                    1.0,
                    cell.color_srgb,
                    cell.alignment,
                    False,
                )
            )
            continue
        selected_font = bold_path if cell.font_weight == "bold" else font_file
        selected_resource = "p6tableb" if cell.font_weight == "bold" else "p6table"
        output_bbox = valid_candidates[0]
        font_size, line_height, fit = _fit_text(
            template.width, template.height, output_bbox, text, cell.font_size,
            selected_font, selected_resource, cell.alignment,
        )
        for candidate_bbox in valid_candidates[1:]:
            candidate_size, candidate_line_height, candidate_fit = _fit_text(
                template.width, template.height, candidate_bbox, text, cell.font_size,
                selected_font, selected_resource, cell.alignment,
            )
            if candidate_fit and (not fit or candidate_size > font_size + 0.02):
                output_bbox = candidate_bbox
                font_size = candidate_size
                line_height = candidate_line_height
                fit = True
        x0, y0 = output_bbox[:2]
        if not fit:
            findings.append(
                _finding(
                    "CELL_TEXT_OVERFLOW",
                    "layout_planner",
                    cell.container_id,
                    "译文在原单元格和字号下限内无法完整装入",
                    cell_bbox=cell.cell_bbox,
                    source_font_size=cell.font_size,
                    minimum_font_size=round(max(4.0, cell.font_size * 0.55), 4),
                )
            )
        placements.append(
            TablePlacement(
                cell.container_id,
                text,
                cell.cell_bbox,
                tuple(round(value, 4) for value in allowed_bbox),
                tuple(round(value, 4) for value in output_bbox),
                (round(x0, 4), round(y0, 4)),
                selected_font,
                selected_resource,
                round(font_size, 4),
                line_height,
                cell.color_srgb,
                cell.alignment,
                fit,
            )
        )
    return TableLayoutPlan(template.page_id, TOOLBOX_KEY, template.structure.structure_sha256, tuple(placements)), tuple(findings)


def _missing_protected_tokens(source_text: str, translated_text: str, tokens: tuple[str, ...]) -> tuple[str, ...]:
    missing = [token for token in tokens if token not in translated_text]
    if not missing:
        return ()
    normalized_source = source_text.replace("⽉", "月").replace("⽇", "日")
    translated_casefold = translated_text.casefold()
    for match in _CHINESE_DATE.finditer(normalized_source):
        month = int(match.group("month"))
        month_token = match.group("month")
        if not 1 <= month <= 12 or month_token not in missing:
            continue
        if match.group("year") not in translated_text or match.group("day") not in translated_text:
            continue
        if re.search(rf"\b{_ENGLISH_MONTHS[month - 1]}\b", translated_casefold):
            missing.remove(month_token)
    return tuple(missing)


def _fit_text(
    width: float,
    height: float,
    bbox: tuple[float, float, float, float],
    text: str,
    source_font_size: float,
    font_file: str,
    font_resource: str,
    alignment: str,
) -> tuple[float, float, bool]:
    minimum = max(4.0, source_font_size * 0.55)
    sizes: list[float] = []
    for scale in _SCALES:
        value = max(minimum, source_font_size * scale)
        if not sizes or abs(value - sizes[-1]) > 0.02:
            sizes.append(value)
    for line_height in _LINE_HEIGHTS:
        for font_size in sizes:
            with fitz.open() as document:
                page = document.new_page(width=width, height=height)
                result = page.insert_textbox(
                    fitz.Rect(bbox),
                    text,
                    fontname=font_resource,
                    fontfile=font_file,
                    fontsize=font_size,
                    lineheight=line_height,
                    align=_fitz_alignment(alignment),
                )
            if result >= 0:
                return font_size, line_height, True
    return minimum, _LINE_HEIGHTS[-1], False


def _fitz_alignment(value: str) -> int:
    return {
        "center": fitz.TEXT_ALIGN_CENTER,
        "right": fitz.TEXT_ALIGN_RIGHT,
    }.get(value, fitz.TEXT_ALIGN_LEFT)


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _finding(code: str, owner: str, container_id: str | None, message: str, **evidence: object) -> TableFinding:
    return TableFinding(code, "HARD", owner, container_id, message, dict(evidence))
