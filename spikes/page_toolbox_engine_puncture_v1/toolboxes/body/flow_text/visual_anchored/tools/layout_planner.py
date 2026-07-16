from __future__ import annotations

import re
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle
from shared_pdf_kernel.fonts import probe_font

from . import TOOLBOX_KEY
from .models import (
    Rect,
    VisualAnchoredFinding,
    VisualAnchoredLayoutPlan,
    VisualAnchoredPlacement,
    VisualAnchoredTemplate,
)


_PROFILES = (
    ("source-rhythm", 1.00, 1.15),
    ("tighter-leading", 1.00, 1.00),
    ("font-92", 0.92, 1.05),
    ("font-84", 0.84, 1.00),
    ("font-76", 0.76, 1.00),
    ("font-68", 0.68, 1.00),
)
_MIN_VISIBILITY_CONTRAST = 1.5


def plan_visual_anchored_layout(
    template: VisualAnchoredTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[VisualAnchoredLayoutPlan, tuple[VisualAnchoredFinding, ...]]:
    actual = [item.container_id for item in bundle.translations]
    actual_set = set(actual)
    expected = [container.container_id for container in template.containers if container.container_id in actual_set]
    if actual != expected or len(actual) != len(actual_set):
        raise ValueError("VISUAL_ANCHORED_TRANSLATION_ID_MISMATCH")

    slots = {slot.slot_id: slot for slot in template.visual_slots}
    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    native_companions = _native_bilingual_companions(template, actual_set)
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    placements: list[VisualAnchoredPlacement] = []
    findings: list[VisualAnchoredFinding] = []
    for container in template.containers:
        if container.container_id not in translated:
            continue
        slot = slots[container.slot_id]
        text = translated[container.container_id]
        selected_font = bold_path if _is_bold(container.font_name) else font_file
        resource = "p12visualb" if selected_font == bold_path and bold_path != font_file else "p12visual"
        companions = native_companions.get(container.container_id, ())
        if companions:
            placements.append(
                VisualAnchoredPlacement(
                    container_id=container.container_id,
                    slot_id=container.slot_id,
                    translated_text=text,
                    output_bbox=container.source_bbox,
                    font_file=selected_font,
                    font_resource=resource,
                    font_size=container.font_size,
                    minimum_font_size=round(max(5.5, container.font_size * 0.68), 4),
                    line_height=1.0,
                    color_srgb=container.color_srgb,
                    alignment=container.alignment,
                    profile="native-target-companion",
                    fit=True,
                    render_text=False,
                    deduplicated_against_container_ids=companions,
                )
            )
            continue
        font = probe_font(Path(selected_font), text)
        if not font.covers_text:
            findings.append(
                _finding(
                    "FONT_GLYPH_MISSING",
                    "visual_anchored_layout_planner",
                    slot.slot_id,
                    container.container_id,
                    "目标字体不能覆盖译文字形",
                    missing_codepoints=font.missing_codepoints,
                )
            )
            placements.append(_unfit(container, text, selected_font, resource))
            continue
        if slot.source_contrast_ratio < _MIN_VISIBILITY_CONTRAST:
            findings.append(
                _finding(
                    "VISUAL_CONTRAST_LOW",
                    "visual_anchored_layout_planner",
                    slot.slot_id,
                    container.container_id,
                    "源槽位文字与固定背景的对比度不足，不能安全保持",
                    source_contrast_ratio=slot.source_contrast_ratio,
                    background_rgb=slot.background_rgb,
                )
            )

        placement = _fit(template, container, text, selected_font, resource)
        placements.append(placement)
        if not placement.fit:
            findings.append(
                _finding(
                    "VISUAL_SLOT_OVERFLOW",
                    "visual_anchored_layout_planner",
                    slot.slot_id,
                    container.container_id,
                    "译文在最低可读字号下仍无法装入原视觉槽位",
                    allowed_bbox=container.allowed_bbox,
                    source_font_size=container.font_size,
                    minimum_font_size=placement.minimum_font_size,
                )
            )

    return (
        VisualAnchoredLayoutPlan(
            page_id=template.page_id,
            toolbox_key=TOOLBOX_KEY,
            structure_sha256=template.structure_sha256,
            placements=tuple(placements),
        ),
        tuple(findings),
    )


def _fit(template, container, text: str, font_file: str, resource: str) -> VisualAnchoredPlacement:
    minimum = max(5.5, container.font_size * 0.68)
    tried: list[tuple[float, float, str]] = []
    for profile, scale, line_height in _PROFILES:
        size = max(minimum, container.font_size * scale)
        key = (round(size, 3), line_height)
        if any((round(old_size, 3), old_height) == key for old_size, old_height, _ in tried):
            continue
        tried.append((size, line_height, profile))
        if _probe(
            template.width,
            template.height,
            container.allowed_bbox,
            text,
            size,
            line_height,
            font_file,
            resource,
            container.alignment,
        ) >= 0:
            return VisualAnchoredPlacement(
                container_id=container.container_id,
                slot_id=container.slot_id,
                translated_text=text,
                output_bbox=container.allowed_bbox,
                font_file=font_file,
                font_resource=resource,
                font_size=round(size, 4),
                minimum_font_size=round(minimum, 4),
                line_height=line_height,
                color_srgb=container.color_srgb,
                alignment=container.alignment,
                profile=profile,
                fit=True,
            )
    return _unfit(container, text, font_file, resource)


def _unfit(container, text: str, font_file: str, resource: str) -> VisualAnchoredPlacement:
    minimum = max(5.5, container.font_size * 0.68)
    return VisualAnchoredPlacement(
        container_id=container.container_id,
        slot_id=container.slot_id,
        translated_text=text,
        output_bbox=container.allowed_bbox,
        font_file=font_file,
        font_resource=resource,
        font_size=round(minimum, 4),
        minimum_font_size=round(minimum, 4),
        line_height=1.0,
        color_srgb=container.color_srgb,
        alignment=container.alignment,
        profile="unfit",
        fit=False,
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
        spare = float(
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
        if spare < 0:
            return spare
        glyph_boxes = [
            tuple(float(value) for value in span["bbox"])
            for block in page.get_text("dict").get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
        if not glyph_boxes:
            return -1.0
        glyph_bbox = (
            min(item[0] for item in glyph_boxes),
            min(item[1] for item in glyph_boxes),
            max(item[2] for item in glyph_boxes),
            max(item[3] for item in glyph_boxes),
        )
        if not _contains(bbox, glyph_bbox, tolerance=0.75):
            return -1.0
        return spare


def _fitz_alignment(value: str) -> int:
    return {
        "LEFT": fitz.TEXT_ALIGN_LEFT,
        "CENTER": fitz.TEXT_ALIGN_CENTER,
        "RIGHT": fitz.TEXT_ALIGN_RIGHT,
    }[value]


def _is_bold(font_name: str) -> bool:
    lowered = font_name.casefold()
    return any(token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold"))


def _native_bilingual_companions(template: VisualAnchoredTemplate, requested_ids: set[str]) -> dict[str, tuple[str, ...]]:
    requested = [container for container in template.containers if container.container_id in requested_ids]
    preserved = [container for container in template.containers if container.container_id not in requested_ids]
    source_best = {
        source.container_id: min(candidates, key=lambda candidate: _pair_distance(source, candidate))
        for source in requested
        if (candidates := [candidate for candidate in preserved if _is_structural_bilingual_pair(source, candidate)])
    }
    target_best = {
        target.container_id: min(candidates, key=lambda candidate: _pair_distance(candidate, target))
        for target in preserved
        if (candidates := [candidate for candidate in requested if _is_structural_bilingual_pair(candidate, target)])
    }
    return {
        source_id: (target.container_id,)
        for source_id, target in source_best.items()
        if target_best.get(target.container_id)
        and target_best[target.container_id].container_id == source_id
    }


def _is_structural_bilingual_pair(source, target) -> bool:
    if {_script(source.source_text), _script(target.source_text)} != {"CJK", "LATIN"}:
        return False
    latin_text = source.source_text if _script(source.source_text) == "LATIN" else target.source_text
    if not _meaningful_latin_companion(latin_text):
        return False
    size_ratio = min(source.font_size, target.font_size) / max(source.font_size, target.font_size)
    source_literals = set(re.findall(r"\d+(?:[.,:/-]\d+)*", source.source_text))
    target_literals = set(re.findall(r"\d+(?:[.,:/-]\d+)*", target.source_text))
    return size_ratio >= 0.55 and source_literals == target_literals and _companion_geometry(source, target)


def _companion_geometry(source, target) -> bool:
    overlap = max(0.0, min(source.source_bbox[2], target.source_bbox[2]) - max(source.source_bbox[0], target.source_bbox[0]))
    narrower = min(source.source_bbox[2] - source.source_bbox[0], target.source_bbox[2] - target.source_bbox[0])
    anchor_delta = min(
        abs(source.source_bbox[0] - target.source_bbox[0]),
        abs(source.source_bbox[2] - target.source_bbox[2]),
        abs((source.source_bbox[0] + source.source_bbox[2]) / 2.0 - (target.source_bbox[0] + target.source_bbox[2]) / 2.0),
    )
    horizontal_gap = max(0.0, source.source_bbox[0] - target.source_bbox[2], target.source_bbox[0] - source.source_bbox[2])
    vertical_overlap = max(0.0, min(source.source_bbox[3], target.source_bbox[3]) - max(source.source_bbox[1], target.source_bbox[1]))
    narrower_height = min(source.source_bbox[3] - source.source_bbox[1], target.source_bbox[3] - target.source_bbox[1])
    adjacent_same_row = (
        vertical_overlap >= narrower_height * 0.5
        and horizontal_gap <= max(source.font_size, target.font_size) * 1.5
    )
    horizontal = overlap >= narrower * 0.5 or anchor_delta <= max(source.font_size, target.font_size) or adjacent_same_row
    vertical_gap = max(0.0, source.source_bbox[1] - target.source_bbox[3], target.source_bbox[1] - source.source_bbox[3])
    return horizontal and vertical_gap <= max(source.font_size, target.font_size) * 5.0


def _pair_distance(source, target) -> tuple[float, float, float, int]:
    vertical_gap = max(0.0, source.source_bbox[1] - target.source_bbox[3], target.source_bbox[1] - source.source_bbox[3])
    source_center = (source.source_bbox[0] + source.source_bbox[2]) / 2.0
    target_center = (target.source_bbox[0] + target.source_bbox[2]) / 2.0
    scale = max(source.font_size, target.font_size)
    return (
        vertical_gap / scale,
        abs(source_center - target_center) / scale,
        abs(source.font_size - target.font_size),
        abs(source.reading_order - target.reading_order),
    )


def _script(value: str) -> str:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", value))
    has_latin = bool(re.search(r"[A-Za-z]", value))
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+./-]*", value)
    if has_cjk and has_latin and latin_tokens and all(
        re.fullmatch(r"[A-Z][A-Z0-9+./-]{0,15}", token)
        for token in latin_tokens
    ):
        return "CJK"
    if has_cjk == has_latin:
        return "MIXED"
    return "CJK" if has_cjk else "LATIN"


def _meaningful_latin_companion(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", text)
    letters = sum(len(re.sub(r"[^A-Za-z]", "", token)) for token in tokens)
    return letters >= 5 and not (len(tokens) == 1 and tokens[0].isupper())


def _contains(outer: Rect, inner: Rect, *, tolerance: float) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _finding(code, owner, slot_id, container_id, message, **evidence):
    return VisualAnchoredFinding(code, "HARD", owner, slot_id, container_id, message, dict(evidence))
