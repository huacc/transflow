import argparse
import json
import sys
from pathlib import Path

import fitz

ROUND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROUND_ROOT / "tools"))

from generate_round22_layout_candidate import Group, initial_font_size, sample_background, render_page_image, text_rect_for_group  # noqa: E402


def rect_values(rect: fitz.Rect) -> list[float]:
    return [round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3)]


def rect_from_values(values: list[float]) -> fitz.Rect:
    return fitz.Rect(values)


def x_overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
    overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    return overlap / max(1.0, min(left.width, right.width))


def rect_center(rect: fitz.Rect) -> fitz.Point:
    return fitz.Point((rect.x0 + rect.x1) / 2.0, (rect.y0 + rect.y1) / 2.0)


def rect_contains_point(rect: fitz.Rect, point: fitz.Point, tolerance: float = 0.8) -> bool:
    return rect.x0 - tolerance <= point.x <= rect.x1 + tolerance and rect.y0 - tolerance <= point.y <= rect.y1 + tolerance


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


def rgb255_from_floats(values: tuple[float, float, float] | None) -> tuple[int, int, int] | None:
    if values is None:
        return None
    return tuple(max(0, min(255, round(float(value) * 255))) for value in values)


def infer_filled_rectangles(page: fitz.Page) -> list[dict]:
    rectangles: list[dict] = []
    for drawing in page.get_drawings():
        fill_rgb = rgb255_from_floats(drawing.get("fill"))
        if fill_rgb is None:
            continue
        for item in drawing.get("items", []):
            if item and item[0] == "re":
                rect = fitz.Rect(item[1])
                if rect.width < 24.0 or rect.height < 16.0:
                    continue
                rectangles.append({"rect": rect, "fill_rgb": fill_rgb})
    rectangles.sort(key=lambda item: (item["rect"].y0, item["rect"].x0))
    return rectangles


def spans_x(segment: tuple[float, float, float], x0: float, x1: float, tolerance: float) -> bool:
    return segment[0] <= x0 + tolerance and segment[1] >= x1 - tolerance


def spans_y(segment: tuple[float, float, float], y0: float, y1: float, tolerance: float) -> bool:
    return segment[1] <= y0 + tolerance and segment[2] >= y1 - tolerance


def infer_line_grid_containers(page: fitz.Page) -> list[fitz.Rect]:
    page_rect = page.rect
    horizontal, vertical = drawing_segments(page)
    hlines = [line for line in horizontal if line[1] - line[0] >= page_rect.width * 0.12]
    vlines = [line for line in vertical if line[2] - line[1] >= page_rect.height * 0.035]
    containers: list[fitz.Rect] = []
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
                line
                for line in hlines
                if abs(line[2] - shared_top) <= tolerance * 2 and spans_x(line, x0, x1, tolerance * 2)
            ]
            bottom_candidates = [
                line
                for line in hlines
                if abs(line[2] - shared_bottom) <= tolerance * 2 and spans_x(line, x0, x1, tolerance * 2)
            ]
            if not top_candidates or not bottom_candidates:
                continue
            rect = fitz.Rect(x0, shared_top, x1, shared_bottom)
            if any(abs(rect.x0 - old.x0) <= 1.0 and abs(rect.y0 - old.y0) <= 1.0 and abs(rect.x1 - old.x1) <= 1.0 and abs(rect.y1 - old.y1) <= 1.0 for old in containers):
                continue
            containers.append(rect)
    containers.sort(key=lambda rect: (rect.y0, rect.x0))
    return containers


def infer_section_rules(page: fitz.Page) -> list[float]:
    page_rect = page.rect
    horizontal, _ = drawing_segments(page)
    rules = []
    for x0, x1, y in horizontal:
        if x1 - x0 >= page_rect.width * 0.55 and page_rect.height * 0.04 <= y <= page_rect.height * 0.93:
            rules.append(y)
    deduped = []
    for y in sorted(rules):
        if not deduped or abs(y - deduped[-1]) > 2.0:
            deduped.append(y)
    return deduped


def in_repeated_band(item: dict, page_rect: fitz.Rect) -> bool:
    rect = rect_from_values(item["target_rect"])
    return item.get("role") == "nav_footer" and (rect.y1 < page_rect.height * 0.10 or rect.y0 > page_rect.height * 0.88)


def estimated_draw_extra_height(item: dict) -> float:
    role = item.get("role")
    if role not in {"body", "red_note", "compact_panel", "nav_footer"}:
        return 0.0
    start = float(item.get("font_start") or item.get("source_font_size") or 0.0)
    floor = float(item.get("font_min") or start)
    return max(0.0, (start - floor) * 3.5)


def estimated_text_height(item: dict, width: float) -> float:
    text = str(item.get("target_text") or "")
    font_size = max(4.0, float(item.get("font_start") or item.get("source_font_size") or 7.0))
    usable_width = max(8.0, width)
    avg_char_width = max(2.0, font_size * (0.42 if item.get("role") in {"body", "compact_panel"} else 0.48))
    chars_per_line = max(5, int(usable_width / avg_char_width))
    plain_len = max(1, len(text.replace("\n", " ")))
    explicit_lines = max(1, text.count("\n") + 1)
    estimated_lines = max(explicit_lines, int((plain_len + chars_per_line - 1) / chars_per_line))
    leading = 1.22 if item.get("role") in {"body", "compact_panel"} else 1.14
    return max(font_size * leading, estimated_lines * font_size * leading)


def shift_item(item: dict, shift_y: float, reason: str, anchor: str | None = None) -> None:
    target = rect_from_values(item["target_rect"])
    target.y0 += shift_y
    target.y1 += shift_y
    update_rects(item, target)
    record = {"reason": reason, "shift_y": round(shift_y, 3)}
    if anchor:
        record["anchor"] = anchor
    item.setdefault("flow_adjustments", []).append(record)


def apply_graphic_boundary_limits(planned: list[dict], containers: list[fitz.Rect], page_rect: fitz.Rect) -> None:
    boundary_roles = {"body", "compact_panel", "red_note", "section_heading"}
    vertical_edges = []
    for container in containers:
        vertical_edges.append((container.x0, container.y0, container.y1))
        vertical_edges.append((container.x1, container.y0, container.y1))
    for item in planned:
        if item.get("role") not in boundary_roles or in_repeated_band(item, page_rect):
            continue
        source = rect_from_values(item["source_rect"])
        target = rect_from_values(item["target_rect"])
        for edge_x, edge_y0, edge_y1 in sorted(vertical_edges, key=lambda edge: edge[0]):
            vertical_overlap = min(target.y1, edge_y1) - max(target.y0, edge_y0)
            if vertical_overlap <= min(target.height, max(1.0, edge_y1 - edge_y0)) * 0.12:
                continue
            if source.x1 <= edge_x - 1.0 < target.x1:
                limited_x1 = edge_x - max(1.6, float(item.get("source_font_size") or 6.0) * 0.20)
                if limited_x1 - target.x0 < 12.0:
                    continue
                target.x1 = min(target.x1, limited_x1)
                target.y1 = max(target.y1, target.y0 + estimated_text_height(item, target.width))
                update_rects(item, target)
                item.setdefault("flow_adjustments", []).append(
                    {
                        "reason": "source_graphic_boundary_limit",
                        "edge_x": round(edge_x, 3),
                    }
                )
                break


def apply_filled_panel_compact_layout(planned: list[dict], filled_rectangles: list[dict], page_rect: fitz.Rect) -> None:
    assignments: dict[int, list[dict]] = {}
    for item in planned:
        if item.get("role") != "compact_panel" or in_repeated_band(item, page_rect):
            continue
        source = rect_from_values(item["source_rect"])
        center = rect_center(source)
        for index, record in enumerate(filled_rectangles):
            rect = record["rect"]
            fill_rgb = record["fill_rgb"]
            brightness = sum(fill_rgb) / 3.0
            if brightness > 235:
                continue
            if rect_contains_point(rect, center):
                assignments.setdefault(index, []).append(item)
                break
    for index, items in assignments.items():
        if len(items) < 2:
            continue
        record = filled_rectangles[index]
        panel = record["rect"]
        fill_rgb = record["fill_rgb"]
        pad = max(3.0, min(7.0, panel.width * 0.045))
        inner = fitz.Rect(panel.x0 + pad, panel.y0 + pad, panel.x1 - pad, panel.y1 - pad)
        ordered = sorted(items, key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
        gap = max(1.0, inner.height * 0.06)
        available = max(8.0, inner.height - gap * (len(ordered) - 1))
        slot_height = available / len(ordered)
        y = inner.y0
        for item in ordered:
            rect = fitz.Rect(inner.x0, y, inner.x1, min(inner.y1, y + slot_height))
            source_size = float(item.get("source_font_size") or 7.0)
            item["font_start"] = round(max(4.8, min(float(item.get("font_start") or source_size), source_size * 0.86)), 3)
            item["font_min"] = round(max(4.2, min(float(item.get("font_min") or source_size), source_size * 0.52)), 3)
            item["background_rgb"] = fill_rgb
            update_rects(item, rect)
            item.setdefault("flow_adjustments", []).append(
                {
                    "reason": "filled_panel_compact_stack",
                    "panel_rect": rect_values(panel),
                    "panel_fill_rgb": fill_rgb,
                }
            )
            y = rect.y1 + gap


def apply_container_layout(planned: list[dict], containers: list[fitz.Rect], page_rect: fitz.Rect) -> None:
    if not containers:
        return
    role_scope = {"red_heading", "section_heading", "body", "red_note"}
    assignments: dict[int, list[dict]] = {}
    for item in planned:
        if item.get("role") not in role_scope or in_repeated_band(item, page_rect):
            continue
        source = rect_from_values(item["source_rect"])
        center = rect_center(source)
        for index, container in enumerate(containers):
            if rect_contains_point(container, center):
                assignments.setdefault(index, []).append(item)
                break
    for index, items in assignments.items():
        if len(items) < 2 and not any(item.get("role") == "red_heading" for item in items):
            continue
        container = containers[index]
        pad = max(3.2, min(7.0, container.width * 0.035))
        inner = fitz.Rect(container.x0 + pad, container.y0 + pad, container.x1 - pad, container.y1 - pad)
        ordered = sorted(items, key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
        heading_items = []
        for item in ordered:
            source = rect_from_values(item["source_rect"])
            top_band = source.y0 <= container.y0 + container.height * 0.45
            compact_height = source.height <= container.height * 0.36
            short_text = len(str(item.get("target_text") or "")) <= 96
            if item.get("role") == "red_heading":
                heading_items.append(item)
            elif item.get("role") in {"section_heading", "red_note"} and top_band and compact_height and short_text:
                heading_items.append(item)
        body_items = [item for item in ordered if item not in heading_items]
        y_cursor = inner.y0
        for item in heading_items:
            source = rect_from_values(item["source_rect"])
            source_size = float(item.get("source_font_size") or 8.0)
            estimated = estimated_text_height(item, inner.width)
            if estimated > source.height * 1.35:
                item["font_start"] = round(max(4.8, min(float(item.get("font_start") or source_size), source_size * 0.84)), 3)
                item["font_min"] = round(max(4.5, min(float(item.get("font_min") or source_size), source_size * 0.48)), 3)
                estimated = estimated_text_height(item, inner.width)
            height = max(
                source.height * 1.08,
                min(container.height * 0.44, estimated, max(source.height * 1.75, source_size * 2.6)),
            )
            y0 = max(y_cursor, min(source.y0, inner.y1 - height))
            rect = fitz.Rect(inner.x0, y0, inner.x1, min(inner.y1, y0 + height))
            update_rects(item, rect)
            item.setdefault("flow_adjustments", []).append(
                {
                    "reason": "source_line_grid_container_heading",
                    "container_rect": rect_values(container),
                }
            )
            y_cursor = max(y_cursor, rect.y1 + max(1.2, pad * 0.45))
        body_items.sort(key=lambda item: (item["source_rect"][1], item["source_rect"][0]))
        for offset, item in enumerate(body_items):
            source = rect_from_values(item["source_rect"])
            source_size = float(item.get("source_font_size") or 7.0)
            item["font_start"] = round(max(4.8, min(float(item.get("font_start") or source_size), source_size * 0.88)), 3)
            item["font_min"] = round(max(4.4, min(float(item.get("font_min") or source_size), source_size * 0.50)), 3)
            remaining_items = max(1, len(body_items) - offset)
            remaining_height = max(8.0, inner.y1 - y_cursor)
            base_height = max(source.height * 1.15, estimated_text_height(item, inner.width))
            if remaining_items == 1:
                height = max(base_height, remaining_height)
            else:
                height = min(max(base_height, remaining_height / remaining_items), remaining_height)
            rect = fitz.Rect(inner.x0, y_cursor, inner.x1, min(inner.y1, y_cursor + height))
            update_rects(item, rect)
            item.setdefault("flow_adjustments", []).append(
                {
                    "reason": "source_line_grid_container_body",
                    "container_rect": rect_values(container),
                    "estimated_text_height": round(base_height, 3),
                }
            )
            y_cursor = rect.y1 + max(1.0, pad * 0.35)


def apply_vertical_flow(planned: list[dict], page_rect: fitz.Rect) -> None:
    flow_roles = {"body", "section_heading", "red_note"}
    flow_items = [item for item in planned if item.get("role") in flow_roles and not in_repeated_band(item, page_rect)]
    flow_items.sort(key=lambda item: (item["target_rect"][1], item["target_rect"][0]))
    for _ in range(6):
        moved = False
        for index, item in enumerate(flow_items):
            rect = rect_from_values(item["target_rect"])
            min_gap = max(1.2, float(item.get("source_font_size") or 6.0) * 0.18)
            for previous in flow_items[:index]:
                source_rect = rect_from_values(item["source_rect"])
                previous_source_rect = rect_from_values(previous["source_rect"])
                if source_rect.y0 <= previous_source_rect.y0:
                    continue
                if x_overlap_ratio(source_rect, previous_source_rect) < 0.30:
                    continue
                prev_rect = rect_from_values(previous["target_rect"])
                if x_overlap_ratio(rect, prev_rect) < 0.36:
                    continue
                required_y0 = prev_rect.y1 + estimated_draw_extra_height(previous) + min_gap
                if rect.y0 >= required_y0:
                    continue
                shift = required_y0 - rect.y0
                bottom_limit = page_rect.height - 22.0
                if rect.y1 + shift > bottom_limit:
                    continue
                rect.y0 += shift
                rect.y1 += shift
                update_rects(item, rect)
                item.setdefault("flow_adjustments", []).append(
                    {
                        "reason": "text_column_vertical_overlap",
                        "after_group": previous["group_id"],
                        "shift_y": round(shift, 3),
                    }
                )
                moved = True
        if not moved:
            break


def apply_section_pushdown(planned: list[dict], rule_ys: list[float], page_rect: fitz.Rect) -> None:
    if not rule_ys:
        return
    for rule_y in sorted(rule_ys):
        above = []
        below = []
        for item in planned:
            if in_repeated_band(item, page_rect):
                continue
            source = rect_from_values(item["source_rect"])
            target = rect_from_values(item["target_rect"])
            if source.y0 < rule_y - 1.0:
                above.append((item, source, target))
            elif source.y0 >= rule_y - 1.0:
                below.append((item, source, target))
        if not above or not below:
            continue
        gap = 4.0
        intrusion = max((target.y1 + estimated_draw_extra_height(item) + gap - rule_y for item, _source, target in above), default=0.0)
        if intrusion <= 0.5:
            continue
        bottom_after = max(target.y1 + intrusion for _item, _source, target in below)
        if bottom_after > page_rect.height - 24.0:
            continue
        for item, _source, _target in below:
            shift_item(item, intrusion, "section_pushdown_after_source_rule", f"rule_y={round(rule_y, 3)}")


def update_rects(item: dict, target_rect: fitz.Rect) -> None:
    source_rect = rect_from_values(item["source_rect"])
    erase_rect = fitz.Rect(source_rect)
    erase_rect |= target_rect
    erase_rect.x0 -= 1.2
    erase_rect.y0 -= 1.2
    erase_rect.x1 += 1.2
    erase_rect.y1 += 1.2
    item["target_rect"] = rect_values(target_rect)
    item["erase_rect"] = rect_values(erase_rect)


def apply_metric_stack_layout(planned: list[dict], page_rect: fitz.Rect) -> None:
    compact_items = [item for item in planned if item.get("role") == "compact_panel"]
    metric_items = [item for item in planned if item.get("role") == "metric_value"]
    used_compact_ids: set[str] = set()
    for metric in sorted(metric_items, key=lambda item: (item["source_rect"][1], item["source_rect"][0])):
        metric_source = rect_from_values(metric["source_rect"])
        metric_height = max(8.0, metric_source.height)
        nearby = []
        for item in compact_items:
            if item["group_id"] in used_compact_ids:
                continue
            item_source = rect_from_values(item["source_rect"])
            if x_overlap_ratio(metric_source, item_source) < 0.22:
                continue
            nearest_metric = min(
                metric_items,
                key=lambda candidate: (
                    999999.0
                    if x_overlap_ratio(rect_from_values(candidate["source_rect"]), item_source) < 0.22
                    else abs(
                        rect_from_values(candidate["source_rect"]).y0
                        + rect_from_values(candidate["source_rect"]).height / 2
                        - (item_source.y0 + item_source.height / 2)
                    )
                ),
            )
            if nearest_metric is not metric:
                continue
            if abs(item_source.y0 - metric_source.y0) > metric_height * 1.8 and abs(item_source.y1 - metric_source.y1) > metric_height * 1.8:
                continue
            nearby.append(item)
        if not nearby:
            continue
        stack = sorted([*nearby, metric], key=lambda item: (item["source_rect"][1], 0 if item is not metric else 1))
        x0 = min(rect_from_values(item["target_rect"]).x0 for item in stack)
        x1 = max(rect_from_values(item["target_rect"]).x1 for item in stack)
        y = min(rect_from_values(item["source_rect"]).y0 for item in stack)
        gap = max(1.2, float(metric.get("source_font_size") or 8.0) * 0.10)
        for item in stack:
            current = rect_from_values(item["target_rect"])
            height = max(8.0, current.height)
            if item.get("role") == "metric_value":
                height = max(height, rect_from_values(item["source_rect"]).height)
            new_rect = fitz.Rect(x0, y, min(page_rect.width - 4.0, x1), min(page_rect.height - 22.0, y + height))
            update_rects(item, new_rect)
            item.setdefault("flow_adjustments", []).append(
                {
                    "reason": "metric_stack_relayout",
                    "anchor_metric_group": metric["group_id"],
                    "stack_role": item.get("role"),
                }
            )
            y = new_rect.y1 + gap
            if item.get("role") == "compact_panel":
                used_compact_ids.add(item["group_id"])


def group_from_json(item: dict) -> Group:
    return Group(
        group_id=item["group_id"],
        page_index=int(item["page_index"]),
        lines=[],
        role=item["role"],
        source_rect=fitz.Rect(item["source_rect"]),
        target_text=item.get("target_text", ""),
        color_int=item.get("color_int"),
        source_font_size=float(item.get("source_font_size") or 0),
        bullet_color_int=item.get("bullet_color_int"),
    )


def run(source_pdf: Path, role_plan: Path, output: Path) -> None:
    roles = json.loads(role_plan.read_text(encoding="utf-8"))
    doc = fitz.open(source_pdf)
    pages = []
    for page in roles["pages"]:
        page_index = int(page["page_index"])
        source_page = doc[page_index]
        page_rect = source_page.rect
        page_image = render_page_image(source_page)
        containers = infer_line_grid_containers(source_page)
        filled_rectangles = infer_filled_rectangles(source_page)
        section_rules = infer_section_rules(source_page)
        groups = [group_from_json(item) for item in page["groups"]]
        planned = []
        for group in groups:
            group.background_rgb = sample_background(page_image, group.source_rect)
            target_rect = text_rect_for_group(group, page_rect, groups)
            start_size, min_size = initial_font_size(group)
            erase_rect = fitz.Rect(group.source_rect)
            erase_rect |= target_rect
            erase_rect.x0 -= 1.2
            erase_rect.y0 -= 1.2
            erase_rect.x1 += 1.2
            erase_rect.y1 += 1.2
            planned.append(
                {
                    "group_id": group.group_id,
                    "page_index": group.page_index,
                    "role": group.role,
                    "source_rect": rect_values(group.source_rect),
                    "erase_rect": rect_values(erase_rect),
                    "target_rect": rect_values(target_rect),
                    "target_text": group.target_text,
                    "color_int": group.color_int,
                    "bullet_color_int": group.bullet_color_int,
                    "source_font_size": group.source_font_size,
                    "font_start": round(start_size, 3),
                    "font_min": round(min_size, 3),
                    "background_rgb": group.background_rgb,
                }
            )
        apply_graphic_boundary_limits(planned, containers, page_rect)
        apply_filled_panel_compact_layout(planned, filled_rectangles, page_rect)
        apply_container_layout(planned, containers, page_rect)
        apply_metric_stack_layout(planned, page_rect)
        apply_vertical_flow(planned, page_rect)
        apply_section_pushdown(planned, section_rules, page_rect)
        pages.append(
            {
                "page_index": page_index,
                "page_rect": rect_values(page_rect),
                "source_line_grid_containers": [rect_values(rect) for rect in containers],
                "source_filled_rectangles": [
                    {"rect": rect_values(record["rect"]), "fill_rgb": record["fill_rgb"]}
                    for record in filled_rectangles
                ],
                "source_section_rules_y": [round(value, 3) for value in section_rules],
                "groups": planned,
            }
        )
    report = {
        "tool": "plan_layout",
        "source_pdf": str(source_pdf),
        "role_plan": str(role_plan),
        "pages": pages,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    doc.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--role-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source_pdf, args.role_plan, args.output)


if __name__ == "__main__":
    main()
