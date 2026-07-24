"""Plan translated table text inside immutable cell boundaries."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from transflow.application.translation_completeness import extract_required_literals
from transflow.toolboxes.leaves.body_table.models import (
    TableCell,
    TableFinding,
    TableLayoutPlan,
    TablePlacement,
    TableTemplate,
)
from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

_SCALES = (1.0, 0.92, 0.85, 0.78, 0.70, 0.62, 0.55)
_LINE_HEIGHTS = (1.05, 1.0, 0.95)


def plan_table_layout(
    template: TableTemplate,
    bundle: PageTranslationBundle,
    policy: P8ToolboxPolicy,
    font_path: Path,
) -> tuple[TableLayoutPlan, tuple[TableFinding, ...]]:
    """Use the Spike fit ladder while leaving rendering to the shared Patch."""

    expected = tuple(item.container_id for item in template.translatable_cells)
    actual = tuple(item.container_id for item in bundle.translations)
    if actual != expected:
        raise ValueError("CELL_TRANSLATION_ID_MISMATCH")
    translated = {
        item.container_id: item.translated_text.strip()
        for item in bundle.translations
    }
    findings: list[TableFinding] = []
    placements: list[TablePlacement] = []
    if not template.structures:
        findings.append(
            TableFinding("TABLE_DIRECT_EVIDENCE_MISSING", "HARD", None)
        )

    with pymupdf.open() as probe_document:
        probe_page = probe_document.new_page(
            width=template.width,
            height=template.height,
        )
        font_name = "TFTableProbe"
        probe_page.insert_font(fontname=font_name, fontfile=str(font_path))
        for cell in template.translatable_cells:
            text = translated[cell.container_id]
            if cell.ownership_ambiguous:
                findings.append(
                    TableFinding(
                        "TABLE_CELL_OWNERSHIP_AMBIGUOUS",
                        "HARD",
                        cell.container_id,
                    )
                )
            missing = tuple(
                item
                for item in extract_required_literals(cell.source_text)
                if item not in text
            )
            if missing:
                findings.append(
                    TableFinding(
                        "PROTECTED_TOKEN_MISSING",
                        "HARD",
                        cell.container_id,
                    )
                )
            output_bbox = _layout_search_region(cell)
            font_size, line_height, fit = _fit_text(
                probe_page,
                output_bbox,
                text,
                cell.font_size,
                policy,
                font_name,
                cell.alignment,
            )
            if not _contains(cell.hard_legal_boundary, output_bbox):
                findings.append(
                    TableFinding(
                        "CROSS_CELL_WRITE",
                        "HARD",
                        cell.container_id,
                    )
                )
                fit = False
            if not fit:
                findings.append(
                    TableFinding(
                        "CELL_TEXT_OVERFLOW",
                        "HARD",
                        cell.container_id,
                    )
                )
            placements.append(
                TablePlacement(
                    container_id=cell.container_id,
                    translated_text=text,
                    hard_legal_boundary=cell.hard_legal_boundary,
                    output_bbox=output_bbox,
                    font_size=font_size,
                    line_height=line_height,
                    color_srgb=cell.color_srgb,
                    alignment=cell.alignment,
                    fit=fit,
                )
            )
    return (
        TableLayoutPlan(
            template.page_id,
            template.toolbox_key,
            tuple(placements),
        ),
        tuple(_unique_findings(findings)),
    )


def _layout_search_region(cell: TableCell) -> tuple[float, float, float, float]:
    boundary = cell.hard_legal_boundary
    if cell.table_id == "page-context":
        y0 = min(cell.source_bbox[1], boundary[1])
        y1 = max(cell.source_bbox[3], boundary[3])
        return (
            round(boundary[0], 4),
            round(y0, 4),
            round(boundary[2], 4),
            round(y1, 4),
        )
    width = boundary[2] - boundary[0]
    height = boundary[3] - boundary[1]
    horizontal = min(max(0.35, cell.font_size * 0.08), max(0.35, width * 0.08))
    vertical = min(max(0.35, cell.font_size * 0.10), max(0.35, height * 0.12))
    x0 = boundary[0] + horizontal
    x1 = boundary[2] - horizontal
    y0 = boundary[1] + vertical
    y1 = boundary[3] - vertical
    if x1 <= x0 + 1.0 or y1 <= y0 + 1.0:
        return (
            round(boundary[0], 4),
            round(boundary[1], 4),
            round(boundary[2], 4),
            round(boundary[3], 4),
        )
    return (
        round(x0, 4),
        round(y0, 4),
        round(x1, 4),
        round(y1, 4),
    )


def _fit_text(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
    text: str,
    source_font_size: float,
    policy: P8ToolboxPolicy,
    font_name: str,
    alignment: str,
) -> tuple[float, float, bool]:
    start = min(policy.maximum_font_size, source_font_size)
    minimum = max(
        min(policy.minimum_font_size, start),
        source_font_size * 0.55,
    )
    sizes: list[float] = []
    for scale in _SCALES:
        value = max(minimum, start * scale)
        if not sizes or abs(value - sizes[-1]) > 0.02:
            sizes.append(value)
    for line_height in _LINE_HEIGHTS:
        for font_size in sizes:
            remainder = page.insert_textbox(
                pymupdf.Rect(bbox),
                text,
                fontname=font_name,
                fontsize=font_size,
                lineheight=line_height,
                align=_fitz_alignment(alignment),
            )
            if remainder >= 0:
                return round(font_size, 4), line_height, True
    return round(minimum, 4), _LINE_HEIGHTS[-1], False


def _fitz_alignment(value: str) -> int:
    return {
        "CENTER": pymupdf.TEXT_ALIGN_CENTER,
        "RIGHT": pymupdf.TEXT_ALIGN_RIGHT,
    }.get(value, pymupdf.TEXT_ALIGN_LEFT)


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float = 0.05,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _unique_findings(
    findings: list[TableFinding],
) -> tuple[TableFinding, ...]:
    output: list[TableFinding] = []
    seen: set[tuple[str, str | None]] = set()
    for finding in findings:
        identity = finding.code, finding.container_id
        if identity not in seen:
            output.append(finding)
            seen.add(identity)
    return tuple(output)
