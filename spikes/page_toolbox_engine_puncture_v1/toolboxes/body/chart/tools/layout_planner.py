from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationBundle
from shared_pdf_kernel.fonts import probe_font

from . import TOOLBOX_KEY
from .models import ChartFinding, ChartLayoutPlan, ChartPlacement, ChartTemplate, Rect


_BASE_PROFILES = (
    ("source-size", 1.00, 1.05),
    ("compact-leading", 1.00, 0.95),
    ("font-90", 0.90, 1.00),
    ("font-80", 0.80, 1.00),
    ("font-75", 0.75, 1.00),
    ("font-72", 0.72, 1.00),
    ("font-72-tight", 0.72, 0.95),
    ("font-68-tight", 0.68, 0.92),
    ("font-65-tight", 0.65, 0.92),
)
_BODY_PROFILES = (
    ("body-spacious-105", 1.05, 1.18),
    ("body-source-spacious", 1.00, 1.18),
    ("body-font-90-spacious", 0.90, 1.18),
    *_BASE_PROFILES,
)
_TABLE_PROFILES = (
    ("table-source", 1.00, 0.95),
    ("table-font-95", 0.95, 0.95),
    ("table-font-90", 0.90, 0.95),
    ("table-font-80", 0.80, 0.95),
    ("table-font-72", 0.72, 0.92),
    ("table-font-68", 0.68, 0.92),
)
_TABLE_ROLES = {"TABLE_HEADER", "TABLE_SECTION", "TABLE_CELL", "TABLE_TOTAL"}


def plan_chart_layout(
    template: ChartTemplate,
    bundle: PageTranslationBundle,
    *,
    font_file: str,
    bold_font_file: str | None = None,
) -> tuple[ChartLayoutPlan, tuple[ChartFinding, ...]]:
    actual = [item.container_id for item in bundle.translations]
    actual_set = set(actual)
    expected = [item.container_id for item in template.containers if item.container_id in actual_set]
    if actual != expected or len(actual) != len(actual_set):
        raise ValueError("CHART_TRANSLATION_ID_MISMATCH")

    translated = {item.container_id: item.translated_text.strip() for item in bundle.translations}
    bold_path = bold_font_file if bold_font_file and Path(bold_font_file).is_file() else font_file
    placements_by_id: dict[str, ChartPlacement] = {}
    findings: list[ChartFinding] = []
    fit_groups: dict[tuple[object, ...], list[tuple[object, str, str, str]]] = {}
    for container in template.containers:
        if container.container_id not in translated:
            continue
        text = translated[container.container_id]
        selected_font = bold_path if _is_bold(container.font_name) else font_file
        resource = "p13chartb" if selected_font == bold_path and bold_path != font_file else "p13chart"
        coverage = probe_font(Path(selected_font), text)
        if not coverage.covers_text:
            placements_by_id[container.container_id] = _unfit(container, text, selected_font, resource)
            findings.append(
                _finding(
                    "FONT_GLYPH_MISSING",
                    "chart_layout_planner",
                    container,
                    "目标字体不能覆盖图表译文字形",
                    missing_codepoints=coverage.missing_codepoints,
                )
            )
            continue
        group_key = _typography_group_key(container)
        fit_groups.setdefault(group_key, []).append((container, text, selected_font, resource))

    for group in fit_groups.values():
        group_placements = _fit_group(template, group)
        for placement, (container, text, selected_font, resource) in zip(group_placements, group):
            placements_by_id[container.container_id] = placement
            if placement.fit:
                continue
            findings.append(
                _finding(
                    "CHART_TEXT_SLOT_OVERFLOW",
                    "chart_layout_planner",
                    container,
                    "译文在最低可读字号下仍无法装入与图表对象关联的安全区域",
                    allowed_bbox=container.allowed_bbox,
                    minimum_font_size=placement.minimum_font_size,
                    role=container.role,
                )
            )

    placements = tuple(
        placements_by_id[container.container_id]
        for container in template.containers
        if container.container_id in translated
    )

    return (
        ChartLayoutPlan(template.page_id, TOOLBOX_KEY, template.structure_sha256, placements),
        tuple(findings),
    )


