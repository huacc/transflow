"""Plan translated text inside immutable visual-owner boundaries."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from transflow.toolboxes.leaves.body_flow_text_visual_anchored.models import (
    Rect,
    VisualAnchoredContainer,
    VisualAnchoredFinding,
    VisualAnchoredLayoutPlan,
    VisualAnchoredPlacement,
    VisualAnchoredRepairAttempt,
    VisualAnchoredTemplate,
)
from transflow.toolboxes.leaves.lifted_contracts import (
    PageTranslationBundle,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

FIT_PROFILES = (
    ("source-rhythm", 1.00, 1.15),
    ("tighter-leading", 1.00, 1.00),
    ("font-92", 0.92, 1.05),
    ("font-84", 0.84, 1.00),
    ("font-76", 0.76, 1.00),
    ("font-68", 0.68, 1.00),
)
MIN_VISIBILITY_CONTRAST = 1.5


def plan_visual_anchored_layout(
    template: VisualAnchoredTemplate,
    bundle: PageTranslationBundle,
    policy: P8ToolboxPolicy,
    font_path: Path,
) -> tuple[
    VisualAnchoredLayoutPlan,
    tuple[VisualAnchoredFinding, ...],
]:
    """Run a finite slot-bound fit ladder without semantic de-dup."""

    actual = tuple(item.container_id for item in bundle.translations)
    expected = tuple(
        item.container_id
        for item in template.translatable_containers
        if item.container_id in set(actual)
    )
    if actual != expected or len(actual) != len(set(actual)):
        raise ValueError("VISUAL_ANCHORED_TRANSLATION_ID_MISMATCH")
    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    containers = tuple(
        item for item in template.translatable_containers if item.container_id in translated
    )
    missing_codepoints = _missing_codepoints(
        font_path,
        tuple(translated.values()),
    )
    findings: list[VisualAnchoredFinding] = [
        VisualAnchoredFinding(code, "HARD", None) for code in template.capability_codes
    ]
    findings.extend(
        VisualAnchoredFinding(
            "VISUAL_SLOT_AMBIGUOUS",
            "HARD",
            container_id,
        )
        for container_id in template.ambiguous_container_ids
    )
    findings.extend(
        VisualAnchoredFinding(
            "VISUAL_BILINGUAL_SEMANTIC_DECISION_REQUIRED",
            "HARD",
            item.source_container_id,
        )
        for item in template.bilingual_candidates
        if item.source_container_id in translated
    )
    if missing_codepoints:
        findings.append(
            VisualAnchoredFinding(
                "FONT_GLYPH_MISSING",
                "HARD",
                containers[0].container_id if containers else None,
            )
        )

    slots = {item.slot_id: item for item in template.visual_slots}
    for container in containers:
        slot = slots[container.slot_id]
        if slot.background_evidence == "KERNEL_GEOMETRY_ONLY":
            findings.append(
                VisualAnchoredFinding(
                    "VISUAL_BACKGROUND_EVIDENCE_MISSING",
                    "HARD",
                    container.container_id,
                )
            )
        elif (
            slot.source_contrast_ratio is not None
            and slot.source_contrast_ratio < MIN_VISIBILITY_CONTRAST
        ):
            findings.append(
                VisualAnchoredFinding(
                    "VISUAL_CONTRAST_LOW",
                    "HARD",
                    container.container_id,
                )
            )

    selected_indices: dict[str, int] = {}
    attempts: list[VisualAnchoredRepairAttempt] = []
    for container in containers:
        selected = len(FIT_PROFILES) - 1
        for index, (profile, scale, line_height) in enumerate(FIT_PROFILES):
            fit, _ = _probe(
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
                    VisualAnchoredRepairAttempt(
                        container.container_id,
                        profile,
                        fit,
                    )
                )
            selected = index
            if fit:
                break
        selected_indices[container.container_id] = selected

    cohorts: dict[
        tuple[str, float, int, str, str],
        list[VisualAnchoredContainer],
    ] = {}
    for container in containers:
        cohorts.setdefault(
            (
                container.font_name,
                container.font_size,
                container.color_srgb,
                container.role,
                container.alignment,
            ),
            [],
        ).append(container)
    for cohort in cohorts.values():
        shared_index = max(selected_indices[item.container_id] for item in cohort)
        for container in cohort:
            selected_indices[container.container_id] = shared_index

    placements = _placements(
        template,
        containers,
        translated,
        policy,
        font_path,
        selected_indices,
    )
    for _ in FIT_PROFILES:
        collision_pair = _first_collision_pair(placements)
        if collision_pair is None:
            break
        changed = False
        for container_id in collision_pair:
            old_index = selected_indices[container_id]
            if old_index >= len(FIT_PROFILES) - 1:
                continue
            new_index = old_index + 1
            selected_indices[container_id] = new_index
            container = next(item for item in containers if item.container_id == container_id)
            profile, scale, line_height = FIT_PROFILES[new_index]
            fit, _ = _probe(
                template,
                container,
                translated[container_id],
                policy,
                font_path,
                scale,
                line_height,
            )
            attempts.append(
                VisualAnchoredRepairAttempt(
                    container_id,
                    profile,
                    fit,
                )
            )
            changed = True
        if not changed:
            break
        placements = _placements(
            template,
            containers,
            translated,
            policy,
            font_path,
            selected_indices,
        )
    for placement in placements:
        if not placement.fit:
            findings.append(
                VisualAnchoredFinding(
                    "VISUAL_SLOT_OVERFLOW",
                    "HARD",
                    placement.container_id,
                )
            )
            continue
        container = next(item for item in containers if item.container_id == placement.container_id)
        slot = slots[container.slot_id]
        glyph_bbox = placement.measured_glyph_bbox
        if glyph_bbox is None or not _contains(
            slot.hard_boundary_bbox,
            glyph_bbox,
            tolerance=0.75,
        ):
            findings.append(
                VisualAnchoredFinding(
                    "VISUAL_GLYPH_OUTSIDE_SLOT",
                    "HARD",
                    placement.container_id,
                )
            )
            continue
        if not _anchor_matches(
            slot.anchor_x,
            glyph_bbox,
            placement.alignment,
            placement.font_size,
        ):
            findings.append(
                VisualAnchoredFinding(
                    "VISUAL_ANCHOR_DRIFT",
                    "HARD",
                    placement.container_id,
                )
            )

    collision_pair = _first_collision_pair(placements)
    if collision_pair is not None:
        findings.append(
            VisualAnchoredFinding(
                "VISUAL_SLOT_TEXT_COLLISION",
                "HARD",
                collision_pair[1],
            )
        )
    return (
        VisualAnchoredLayoutPlan(
            template.page_id,
            template.toolbox_key,
            template.structure_sha256,
            placements,
            tuple(attempts),
        ),
        tuple(_deduplicate_findings(findings)),
    )


def _placement(
    template: VisualAnchoredTemplate,
    container: VisualAnchoredContainer,
    text: str,
    policy: P8ToolboxPolicy,
    font_path: Path,
    profile_index: int,
) -> VisualAnchoredPlacement:
    profile, scale, line_height = FIT_PROFILES[profile_index]
    fit, glyph_bbox = _probe(
        template,
        container,
        text,
        policy,
        font_path,
        scale,
        line_height,
    )
    return VisualAnchoredPlacement(
        container_id=container.container_id,
        slot_id=container.slot_id,
        translated_text=text,
        output_bbox=container.allowed_bbox,
        measured_glyph_bbox=glyph_bbox,
        font_size=_font_size(container, policy, scale),
        minimum_font_size=_minimum_font_size(container, policy),
        line_height=line_height,
        color_srgb=container.color_srgb,
        alignment=container.alignment,
        profile=profile,
        fit=fit,
    )


def _placements(
    template: VisualAnchoredTemplate,
    containers: tuple[VisualAnchoredContainer, ...],
    translated: dict[str, str],
    policy: P8ToolboxPolicy,
    font_path: Path,
    selected_indices: dict[str, int],
) -> tuple[VisualAnchoredPlacement, ...]:
    return tuple(
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


def _probe(
    template: VisualAnchoredTemplate,
    container: VisualAnchoredContainer,
    text: str,
    policy: P8ToolboxPolicy,
    font_path: Path,
    scale: float,
    line_height: float,
) -> tuple[bool, Rect | None]:
    font_size = _font_size(container, policy, scale)
    with pymupdf.open() as document:
        page = document.new_page(
            width=template.width,
            height=template.height,
        )
        font_name = "TFVisualProbe"
        remainder = page.insert_textbox(
            pymupdf.Rect(container.allowed_bbox),
            text,
            fontname=font_name,
            fontfile=str(font_path),
            fontsize=font_size,
            lineheight=line_height,
            color=_color(container.color_srgb),
            align=_fitz_alignment(container.alignment),
        )
        if remainder < 0:
            return False, None
        glyph_boxes = [
            tuple(float(value) for value in span["bbox"])
            for block in page.get_text("dict").get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
    if not glyph_boxes:
        return False, None
    glyph_bbox = _round_rect(
        (
            min(item[0] for item in glyph_boxes),
            min(item[1] for item in glyph_boxes),
            max(item[2] for item in glyph_boxes),
            max(item[3] for item in glyph_boxes),
        )
    )
    return (
        _contains(
            container.hard_boundary_bbox,
            glyph_bbox,
            tolerance=0.75,
        ),
        glyph_bbox,
    )


def _font_size(
    container: VisualAnchoredContainer,
    policy: P8ToolboxPolicy,
    scale: float,
) -> float:
    return round(
        max(
            _minimum_font_size(container, policy),
            container.font_size * scale,
        ),
        4,
    )


def _minimum_font_size(
    container: VisualAnchoredContainer,
    policy: P8ToolboxPolicy,
) -> float:
    return round(
        max(
            policy.minimum_font_size,
            container.font_size * 0.68,
        ),
        4,
    )


def _missing_codepoints(
    font_path: Path,
    texts: tuple[str, ...],
) -> tuple[str, ...]:
    font = pymupdf.Font(fontfile=str(font_path))
    return tuple(
        f"U+{ord(character):04X}"
        for character in dict.fromkeys(
            character for text in texts for character in text if not character.isspace()
        )
        if not font.has_glyph(ord(character))
    )


def _anchor_matches(
    anchor_x: float,
    glyph_bbox: Rect,
    alignment: str,
    font_size: float,
) -> bool:
    actual = (
        glyph_bbox[2]
        if alignment == "RIGHT"
        else (glyph_bbox[0] + glyph_bbox[2]) / 2.0
        if alignment == "CENTER"
        else glyph_bbox[0]
    )
    return abs(actual - anchor_x) <= max(1.5, font_size * 0.20)


def _first_collision_pair(
    placements: tuple[VisualAnchoredPlacement, ...],
) -> tuple[str, str] | None:
    for index, current in enumerate(placements):
        if not current.fit or current.measured_glyph_bbox is None:
            continue
        for previous in placements[:index]:
            if (
                previous.fit
                and previous.measured_glyph_bbox is not None
                and _intersection_area(
                    current.measured_glyph_bbox,
                    previous.measured_glyph_bbox,
                )
                > 0.05
            ):
                return (
                    previous.container_id,
                    current.container_id,
                )
    return None


def _deduplicate_findings(
    findings: list[VisualAnchoredFinding],
) -> tuple[VisualAnchoredFinding, ...]:
    return tuple({(item.code, item.container_id): item for item in findings}.values())


def _fitz_alignment(value: str) -> int:
    return {
        "LEFT": pymupdf.TEXT_ALIGN_LEFT,
        "CENTER": pymupdf.TEXT_ALIGN_CENTER,
        "RIGHT": pymupdf.TEXT_ALIGN_RIGHT,
    }.get(value, pymupdf.TEXT_ALIGN_LEFT)


def _color(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )


def _contains(
    outer: Rect,
    inner: Rect,
    *,
    tolerance: float,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(
        0.0,
        min(left[2], right[2]) - max(left[0], right[0]),
    ) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _round_rect(rect: Rect) -> Rect:
    x0, y0, x1, y1 = rect
    return (
        round(float(x0), 4),
        round(float(y0), 4),
        round(float(x1), 4),
        round(float(y1), 4),
    )
