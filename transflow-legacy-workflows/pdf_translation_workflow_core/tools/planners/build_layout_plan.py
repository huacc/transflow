"""Project a role plan into a run-local layout plan.

tool_name: build_layout_plan
category: planners
input_contract: role_plan JSON, layout_policy JSON, output layout plan path
output_contract: layout_plan JSON with target rects, erase rects, draw modes, font profiles, and validation hints
failure_signals: missing role plan, missing policy, invalid rects, empty groups
fallback: caller keeps legacy generator layout path or routes to S_FAIL_PROCESS_CONTRACT when layout plan is mandatory
anti_overfit_statement: computes target rects from current-page role_plan geometry, source font sizes, target text length, and policy metadata only; no sample filename, page number, text, fixed coordinate, or reference PDF is used
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


CONSTRAINED_ROLES = {"table_cell", "legend", "vertical_nav", "nav_footer"}
LOCAL_EXPAND_ROLES = {"title", "heading", "section_heading", "red_heading", "red_note", "compact_panel", "metric_value"}
FLOW_ROLES = {"body", "body_flow", "footnote"}


def rect_values(values: list[Any]) -> list[float]:
    rect = [float(v) for v in values]
    if len(rect) != 4:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(v, 3) for v in rect]


def rect_width(rect: list[float]) -> float:
    return max(0.0, rect[2] - rect[0])


def rect_height(rect: list[float]) -> float:
    return max(0.0, rect[3] - rect[1])


def union_rect(rects: list[list[float]]) -> list[float]:
    if not rects:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(min(rect[0] for rect in rects), 3),
        round(min(rect[1] for rect in rects), 3),
        round(max(rect[2] for rect in rects), 3),
        round(max(rect[3] for rect in rects), 3),
    ]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def expand_rect(rect: list[float], pad_x: float, pad_y: float, page_rect: list[float]) -> list[float]:
    return [
        round(clamp(rect[0] - pad_x, page_rect[0], page_rect[2]), 3),
        round(clamp(rect[1] - pad_y, page_rect[1], page_rect[3]), 3),
        round(clamp(rect[2] + pad_x, page_rect[0], page_rect[2]), 3),
        round(clamp(rect[3] + pad_y, page_rect[1], page_rect[3]), 3),
    ]


def overlap_area(left: list[float], right: list[float]) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def x_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(rect_width(left), rect_width(right)))


def vertical_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap / max(1.0, min(rect_height(left), rect_height(right)))


def rect_center(rect: list[float]) -> tuple[float, float]:
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def rect_contains_point(rect: list[float], point: tuple[float, float], tolerance: float = 0.8) -> bool:
    return rect[0] - tolerance <= point[0] <= rect[2] + tolerance and rect[1] - tolerance <= point[1] <= rect[3] + tolerance


def fitz_rect_values(rect: fitz.Rect) -> list[float]:
    return [round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3)]


def rgb255_from_floats(values: tuple[float, float, float] | None) -> tuple[int, int, int] | None:
    if values is None:
        return None
    return tuple(max(0, min(255, round(float(value) * 255))) for value in values)


def drawing_segments(page: fitz.Page) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l":
                p0 = item[1]
                p1 = item[2]
                if abs(p0.y - p1.y) <= 0.8 and abs(p0.x - p1.x) >= 12.0:
                    horizontal.append((min(p0.x, p1.x), max(p0.x, p1.x), (p0.y + p1.y) / 2.0))
                elif abs(p0.x - p1.x) <= 0.8 and abs(p0.y - p1.y) >= 12.0:
                    vertical.append(((p0.x + p1.x) / 2.0, min(p0.y, p1.y), max(p0.y, p1.y)))
            elif item[0] == "re":
                rect = fitz.Rect(item[1])
                if rect.width >= 12.0:
                    horizontal.append((rect.x0, rect.x1, rect.y0))
                    horizontal.append((rect.x0, rect.x1, rect.y1))
                if rect.height >= 12.0:
                    vertical.append((rect.x0, rect.y0, rect.y1))
                    vertical.append((rect.x1, rect.y0, rect.y1))
    return horizontal, vertical


def spans_x(segment: tuple[float, float, float], x0: float, x1: float, tolerance: float) -> bool:
    return segment[0] <= x0 + tolerance and segment[1] >= x1 - tolerance


def infer_line_grid_containers(page: fitz.Page) -> list[list[float]]:
    page_rect = page.rect
    horizontal, vertical = drawing_segments(page)
    hlines = [line for line in horizontal if line[1] - line[0] >= page_rect.width * 0.12]
    vlines = [line for line in vertical if line[2] - line[1] >= page_rect.height * 0.035]
    containers: list[list[float]] = []
    tolerance = 1.8
    bands: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for line in vlines:
        key = (round(line[1]), round(line[2]))
        bands.setdefault(key, []).append(line)
    for band_lines in bands.values():
        ordered = sorted(band_lines, key=lambda line: line[0])
        deduped: list[tuple[float, float, float]] = []
        for line in ordered:
            if deduped and abs(line[0] - deduped[-1][0]) <= tolerance:
                continue
            deduped.append(line)
        for left, right in zip(deduped, deduped[1:]):
            x0, x1 = left[0], right[0]
            width = x1 - x0
            if width < page_rect.width * 0.10 or width > page_rect.width * 0.45:
                continue
            shared_top = max(left[1], right[1])
            shared_bottom = min(left[2], right[2])
            if shared_bottom - shared_top < page_rect.height * 0.035:
                continue
            top_candidates = [
                line for line in hlines if abs(line[2] - shared_top) <= tolerance * 2 and spans_x(line, x0, x1, tolerance * 2)
            ]
            bottom_candidates = [
                line for line in hlines if abs(line[2] - shared_bottom) <= tolerance * 2 and spans_x(line, x0, x1, tolerance * 2)
            ]
            if not top_candidates or not bottom_candidates:
                continue
            rect = [round(x0, 3), round(shared_top, 3), round(x1, 3), round(shared_bottom, 3)]
            if any(all(abs(rect[index] - old[index]) <= 1.0 for index in range(4)) for old in containers):
                continue
            containers.append(rect)
    return sorted(containers, key=lambda rect: (rect[1], rect[0]))


def infer_filled_rectangles(page: fitz.Page) -> list[dict[str, Any]]:
    rectangles: list[dict[str, Any]] = []
    for drawing in page.get_drawings():
        fill_rgb = rgb255_from_floats(drawing.get("fill"))
        if fill_rgb is None:
            continue
        for item in drawing.get("items", []):
            if item and item[0] == "re":
                rect = fitz.Rect(item[1])
                if rect.width < 24.0 or rect.height < 16.0:
                    continue
                rectangles.append({"rect": fitz_rect_values(rect), "fill_rgb": list(fill_rgb)})
    return sorted(rectangles, key=lambda item: (item["rect"][1], item["rect"][0]))


def infer_section_rules(page: fitz.Page) -> list[float]:
    page_rect = page.rect
    horizontal, _ = drawing_segments(page)
    rules = []
    for x0, x1, y in horizontal:
        if x1 - x0 >= page_rect.width * 0.55 and page_rect.height * 0.04 <= y <= page_rect.height * 0.93:
            rules.append(y)
    deduped: list[float] = []
    for y in sorted(rules):
        if not deduped or abs(y - deduped[-1]) > 2.0:
            deduped.append(round(y, 3))
    return deduped


def text_length_factor(text: str, target_language: str) -> float:
    if target_language == "zh":
        return max(1.0, len(text) * 0.95)
    return max(1.0, len(text) * 0.52)


def estimate_text_height(text: str, width: float, font_size: float, target_language: str, role: str) -> float:
    usable_width = max(font_size * 2.0, width)
    avg_char_width = max(1.0, font_size * (0.92 if target_language == "zh" else 0.50))
    chars_per_line = max(1, int(usable_width / avg_char_width))
    explicit_lines = max(1, text.count("\n") + 1)
    estimated_lines = max(explicit_lines, math.ceil(text_length_factor(text.replace("\n", " "), target_language) / chars_per_line))
    leading = 1.24 if role in FLOW_ROLES else 1.14
    return estimated_lines * font_size * leading


def in_repeated_band(item: dict[str, Any], page_rect: list[float]) -> bool:
    rect = rect_values(item["target_rect"])
    page_height = max(1.0, page_rect[3] - page_rect[1])
    return item.get("role") == "nav_footer" and (rect[3] < page_height * 0.10 or rect[1] > page_height * 0.88)


def estimated_draw_extra_height(item: dict[str, Any]) -> float:
    if item.get("role") not in {"body", "body_flow", "red_note", "compact_panel", "nav_footer"}:
        return 0.0
    source_size = max(1.0, float(item.get("source_font_size") or 1.0))
    profile = item.get("font_profile") if isinstance(item.get("font_profile"), dict) else {}
    start_ratio = float(profile.get("target_start_source_ratio") or 1.0)
    min_ratio = float(profile.get("target_min_source_ratio") or 0.62)
    return max(0.0, source_size * (start_ratio - min_ratio) * 3.5)


def update_rects(item: dict[str, Any], target_rect: list[float], page_rect: list[float]) -> None:
    source = rect_values(item["source_rect"])
    source_font = max(1.0, float(item.get("source_font_size") or 1.0))
    target = [
        round(clamp(target_rect[0], page_rect[0], page_rect[2]), 3),
        round(clamp(target_rect[1], page_rect[1], page_rect[3]), 3),
        round(clamp(target_rect[2], page_rect[0], page_rect[2]), 3),
        round(clamp(target_rect[3], page_rect[1], page_rect[3]), 3),
    ]
    target[2] = max(target[0] + 2.0, target[2])
    target[3] = max(target[1] + 2.0, target[3])
    item["target_rect"] = target
    union = [
        min(source[0], target[0]) - source_font * 0.08,
        min(source[1], target[1]) - source_font * 0.08,
        max(source[2], target[2]) + source_font * 0.08,
        max(source[3], target[3]) + source_font * 0.08,
    ]
    item["erase_rect"] = expand_rect(
        [round(union[0], 3), round(union[1], 3), round(union[2], 3), round(union[3], 3)],
        0.0,
        0.0,
        page_rect,
    )


def record_adjustment(item: dict[str, Any], reason: str, **values: Any) -> None:
    item.setdefault("flow_adjustments", []).append({"reason": reason, **values})


def same_column_next_top(current: dict[str, Any], groups: list[dict[str, Any]]) -> float | None:
    current_rect = rect_values(current["source_rect"])
    candidates: list[float] = []
    for other in groups:
        if other is current:
            continue
        other_rect = rect_values(other["source_rect"])
        if other_rect[1] <= current_rect[1] + 0.5:
            continue
        if x_overlap_ratio(current_rect, other_rect) < 0.22:
            continue
        candidates.append(other_rect[1])
    return min(candidates) if candidates else None


def role_draw_mode(role: str, policy: dict[str, Any]) -> str:
    draw_modes = policy.get("draw_modes", {}) if isinstance(policy.get("draw_modes"), dict) else {}
    if isinstance(draw_modes.get(role), dict) and draw_modes[role].get("mode"):
        return str(draw_modes[role]["mode"])
    if role == "vertical_nav":
        return "rotated_horizontal_text_image"
    if role in CONSTRAINED_ROLES:
        return "textbox_or_constrained_text_image"
    return "textbox"


def apply_translation_growth_slots(planned: list[dict[str, Any]], page_rect: list[float], target_language: str) -> None:
    growth_roles = {"title", "body", "body_flow", "section_heading", "red_note", "compact_panel"}
    page_height = max(1.0, page_rect[3] - page_rect[1])
    for item in planned:
        if item.get("role") not in growth_roles or in_repeated_band(item, page_rect):
            continue
        rect = rect_values(item["target_rect"])
        source = rect_values(item["source_rect"])
        source_size = max(1.0, float(item.get("source_font_size") or 1.0))
        desired = estimate_text_height(str(item.get("target_text") or ""), rect_width(rect), source_size, target_language, str(item.get("role") or "body"))
        next_tops = [
            rect_values(other["source_rect"])[1]
            for other in planned
            if other is not item
            and rect_values(other["source_rect"])[1] > source[1] + 0.6
            and x_overlap_ratio(source, rect_values(other["source_rect"])) >= 0.24
        ]
        bottom_limit = page_rect[3] - max(20.0, page_height * 0.025)
        if next_tops:
            bottom_limit = min(bottom_limit, min(next_tops) - max(1.8, source_size * 0.25))
        if item.get("role") == "compact_panel":
            bottom_limit = min(bottom_limit, rect[1] + max(rect_height(rect) * 1.7, source_size * 3.0))
        desired = min(bottom_limit - rect[1], max(rect_height(rect), desired))
        if desired > rect_height(rect) + 0.8:
            rect[3] = round(rect[1] + desired, 3)
            update_rects(item, rect, page_rect)
            record_adjustment(item, "translation_growth_slot_expand", estimated_text_height=round(desired, 3), target_height=round(rect_height(rect), 3))


def apply_vertical_flow(planned: list[dict[str, Any]], page_rect: list[float]) -> None:
    flow_roles = {"body", "body_flow", "section_heading", "red_note"}
    anchor_roles = flow_roles | {"title", "heading", "red_heading", "metric_value", "compact_panel"}
    flow_items = [item for item in planned if item.get("role") in flow_roles and not in_repeated_band(item, page_rect)]
    flow_items.sort(key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
    ordered_anchors = [
        item
        for item in planned
        if item.get("role") in anchor_roles and not in_repeated_band(item, page_rect)
    ]
    ordered_anchors.sort(key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
    for _pass_index in range(3):
        changed = False
        for item in flow_items:
            rect = rect_values(item["target_rect"])
            source_rect = rect_values(item["source_rect"])
            min_gap = max(1.2, float(item.get("source_font_size") or 6.0) * 0.18)
            for previous in ordered_anchors:
                if previous is item:
                    continue
                previous_source = rect_values(previous["source_rect"])
                if source_rect[1] <= previous_source[1]:
                    continue
                prev_rect = rect_values(previous["target_rect"])
                same_source_column = x_overlap_ratio(source_rect, previous_source) >= 0.30
                same_output_column = x_overlap_ratio(rect, prev_rect) >= 0.30
                if not same_source_column and not same_output_column:
                    continue
                needed_y0 = prev_rect[3] + min_gap
                if rect[1] >= needed_y0:
                    continue
                shift = needed_y0 - rect[1]
                if rect[3] + shift > page_rect[3] - max(18.0, float(item.get("source_font_size") or 6.0) * 1.8):
                    continue
                rect[1] += shift
                rect[3] += shift
                update_rects(item, rect, page_rect)
                record_adjustment(item, "text_column_vertical_overlap", shift_y=round(shift, 3), previous_group_id=previous.get("group_id"))
                changed = True
            if changed:
                break
        if not changed:
            break


def apply_section_pushdown(planned: list[dict[str, Any]], rule_ys: list[float], page_rect: list[float]) -> None:
    if len(rule_ys) >= 12:
        sorted_rules = sorted(rule_ys)
        gaps = [b - a for a, b in zip(sorted_rules, sorted_rules[1:]) if b > a]
        median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 999.0
        if median_gap <= max(14.0, (page_rect[3] - page_rect[1]) * 0.018):
            return
    movable_roles = {"body", "body_flow", "section_heading", "red_note", "compact_panel"}
    for rule_y in rule_ys:
        above: list[tuple[dict[str, Any], list[float], list[float]]] = []
        below: list[tuple[dict[str, Any], list[float], list[float]]] = []
        for item in planned:
            if item.get("role") not in movable_roles or in_repeated_band(item, page_rect):
                continue
            source = rect_values(item["source_rect"])
            target = rect_values(item["target_rect"])
            if source[1] < rule_y - 1.0:
                above.append((item, source, target))
            else:
                below.append((item, source, target))
        if not above or not below:
            continue
        gap = max(2.0, max(float(item.get("source_font_size") or 6.0) for item, _source, _target in above) * 0.22)
        intrusion = max((target[3] + estimated_draw_extra_height(item) + gap - rule_y for item, _source, target in above), default=0.0)
        if intrusion <= 1.0:
            continue
        bottom_after = max(target[3] + intrusion for _item, _source, target in below)
        if bottom_after > page_rect[3] - 18.0:
            continue
        for item, _source, target in below:
            shifted = [target[0], target[1] + intrusion, target[2], target[3] + intrusion]
            update_rects(item, shifted, page_rect)
            record_adjustment(item, "section_pushdown_after_source_rule", shift_y=round(intrusion, 3), rule_y=round(rule_y, 3))


def infer_table_regions(planned: list[dict[str, Any]], page_rect: list[float]) -> list[list[float]]:
    table_rects = [rect_values(item["source_rect"]) for item in planned if item.get("role") == "table_cell"]
    if len(table_rects) < 6:
        return []
    table_rects.sort(key=lambda rect: (rect[1], rect[0]))
    regions: list[list[float]] = []
    current: list[list[float]] = []
    for rect in table_rects:
        if not current:
            current = [rect]
            continue
        current_union = union_rect(current)
        gap = rect[1] - current_union[3]
        if gap <= max(8.0, rect_height(rect) * 1.8) and x_overlap_ratio(rect, current_union) >= 0.06:
            current.append(rect)
        else:
            if len(current) >= 6:
                regions.append(union_rect(current))
            current = [rect]
    if len(current) >= 6:
        regions.append(union_rect(current))
    return [region for region in regions if rect_width(region) >= (page_rect[2] - page_rect[0]) * 0.22]


def pack_flow_above_table_regions(planned: list[dict[str, Any]], page_rect: list[float]) -> None:
    for region in infer_table_regions(planned, page_rect):
        previous_bottom = max((rect_values(item["target_rect"])[3] for item in planned if rect_values(item["source_rect"])[3] < region[1] - 1.0), default=page_rect[1])
        candidates = [
            item
            for item in planned
            if item.get("role") in {"body", "body_flow", "section_heading", "red_note"}
            and not in_repeated_band(item, page_rect)
            and rect_values(item["source_rect"])[1] >= previous_bottom - 1.0
            and rect_values(item["source_rect"])[3] <= region[1] + 0.8
            and (x_overlap_ratio(rect_values(item["source_rect"]), region) >= 0.12 or x_overlap_ratio(rect_values(item["target_rect"]), region) >= 0.12)
        ]
        if len(candidates) < 2:
            continue
        columns: list[list[dict[str, Any]]] = []
        for item in sorted(candidates, key=lambda value: (value["source_rect"][0], value["source_rect"][1])):
            source = rect_values(item["source_rect"])
            for column in columns:
                column_source = rect_values(column[0]["source_rect"])
                if x_overlap_ratio(source, column_source) >= 0.22 or abs(source[0] - column_source[0]) <= max(10.0, rect_width(source) * 0.25):
                    column.append(item)
                    break
            else:
                columns.append([item])
        for column in columns:
            ordered = sorted(column, key=lambda value: value["source_rect"][1])
            top = max(previous_bottom + 2.0, min(rect_values(item["source_rect"])[1] for item in ordered))
            bottom = region[1] - max(2.4, max(float(item.get("source_font_size") or 6.0) for item in ordered) * 0.28)
            available = bottom - top
            if available <= 8.0:
                continue
            gap = max(1.2, min(float(item.get("source_font_size") or 6.0) for item in ordered) * 0.18)
            total_source_height = sum(rect_height(rect_values(item["source_rect"])) for item in ordered)
            scale = max(0.55, min(1.0, (available - gap * (len(ordered) - 1)) / max(1.0, total_source_height)))
            y = top
            for item in ordered:
                source_size = float(item.get("source_font_size") or 6.0)
                height = max(source_size * 1.05, rect_height(rect_values(item["source_rect"])) * scale)
                rect = rect_values(item["target_rect"])
                new_rect = [rect[0], round(y, 3), rect[2], round(y + height, 3)]
                update_rects(item, new_rect, page_rect)
                profile = item.get("font_profile") if isinstance(item.get("font_profile"), dict) else {}
                profile["target_start_source_ratio"] = round(max(0.62, min(float(profile.get("target_start_source_ratio") or 1.0), scale)), 3)
                profile["target_min_source_ratio"] = round(max(0.50, min(float(profile.get("target_min_source_ratio") or 0.62), 0.58)), 3)
                item["font_profile"] = profile
                record_adjustment(item, "table_region_obstacle_pack", table_region=region, vertical_scale=round(scale, 3))
                y += height + gap


def apply_container_layout(planned: list[dict[str, Any]], containers: list[list[float]], page_rect: list[float], target_language: str) -> None:
    if not containers:
        return
    for container in containers:
        items = [
            item
            for item in planned
            if item.get("role") in {"heading", "section_heading", "body", "body_flow", "compact_panel", "red_note"}
            and rect_contains_point(container, rect_center(rect_values(item["source_rect"])))
        ]
        if not items:
            continue
        pad = max(3.2, min(7.0, rect_width(container) * 0.035))
        inner = [container[0] + pad, container[1] + pad, container[2] - pad, container[3] - pad]
        ordered = sorted(items, key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
        y = inner[1]
        for item in ordered:
            source = rect_values(item["source_rect"])
            source_size = float(item.get("source_font_size") or 7.0)
            role = str(item.get("role") or "body")
            estimated = estimate_text_height(str(item.get("target_text") or ""), rect_width(inner), source_size, target_language, role)
            height = max(rect_height(source) * 1.12, min(estimated * 1.08, max(source_size * 2.2, rect_height(inner) * 0.46)))
            if y + height > inner[3]:
                height = max(source_size * 1.12, inner[3] - y)
            if height <= 2.0:
                continue
            target = [inner[0], round(y, 3), inner[2], round(y + height, 3)]
            update_rects(item, target, page_rect)
            profile = item.get("font_profile") if isinstance(item.get("font_profile"), dict) else {}
            profile["target_start_source_ratio"] = round(max(0.56, min(float(profile.get("target_start_source_ratio") or 1.0), 0.88)), 3)
            profile["target_min_source_ratio"] = round(max(0.50, min(float(profile.get("target_min_source_ratio") or 0.62), 0.56)), 3)
            item["font_profile"] = profile
            record_adjustment(item, "source_line_grid_container_flow", container_rect=container)
            y += height + max(1.0, source_size * 0.16)


def apply_filled_panel_compact_layout(planned: list[dict[str, Any]], filled_rectangles: list[dict[str, Any]], page_rect: list[float]) -> None:
    for panel_record in filled_rectangles:
        panel = rect_values(panel_record["rect"])
        items = [
            item
            for item in planned
            if item.get("role") == "compact_panel"
            and rect_contains_point(panel, rect_center(rect_values(item["source_rect"])))
        ]
        if not items:
            continue
        pad = max(3.0, min(7.0, rect_width(panel) * 0.045))
        inner = [panel[0] + pad, panel[1] + pad, panel[2] - pad, panel[3] - pad]
        ordered = sorted(items, key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
        step = max(4.0, rect_height(inner) / max(1, len(ordered)))
        y = inner[1]
        for item in ordered:
            source_size = float(item.get("source_font_size") or 7.0)
            height = max(source_size * 1.18, min(step * 0.82, rect_height(inner)))
            update_rects(item, [inner[0], round(y, 3), inner[2], round(min(inner[3], y + height), 3)], page_rect)
            item["background_rgb"] = panel_record.get("fill_rgb")
            profile = item.get("font_profile") if isinstance(item.get("font_profile"), dict) else {}
            profile["target_start_source_ratio"] = round(max(0.52, min(float(profile.get("target_start_source_ratio") or 1.0), 0.86)), 3)
            profile["target_min_source_ratio"] = round(max(0.48, min(float(profile.get("target_min_source_ratio") or 0.62), 0.52)), 3)
            item["font_profile"] = profile
            record_adjustment(item, "filled_panel_compact_stack", panel_rect=panel, panel_fill_rgb=panel_record.get("fill_rgb"))
            y += step


def target_rect_for_group(group: dict[str, Any], page_rect: list[float], groups: list[dict[str, Any]], target_language: str) -> tuple[list[float], list[dict[str, Any]], str]:
    role = str(group.get("role") or "body")
    source = rect_values(group.get("source_rect", [0, 0, 0, 0]))
    source_font = max(1.0, float(group.get("source_font_size") or 1.0))
    page_width = max(1.0, page_rect[2] - page_rect[0])
    page_height = max(1.0, page_rect[3] - page_rect[1])
    margin = max(source_font * 1.2, page_width * 0.035)
    left_margin = max(page_rect[0] + page_width * 0.035, page_rect[0] + source_font * 1.4)
    right_margin = page_rect[2] - max(page_width * 0.035, source_font * 1.4)
    gap = max(4.0, source_font * 0.70)
    adjustments: list[dict[str, Any]] = []

    if role in CONSTRAINED_ROLES:
        target = expand_rect(source, source_font * 0.10, source_font * 0.08, page_rect)
        return target, [{"reason": "constrained_role_preserve_source_slot"}], "constrained_slot"

    target = expand_rect(source, source_font * 0.20, source_font * 0.10, page_rect)
    same_band = [
        rect_values(other.get("source_rect", [0, 0, 0, 0]))
        for other in groups
        if other is not group
        and vertical_overlap_ratio(source, rect_values(other.get("source_rect", [0, 0, 0, 0]))) > 0.25
    ]
    right_obstacles = [other[0] for other in same_band if other[0] > source[0] + gap]
    left_obstacles = [other[2] for other in same_band if other[2] < source[0] - gap]
    column_right = min(right_obstacles) - gap if right_obstacles else right_margin
    column_left = max(left_obstacles) + gap if left_obstacles else left_margin
    target[0] = max(column_left, target[0])
    width_cap = max(8.0, column_right - target[0])

    target_text = str(group.get("target_text") or "")
    if role in {"body", "body_flow", "red_note", "section_heading"}:
        if source[0] < page_rect[0] + page_width * 0.38:
            desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.42, page_width * 0.36)))
        else:
            desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.30, page_width * 0.32)))
    elif role == "title":
        estimated_width = text_length_factor(target_text, target_language) * max(3.0, source_font * (0.46 if target_language == "en" else 0.90))
        base_width = page_width * (0.78 if estimated_width > rect_width(source) * 1.25 else 0.42)
        desired_width = max(rect_width(source), min(width_cap, base_width))
    elif role == "metric_value":
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.25, page_width * 0.20)))
    elif role == "red_heading":
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.36, page_width * 0.22)))
    elif role == "compact_panel":
        estimated_width = text_length_factor(target_text, target_language) * max(2.0, source_font * 0.38)
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.15, min(estimated_width, page_width * 0.28))))
    else:
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.15, page_width * 0.28)))
    if desired_width > rect_width(target) + 0.5:
        target[2] = round(min(column_right, page_rect[2] - margin, target[0] + desired_width), 3)
        adjustments.append({"reason": "target_text_growth_width_expand", "desired_width": round(desired_width, 3)})

    desired_height = estimate_text_height(target_text, rect_width(target), source_font, target_language, role)
    next_top = same_column_next_top(group, groups)
    local_gap = max(1.5, source_font * 0.35)
    bottom_limit = page_rect[3] - max(12.0, page_height * 0.025)
    if next_top is not None:
        bottom_limit = min(bottom_limit, next_top - local_gap)
    height_cap = max(rect_height(target), bottom_limit - target[1])
    desired_height = min(height_cap, max(rect_height(target), desired_height))
    if desired_height > rect_height(target) + 0.5:
        target[3] = round(target[1] + desired_height, 3)
        adjustments.append({"reason": "target_text_growth_height_expand", "desired_height": round(desired_height, 3)})

    if role in FLOW_ROLES:
        mode = "fluid_body" if role == "body_flow" else "source_anchor_expanded_flow"
    elif role in LOCAL_EXPAND_ROLES:
        mode = "local_expandable_slot"
    else:
        mode = "source_anchor_slot"
    return target, adjustments or [{"reason": "source_anchor_rect_retained"}], mode


def build_layout_plan(role_plan_path: Path, layout_policy_path: Path, source_pdf_path: Path | None = None) -> dict[str, Any]:
    role_plan = read_json(role_plan_path)
    layout_policy = read_json(layout_policy_path)
    target_language = str(role_plan.get("target_language") or layout_policy.get("target_language") or "zh")
    source_doc = fitz.open(source_pdf_path) if source_pdf_path else None
    pages_out: list[dict[str, Any]] = []
    total_groups = 0
    all_overlaps: list[dict[str, Any]] = []

    for page in role_plan.get("pages", []):
        page_index = int(page.get("page_index", 0))
        page_rect = rect_values(page.get("page_rect", [0, 0, 0, 0]))
        source_groups = list(page.get("groups", []))
        planned_groups: list[dict[str, Any]] = []
        source_page = source_doc[page_index] if source_doc is not None and 0 <= page_index < source_doc.page_count else None
        containers = infer_line_grid_containers(source_page) if source_page is not None else []
        filled_rectangles = infer_filled_rectangles(source_page) if source_page is not None else []
        section_rules = infer_section_rules(source_page) if source_page is not None else []
        for group in source_groups:
            role = str(group.get("role") or "body")
            source_rect = rect_values(group.get("source_rect", [0, 0, 0, 0]))
            target_rect, adjustments, mode = target_rect_for_group(group, page_rect, source_groups, target_language)
            source_font = max(1.0, float(group.get("source_font_size") or 1.0))
            estimated_height = estimate_text_height(str(group.get("target_text") or ""), rect_width(target_rect), source_font, target_language, role)
            fit_status = "estimated_fit" if estimated_height <= rect_height(target_rect) + source_font * 0.30 else "estimated_overflow"
            planned_groups.append(
                {
                    "group_id": group.get("group_id"),
                    "line_ids": group.get("line_ids", []),
                    "role": role,
                    "layout_mode": mode,
                    "source_rect": source_rect,
                    "erase_rect": expand_rect(source_rect, source_font * 0.08, source_font * 0.08, page_rect),
                    "target_rect": target_rect,
                    "target_text": group.get("target_text", ""),
                    "source_font_size": round(source_font, 3),
                    "font_profile": {
                        "sizing_mode": "source_relative",
                        "source_font_size": round(source_font, 3),
                        "target_start_source_ratio": 1.0,
                        "target_min_source_ratio": 0.62 if role not in {"metric_value", "heading", "red_heading"} else 0.70,
                        "page_quantile_reference": "current_page_from_role_plan",
                    },
                    "draw_mode": role_draw_mode(role, layout_policy),
                    "fit_estimate": {
                        "status": fit_status,
                        "estimated_text_height": round(estimated_height, 3),
                        "target_height": round(rect_height(target_rect), 3),
                    },
                    "layout_adjustments": adjustments,
                    "source_role_evidence": group.get("role_evidence", {}),
                }
            )

        apply_container_layout(planned_groups, containers, page_rect, target_language)
        apply_filled_panel_compact_layout(planned_groups, filled_rectangles, page_rect)
        apply_translation_growth_slots(planned_groups, page_rect, target_language)
        apply_vertical_flow(planned_groups, page_rect)
        apply_section_pushdown(planned_groups, section_rules, page_rect)
        apply_vertical_flow(planned_groups, page_rect)
        pack_flow_above_table_regions(planned_groups, page_rect)

        for item in planned_groups:
            role = str(item.get("role") or "body")
            source_font = max(1.0, float(item.get("source_font_size") or 1.0))
            target_rect = rect_values(item["target_rect"])
            estimated_height = estimate_text_height(str(item.get("target_text") or ""), rect_width(target_rect), source_font, target_language, role)
            item["fit_estimate"] = {
                "status": "estimated_fit" if estimated_height <= rect_height(target_rect) + source_font * 0.30 else "estimated_overflow",
                "estimated_text_height": round(estimated_height, 3),
                "target_height": round(rect_height(target_rect), 3),
            }

        for left_index, left in enumerate(planned_groups):
            for right in planned_groups[left_index + 1 :]:
                area = overlap_area(left["target_rect"], right["target_rect"])
                if area <= 0:
                    continue
                ratio = area / max(1.0, min(rect_width(left["target_rect"]) * rect_height(left["target_rect"]), rect_width(right["target_rect"]) * rect_height(right["target_rect"])))
                if ratio >= 0.08:
                    all_overlaps.append(
                        {
                            "page_index": int(page.get("page_index", 0)),
                            "left_group_id": left.get("group_id"),
                            "right_group_id": right.get("group_id"),
                            "overlap_ratio_of_smaller": round(ratio, 4),
                            "repair_hint": "layout_plan_overlap_requires_S6_relayout",
                        }
                    )

        total_groups += len(planned_groups)
        pages_out.append(
            {
                "page_index": page_index,
                "page_rect": page_rect,
                "source_line_grid_containers": containers,
                "source_filled_rectangles": filled_rectangles,
                "source_section_rules_y": section_rules,
                "groups": planned_groups,
            }
        )

    if total_groups <= 0:
        raise ValueError("layout plan requires at least one role group")
    if source_doc is not None:
        source_doc.close()

    return {
        "tool": "build_layout_plan",
        "policy_version": "layout_plan_v2.generator_consumable",
        "behavior_mode": "generator_consumable",
        "layout_plan_consumable_by_generator": True,
        "role_plan": rel(role_plan_path),
        "role_plan_sha256": sha256_file(role_plan_path),
        "layout_policy": rel(layout_policy_path),
        "layout_policy_sha256": sha256_file(layout_policy_path),
        "source_pdf": None if source_pdf_path is None else rel(source_pdf_path),
        "source_pdf_sha256": None if source_pdf_path is None else sha256_file(source_pdf_path),
        "source_language": role_plan.get("source_language"),
        "target_language": target_language,
        "target_text_field": role_plan.get("target_text_field"),
        "group_count": total_groups,
        "estimated_overlap_count": len(all_overlaps),
        "estimated_overlaps": all_overlaps[:200],
        "anti_overfit": "all target rects are projected from current role_plan geometry, source font size, target text length, and generic role classes only",
        "pages": pages_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-plan", required=True)
    parser.add_argument("--layout-policy", required=True)
    parser.add_argument("--source-pdf")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    role_plan_path = resolve_workspace_path(args.role_plan)
    layout_policy_path = resolve_workspace_path(args.layout_policy)
    source_pdf_path = resolve_workspace_path(args.source_pdf) if args.source_pdf else None
    out_path = resolve_workspace_path(args.out)
    write_json(out_path, build_layout_plan(role_plan_path, layout_policy_path, source_pdf_path))


if __name__ == "__main__":
    main()
