from __future__ import annotations

from pathlib import Path
import re
from statistics import median

import fitz

from page_toolbox_puncture.contracts import PageFacts, PageTranslationBundle

from . import TOOLBOX_KEY
from .layout_planner import _color
from .models import SingleColumnTemplate, TextContainer, ToolboxFinding
from .p4_models import P4LayoutPlan, P4LayoutProfile, P4Placement, P4RepairAttempt


P4_PROFILES = (
    P4LayoutProfile("vertical-natural", 1.00, 1.15, 1.00),
    P4LayoutProfile("paragraph-gap-compact", 1.00, 1.08, 0.80),
    P4LayoutProfile("line-gap-compact", 1.00, 1.00, 0.65),
    P4LayoutProfile("font-98", 0.98, 1.00, 0.60),
    P4LayoutProfile("font-95", 0.95, 1.00, 0.55),
    P4LayoutProfile("font-92", 0.92, 0.98, 0.50),
    P4LayoutProfile("font-88", 0.88, 0.98, 0.45),
    P4LayoutProfile("font-84", 0.84, 0.96, 0.40),
    P4LayoutProfile("font-80", 0.80, 0.95, 0.35),
    P4LayoutProfile("font-75", 0.75, 0.95, 0.30),
    P4LayoutProfile("font-72", 0.72, 0.95, 0.25),
)


def build_best_p4_plan(
    *,
    facts: PageFacts,
    template: SingleColumnTemplate,
    translations: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    font_resource: str = "p4cjk",
) -> tuple[P4LayoutPlan | None, tuple[P4RepairAttempt, ...]]:
    attempts: list[P4RepairAttempt] = []
    last_plan: P4LayoutPlan | None = None
    orphan_blocked_font_scale: float | None = None
    for index, profile in enumerate(P4_PROFILES):
        if orphan_blocked_font_scale is not None and profile.font_scale >= orphan_blocked_font_scale - 0.0001:
            continue
        plan, findings = plan_with_profile(
            facts=facts,
            template=template,
            translations=translations,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            font_resource=font_resource,
            profile=profile,
        )
        last_plan = plan
        fit = not any(finding.severity == "HARD" for finding in findings)
        attempts.append(P4RepairAttempt(index, profile.profile_id, profile.font_scale, profile.line_height, profile.gap_scale, fit, findings))
        if fit:
            return plan, tuple(attempts)
        if any(finding.code == "P4_ORPHAN_PUNCTUATION" for finding in findings):
            orphan_blocked_font_scale = profile.font_scale
    return last_plan, tuple(attempts)


def plan_with_profile(
    *,
    facts: PageFacts,
    template: SingleColumnTemplate,
    translations: PageTranslationBundle,
    source_language: str,
    target_language: str,
    font_file: str,
    font_resource: str,
    profile: P4LayoutProfile,
) -> tuple[P4LayoutPlan, tuple[ToolboxFinding, ...]]:
    translated_by_id = {item.container_id: item.translated_text for item in translations.translations}
    if list(translated_by_id) != [item.container_id for item in template.containers]:
        raise ValueError("translation_ids_do_not_match_template_order")
    main = [item for item in template.containers if item.role != "margin"]
    margins = [item for item in template.containers if item.role == "margin"]
    if not main:
        raise ValueError("single_column_template_has_no_main_flow")
    column_left, column_right = _column_bounds(main, template.width)
    content_top = min(item.source_bbox[1] for item in main)
    lower_margins = [item.source_bbox[1] for item in margins if item.source_bbox[1] > content_top]
    locked_footer_tops = [item.bbox[1] for item in facts.text_objects if item.bbox[1] >= template.height * 0.90]
    bottom_guards = lower_margins + locked_footer_tops
    content_bottom = min(bottom_guards) - 4.0 if bottom_guards else template.height - 20.0
    findings: list[ToolboxFinding] = []
    placements_by_id: dict[str, P4Placement] = {}
    body_font_evidence = [item.font_size for item in main if item.role in {"body", "list"}]
    body_font_baseline = median(body_font_evidence) if body_font_evidence else median(item.font_size for item in main)
    cursor_y: float | None = None
    previous: TextContainer | None = None

    for container in main:
        x0, source_y0, source_x1, source_y1 = container.source_bbox
        horizontal_policy, x1 = _horizontal_target(container, main, column_right)
        if x1 <= x0 or x1 > template.width - 8.0:
            findings.append(ToolboxFinding("P4_HORIZONTAL_FLOW_ESCAPE", "HARD", "p4_layout_planner", container.container_id, "目标文字框离开单列水平边界"))
            x1 = min(template.width - 8.0, max(source_x1, x0 + 4.0))
        source_gap = 0.0 if previous is None else max(0.0, source_y0 - previous.source_bbox[3])
        target_gap = source_gap if previous is None else min(48.0, source_gap) * profile.gap_scale
        source_font_size = body_font_baseline if container.role in {"body", "list"} else container.font_size
        flow_policy = "source_anchor_cap"
        if cursor_y is None:
            y0 = source_y0
        elif previous is not None and previous.role == "body" and container.role == "body":
            target_gap = source_gap
            y0 = cursor_y + target_gap
            flow_policy = "body_flow_grouping"
        else:
            natural_y0 = cursor_y + target_gap
            upward_limit = source_y0 - source_font_size * 3.0
            y0 = max(natural_y0, upward_limit)
        font_size = max(6.0, source_font_size * profile.font_scale)
        if font_size + 0.01 < max(6.0, source_font_size * 0.72):
            findings.append(ToolboxFinding("P4_FONT_TOO_SMALL", "HARD", "p4_layout_planner", container.container_id, "字号低于 P4 下限"))
        placement_font_file, placement_font_resource = _font_variant(font_file, font_resource, container.font_weight)
        placement_line_height = profile.line_height
        height = _minimum_text_height(
            page_width=template.width,
            page_height=template.height,
            width=x1 - x0,
            text=translated_by_id[container.container_id],
            font_size=font_size,
            line_height=placement_line_height,
            font_file=placement_font_file,
            font_resource=placement_font_resource,
            color_srgb=container.color_srgb,
        )
        rendered_lines = _rendered_lines(
            page_width=template.width,
            page_height=template.height,
            width=x1 - x0,
            height=height,
            text=translated_by_id[container.container_id],
            font_size=font_size,
            line_height=placement_line_height,
            font_file=placement_font_file,
            font_resource=placement_font_resource,
            color_srgb=container.color_srgb,
        )
        vertical_policy = flow_policy
        source_height = source_y1 - source_y0
        height_ratio = source_height / max(height, 1.0)
        if len(rendered_lines) >= 3 and height_ratio >= 1.25:
            expansion_factor = min(1.22, 1.0 + (height_ratio - 1.0) * 0.5)
            placement_line_height = min(1.40, max(placement_line_height, 1.15 * expansion_factor))
            height = _minimum_text_height(
                page_width=template.width,
                page_height=template.height,
                width=x1 - x0,
                text=translated_by_id[container.container_id],
                font_size=font_size,
                line_height=placement_line_height,
                font_file=placement_font_file,
                font_resource=placement_font_resource,
                color_srgb=container.color_srgb,
            )
            vertical_policy = f"{flow_policy}+line_height_adjust"
        if len(rendered_lines) > 1 and re.fullmatch(r"[，。；：！？、）】》”’…]+", rendered_lines[-1]):
            findings.append(ToolboxFinding("P4_ORPHAN_PUNCTUATION", "HARD", "p4_layout_planner", container.container_id, "句末标点被单独挤到新行"))
        y1 = y0 + height
        fit = y1 <= content_bottom + 0.01
        if not fit:
            findings.append(ToolboxFinding("P4_VERTICAL_PAGE_ESCAPE", "HARD", "p4_layout_planner", container.container_id, "纵向文字流越过页脚或页面底边"))
        placements_by_id[container.container_id] = P4Placement(
            container.container_id,
            translated_by_id[container.container_id],
            container.role,
            container.source_bbox,
            (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)),
            horizontal_policy,
            round(source_font_size, 4),
            round(font_size, 4),
            round(placement_line_height, 4),
            vertical_policy,
            round(source_gap, 4),
            round(target_gap, 4),
            container.color_srgb,
            container.font_weight,
            fit,
        )
        cursor_y = y1
        previous = container

    for container in margins:
        x0, y0, x1, y1 = container.source_bbox
        font_size = max(6.0, container.font_size * profile.font_scale)
        required = _minimum_text_height(template.width, template.height, x1 - x0, translated_by_id[container.container_id], font_size, profile.line_height, font_file, font_resource, container.color_srgb)
        output_y1 = max(y1, y0 + required)
        fit = output_y1 <= template.height - 4.0
        if not fit:
            findings.append(ToolboxFinding("P4_VERTICAL_PAGE_ESCAPE", "HARD", "p4_layout_planner", container.container_id, "页眉页脚文字越过页面边界"))
        placements_by_id[container.container_id] = P4Placement(
            container.container_id,
            translated_by_id[container.container_id],
            container.role,
            container.source_bbox,
            (x0, y0, x1, round(output_y1, 4)),
            "fixed_margin",
            container.font_size,
            round(font_size, 4),
            profile.line_height,
            "fixed_margin",
            0.0,
            0.0,
            container.color_srgb,
            container.font_weight,
            fit,
        )

    placements = tuple(placements_by_id[item.container_id] for item in template.containers)
    plan = P4LayoutPlan(
        template.page_id,
        TOOLBOX_KEY,
        source_language,
        target_language,
        profile.profile_id,
        font_file,
        font_resource,
        round(column_left, 4),
        round(column_right, 4),
        round(content_top, 4),
        round(content_bottom, 4),
        placements,
    )
    return plan, tuple(findings)


