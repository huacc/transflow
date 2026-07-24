"""Plan translated text inside immutable anchored owner boundaries."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from transflow.toolboxes.leaves.body_anchored_blocks.models import (
    AnchoredBlocksTemplate,
    AnchoredContainer,
    AnchoredFinding,
    AnchoredLayoutPlan,
    AnchoredPlacement,
    AnchoredRepairAttempt,
    Rect,
)
from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

FIT_PROFILES = (
    ("source-rhythm", 1.00, 1.15),
    ("font-92", 0.92, 1.08),
    ("font-84", 0.84, 1.04),
    ("font-76", 0.76, 1.00),
    ("font-68", 0.68, 1.00),
)


def plan_anchored_layout(
    template: AnchoredBlocksTemplate,
    bundle: PageTranslationBundle,
    policy: P8ToolboxPolicy,
    font_path: Path,
) -> tuple[AnchoredLayoutPlan, tuple[AnchoredFinding, ...]]:
    """Select bounded fit profiles and preserve source style relationships."""

    translated = {
        item.container_id: item.translated_text.strip()
        for item in bundle.translations
    }
    containers = tuple(
        item
        for item in template.translatable_containers
        if item.container_id in translated
    )
    selected_indices: dict[str, int] = {}
    attempts: list[AnchoredRepairAttempt] = []
    for container in containers:
        selected = len(FIT_PROFILES) - 1
        for index, (profile, scale, line_height) in enumerate(FIT_PROFILES):
            fit, _ = _measure(
                template,
                container,
                translated[container.container_id],
                policy,
                font_path,
                scale,
                line_height,
            )
            if index or not fit:
                attempts.append(
                    AnchoredRepairAttempt(
                        container.container_id,
                        container.block_owner_id,
                        profile,
                        fit,
                    )
                )
            selected = index
            if fit:
                break
        selected_indices[container.container_id] = selected

    by_style: dict[
        tuple[str, float, int, str],
        list[AnchoredContainer],
    ] = {}
    for container in containers:
        by_style.setdefault(
            (
                container.font_name,
                container.font_size,
                container.color_srgb,
                container.role,
            ),
            [],
        ).append(container)
    for group in by_style.values():
        shared_index = max(
            selected_indices[item.container_id] for item in group
        )
        for container in group:
            selected_indices[container.container_id] = shared_index

    placements = tuple(
        _placement(
            template,
            container,
            translated[container.container_id],
            policy,
            font_path,
            selected_indices[container.container_id],
        )
        for container in containers
    )
    findings: list[AnchoredFinding] = []
    if template.ambiguous_container_ids:
        findings.append(
            AnchoredFinding(
                "ANCHORED_BLOCK_OWNERSHIP_AMBIGUOUS",
                "HARD",
                template.ambiguous_container_ids[0],
            )
        )
    first_unfit = next((item for item in placements if not item.fit), None)
    if first_unfit is not None:
        findings.append(
            AnchoredFinding(
                "ANCHORED_BLOCK_TEXT_OVERFLOW",
                "HARD",
                first_unfit.container_id,
            )
        )
    collision = _first_collision(placements)
    if collision is not None:
        findings.append(
            AnchoredFinding(
                "ANCHORED_BLOCK_TEXT_COLLISION",
                "HARD",
                collision,
            )
        )
    return (
        AnchoredLayoutPlan(
            template.page_id,
            template.toolbox_key,
            template.structure_sha256,
            placements,
            tuple(attempts),
        ),
        tuple(findings),
    )


def _placement(
    template: AnchoredBlocksTemplate,
    container: AnchoredContainer,
    text: str,
    policy: P8ToolboxPolicy,
    font_path: Path,
    profile_index: int,
) -> AnchoredPlacement:
    profile, scale, line_height = FIT_PROFILES[profile_index]
    fit, height = _measure(
        template,
        container,
        text,
        policy,
        font_path,
        scale,
        line_height,
    )
    x0, y0, x1, y1 = container.allowed_bbox
    return AnchoredPlacement(
        container.container_id,
        container.block_owner_id,
        text,
        (x0, y0, x1, round(min(y1, y0 + height), 4)),
        _font_size(container, policy, scale),
        line_height,
        container.color_srgb,
        container.alignment,
        profile,
        fit,
    )


def _measure(
    template: AnchoredBlocksTemplate,
    container: AnchoredContainer,
    text: str,
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
    line_height: float,
) -> tuple[bool, float]:
    width = max(
        container.allowed_bbox[2] - container.allowed_bbox[0],
        4.0,
    )
    available = max(
        container.allowed_bbox[3] - container.allowed_bbox[1],
        2.0,
    )
    font_size = _font_size(container, policy, scale)
    low = min(available, max(font_size * line_height, 2.0))
    with pymupdf.open() as document:
        page = document.new_page(
            width=template.width,
            height=max(template.height, available + 10.0),
        )
        font_name = "TFAnchoredProbe"
        page.insert_font(fontname=font_name, fontfile=str(font_path))
        if (
            page.insert_textbox(
                pymupdf.Rect(0, 0, width, available),
                text,
                fontname=font_name,
                fontsize=font_size,
                lineheight=line_height,
                color=_color(container.color_srgb),
                align=_fitz_alignment(container.alignment),
            )
            < 0
        ):
            return False, available
        high = available
        for _ in range(10):
            middle = (low + high) / 2.0
            remainder = page.insert_textbox(
                pymupdf.Rect(0, 0, width, middle),
                text,
                fontname=font_name,
                fontsize=font_size,
                lineheight=line_height,
                color=_color(container.color_srgb),
                align=_fitz_alignment(container.alignment),
            )
            if remainder >= 0:
                high = middle
            else:
                low = middle
    return True, round(min(available, high + 1.0), 4)


def _font_size(
    container: AnchoredContainer,
    policy: P8ToolboxPolicy,
    scale: float,
) -> float:
    source = min(policy.maximum_font_size, container.font_size)
    return round(
        max(
            policy.minimum_font_size,
            source * policy.font_scale * scale,
        ),
        4,
    )


def _first_collision(
    placements: tuple[AnchoredPlacement, ...],
) -> str | None:
    for index, current in enumerate(placements):
        if not current.fit:
            continue
        for previous in placements[:index]:
            if previous.fit and _intersection_area(
                current.output_bbox,
                previous.output_bbox,
            ) > 0.05:
                return current.container_id
    return None


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _fitz_alignment(value: str) -> int:
    return {"LEFT": 0, "CENTER": 1, "RIGHT": 2}.get(value, 0)


def _color(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )
