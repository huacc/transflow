from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz

from transflow.toolboxes.leaves.lifted_contracts import PageTranslationBundle

from .constants import TOOLBOX_KEY
from .models import CoverFinding, CoverLayoutPlan, CoverPlacement, CoverTemplate, Rect

_SCALES = (1.0, 0.92, 0.84, 0.76, 0.68, 0.60)
_LINE_HEIGHT = 1.0


def plan_cover_layout(
    template: CoverTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[CoverLayoutPlan, tuple[CoverFinding, ...]]:
    if template.visual_only:
        raise ValueError("visual_only_cover_has_no_layout")
    eligible = [
        container.container_id for container in template.containers if container.translatable
    ]
    actual = [item.container_id for item in bundle.translations]
    actual_set = set(actual)
    expected = [container_id for container_id in eligible if container_id in actual_set]
    if actual != expected or any(container_id not in eligible for container_id in actual):
        raise ValueError("COVER_TRANSLATION_ID_MISMATCH")

    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    native_companions = _native_bilingual_companions(template, actual_set)
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    placements: list[CoverPlacement] = []
    findings: list[CoverFinding] = []
    for container in template.containers:
        if container.translatable and container.container_id not in translated:
            continue
        text = (
            translated[container.container_id] if container.translatable else container.source_text
        )
        companions = _deduplicated_companions(
            template,
            container,
            text,
            actual_set,
            native_companions,
        )
        use_bold = _is_bold(container.font_name)
        selected_font = bold_path if use_bold else font_file
        resource = "p9coverb" if use_bold else "p9cover"
        alignment = container.anchor
        if companions:
            font_size, output_bbox, fit = container.font_size, container.source_bbox, True
        else:
            font_size, output_bbox, fit = _fit_text(
                template.width,
                template.height,
                container.allowed_bbox,
                container.source_bbox,
                text,
                container.font_size,
                selected_font,
                resource,
                alignment,
                container.role,
            )
        if not fit:
            findings.append(
                _finding(
                    "COVER_TEXT_OVERFLOW",
                    "cover_layout_planner",
                    container.container_id,
                    "译文无法在封面原锚点的安全留白范围内装入",
                    source_bbox=container.source_bbox,
                    allowed_bbox=container.allowed_bbox,
                    anchor=container.anchor,
                )
            )
        placements.append(
            CoverPlacement(
                container_id=container.container_id,
                translated_text=text,
                render_text=not companions,
                deduplicated_against_container_ids=companions,
                output_bbox=output_bbox,
                font_file=selected_font,
                font_resource=resource,
                font_size=round(font_size, 4),
                line_height=_LINE_HEIGHT,
                color_srgb=container.color_srgb,
                alignment=alignment,
                fit=fit,
            )
        )
    return CoverLayoutPlan(
        template.page_id, TOOLBOX_KEY, template.structure_sha256, tuple(placements)
    ), tuple(findings)


def _fit_text(
    page_width: float,
    page_height: float,
    allowed: Rect,
    source: Rect,
    text: str,
    source_font_size: float,
    font_file: str,
    font_resource: str,
    alignment: str,
    role: str,
) -> tuple[float, Rect, bool]:
    minimum = max(
        8.0 if role == "title" else 5.0, source_font_size * (0.68 if role == "title" else 0.60)
    )
    sizes: list[float] = []
    for scale in _SCALES:
        value = max(minimum, source_font_size * scale)
        if not sizes or abs(value - sizes[-1]) > 0.02:
            sizes.append(value)

    for size in sizes:
        minimum_height = _minimum_fitting_height(
            page_width,
            page_height,
            allowed,
            text,
            size,
            font_file,
            font_resource,
            alignment,
        )
        if minimum_height is None:
            continue
        target_height = max(source[3] - source[1], minimum_height + 0.5)
        if target_height > allowed[3] - allowed[1] + 0.01:
            continue
        center_y = (source[1] + source[3]) / 2.0
        y0 = max(allowed[1], min(center_y - target_height / 2.0, allowed[3] - target_height))
        output = (allowed[0], y0, allowed[2], y0 + target_height)
        if (
            _probe(page_width, page_height, output, text, size, font_file, font_resource, alignment)
            >= 0
        ):
            return size, _round_rect(output), True
    return minimum, _round_rect(allowed), False


def _minimum_fitting_height(
    page_width: float,
    page_height: float,
    allowed: Rect,
    text: str,
    font_size: float,
    font_file: str,
    font_resource: str,
    alignment: str,
) -> float | None:
    maximum = allowed[3] - allowed[1]
    spare_height = _probe(
        page_width, page_height, allowed, text, font_size, font_file, font_resource, alignment
    )
    if spare_height < 0:
        return None
    return max(font_size * 0.8, maximum - spare_height + 1.0)


def _probe(
    page_width: float,
    page_height: float,
    bbox: Rect,
    text: str,
    font_size: float,
    font_file: str,
    font_resource: str,
    alignment: str,
) -> float:
    with fitz.open() as document:
        page = document.new_page(width=page_width, height=page_height)
        return float(
            page.insert_textbox(
                fitz.Rect(bbox),
                text,
                fontname=font_resource,
                fontfile=font_file,
                fontsize=font_size,
                lineheight=_LINE_HEIGHT,
                align=_fitz_alignment(alignment),
            )
        )


def _fitz_alignment(anchor: str) -> int:
    return {
        "LEFT": fitz.TEXT_ALIGN_LEFT,
        "CENTER": fitz.TEXT_ALIGN_CENTER,
        "RIGHT": fitz.TEXT_ALIGN_RIGHT,
    }[anchor]


def _is_bold(font_name: str) -> bool:
    lowered = font_name.casefold()
    return any(
        token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold")
    ) or lowered.endswith(("-bd", "-demi"))


def _deduplicated_companions(
    template: CoverTemplate,
    container,
    translated_text: str,
    requested_ids: set[str],
    native_companions: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if not container.translatable:
        return ()
    preserved = [
        candidate
        for candidate in template.containers
        if candidate.translatable and candidate.container_id not in requested_ids
    ]
    target = _normalized(translated_text)
    groups = [
        preserved[start : start + length]
        for length in range(1, min(3, len(preserved)) + 1)
        for start in range(len(preserved) - length + 1)
    ]
    for group in groups:
        if _normalized("".join(item.source_text for item in group)) != target:
            continue
        if _is_companion_group(container, group, maximum_gap_factor=5.0):
            return tuple(item.container_id for item in group)
    return native_companions.get(container.container_id, ())


def _native_bilingual_companions(
    template: CoverTemplate,
    requested_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    requested = [
        container
        for container in template.containers
        if container.translatable and container.container_id in requested_ids
    ]
    preserved = [
        container
        for container in template.containers
        if container.translatable and container.container_id not in requested_ids
    ]
    source_best = {
        source.container_id: min(
            candidates, key=lambda candidate: _pair_distance(source, candidate)
        )
        for source in requested
        if (
            candidates := [
                candidate
                for candidate in preserved
                if _is_structural_bilingual_pair(source, candidate)
            ]
        )
    }
    target_best = {
        target.container_id: min(
            candidates, key=lambda candidate: _pair_distance(candidate, target)
        )
        for target in preserved
        if (
            candidates := [
                candidate
                for candidate in requested
                if _is_structural_bilingual_pair(candidate, target)
            ]
        )
    }
    return {
        source_id: (target.container_id,)
        for source_id, target in source_best.items()
        if target_best.get(target.container_id)
        and target_best[target.container_id].container_id == source_id
    }


def _is_companion_group(container, group: list, *, maximum_gap_factor: float = 1.5) -> bool:
    if not _horizontally_related(container, group):
        return False
    group_bbox = (
        min(item.source_bbox[0] for item in group),
        min(item.source_bbox[1] for item in group),
        max(item.source_bbox[2] for item in group),
        max(item.source_bbox[3] for item in group),
    )
    vertical_gap = max(
        0.0,
        container.source_bbox[1] - group_bbox[3],
        group_bbox[1] - container.source_bbox[3],
    )
    return (
        vertical_gap
        <= max(container.font_size, *(item.font_size for item in group)) * maximum_gap_factor
    )


def _is_structural_bilingual_pair(source, target) -> bool:
    source_script = _script(source.source_text)
    target_script = _script(target.source_text)
    if {source_script, target_script} != {"CJK", "LATIN"}:
        return False
    size_ratio = min(source.font_size, target.font_size) / max(source.font_size, target.font_size)
    if size_ratio < 0.55:
        return False
    source_literals = set(re.findall(r"\d+(?:[.,:/-]\d+)*", source.source_text))
    target_literals = set(re.findall(r"\d+(?:[.,:/-]\d+)*", target.source_text))
    return source_literals == target_literals and _is_companion_group(source, [target])


def _horizontally_related(container, group: list) -> bool:
    group_bbox = (
        min(item.source_bbox[0] for item in group),
        min(item.source_bbox[1] for item in group),
        max(item.source_bbox[2] for item in group),
        max(item.source_bbox[3] for item in group),
    )
    overlap = max(
        0.0,
        min(container.source_bbox[2], group_bbox[2]) - max(container.source_bbox[0], group_bbox[0]),
    )
    narrower_width = min(
        container.source_bbox[2] - container.source_bbox[0], group_bbox[2] - group_bbox[0]
    )
    if overlap >= narrower_width * 0.50:
        return True
    tolerance = max(container.font_size, *(item.font_size for item in group))
    centers = (
        abs(container.source_bbox[0] - group_bbox[0]),
        abs(container.source_bbox[2] - group_bbox[2]),
        abs(
            (container.source_bbox[0] + container.source_bbox[2]) / 2.0
            - (group_bbox[0] + group_bbox[2]) / 2.0
        ),
    )
    return min(centers) <= tolerance


def _pair_distance(source, target) -> tuple[float, float, float, int]:
    vertical_gap = max(
        0.0,
        source.source_bbox[1] - target.source_bbox[3],
        target.source_bbox[1] - source.source_bbox[3],
    )
    source_center = (source.source_bbox[0] + source.source_bbox[2]) / 2.0
    target_center = (target.source_bbox[0] + target.source_bbox[2]) / 2.0
    return (
        vertical_gap / max(source.font_size, target.font_size),
        abs(source_center - target_center) / max(source.font_size, target.font_size),
        abs(source.font_size - target.font_size),
        abs(source.reading_order - target.reading_order),
    )


def _script(value: str) -> str:
    has_cjk = any("\u3400" <= character <= "\u9fff" for character in value)
    has_latin = any("a" <= character.casefold() <= "z" for character in value)
    if has_cjk == has_latin:
        return "MIXED"
    return "CJK" if has_cjk else "LATIN"


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]


def _finding(
    code: str, owner: str, container_id: str | None, message: str, **evidence: object
) -> CoverFinding:
    return CoverFinding(code, "HARD", owner, container_id, message, dict(evidence))