def _fit_group(template, group: list[tuple[object, str, str, str]]) -> list[ChartPlacement]:
    profiles = _profiles(group[0][0])
    seen: set[tuple[tuple[float, float], ...]] = set()
    best_count = -1
    best: tuple[str, float, tuple[float, ...], list[tuple[str, Rect, str] | None]] | None = None
    for profile, scale, line_height in profiles:
        sizes = tuple(max(_minimum_font_size(container), container.font_size * scale) for container, _, _, _ in group)
        key = tuple((round(size, 3), line_height) for size in sizes)
        if key in seen:
            continue
        seen.add(key)
        selected_slots: list[tuple[str, Rect, str] | None] = []
        for size, (container, text, font_file, resource) in zip(sizes, group):
            selected = next(
                (
                    (f"{slot_policy}{variant_profile}", bbox, variant_text)
                    for variant_text, variant_profile in _layout_text_variants(container, text)
                    for slot_policy, bbox in _slot_profiles(container, size)
                    if _probe(
                        template.width,
                        template.height,
                        bbox,
                        variant_text,
                        size,
                        line_height,
                        font_file,
                        resource,
                        container.alignment,
                        container.rotation,
                    )
                ),
                None,
            )
            selected_slots.append(selected)
        fit_count = sum(selected is not None for selected in selected_slots)
        if fit_count > best_count:
            best_count = fit_count
            best = (profile, line_height, sizes, selected_slots)
        if fit_count == len(group):
            placements: list[ChartPlacement] = []
            for index, (size, (container, text, font_file, resource)) in enumerate(zip(sizes, group)):
                selected = selected_slots[index]
                assert selected is not None
                placements.append(
                    ChartPlacement(
                    container.container_id,
                    selected[2],
                    selected[1],
                    font_file,
                    resource,
                    round(size, 4),
                    round(_minimum_font_size(container), 4),
                    line_height,
                    container.color_srgb,
                    container.alignment,
                    f"{selected[0]}/{profile}",
                    True,
                    container.rotation,
                )
                )
            return placements
    if best is None:
        return [_unfit(container, text, font_file, resource) for container, text, font_file, resource in group]
    profile, line_height, sizes, selected_slots = best
    placements: list[ChartPlacement] = []
    for index, (size, (container, text, font_file, resource)) in enumerate(zip(sizes, group)):
        selected = selected_slots[index]
        if selected is None:
            placements.append(_unfit(container, text, font_file, resource))
            continue
        placements.append(
            ChartPlacement(
                container.container_id,
                selected[2],
                selected[1],
                font_file,
                resource,
                round(size, 4),
                round(_minimum_font_size(container), 4),
                line_height,
                container.color_srgb,
                container.alignment,
                f"{selected[0]}/{profile}",
                True,
                container.rotation,
            )
        )
    return placements


def _layout_text_variants(container, text: str) -> tuple[tuple[str, str], ...]:
    variants: list[tuple[str, str]] = []
    if (
        container.role == "AXIS_OR_CATEGORY_LABEL"
        and container.anchor_relation == "OVERLAY"
        and re.fullmatch(r"[\u3400-\u9fff]{5,}", text)
        and container.allowed_bbox[3] - container.allowed_bbox[1] >= container.font_size * 2.4
        and len(text) * container.font_size
        > container.allowed_bbox[2] - container.allowed_bbox[0] - container.font_size * 0.75
    ):
        split_at = (len(text) + 1) // 2
        variants.append((f"{text[:split_at]}\n{text[split_at:]}", "+balanced-cjk-wrap"))
    variants.append((text, ""))
    if container.role in {"AXIS_OR_CATEGORY_LABEL", "LEGEND_LABEL"}:
        hyphen_wrapped = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "-\n", text)
        if hyphen_wrapped != text:
            variants.append((hyphen_wrapped, "+hyphen-wrap"))
    return tuple(variants)