def _column_bounds(main: list[TextContainer], page_width: float) -> tuple[float, float]:
    body = [item for item in main if item.role in {"body", "list"} and len(item.source_text) >= 60]
    evidence = body or main
    left = median(item.source_bbox[0] for item in evidence)
    right_values = sorted(item.source_bbox[2] for item in evidence)
    right = right_values[min(len(right_values) - 1, max(0, round((len(right_values) - 1) * 0.9)))]
    return max(8.0, left), min(page_width - 12.0, max(right, left + 40.0))


def _horizontal_target(container: TextContainer, main: list[TextContainer], column_right: float) -> tuple[str, float]:
    x0, y0, source_x1, y1 = container.source_bbox
    column_width = max(1.0, column_right - x0)
    source_width = source_x1 - x0
    short_line = container.role == "heading" or len(container.source_text) <= 80
    clear_right = not any(
        other.container_id != container.container_id
        and other.source_bbox[0] >= source_x1 + 2.0
        and min(y1, other.source_bbox[3]) > max(y0, other.source_bbox[1])
        for other in main
    )
    if short_line and source_width < column_width * 0.8 and clear_right:
        return "exceptional_short_line_expand", column_right
    return "normal_flow_width_invariant", source_x1


def _minimum_text_height(
    page_width: float,
    page_height: float,
    width: float,
    text: str,
    font_size: float,
    line_height: float,
    font_file: str,
    font_resource: str,
    color_srgb: int,
) -> float:
    low = max(font_size * line_height, 2.0)
    high = max(page_height * 1.8, low + 10.0)
    with fitz.open() as probe:
        for _ in range(11):
            middle = (low + high) / 2.0
            page = probe.new_page(width=page_width, height=max(page_height, middle + 10.0))
            result = page.insert_textbox(
                fitz.Rect(0, 0, width, middle),
                text,
                fontname=font_resource,
                fontfile=font_file,
                fontsize=font_size,
                lineheight=line_height,
                color=_color(color_srgb),
            )
            if result >= 0:
                high = middle
            else:
                low = middle
    return round(high + 1.0, 4)


def _font_variant(font_file: str, font_resource: str, font_weight: str) -> tuple[str, str]:
    if font_weight != "bold":
        return font_file, font_resource
    path = Path(font_file)
    candidates = []
    if path.name.casefold() == "msyh.ttc":
        candidates.append(path.with_name("msyhbd.ttc"))
    candidates.append(path.with_name(f"{path.stem}bd{path.suffix}"))
    bold_file = next((candidate for candidate in candidates if candidate.is_file()), None)
    return (str(bold_file), f"{font_resource}_bold") if bold_file else (font_file, font_resource)


def _rendered_lines(
    *,
    page_width: float,
    page_height: float,
    width: float,
    height: float,
    text: str,
    font_size: float,
    line_height: float,
    font_file: str,
    font_resource: str,
    color_srgb: int,
) -> tuple[str, ...]:
    with fitz.open() as probe:
        page = probe.new_page(width=page_width, height=max(page_height, height + 10.0))
        page.insert_textbox(
            fitz.Rect(0, 0, width, height + 2.0),
            text,
            fontname=font_resource,
            fontfile=font_file,
            fontsize=font_size,
            lineheight=line_height,
            color=_color(color_srgb),
        )
        return tuple(line.strip() for line in page.get_text("text").splitlines() if line.strip())
