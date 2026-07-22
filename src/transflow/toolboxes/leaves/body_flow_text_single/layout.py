"""实现 single 的固定锚点、向右扩展和确定性字号曲线。"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    MAXIMUM_LINE_HEIGHT,
    MINIMUM_LINE_HEIGHT,
    SinglePlacement,
    SingleTextContainer,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy


def _color(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
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
        for _ in range(11):
            middle = (low + high) / 2.0
            remainder = page.insert_textbox(
                pymupdf.Rect(0, 0, width, middle),
                text,
                fontname="TFSingleProbe",
                fontfile=str(font_path),
                fontsize=font_size,
                lineheight=line_height,
                color=_color(color_srgb),
            )
            if remainder >= 0:
                high = middle
            else:
                low = middle
    return round(high + 1.0, 4)


def _content_bottom(
    facts: ExtractedPageFacts,
    containers: tuple[SingleTextContainer, ...],
) -> float:
    owned_ids = {
        object_id for container in containers for object_id in container.source_object_ids
    }
    footer_tops = [
        span.bbox[1]
        for span in facts.text_spans
        if span.object_id not in owned_ids
        and span.bbox[1] >= facts.page.height_points * 0.90
    ]
    footer_tops.extend(
        container.source_bbox[1]
        for container in containers
        if container.role == "margin"
        and container.source_bbox[1] >= facts.page.height_points * 0.90
    )
    return (
        min(footer_tops) - 4.0
        if footer_tops
        else facts.page.height_points - 18.0
    )


def _margin_right_edge(
    facts: ExtractedPageFacts,
    container: SingleTextContainer,
) -> float:
    _x0, y0, source_x1, y1 = container.source_bbox
    blockers = [
        span.bbox[0]
        for span in facts.text_spans
        if span.object_id not in container.source_object_ids
        and span.bbox[0] >= source_x1 - 0.5
        and min(y1, span.bbox[3]) - max(y0, span.bbox[1]) > 0.5
    ]
    available_right = (
        min(blockers) - 4.0 if blockers else facts.page.width_points - 18.0
    )
    return max(source_x1, min(facts.page.width_points - 18.0, available_right))


def _plan_at_scale(
    facts: ExtractedPageFacts,
    containers: tuple[SingleTextContainer, ...],
    translated_by_container: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
) -> tuple[SinglePlacement, ...]:
    body = tuple(item for item in containers if item.role != "margin")
    right_edge = (
        min(
            facts.page.width_points - 24.0,
            max(item.source_bbox[2] for item in body),
        )
        if body
        else facts.page.width_points - 24.0
    )
    bottom = _content_bottom(facts, containers)
    placements: dict[str, SinglePlacement] = {}
    cursor_y: float | None = None
    previous: SingleTextContainer | None = None
    for container in body:
        x0, source_y0, source_x1, _source_y1 = container.source_bbox
        source_size = min(policy.maximum_font_size, container.font_size)
        minimum = max(policy.minimum_font_size, source_size * 0.72)
        font_size = max(minimum, source_size * scale)
        line_height = min(
            MAXIMUM_LINE_HEIGHT,
            max(MINIMUM_LINE_HEIGHT, container.preferred_line_height),
        )
        source_gap = (
            0.0
            if previous is None
            else max(0.0, source_y0 - previous.source_bbox[3])
        )
        if cursor_y is None:
            y0 = source_y0
        elif previous is not None and previous.role == "body" and container.role == "body":
            y0 = cursor_y + source_gap
        else:
            natural_y0 = cursor_y + min(48.0, source_gap)
            y0 = max(natural_y0, source_y0 - source_size * 3.0)
        x1 = max(source_x1, right_edge)
        height = _minimum_text_height(
            facts,
            x1 - x0,
            translated_by_container[container.container_id],
            font_path,
            font_size,
            line_height,
            container.color_srgb,
        )
        y1 = y0 + height
        placements[container.container_id] = SinglePlacement(
            container_id=container.container_id,
            translated_text=translated_by_container[container.container_id],
            output_bbox=(
                round(x0, 4),
                round(y0, 4),
                round(x1, 4),
                round(y1, 4),
            ),
            font_size=round(font_size, 4),
            line_height=round(line_height, 4),
            color_srgb=container.color_srgb,
            fit=y1 <= bottom + 0.01,
        )
        cursor_y = y1
        previous = container

    for container in (item for item in containers if item.role == "margin"):
        x0, y0, source_x1, _ = container.source_bbox
        x1 = _margin_right_edge(facts, container)
        source_size = min(policy.maximum_font_size, container.font_size)
        minimum = max(policy.minimum_font_size, source_size * 0.72)
        font_size = max(minimum, source_size * scale)
        line_height = min(
            MAXIMUM_LINE_HEIGHT,
            max(MINIMUM_LINE_HEIGHT, container.preferred_line_height),
        )
        height = _minimum_text_height(
            facts,
            x1 - x0,
            translated_by_container[container.container_id],
            font_path,
            font_size,
            line_height,
            container.color_srgb,
        )
        y1 = y0 + height
        placements[container.container_id] = SinglePlacement(
            container_id=container.container_id,
            translated_text=translated_by_container[container.container_id],
            output_bbox=(
                round(x0, 4),
                round(y0, 4),
                round(max(source_x1, x1), 4),
                round(y1, 4),
            ),
            font_size=round(font_size, 4),
            line_height=round(line_height, 4),
            color_srgb=container.color_srgb,
            fit=y1 <= facts.crop_box[3] - 4.0 + 0.01,
        )
    return tuple(placements[item.container_id] for item in containers)


def plan_placements(
    facts: ExtractedPageFacts,
    containers: tuple[SingleTextContainer, ...],
    translated_by_container: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
) -> tuple[SinglePlacement, ...]:
    """按既有自然纵向流，为整页选择首个可容纳字号档。"""

    if tuple(translated_by_container) != tuple(item.container_id for item in containers):
        raise ValueError("single_translation_ids_do_not_match_reading_order")
    last: tuple[SinglePlacement, ...] = ()
    for scale in (1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.72):
        last = _plan_at_scale(
            facts,
            containers,
            translated_by_container,
            policy,
            font_path,
            scale,
        )
        if all(item.fit for item in last):
            return last
    return last