def _slot_profiles(container, font_size: float) -> tuple[tuple[str, Rect], ...]:
    """Return finite, anchor-preserving slots from tightest to most spacious."""

    source = container.source_bbox
    allowed = container.allowed_bbox
    source_bottom = min(
        allowed[3],
        max(source[3], source[1] + font_size * 1.45),
    )
    safe_left, safe_right = allowed[0], allowed[2]
    if container.alignment == "CENTER":
        source_center = (source[0] + source[2]) / 2.0
        half_width = min(source_center - allowed[0], allowed[2] - source_center)
        safe_left, safe_right = source_center - half_width, source_center + half_width
    candidates: tuple[tuple[str, Rect], ...] = (
        (
            "source-box",
            (
                max(allowed[0], source[0]),
                max(allowed[1], source[1]),
                min(allowed[2], source[2]),
                source_bottom,
            ),
        ),
        (
            "safe-horizontal",
            (safe_left, max(allowed[1], source[1]), safe_right, source_bottom),
        ),
        ("safe-wrap", (safe_left, allowed[1], safe_right, allowed[3])),
    )
    if container.role == "LEGEND_LABEL":
        row_height = font_size * 1.45
        source_center = (source[1] + source[3]) / 2.0
        row_top = max(allowed[1], min(source_center - row_height / 2.0, allowed[3] - row_height))
        row_bottom = min(allowed[3], row_top + row_height)
        candidates = (
            ("legend-row", (safe_left, row_top, safe_right, row_bottom)),
            *candidates,
        )
    result: list[tuple[str, Rect]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for policy, bbox in candidates:
        rounded = tuple(round(float(value), 4) for value in bbox)
        if rounded in seen or rounded[2] <= rounded[0] or rounded[3] <= rounded[1]:
            continue
        seen.add(rounded)
        result.append((policy, rounded))
    return tuple(result)


def layout_rule_trace(template: ChartTemplate, plan: ChartLayoutPlan) -> tuple[dict[str, object], ...]:
    """Expose the deterministic rule decision used for every translated container."""

    containers = {item.container_id: item for item in template.containers}
    records: list[dict[str, object]] = []
    for placement in plan.placements:
        container = containers[placement.container_id]
        slot_policy, _, typography_profile = placement.profile.partition("/")
        if not placement.fit:
            failure_class = "translated_text_exceeds_safe_page_region"
            repair_atom = "translated_diagnostic_render"
        elif slot_policy == "safe-horizontal":
            failure_class = "source_bbox_too_narrow_with_safe_horizontal_space"
            repair_atom = "safe_horizontal_expansion"
        elif slot_policy == "safe-wrap":
            failure_class = "single_line_or_source_bbox_insufficient_with_safe_vertical_space"
            repair_atom = "safe_multiline_reflow"
        elif typography_profile not in {"source-size", "body-spacious-105", "body-source-spacious", "table-source"}:
            failure_class = "safe_region_requires_source_relative_typography_reduction"
            repair_atom = "group_typography_scale_reduction"
        else:
            failure_class = None
            repair_atom = None
        records.append(
            {
                "schema_version": "p13-chart-layout-rule/v1",
                "rule_verdict": "PASS" if placement.fit else "FAIL",
                "container_id": placement.container_id,
                "selected_failure_class": failure_class,
                "dispatch_result": {
                    "dispatch_table": "contracts/failure_dispatch_table.json",
                    "selected_repair_atom": repair_atom,
                    "bound_tool": (
                        "tools/renderer.py"
                        if repair_atom == "translated_diagnostic_render"
                        else "tools/layout_planner.py"
                    ),
                },
                "evidence": {
                    "source_bbox": container.source_bbox,
                    "safe_bbox": container.allowed_bbox,
                    "output_bbox": placement.output_bbox,
                    "slot_policy": slot_policy,
                    "typography_profile": typography_profile or placement.profile,
                    "source_glyph_bbox_is_not_a_hard_width_boundary": True,
                    "page_boundary_respected": _contains(
                        (0.0, 0.0, template.width, template.height),
                        placement.output_bbox,
                        tolerance=0.01,
                    ),
                },
            }
        )
    return tuple(records)


def materialize_translated_diagnostic_plan(
    template: ChartTemplate,
    plan: ChartLayoutPlan,
) -> tuple[ChartTemplate, ChartLayoutPlan, tuple[dict[str, object], ...]]:
    """Make every valid translation visible on-page when the product layout is unfit."""

    containers = {item.container_id: item for item in template.containers}
    diagnostic_allowed: dict[str, Rect] = {}
    placements: list[ChartPlacement] = []
    records: list[dict[str, object]] = []
    for placement in plan.placements:
        if placement.fit:
            placements.append(placement)
            continue
        container = containers[placement.container_id]
        selected = next(
            (
                (policy, bbox, font_size, line_height)
                for policy, bbox in _diagnostic_slot_profiles(template, container)
                for font_size, line_height in _diagnostic_typography_profiles(placement.minimum_font_size)
                if _probe(
                    template.width,
                    template.height,
                    bbox,
                    placement.translated_text,
                    font_size,
                    line_height,
                    placement.font_file,
                    placement.font_resource,
                    placement.alignment,
                    placement.rotation,
                )
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"translated_diagnostic_text_cannot_fit_page:{placement.container_id}")
        policy, bbox, font_size, line_height = selected
        diagnostic_allowed[placement.container_id] = bbox
        placements.append(
            replace(
                placement,
                output_bbox=bbox,
                font_size=font_size,
                line_height=line_height,
                profile=f"{policy}/diagnostic-translated",
                fit=True,
            )
        )
        records.append(
            {
                "container_id": placement.container_id,
                "operation_type": "translated_diagnostic_render",
                "slot_policy": policy,
                "output_bbox": bbox,
                "font_size": font_size,
                "line_height": line_height,
                "page_extended": False,
                "product_acceptance": False,
            }
        )
    diagnostic_template = replace(
        template,
        containers=tuple(
            replace(container, allowed_bbox=diagnostic_allowed.get(container.container_id, container.allowed_bbox))
            for container in template.containers
        ),
    )
    return diagnostic_template, replace(plan, placements=tuple(placements)), tuple(records)


def _diagnostic_slot_profiles(template: ChartTemplate, container) -> tuple[tuple[str, Rect], ...]:
    margin_x = max(6.0, template.width * 0.015)
    margin_bottom = max(6.0, template.height * 0.015)
    top = max(0.0, min(container.source_bbox[1], template.height - margin_bottom - 1.0))
    bottom = template.height - margin_bottom
    if container.rotation:
        page_lane = (margin_x, top, template.width - margin_x, bottom)
    elif container.alignment == "RIGHT":
        page_lane = (margin_x, top, min(template.width - margin_x, container.source_bbox[2]), bottom)
    elif container.alignment == "CENTER":
        page_lane = (margin_x, top, template.width - margin_x, bottom)
    else:
        page_lane = (max(margin_x, container.source_bbox[0]), top, template.width - margin_x, bottom)
    candidates = (("diagnostic-safe-wrap", container.allowed_bbox), ("diagnostic-page-wrap", page_lane))
    result: list[tuple[str, Rect]] = []
    seen: set[Rect] = set()
    for policy, bbox in candidates:
        rounded = tuple(round(float(value), 4) for value in bbox)
        if rounded in seen or rounded[2] <= rounded[0] or rounded[3] <= rounded[1]:
            continue
        seen.add(rounded)
        result.append((policy, rounded))
    return tuple(result)


def _diagnostic_typography_profiles(minimum_font_size: float) -> tuple[tuple[float, float], ...]:
    sizes = (
        minimum_font_size,
        minimum_font_size * 0.90,
        minimum_font_size * 0.80,
        minimum_font_size * 0.70,
        minimum_font_size * 0.60,
        minimum_font_size * 0.50,
        minimum_font_size * 0.40,
        minimum_font_size * 0.30,
        minimum_font_size * 0.20,
        0.75,
    )
    result: list[tuple[float, float]] = []
    seen: set[float] = set()
    for value in sizes:
        size = round(max(0.75, value), 4)
        if size in seen:
            continue
        seen.add(size)
        result.append((size, 0.92))
    return tuple(result)


def _typography_group_key(container) -> tuple[object, ...]:
    if container.role == "TABLE_HEADER":
        return ("TABLE_HEADER", container.association_id)
    if container.role in _TABLE_ROLES:
        return ("TABLE_BODY", container.association_id)
    if _body_text(container):
        source_size_bucket = round(container.font_size * 2.0) / 2.0
        return ("BODY", _is_bold(container.font_name), source_size_bucket)
    if container.role in {"LEGEND_LABEL", "AXIS_OR_CATEGORY_LABEL"}:
        source_size_bucket = round(container.font_size * 2.0) / 2.0
        return (
            "CHART_LABEL",
            container.role,
            container.association_id,
            container.alignment,
            container.rotation,
            _is_bold(container.font_name),
            source_size_bucket,
            container.color_srgb,
        )
    return ("CONTAINER", container.container_id)


def _profiles(container):
    if container.role in _TABLE_ROLES:
        return _TABLE_PROFILES
    if _body_text(container):
        source_height = container.source_bbox[3] - container.source_bbox[1]
        if source_height >= container.font_size * 2.2:
            return _BODY_PROFILES
        return _BODY_PROFILES[1:]
    if _narrow_internal_overlay_label(container):
        return (*_BASE_PROFILES, ("font-50-internal-overlay", 0.50, 0.92))
    if container.role == "AXIS_OR_CATEGORY_LABEL" and container.rotation:
        return (*_BASE_PROFILES, ("font-60-vertical", 0.60, 0.90), ("font-55-vertical", 0.55, 0.90))
    return _BASE_PROFILES


def _body_text(container) -> bool:
    return container.role == "ANNOTATION"


def _narrow_internal_overlay_label(container) -> bool:
    return (
        container.role == "AXIS_OR_CATEGORY_LABEL"
        and container.anchor_relation == "OVERLAY"
        and container.alignment == "CENTER"
        and container.allowed_bbox[2] - container.allowed_bbox[0] <= container.font_size * 4.0
    )


def _unfit(container, text: str, font_file: str, resource: str) -> ChartPlacement:
    minimum = _minimum_font_size(container)
    return ChartPlacement(
        container.container_id,
        text,
        container.allowed_bbox,
        font_file,
        resource,
        round(minimum, 4),
        round(minimum, 4),
        1.0,
        container.color_srgb,
        container.alignment,
        "unfit",
        False,
        container.rotation,
    )


def _minimum_font_size(container) -> float:
    if container.role == "AXIS_OR_CATEGORY_LABEL" and container.rotation:
        floor = 4.0
        relative_floor = 0.55
    elif _narrow_internal_overlay_label(container):
        floor = 4.5
        relative_floor = 0.50
    else:
        floor = 4.5 if container.role in {"AXIS_OR_CATEGORY_LABEL", "LEGEND_LABEL", "PAGE_HEADER", "PAGE_FOOTER"} else 5.5
        relative_floor = 0.65 if container.role in {"AXIS_OR_CATEGORY_LABEL", "LEGEND_LABEL"} else 0.68
    return max(floor, container.font_size * relative_floor)


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
    rotation: int,
) -> bool:
    with fitz.open() as document:
        page = document.new_page(width=page_width, height=page_height)
        spare = page.insert_textbox(
            fitz.Rect(bbox),
            text,
            fontname=resource,
            fontfile=font_file,
            fontsize=font_size,
            lineheight=line_height,
            align=_fitz_alignment(alignment),
            rotate=rotation,
        )
        if spare < 0:
            return False
        text_dict = page.get_text("dict")
        line_texts = [
            "".join(str(span.get("text") or "") for span in line.get("spans", [])).strip()
            for block in text_dict.get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
        ]
        line_texts = [line for line in line_texts if line]
        if _has_orphan_punctuation(line_texts):
            return False
        if _has_word_fragmentation(text, line_texts):
            return False
        glyphs = [
            tuple(float(value) for value in span["bbox"])
            for block in text_dict.get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        ]
        if not glyphs:
            return False
        glyph_bbox = (
            min(item[0] for item in glyphs),
            min(item[1] for item in glyphs),
            max(item[2] for item in glyphs),
            max(item[3] for item in glyphs),
        )
        return _contains(bbox, glyph_bbox, tolerance=0.75)


def _has_orphan_punctuation(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    return bool(re.fullmatch(r"[.,;:!?…，。；：！？、)\]】）]+", lines[-1].strip()))


def _has_word_fragmentation(text: str, lines: list[str]) -> bool:
    """Reject line breaks inserted inside a Latin word; ordinary wrapping is valid."""

    if len(lines) < 2:
        return False
    numeric_tokens = set(
        re.findall(r"\d+(?:[.,:/-]\d+)+(?:%|[A-Za-z\u3400-\u9fff]{1,4})?", text)
    )
    for left, right in zip(lines, lines[1:]):
        for token in numeric_tokens:
            if any(
                token[index].isdigit()
                and left.rstrip().endswith(token[:index])
                and right.lstrip().startswith(token[index:])
                for index in range(1, len(token))
            ):
                return True
    source_words = {item.casefold() for item in re.findall(r"[A-Za-z]{4,}", text)}
    for start in range(len(lines) - 1):
        left = re.search(r"([A-Za-z]+)$", lines[start].rstrip())
        if left is None:
            continue
        combined = left.group(1).casefold()
        for end in range(start + 1, len(lines)):
            right = re.match(r"([A-Za-z]+)", lines[end].lstrip())
            if right is None:
                break
            combined += right.group(1).casefold()
            if combined in source_words:
                return True
            if not any(word.startswith(combined) for word in source_words):
                break
            if not re.fullmatch(r"[A-Za-z]+", lines[end].strip()):
                break
    return False


def _fitz_alignment(value: str) -> int:
    return {"LEFT": fitz.TEXT_ALIGN_LEFT, "CENTER": fitz.TEXT_ALIGN_CENTER, "RIGHT": fitz.TEXT_ALIGN_RIGHT}[value]


def _is_bold(font_name: str) -> bool:
    value = font_name.casefold()
    return any(token in value for token in ("bold", "black", "heavy", "semibold", "xbold"))


def _contains(outer: Rect, inner: Rect, *, tolerance: float) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _finding(code: str, owner: str, container, message: str, **evidence: object) -> ChartFinding:
    return ChartFinding(code, "HARD", owner, container.association_id, container.container_id, message, dict(evidence))
