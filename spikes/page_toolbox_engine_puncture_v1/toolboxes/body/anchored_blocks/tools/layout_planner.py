from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle
from shared_pdf_kernel.fonts import probe_font

from . import TOOLBOX_KEY
from .models import (
    AnchoredBlocksTemplate,
    AnchoredFinding,
    AnchoredLayoutPlan,
    AnchoredPlacement,
    BlockRepairAttempt,
    Rect,
)


_PROFILES = (
    ("source-rhythm", 1.00, 1.15),
    ("tighter-leading", 1.00, 1.04),
    ("font-92", 0.92, 1.08),
    ("font-84", 0.84, 1.04),
    ("font-76", 0.76, 1.00),
    ("font-68", 0.68, 1.00),
)


def plan_anchored_layout(
    template: AnchoredBlocksTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[AnchoredLayoutPlan, tuple[AnchoredFinding, ...]]:
    expected = [container.container_id for container in template.containers]
    actual = [item.container_id for item in bundle.translations]
    if actual != expected:
        raise ValueError("ANCHORED_BLOCKS_TRANSLATION_ID_MISMATCH")
    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file

    placements: list[AnchoredPlacement] = []
    attempts: list[BlockRepairAttempt] = []
    findings: list[AnchoredFinding] = []
    for container in template.containers:
        text = translated[container.container_id]
        selected_font = bold_path if _is_bold(container.font_name) else font_file
        resource = "p11anchoredb" if selected_font == bold_path and bold_path != font_file else "p11anchored"
        font_probe = probe_font(Path(selected_font), text)
        if not font_probe.covers_text:
            findings.append(
                _finding(
                    "FONT_GLYPH_MISSING",
                    "anchored_blocks_layout_planner",
                    container.block_owner_id,
                    container.container_id,
                    "目标字体不能覆盖译文字形",
                    missing_codepoints=font_probe.missing_codepoints,
                )
            )
            placements.append(_unfit_placement(container, text, selected_font, resource))
            continue

        placement, local_attempts = _fit_container(
            template,
            container,
            text,
            selected_font,
            resource,
        )
        attempts.extend(local_attempts)
        placements.append(placement)
        if not placement.fit:
            findings.append(
                _finding(
                    "ANCHORED_BLOCK_TEXT_OVERFLOW",
                    "anchored_blocks_layout_planner",
                    container.block_owner_id,
                    container.container_id,
                    "译文在有限局部修复后仍无法装入所属块的安全边界",
                    source_bbox=container.source_bbox,
                    allowed_bbox=container.allowed_bbox,
                )
            )

    placements = _harmonize_owner_styles(template, placements)
    placements = _repair_text_collisions(template, placements)
    for item in _cell_or_style_findings(template, placements):
        findings.append(item)

    unreadable = _unreadable_wraps(placements, template.width, template.height)
    for item in unreadable:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_UNREADABLE_WRAP",
                "anchored_blocks_layout_planner",
                item["block_owner_id"],
                item["container_id"],
                "译文在窄框内形成逐字竖排，虽未溢出但不可读",
                line_count=item["line_count"],
                maximum_characters_per_line=item["maximum_characters_per_line"],
            )
        )

    collisions = _cross_owner_collisions(placements, template.width, template.height)
    if collisions:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_TEXT_COLLISION",
                "anchored_blocks_layout_planner",
                None,
                None,
                "候选容器的实际文字墨迹区域发生碰撞",
                collisions=collisions,
            )
        )
    plan = AnchoredLayoutPlan(
        template.page_id,
        TOOLBOX_KEY,
        template.structure_sha256,
        tuple(placements),
        tuple(attempts),
    )
    return plan, tuple(findings)


def _repair_text_collisions(
    template: AnchoredBlocksTemplate,
    placements: list[AnchoredPlacement],
) -> list[AnchoredPlacement]:
    containers = {container.container_id: container for container in template.containers}
    result = list(placements)
    for _attempt in range(12):
        collisions = _cross_owner_collisions(result, template.width, template.height)
        if not collisions:
            break
        owner_by_container = {
            placement.container_id: placement.block_owner_id
            for placement in result
        }
        affected_owners = {
            owner_by_container[container_id]
            for collision in collisions
            for container_id in (collision["left"], collision["right"])
        }
        changed = False
        for index, placement in enumerate(result):
            container = containers[placement.container_id]
            if (
                not placement.fit
                or placement.block_owner_id not in affected_owners
                or container.font_size < 8.0
            ):
                continue
            minimum = max(5.5, container.font_size * 0.68)
            size = round(max(minimum, placement.font_size - 0.2), 4)
            if size >= placement.font_size - 0.01:
                continue
            result[index] = replace(
                placement,
                font_size=size,
                profile=placement.profile + "+collision-repair",
            )
            changed = True
        if not changed:
            break
        result = _harmonize_owner_styles(template, result)
    return result


def _fit_container(template, container, text: str, font_file: str, resource: str):
    attempts: list[BlockRepairAttempt] = []
    minimum = max(5.5, container.font_size * 0.68)
    for index, (name, scale, line_height) in enumerate(_PROFILES):
        size = max(minimum, container.font_size * scale)
        spare = _probe(
            template.width,
            template.height,
            container.allowed_bbox,
            text,
            size,
            line_height,
            font_file,
            resource,
            container.alignment,
        )
        fit = spare >= 0
        if index or not fit:
            attempts.append(
                BlockRepairAttempt(
                    container.container_id,
                    container.block_owner_id,
                    name,
                    fit,
                    "target_block_fit_without_cross_owner_change" if fit else "target_block_still_overflowing",
                )
            )
        if fit:
            return (
                AnchoredPlacement(
                    container.container_id,
                    container.block_owner_id,
                    text,
                    container.allowed_bbox,
                    font_file,
                    resource,
                    round(size, 4),
                    line_height,
                    container.color_srgb,
                    container.alignment,
                    name,
                    True,
                ),
                attempts,
            )
    return _unfit_placement(container, text, font_file, resource), attempts


def _harmonize_owner_styles(
    template: AnchoredBlocksTemplate,
    placements: list[AnchoredPlacement],
) -> list[AnchoredPlacement]:
    containers = {container.container_id: container for container in template.containers}
    result = list(placements)
    fit_indices = [index for index, placement in enumerate(result) if placement.fit]
    reductions = {
        index: max(0.0, containers[result[index].container_id].font_size - result[index].font_size)
        for index in fit_indices
    }
    constraint_groups: list[list[int]] = []
    for owner in template.block_owners:
        owner_indices = [
            index
            for index, placement in enumerate(result)
            if placement.block_owner_id == owner.owner_id and placement.fit
        ]
        normal_indices = [
            index
            for index in owner_indices
            if containers[result[index].container_id].font_size >= 8.0
        ]
        micro_indices = [index for index in owner_indices if index not in normal_indices]
        if len(normal_indices) >= 2:
            constraint_groups.append(normal_indices)
        if len(micro_indices) >= 2:
            constraint_groups.append(micro_indices)
    by_source_style: dict[tuple[str, float, int, str], list[int]] = {}
    for index in fit_indices:
        container = containers[result[index].container_id]
        key = (container.font_name, container.font_size, container.color_srgb, container.role)
        by_source_style.setdefault(key, []).append(index)
    constraint_groups.extend(indices for indices in by_source_style.values() if len(indices) >= 2)

    changed = True
    while changed:
        changed = False
        for indices in constraint_groups:
            reduction = max(reductions[index] for index in indices)
            for index in indices:
                if reductions[index] < reduction - 0.01:
                    reductions[index] = reduction
                    changed = True

    style_line_heights: dict[tuple[str, float, int, str], float] = {}
    for key, indices in by_source_style.items():
        style_line_heights[key] = min(result[index].line_height for index in indices)
    for index in fit_indices:
        placement = result[index]
        container = containers[placement.container_id]
        key = (container.font_name, container.font_size, container.color_srgb, container.role)
        size = round(max(5.5, container.font_size - reductions[index]), 4)
        line_height = style_line_heights[key]
        if abs(size - placement.font_size) <= 0.01 and abs(line_height - placement.line_height) <= 0.01:
            continue
        result[index] = replace(
            placement,
            font_size=size,
            line_height=line_height,
            profile=f"{placement.profile}+style-system",
        )
    return result


