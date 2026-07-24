"""Plan multi-column text independently per source-derived column band."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.body_flow_text_multi.models import (
    ColumnBand,
    MultiColumnLayoutPlan,
    MultiColumnTemplate,
    MultiFinding,
    MultiPlacement,
    MultiTextContainer,
)
from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

FIT_PROFILES = (
    ("source", 1.00),
    ("compact-95", 0.95),
    ("compact-90", 0.90),
    ("compact-85", 0.85),
    ("compact-80", 0.80),
    ("compact-75", 0.75),
    ("minimum-72", 0.72),
)


def plan_multi_column_layout(
    facts: ExtractedPageFacts,
    template: MultiColumnTemplate,
    bundle: PageTranslationBundle,
    policy: P8ToolboxPolicy,
    font_path: Path,
) -> tuple[MultiColumnLayoutPlan, tuple[MultiFinding, ...]]:
    """Select the first deterministic profile fitting every owned container."""

    translated = {
        item.container_id: item.translated_text for item in bundle.translations
    }
    if not template.columns:
        plan = MultiColumnLayoutPlan(
            template.page_id,
            template.toolbox_key,
            "unsupported",
            (),
        )
        return plan, (
            MultiFinding(
                "MULTI_COLUMN_COUNT_UNSUPPORTED",
                "HARD",
                template.ambiguous_container_ids[0]
                if template.ambiguous_container_ids
                else None,
            ),
        )

    last: tuple[MultiPlacement, ...] = ()
    selected = FIT_PROFILES[-1][0]
    for profile_id, scale in FIT_PROFILES:
        last = _plan_at_scale(
            facts,
            template,
            translated,
            policy,
            font_path,
            scale,
        )
        selected = profile_id
        if all(item.fit for item in last):
            break
    findings: list[MultiFinding] = []
    if template.ambiguous_container_ids:
        findings.append(
            MultiFinding(
                "MULTI_COLUMN_OWNERSHIP_AMBIGUOUS",
                "HARD",
                template.ambiguous_container_ids[0],
            )
        )
    first_unfit = next((item for item in last if not item.fit), None)
    if first_unfit is not None:
        findings.append(
            MultiFinding(
                "MULTI_TEXT_OVERFLOW",
                "HARD",
                first_unfit.container_id,
            )
        )
    return (
        MultiColumnLayoutPlan(
            template.page_id,
            template.toolbox_key,
            selected,
            last,
        ),
        tuple(findings),
    )


def _plan_at_scale(
    facts: ExtractedPageFacts,
    template: MultiColumnTemplate,
    translated: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
) -> tuple[MultiPlacement, ...]:
    assignment = {
        item.container_id: item.column_id for item in template.assignments
    }
    placements: dict[str, MultiPlacement] = {}
    for column in template.columns:
        values = [
            item
            for item in template.containers
            if assignment[item.container_id] == column.column_id
        ]
        placements.update(
            _plan_column(
                facts,
                column,
                values,
                translated,
                policy,
                font_path,
                scale,
            )
        )
    for container in template.containers:
        kind = assignment[container.container_id]
        if kind not in {"span", "margin"}:
            continue
        placements[container.container_id] = _plan_fixed(
            facts,
            container,
            translated,
            policy,
            font_path,
            scale,
            page_bottom=(
                facts.page.height_points - 4.0
                if kind == "margin"
                else facts.page.height_points - 20.0
            ),
        )
    ordered = tuple(
        placements[item.container_id]
        for item in template.containers
        if item.container_id in placements
    )
    return _mark_collisions(ordered, assignment)


def _plan_column(
    facts: ExtractedPageFacts,
    column: ColumnBand,
    containers: list[MultiTextContainer],
    translated: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
) -> dict[str, MultiPlacement]:
    output: dict[str, MultiPlacement] = {}
    cursor: float | None = None
    previous: MultiTextContainer | None = None
    for container in containers:
        source_size = min(policy.maximum_font_size, container.font_size)
        font_size = max(
            policy.minimum_font_size,
            source_size * policy.font_scale * scale,
        )
        line_height = min(1.35, max(1.20, container.preferred_line_height))
        source_gap = (
            0.0
            if previous is None
            else max(0.0, container.source_bbox[1] - previous.source_bbox[3])
        )
        y0 = (
            container.source_bbox[1]
            if cursor is None
            else cursor + min(source_gap, max(font_size * 1.5, 3.0))
        )
        text = translated.get(container.container_id, container.source_text)
        height = _minimum_text_height(
            facts,
            max(column.right - column.left, 4.0),
            text,
            font_path,
            font_size,
            line_height,
            container.color_srgb,
        )
        y1 = y0 + height
        output[container.container_id] = MultiPlacement(
            container.container_id,
            text,
            container.role,
            container.source_bbox,
            (
                round(column.left, 4),
                round(y0, 4),
                round(column.right, 4),
                round(y1, 4),
            ),
            round(font_size, 4),
            round(line_height, 4),
            container.color_srgb,
            y1 <= column.content_bottom + 0.01,
        )
        cursor = y1
        previous = container
    return output


def _plan_fixed(
    facts: ExtractedPageFacts,
    container: MultiTextContainer,
    translated: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
    *,
    page_bottom: float,
) -> MultiPlacement:
    x0, y0, source_x1, _ = container.source_bbox
    source_size = min(policy.maximum_font_size, container.font_size)
    font_size = max(
        policy.minimum_font_size,
        source_size * policy.font_scale * scale,
    )
    line_height = min(1.35, max(1.20, container.preferred_line_height))
    x1 = min(
        facts.page.width_points - 4.0,
        max(source_x1, x0 + facts.page.width_points * 0.45),
    )
    text = translated.get(container.container_id, container.source_text)
    height = _minimum_text_height(
        facts,
        max(x1 - x0, 4.0),
        text,
        font_path,
        font_size,
        line_height,
        container.color_srgb,
    )
    y1 = y0 + height
    return MultiPlacement(
        container.container_id,
        text,
        container.role,
        container.source_bbox,
        (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)),
        round(font_size, 4),
        round(line_height, 4),
        container.color_srgb,
        y1 <= page_bottom + 0.01,
    )


def _minimum_text_height(
    facts: ExtractedPageFacts,
    width: float,
    text: str,
    font_path: Path,
    font_size: float,
    line_height: float,
    color_srgb: int,
) -> float:
    low = max(font_size * line_height, 2.0)
    high = max(facts.page.height_points * 1.8, low + 10.0)
    with pymupdf.open() as document:
        page = document.new_page(
            width=facts.page.width_points,
            height=max(facts.page.height_points, high + 10.0),
        )
        font_name = "TFMultiProbe"
        page.insert_font(fontname=font_name, fontfile=str(font_path))
        for _ in range(11):
            middle = (low + high) / 2.0
            remainder = page.insert_textbox(
                pymupdf.Rect(0, 0, width, middle),
                text,
                fontname=font_name,
                fontsize=font_size,
                lineheight=line_height,
                color=_color(color_srgb),
            )
            if remainder >= 0:
                high = middle
            else:
                low = middle
    return round(high + 1.0, 4)


def _mark_collisions(
    placements: tuple[MultiPlacement, ...],
    assignment: dict[str, str],
) -> tuple[MultiPlacement, ...]:
    output = list(placements)
    for index, current in enumerate(output):
        if assignment.get(current.container_id) == "margin":
            continue
        collision = any(
            assignment.get(previous.container_id) != "margin"
            and _intersection_area(current.output_bbox, previous.output_bbox) > 0.05
            for previous in output[:index]
        )
        if collision and current.fit:
            output[index] = MultiPlacement(
                current.container_id,
                current.translated_text,
                current.role,
                current.source_bbox,
                current.output_bbox,
                current.font_size,
                current.line_height,
                current.color_srgb,
                False,
            )
    return tuple(output)


def _intersection_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _color(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )
