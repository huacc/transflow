from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import Rect, VisualAnchoredContainer, VisualAnchoredTemplate, VisualTextSlot


class VisualAnchoredCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Line:
    objects: tuple[TextObjectFact, ...]
    text: str
    bbox: Rect
    font_size: float
    color_srgb: int


@dataclass(frozen=True)
class _Visual:
    object_id: str
    bbox: Rect
    kind: str


def build_visual_anchored_template(
    facts: PageFacts,
    source_pdf: Path | None = None,
) -> VisualAnchoredTemplate:
    visuals = tuple(
        [_Visual(item.object_id, item.bbox, "image") for item in facts.image_objects]
        + [_Visual(item.object_id, item.bbox, "drawing") for item in facts.drawing_objects]
    )
    page_area = facts.width * facts.height
    if not visuals or max((_area(item.bbox) for item in visuals), default=0.0) < page_area * 0.08:
        raise VisualAnchoredCapabilityError("VISUAL_ANCHOR_NOT_FOUND")

    margin_protected_ids = {
        item.object_id
        for item in facts.text_objects
        if _margin_page_number(item, facts.height)
    }
    lines = _logical_lines(tuple(item for item in facts.text_objects if item.object_id not in margin_protected_ids))
    editable_lines = [line for line in lines if not _protected(line)]
    protected_ids = tuple(
        dict.fromkeys(
            sorted(margin_protected_ids)
            + [
                object_.object_id
                for line in lines
                if _protected(line)
                for object_ in line.objects
            ]
        )
    )
    if not editable_lines:
        raise VisualAnchoredCapabilityError("VISUAL_ANCHORED_NATIVE_TEXT_NOT_FOUND")

    groups = _container_groups(editable_lines)
    median_font = statistics.median(line.font_size for line in editable_lines)
    slots: list[VisualTextSlot] = []
    containers: list[VisualAnchoredContainer] = []
    for reading_order, group in enumerate(groups):
        source_bbox = _union([line.bbox for line in group])
        source_ids = tuple(object_.object_id for line in group for object_ in line.objects)
        background = tuple(
            item for item in visuals if _center_inside(source_bbox, item.bbox) or _coverage(item.bbox, source_bbox) >= 0.20
        )
        panel = _containing_panel(source_bbox, background, page_area)
        alignment = _alignment(group, panel, groups, facts.width)
        boundary, allowed, anchor_ids = _slot_geometry(
            group,
            source_bbox,
            groups,
            visuals,
            background,
            panel,
            alignment,
            facts.width,
            facts.height,
        )
        background_ids = tuple(item.object_id for item in background) or ("page-canvas",)
        if not anchor_ids:
            anchor_ids = (min(visuals, key=lambda item: _rect_gap(item.bbox, source_bbox)).object_id,)
        color = _dominant_color(group)
        background_rgb = _sample_background(source_pdf, facts.page_index, allowed, color)
        contrast = round(_contrast_ratio(_rgb(color), background_rgb), 4)
        slot_id = f"visual-slot-{reading_order:03d}"
        safe_padding = (
            round(allowed[0] - boundary[0], 4),
            round(allowed[1] - boundary[1], 4),
            round(boundary[2] - allowed[2], 4),
            round(boundary[3] - allowed[3], 4),
        )
        slots.append(
            VisualTextSlot(
                slot_id=slot_id,
                boundary_bbox=boundary,
                allowed_bbox=allowed,
                safe_padding=safe_padding,
                source_object_ids=source_ids,
                background_object_ids=background_ids,
                anchor_object_ids=anchor_ids,
                background_rgb=background_rgb,
                source_contrast_ratio=contrast,
                z_order="TEXT_OVER_VISUAL" if background else "TEXT_OVER_PAGE_CANVAS",
                alignment=alignment,
                reading_order=reading_order,
            )
        )
        font_size = statistics.median(line.font_size for line in group)
        first = group[0].objects[0]
        containers.append(
            VisualAnchoredContainer(
                container_id=slot_id,
                slot_id=slot_id,
                source_object_ids=source_ids,
                source_text=_joined_text(group),
                source_bbox=source_bbox,
                allowed_bbox=allowed,
                reading_order=reading_order,
                required_literals=_required_literals(_joined_text(group)),
                role=_role(font_size, median_font),
                font_name=first.font_name,
                font_size=round(font_size, 4),
                color_srgb=color,
                alignment=alignment,
            )
        )

    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "page_id": facts.page_id,
            "slots": slots,
            "containers": containers,
            "protected_object_ids": protected_ids,
        }
    )
    return VisualAnchoredTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        visual_slots=tuple(slots),
        containers=tuple(containers),
        protected_object_ids=protected_ids,
        structure_sha256=structure_sha256,
    )


def _logical_lines(objects: tuple[TextObjectFact, ...]) -> list[_Line]:
    canonical_objects, aliases_by_id = _canonical_text_objects(objects)
    bands: list[list[TextObjectFact]] = []
    for item in sorted(canonical_objects, key=lambda value: (_center_y(value.bbox), value.bbox[0])):
        target = next((band for band in reversed(bands) if any(_same_baseline(member, item) for member in band)), None)
        if target is None:
            bands.append([item])
        else:
            target.append(item)

    rows: list[list[TextObjectFact]] = []
    for band in bands:
        current: list[TextObjectFact] = []
        for item in sorted(band, key=lambda value: (value.bbox[0], value.bbox[1])):
            if current and not _same_row(current, item):
                rows.append(current)
                current = []
            current.append(item)
        if current:
            rows.append(current)
    result = []
    for row in rows:
        ordered = tuple(sorted(row, key=lambda value: value.bbox[0]))
        source_objects = tuple(
            alias
            for item in ordered
            for alias in aliases_by_id[item.object_id]
        )
        result.append(
            _Line(
                objects=source_objects,
                text=_join_fragments(ordered),
                bbox=_union([item.bbox for item in ordered]),
                font_size=statistics.median(item.font_size for item in ordered),
                color_srgb=statistics.mode(item.color_srgb for item in ordered),
            )
        )
    return sorted(result, key=lambda line: (line.bbox[1], line.bbox[0]))


def _canonical_text_objects(
    objects: tuple[TextObjectFact, ...],
) -> tuple[tuple[TextObjectFact, ...], dict[str, tuple[TextObjectFact, ...]]]:
    representatives: dict[tuple[object, ...], TextObjectFact] = {}
    aliases: dict[str, list[TextObjectFact]] = {}
    for item in objects:
        key = (
            item.text,
            tuple(round(value, 3) for value in item.bbox),
            item.font_name,
            round(item.font_size, 3),
            item.color_srgb,
        )
        representative = representatives.get(key)
        if representative is None:
            representatives[key] = item
            aliases[item.object_id] = [item]
        else:
            aliases[representative.object_id].append(item)
    return tuple(representatives.values()), {
        object_id: tuple(items)
        for object_id, items in aliases.items()
    }


def _same_baseline(left: TextObjectFact, right: TextObjectFact) -> bool:
    if _drop_cap_pair(left, right) or _drop_cap_pair(right, left):
        return True
    return abs(_center_y(left.bbox) - _center_y(right.bbox)) <= max(
        2.0,
        min(left.font_size, right.font_size) * 0.35,
    )


def _same_row(row: list[TextObjectFact], candidate: TextObjectFact) -> bool:
    previous = row[-1]
    center_delta = abs(_center_y(previous.bbox) - _center_y(candidate.bbox))
    horizontal_gap = candidate.bbox[0] - previous.bbox[2]
    if _drop_cap_pair(previous, candidate):
        return True
    return (
        (
            _script(previous.text) == _script(candidate.text)
            or _neutral(previous.text)
            or _neutral(candidate.text)
            or _inline_identifier_cjk_pair(previous.text, candidate.text)
        )
        and
        center_delta <= max(2.0, min(previous.font_size, candidate.font_size) * 0.35)
        and horizontal_gap >= -max(previous.font_size, candidate.font_size)
        and horizontal_gap <= max(previous.font_size, candidate.font_size) * 1.25
    )


def _container_groups(lines: list[_Line]) -> list[list[_Line]]:
    groups: list[list[_Line]] = []
    for line in lines:
        target = next((group for group in reversed(groups) if _can_join(group, line)), None)
        if target is None:
            groups.append([line])
        else:
            target.append(line)
    return groups


def _can_join(group: list[_Line], candidate: _Line) -> bool:
    previous = group[-1]
    gap = candidate.bbox[1] - previous.bbox[3]
    font_ratio = abs(candidate.font_size - previous.font_size) / max(candidate.font_size, previous.font_size, 0.1)
    anchor_delta = min(
        abs(candidate.bbox[0] - previous.bbox[0]),
        abs(candidate.bbox[2] - previous.bbox[2]),
        abs((candidate.bbox[0] + candidate.bbox[2]) / 2.0 - (previous.bbox[0] + previous.bbox[2]) / 2.0),
    )
    return (
        previous.color_srgb == candidate.color_srgb
        and _script(previous.text) == _script(candidate.text)
        and _font_style(previous.objects[0].font_name) == _font_style(candidate.objects[0].font_name)
        and font_ratio <= 0.12
        and anchor_delta <= max(3.0, min(previous.font_size, candidate.font_size) * 0.5)
        and -min(previous.font_size, candidate.font_size) * 1.7 <= gap <= max(3.0, min(previous.font_size, candidate.font_size) * 0.9)
    )


def _slot_geometry(
    group: list[_Line],
    source: Rect,
    groups: list[list[_Line]],
    visuals: tuple[_Visual, ...],
    background: tuple[_Visual, ...],
    panel: _Visual | None,
    alignment: str,
    page_width: float,
    page_height: float,
) -> tuple[Rect, Rect, tuple[str, ...]]:
    if panel is not None:
        inset = min(4.0, max(1.5, min(panel.bbox[2] - panel.bbox[0], panel.bbox[3] - panel.bbox[1]) * 0.04))
        boundary = _round_rect(panel.bbox)
        lane_left = min(source[0], boundary[0] + inset)
        lane_right = max(source[2], boundary[2] - inset)
        for other in groups:
            if other is group:
                continue
            other_bbox = _union([line.bbox for line in other])
            overlap = _axis_overlap((source[1], source[3]), (other_bbox[1], other_bbox[3]))
            if overlap < min(source[3] - source[1], other_bbox[3] - other_bbox[1]) * 0.5:
                continue
            if other_bbox[0] >= source[2]:
                lane_right = min(lane_right, (source[2] + other_bbox[0]) / 2.0)
            elif other_bbox[2] <= source[0]:
                lane_left = max(lane_left, (other_bbox[2] + source[0]) / 2.0)
        if alignment == "LEFT":
            lane_left = source[0]
        elif alignment == "RIGHT":
            lane_right = source[2]
        allowed = _round_rect((lane_left, source[1], lane_right, source[3]))
        return boundary, allowed, (panel.object_id,)

    if len(group) == 1:
        guides = [item for item in visuals if _near_horizontal_guide(item.bbox, source, group[0].font_size)]
        if guides:
            guide = min(guides, key=lambda item: _rect_gap(item.bbox, source))
            if guide.bbox[1] >= source[3]:
                boundary = (source[0], source[1], max(source[2], guide.bbox[2]), guide.bbox[1] - 0.5)
            else:
                boundary = (source[0], source[1], max(source[2], guide.bbox[2]), source[3] + 1.0)
            boundary = _round_rect(boundary)
            return boundary, boundary, (guide.object_id,)

    allowed = _page_horizontal_lane(group, source, groups, visuals, alignment, page_width, page_height)
    return allowed, allowed, tuple(item.object_id for item in background)


def _containing_panel(source: Rect, background: tuple[_Visual, ...], page_area: float) -> _Visual | None:
    source_area = _area(source)
    candidates = [
        item
        for item in background
        if _contains(item.bbox, source, tolerance=1.5)
        and _area(item.bbox) >= source_area * 1.05
        and _area(item.bbox) <= source_area * 40.0
        and _area(item.bbox) <= page_area * 0.65
    ]
    return min(candidates, key=lambda item: _area(item.bbox)) if candidates else None


def _page_horizontal_lane(
    group: list[_Line],
    source: Rect,
    groups: list[list[_Line]],
    visuals: tuple[_Visual, ...],
    alignment: str,
    page_width: float,
    page_height: float,
) -> Rect:
    group_rects = [_union([line.bbox for line in item]) for item in groups]
    content_left = max(0.0, min(rect[0] for rect in group_rects))
    content_right = min(page_width, max(rect[2] for rect in group_rects))
    margin = max(12.0, min(content_left, page_width - content_right))
    lane_left = margin
    lane_right = page_width - margin

    for other, other_bbox in zip(groups, group_rects):
        if other is group:
            continue
        overlap = _axis_overlap((source[1], source[3]), (other_bbox[1], other_bbox[3]))
        if overlap < min(source[3] - source[1], other_bbox[3] - other_bbox[1]) * 0.5:
            continue
        if other_bbox[0] >= source[2]:
            lane_right = min(lane_right, (source[2] + other_bbox[0]) / 2.0)
        elif other_bbox[2] <= source[0]:
            lane_left = max(lane_left, (other_bbox[2] + source[0]) / 2.0)

    for visual in visuals:
        if visual.kind != "image" or _contains(visual.bbox, source, tolerance=1.5):
            continue
        overlap = _axis_overlap((source[1], source[3]), (visual.bbox[1], visual.bbox[3]))
        if overlap < (source[3] - source[1]) * 0.5:
            continue
        if visual.bbox[0] >= source[2]:
            lane_right = min(lane_right, visual.bbox[0] - 4.0)
        elif visual.bbox[2] <= source[0]:
            lane_left = max(lane_left, visual.bbox[2] + 4.0)

    if alignment == "LEFT":
        lane_left = source[0]
    elif alignment == "RIGHT":
        lane_right = source[2]
    lane_left = min(lane_left, source[0])
    lane_right = max(lane_right, source[2])
    desired_bottom = source[1] + max(source[3] - source[1], statistics.median(line.font_size for line in group) * 2.4)
    bottom_limit = page_height - max(12.0, page_height * 0.015)
    lane_width = lane_right - lane_left
    for other, other_bbox in zip(groups, group_rects):
        if other is group or other_bbox[1] < source[3] - 0.5:
            continue
        horizontal_overlap = _axis_overlap((lane_left, lane_right), (other_bbox[0], other_bbox[2]))
        if horizontal_overlap >= min(lane_width, other_bbox[2] - other_bbox[0]) * 0.15:
            bottom_limit = min(bottom_limit, other_bbox[1] - 2.0)
    for visual in visuals:
        if visual.kind != "image" or visual.bbox[1] < source[3] or _contains(visual.bbox, source, tolerance=1.5):
            continue
        horizontal_overlap = _axis_overlap((lane_left, lane_right), (visual.bbox[0], visual.bbox[2]))
        if horizontal_overlap >= min(lane_width, visual.bbox[2] - visual.bbox[0]) * 0.15:
            bottom_limit = min(bottom_limit, visual.bbox[1] - 4.0)
    lane_bottom = max(source[3], min(desired_bottom, bottom_limit))
    return _round_rect((lane_left, source[1], lane_right, lane_bottom))


def _near_horizontal_guide(rect: Rect, source: Rect, font_size: float) -> bool:
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    horizontal_coverage = _axis_overlap((rect[0], rect[2]), (source[0], source[2])) / max(0.1, source[2] - source[0])
    return width >= height * 6.0 and horizontal_coverage >= 0.7 and _rect_gap(rect, source) <= max(4.0, font_size)


def _protected(line: _Line) -> bool:
    text = line.text.strip()
    if not re.search(r"[A-Za-z\u3400-\u9fff]", text):
        return True
    return bool(re.fullmatch(r"(?:https?://|www\.)\S+", text, flags=re.IGNORECASE))


def _margin_page_number(item: TextObjectFact, page_height: float) -> bool:
    text = item.text.strip()
    return bool(
        re.fullmatch(r"\d{1,3}", text)
        and (item.bbox[1] <= page_height * 0.10 or item.bbox[3] >= page_height * 0.90)
    )


def _required_literals(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            re.findall(
                r"(?:https?://\S+|www\.\S+|\b[A-Z]{2,}\d+[A-Z0-9-]*\b|"
                r"(?<=[\u3400-\u9fff])[A-Z][A-Z0-9+./-]{0,15}|"
                r"[A-Z][A-Z0-9+./-]{0,15}(?=[\u3400-\u9fff])|"
                r"\d+(?:[.,:/-]\d+)*%?)",
                text,
            )
        )
    )


def _joined_text(lines: list[_Line]) -> str:
    result = ""
    for line in lines:
        text = line.text.strip()
        if not result:
            result = text
        elif _contains_cjk(result) and _contains_cjk(text):
            result += text
        else:
            result += " " + text
    return result


def _join_fragments(objects: tuple[TextObjectFact, ...]) -> str:
    result = ""
    for index, item in enumerate(objects):
        text = item.text.strip()
        if not result:
            result += text
        elif _fragment_separator(objects[index - 1], item, result, text):
            result += " " + text
        else:
            result += text
    return result


def _fragment_separator(
    previous: TextObjectFact,
    current: TextObjectFact,
    result: str,
    current_text: str,
) -> bool:
    if _drop_cap_pair(previous, current) or (_contains_cjk(result) and _contains_cjk(current_text)):
        return False
    previous_text = previous.text.strip()
    if len(previous_text) == 1 and len(current_text) == 1:
        gap = current.bbox[0] - previous.bbox[2]
        return gap > max(0.5, min(previous.font_size, current.font_size) * 0.20)
    return True


def _drop_cap_pair(left: TextObjectFact, right: TextObjectFact) -> bool:
    left_text = left.text.strip()
    right_text = right.text.strip()
    overlap = _axis_overlap((left.bbox[1], left.bbox[3]), (right.bbox[1], right.bbox[3]))
    right_height = max(0.1, right.bbox[3] - right.bbox[1])
    horizontal_gap = right.bbox[0] - left.bbox[2]
    return (
        len(left_text) == 1
        and left_text.isalpha()
        and bool(re.match(r"[a-z]", right_text))
        and left.font_size >= right.font_size * 1.5
        and right.bbox[1] - left.bbox[1] <= right.font_size * 0.6
        and overlap >= right_height * 0.5
        and horizontal_gap <= right.font_size * 0.75
    )


def _neutral(text: str) -> bool:
    return not re.search(r"[A-Za-z\u3400-\u9fff]", text)


def _inline_identifier_cjk_pair(left: str, right: str) -> bool:
    return (
        _contains_cjk(left) and _is_inline_identifier(right)
        or _contains_cjk(right) and _is_inline_identifier(left)
    )


def _is_inline_identifier(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9+./-]{0,15}", text.strip()))


def _latin_is_inline_identifiers(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+./-]*", text)
    return bool(tokens) and all(_is_inline_identifier(token) for token in tokens)


def _role(font_size: float, median_font: float) -> str:
    if font_size >= median_font * 1.7:
        return "title"
    if font_size >= median_font * 1.15:
        return "heading"
    return "body"


def _alignment(
    group: list[_Line],
    panel: _Visual | None,
    groups: list[list[_Line]],
    page_width: float,
) -> str:
    lefts = [line.bbox[0] for line in group]
    rights = [line.bbox[2] for line in group]
    centers = [(line.bbox[0] + line.bbox[2]) / 2.0 for line in group]
    if len(group) >= 2 and max(centers) - min(centers) < max(2.0, group[0].font_size * 0.25) and max(lefts) - min(lefts) > 4.0:
        return "CENTER"
    if len(group) >= 2 and max(rights) - min(rights) < max(2.0, group[0].font_size * 0.25) and max(lefts) - min(lefts) > 4.0:
        return "RIGHT"
    if len(group) >= 2 and max(lefts) - min(lefts) < max(2.0, group[0].font_size * 0.25):
        return "LEFT"
    if panel is not None:
        source = _union([line.bbox for line in group])
        panel_center = (panel.bbox[0] + panel.bbox[2]) / 2.0
        source_center = (source[0] + source[2]) / 2.0
        if abs(source_center - panel_center) <= max(group[0].font_size, (panel.bbox[2] - panel.bbox[0]) * 0.08):
            return "CENTER"
    source = _union([line.bbox for line in group])
    other_rects = [_union([line.bbox for line in item]) for item in groups if item is not group]
    same_row = [
        rect
        for rect in other_rects
        if _axis_overlap((source[1], source[3]), (rect[1], rect[3]))
        >= min(source[3] - source[1], rect[3] - rect[1]) * 0.5
    ]
    has_right_neighbor = any(rect[0] >= source[2] for rect in same_row)
    has_left_neighbor = any(rect[2] <= source[0] for rect in same_row)
    if source[0] >= page_width * 0.5 and has_right_neighbor and not has_left_neighbor:
        return "RIGHT"
    content_right = max([source[2], *(rect[2] for rect in other_rects)])
    if source[0] >= page_width * 0.65 and content_right - source[2] <= max(4.0, group[0].font_size * 1.5):
        return "RIGHT"
    return "LEFT"


def _dominant_color(group: list[_Line]) -> int:
    return statistics.mode(line.color_srgb for line in group)


def _sample_background(source_pdf: Path | None, page_index: int, bbox: Rect, foreground: int) -> tuple[int, int, int]:
    if source_pdf is None:
        return (255, 255, 255)
    with fitz.open(source_pdf) as document:
        pixmap = document[page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=fitz.Rect(bbox), alpha=False)
    channels = pixmap.n
    samples = pixmap.samples
    foreground_rgb = _rgb(foreground)
    pixels = []
    step = max(1, (pixmap.width * pixmap.height) // 6000)
    for index in range(0, pixmap.width * pixmap.height, step):
        offset = index * channels
        rgb = tuple(samples[offset + channel] for channel in range(3))
        if sum((rgb[channel] - foreground_rgb[channel]) ** 2 for channel in range(3)) ** 0.5 >= 28.0:
            pixels.append(rgb)
    if not pixels:
        return (255, 255, 255)
    return tuple(int(statistics.median(pixel[channel] for pixel in pixels)) for channel in range(3))


def _contrast_ratio(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    a, b = _luminance(left), _luminance(right)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for item in rgb:
        value = item / 255.0
        values.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def _rgb(value: int) -> tuple[int, int, int]:
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _script(text: str) -> str:
    has_cjk = _contains_cjk(text)
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_cjk and has_latin and _latin_is_inline_identifiers(text):
        return "CJK"
    if has_cjk == has_latin:
        return "MIXED"
    return "CJK" if has_cjk else "LATIN"


def _font_style(font_name: str) -> tuple[bool, bool]:
    lowered = font_name.casefold()
    bold = any(token in lowered for token in ("bold", "black", "heavy", "semibold", "xbold"))
    italic = any(token in lowered for token in ("italic", "oblique")) or lowered.endswith(("-it", "it"))
    return bold, italic


def _union(rects: list[Rect]) -> Rect:
    return _round_rect((min(item[0] for item in rects), min(item[1] for item in rects), max(item[2] for item in rects), max(item[3] for item in rects)))


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.0) -> bool:
    return inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance


def _center_inside(inner: Rect, outer: Rect) -> bool:
    center = ((inner[0] + inner[2]) / 2.0, (inner[1] + inner[3]) / 2.0)
    return outer[0] <= center[0] <= outer[2] and outer[1] <= center[1] <= outer[3]


def _coverage(cover: Rect, target: Rect) -> float:
    return _intersection_area(cover, target) / max(0.001, _area(target))


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _axis_overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rect_gap(left: Rect, right: Rect) -> float:
    dx = max(left[0] - right[2], right[0] - left[2], 0.0)
    dy = max(left[1] - right[3], right[1] - left[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _center_y(rect: Rect) -> float:
    return (rect[1] + rect[3]) / 2.0


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(item), 4) for item in rect)