def _cell_or_style_findings(
    template: AnchoredBlocksTemplate,
    placements: list[AnchoredPlacement],
) -> list[AnchoredFinding]:
    containers = {container.container_id: container for container in template.containers}
    findings: list[AnchoredFinding] = []
    outside = [
        placement.container_id
        for placement in placements
        if placement.fit and not _contains(containers[placement.container_id].slot_bbox, placement.output_bbox)
    ]
    if outside:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_CELL_OVERFLOW",
                "anchored_blocks_layout_planner",
                None,
                None,
                "译文写入区域越出所属单元格或卡片槽位",
                container_ids=outside,
            )
        )
    drift = []
    by_owner: dict[str, list[AnchoredPlacement]] = {}
    by_source_style: dict[tuple[str, float, int, str], list[AnchoredPlacement]] = {}
    for placement in placements:
        if placement.fit:
            by_owner.setdefault(placement.block_owner_id, []).append(placement)
            container = containers[placement.container_id]
            key = (container.font_name, container.font_size, container.color_srgb, container.role)
            by_source_style.setdefault(key, []).append(placement)
    compared: set[tuple[str, str]] = set()
    for placement_group in [*by_owner.values(), *by_source_style.values()]:
        for index, left in enumerate(placement_group):
            left_container = containers[left.container_id]
            for right in placement_group[index + 1 :]:
                pair = tuple(sorted((left.container_id, right.container_id)))
                if pair in compared:
                    continue
                compared.add(pair)
                right_container = containers[right.container_id]
                if (
                    left.block_owner_id == right.block_owner_id
                    and (left_container.font_size < 8.0) != (right_container.font_size < 8.0)
                ):
                    continue
                source_delta = left_container.font_size - right_container.font_size
                target_delta = left.font_size - right.font_size
                if abs(source_delta - target_delta) > 0.15:
                    drift.append(
                        {
                            "left": left.container_id,
                            "right": right.container_id,
                            "source_font_size_delta": round(source_delta, 4),
                            "target_font_size_delta": round(target_delta, 4),
                        }
                    )
    if drift:
        findings.append(
            _finding(
                "ANCHORED_BLOCK_STYLE_SCALE_DRIFT",
                "anchored_blocks_layout_planner",
                None,
                None,
                "同一块或同源样式的译文字号未保持源文相对关系",
                pairs=drift,
            )
        )
    return findings


def _unfit_placement(container, text: str, font_file: str, resource: str) -> AnchoredPlacement:
    return AnchoredPlacement(
        container.container_id,
        container.block_owner_id,
        text,
        container.allowed_bbox,
        font_file,
        resource,
        round(max(5.5, container.font_size * 0.68), 4),
        1.0,
        container.color_srgb,
        container.alignment,
        "unfit",
        False,
    )


def _probe(
    page_width: float,
    page_height: float,
    bbox: Rect,
    text: str,
    font_size: float,
    line_height: float,
    font_file: str,
    resource: str,
    alignment: str,
) -> float:
    with fitz.open() as document:
        page = document.new_page(width=page_width, height=page_height)
        return float(
            page.insert_textbox(
                fitz.Rect(bbox),
                text,
                fontname=resource,
                fontfile=font_file,
                fontsize=font_size,
                lineheight=line_height,
                align=_fitz_alignment(alignment),
            )
        )


def _cross_owner_collisions(
    placements: list[AnchoredPlacement],
    page_width: float,
    page_height: float,
) -> list[dict[str, object]]:
    collisions = []
    painted: dict[str, Rect] = {}
    for index, left in enumerate(placements):
        if not left.fit:
            continue
        for right in placements[index + 1 :]:
            if not right.fit:
                continue
            if _intersection_area(left.output_bbox, right.output_bbox) <= 0.05:
                continue
            left_painted = painted.setdefault(
                left.container_id,
                _painted_bbox(left, page_width, page_height),
            )
            right_painted = painted.setdefault(
                right.container_id,
                _painted_bbox(right, page_width, page_height),
            )
            area = _intersection_area(left_painted, right_painted)
            if area > 0.05:
                collisions.append(
                    {
                        "left": left.container_id,
                        "right": right.container_id,
                        "intersection_area": round(area, 4),
                        "same_block_owner": left.block_owner_id == right.block_owner_id,
                        "left_painted_bbox": left_painted,
                        "right_painted_bbox": right_painted,
                    }
                )
    return collisions


def _unreadable_wraps(
    placements: list[AnchoredPlacement],
    page_width: float,
    page_height: float,
) -> list[dict[str, object]]:
    unreadable = []
    for placement in placements:
        width = placement.output_bbox[2] - placement.output_bbox[0]
        if not placement.fit or len(placement.translated_text.strip()) < 3 or width >= placement.font_size * 2.5:
            continue
        with fitz.open() as document:
            page = document.new_page(width=page_width, height=page_height)
            spare = page.insert_textbox(
                fitz.Rect(placement.output_bbox),
                placement.translated_text,
                fontname=placement.font_resource,
                fontfile=placement.font_file,
                fontsize=placement.font_size,
                lineheight=placement.line_height,
                align=_fitz_alignment(placement.alignment),
            )
            if spare < 0:
                continue
            lines = [
                "".join(span.get("text", "") for span in line.get("spans", []))
                for block in page.get_text("dict").get("blocks", [])
                for line in block.get("lines", [])
            ]
        counts = [len("".join(line.split())) for line in lines if line.strip()]
        if len(counts) >= 3 and max(counts, default=0) <= 2:
            unreadable.append(
                {
                    "container_id": placement.container_id,
                    "block_owner_id": placement.block_owner_id,
                    "line_count": len(counts),
                    "maximum_characters_per_line": max(counts),
                }
            )
    return unreadable


def _painted_bbox(
    placement: AnchoredPlacement,
    page_width: float,
    page_height: float,
) -> Rect:
    with fitz.open() as document:
        page = document.new_page(width=page_width, height=page_height)
        spare = page.insert_textbox(
            fitz.Rect(placement.output_bbox),
            placement.translated_text,
            fontname=placement.font_resource,
            fontfile=placement.font_file,
            fontsize=placement.font_size,
            lineheight=placement.line_height,
            align=_fitz_alignment(placement.alignment),
        )
        if spare < 0:
            return placement.output_bbox
        blocks = [
            fitz.Rect(block[:4])
            for block in page.get_text("blocks")
            if len(block) > 4 and str(block[4]).strip()
        ]
        if not blocks:
            return placement.output_bbox
        painted = blocks[0]
        for block in blocks[1:]:
            painted |= block
        return tuple(round(float(value), 4) for value in painted)  # type: ignore[return-value]


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _is_bold(font_name: str) -> bool:
    lowered = font_name.casefold()
    return any(token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold"))


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _contains(outer: Rect, inner: Rect) -> bool:
    return (
        outer[0] - 0.01 <= inner[0]
        and outer[1] - 0.01 <= inner[1]
        and inner[2] <= outer[2] + 0.01
        and inner[3] <= outer[3] + 0.01
    )


def _finding(code, owner, block_owner_id, container_id, message, **evidence):
    return AnchoredFinding(code, "HARD", owner, block_owner_id, container_id, message, dict(evidence))
