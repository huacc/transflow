from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import replace
from statistics import median

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact

from . import TOOLBOX_KEY
from .models import SingleColumnTemplate, TextContainer


LIST_PREFIX = re.compile(r"^\s*(?:[\uf0b7\u2022\u25cf\u25aa\-]|\(?[0-9]+[.)]|\(?[A-Za-z][.)])\s+")


def build_page_template(facts: PageFacts) -> SingleColumnTemplate:
    grouped: dict[int, list[TextObjectFact]] = defaultdict(list)
    for text_object in facts.text_objects:
        grouped[text_object.block_index].append(text_object)
    if not grouped:
        raise ValueError("single_column_page_has_no_native_text")

    page_font_median = median(item.font_size for item in facts.text_objects)
    rows: list[tuple[tuple[float, float], int, int, list[TextObjectFact]]] = []
    for block_index, objects in grouped.items():
        for group_index, group in enumerate(_split_block(objects)):
            x0 = min(item.bbox[0] for item in group)
            y0 = min(item.bbox[1] for item in group)
            rows.append(((round(y0, 4), round(x0, 4)), block_index, group_index, group))
    rows.sort(key=lambda item: item[0])

    containers: list[TextContainer] = []
    for reading_order, (_, block_index, group_index, objects) in enumerate(rows):
        ordered_all = sorted(objects, key=lambda item: (item.line_index, item.span_index))
        marker_objects = [item for item in ordered_all if item.text.strip() in {"\uf0b7", "•", "●", "▪"}]
        ordered = [item for item in ordered_all if item not in marker_objects]
        if not ordered:
            continue
        bbox = (
            min(item.bbox[0] for item in ordered),
            min(item.bbox[1] for item in ordered),
            max(item.bbox[2] for item in ordered),
            max(item.bbox[3] for item in ordered),
        )
        source_text = _merge_text(ordered)
        max_font_size = max(item.font_size for item in ordered)
        representative = max(
            ordered,
            key=lambda item: (item.font_size, len(item.text), item.line_index, item.span_index),
        )
        containers.append(
            TextContainer(
                container_id=f"block-{block_index:04d}-{group_index:03d}",
                source_object_ids=tuple(item.object_id for item in ordered),
                source_text=source_text,
                reading_order=reading_order,
                role="list" if marker_objects else _role(source_text, bbox, facts.height, max_font_size, page_font_median, ordered),
                source_bbox=tuple(round(value, 4) for value in bbox),
                anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                font_size=round(max_font_size, 4),
                color_srgb=representative.color_srgb,
                font_weight=_font_weight(ordered),
                preserved_prefix="•" if marker_objects else None,
            )
        )
    return SingleColumnTemplate(facts.page_id, TOOLBOX_KEY, facts.width, facts.height, tuple(containers))


def build_p4_page_template(facts: PageFacts) -> SingleColumnTemplate:
    base = build_page_template(facts)
    marker_assignments = _marker_assignments(base, facts)
    body_font_evidence = [round(item.font_size, 1) for item in base.containers if item.role != "margin" and len(item.source_text) > 24]
    body_font_baseline = Counter(body_font_evidence).most_common(1)[0][0] if body_font_evidence else median(item.font_size for item in base.containers)
    normalized: list[TextContainer] = []
    for container in base.containers:
        if _is_vertical_decoration(container, facts.height) or _is_locked_margin(container, facts.height):
            continue
        role = container.role
        height = container.source_bbox[3] - container.source_bbox[1]
        false_body_heading = len(container.source_text) > 24 and container.font_size <= body_font_baseline * 1.08
        if role == "heading" and (false_body_heading or len(container.source_text) > 180 or height > container.font_size * 2.5):
            role = "body"
        marker = marker_assignments.get(container.container_id)
        source_bbox = container.source_bbox
        source_object_ids = container.source_object_ids
        source_text = container.source_text
        preserved_prefix = container.preserved_prefix
        if marker is not None:
            source_object_ids = (marker.object_id,) + source_object_ids
            preserved_prefix = "•"
        else:
            detached = _detach_numbered_prefix(container, facts)
            if detached is not None:
                preserved_prefix, source_text, source_object_ids, source_bbox = detached
        normalized.append(
            replace(
                container,
                source_object_ids=source_object_ids,
                source_text=_canonicalize_bullets(source_text),
                role=role,
                source_bbox=source_bbox,
                anchor=(source_bbox[0], source_bbox[1]),
                preserved_prefix=preserved_prefix,
            )
        )

    return replace(base, containers=merge_flow_containers(tuple(normalized)))


def merge_flow_containers(containers: tuple[TextContainer, ...]) -> tuple[TextContainer, ...]:
    merged: list[TextContainer] = []
    for container in containers:
        if merged and _can_merge_flow_lines(merged[-1], container):
            previous = merged[-1]
            source_text = previous.source_text.rstrip()
            continuation = container.source_text.lstrip()
            if source_text.endswith("-") and continuation[:1].islower():
                source_text = source_text[:-1] + continuation
            else:
                separator = "" if _is_han(source_text[-1:]) and _is_han(continuation[:1]) else " "
                source_text += separator + continuation
            merged[-1] = replace(
                previous,
                source_object_ids=previous.source_object_ids + container.source_object_ids,
                source_text=source_text,
                source_bbox=(
                    min(previous.source_bbox[0], container.source_bbox[0]),
                    min(previous.source_bbox[1], container.source_bbox[1]),
                    max(previous.source_bbox[2], container.source_bbox[2]),
                    max(previous.source_bbox[3], container.source_bbox[3]),
                ),
            )
        else:
            merged.append(container)
    return tuple(replace(container, reading_order=index) for index, container in enumerate(merged))


def _is_vertical_decoration(container: TextContainer, page_height: float) -> bool:
    width = container.source_bbox[2] - container.source_bbox[0]
    height = container.source_bbox[3] - container.source_bbox[1]
    return height >= page_height * 0.10 and height > max(40.0, width * 3.0)


def _marker_assignments(template: SingleColumnTemplate, facts: PageFacts) -> dict[str, TextObjectFact]:
    assignments: dict[str, TextObjectFact] = {}
    for block_index in {item.block_index for item in facts.text_objects}:
        containers = sorted(
            [item for item in template.containers if item.preserved_prefix and item.container_id.startswith(f"block-{block_index:04d}-")],
            key=lambda item: item.source_bbox[1],
        )
        markers = sorted(
            [item for item in facts.text_objects if item.block_index == block_index and item.text.strip() in {"\uf0b7", "•", "●", "▪"}],
            key=lambda item: item.bbox[1],
        )
        for container, marker in zip(containers, markers):
            assignments[container.container_id] = marker
    return assignments


def _detach_numbered_prefix(
    container: TextContainer,
    facts: PageFacts,
) -> tuple[str, str, tuple[str, ...], tuple[float, float, float, float]] | None:
    if container.role != "list":
        return None
    match = re.match(r"^\s*(\(?[0-9A-Za-z]+[.)])\s+(.+)$", container.source_text, flags=re.DOTALL)
    if not match:
        return None
    prefix, body_text = match.group(1), match.group(2).strip()
    by_id = {item.object_id: item for item in facts.text_objects}
    marker = next(
        (by_id[object_id] for object_id in container.source_object_ids if by_id[object_id].text.strip() == prefix),
        None,
    )
    if marker is None:
        return None
    body_ids = tuple(object_id for object_id in container.source_object_ids if object_id != marker.object_id)
    if not body_ids:
        return None
    body_objects = [by_id[object_id] for object_id in body_ids]
    body_bbox = (
        min(item.bbox[0] for item in body_objects),
        min(item.bbox[1] for item in body_objects),
        max(item.bbox[2] for item in body_objects),
        max(item.bbox[3] for item in body_objects),
    )
    return prefix, body_text, (marker.object_id,) + body_ids, body_bbox


def _is_locked_margin(container: TextContainer, page_height: float) -> bool:
    if container.role != "margin":
        return False
    return container.source_bbox[1] >= page_height * 0.90 or bool(re.fullmatch(r"\s*\d+(?:\s*/\s*\d+)?\s*", container.source_text))


def _canonicalize_bullets(text: str) -> str:
    text = text.replace("\uf0b7", "•")
    return re.sub(r"\s*•\s*", "\n• ", text).strip()


def _can_merge_flow_lines(previous: TextContainer, current: TextContainer) -> bool:
    previous_is_flow = previous.role in {"body", "list"} or (
        previous.role == "heading"
        and previous.font_weight == "regular"
        and current.font_weight == "regular"
    )
    if not previous_is_flow or current.role not in {"body", "heading"}:
        return False
    if previous.role == "list" and len(previous.source_text) < 40:
        return False
    if previous.color_srgb != current.color_srgb or abs(previous.font_size - current.font_size) > 0.35:
        return False
    indent = current.source_bbox[0] - previous.source_bbox[0]
    if indent < -max(6.0, previous.font_size * 0.75) or indent > max(30.0, previous.font_size * 4.0):
        return False
    gap = current.source_bbox[1] - previous.source_bbox[3]
    if not (-0.5 <= gap <= max(3.0, min(previous.font_size, current.font_size) * 0.75)):
        return False
    return not bool(re.search(r"[。！？.!?:：；;]\s*$", previous.source_text))


def _is_han(value: str) -> bool:
    return bool(value and "\u3400" <= value <= "\u9fff")


def _font_weight(objects: list[TextObjectFact]) -> str:
    total = sum(max(1, len(item.text.strip())) for item in objects)
    bold = sum(max(1, len(item.text.strip())) for item in objects if "bold" in item.font_name.casefold())
    return "bold" if bold * 2 >= total else "regular"


def _split_block(objects: list[TextObjectFact]) -> list[list[TextObjectFact]]:
    by_line: dict[int, list[TextObjectFact]] = defaultdict(list)
    for item in objects:
        by_line[item.line_index].append(item)
    lines: list[tuple[int, list[TextObjectFact], str, float, float, float, float, float, str]] = []
    for line_index, spans in sorted(by_line.items()):
        ordered = sorted(spans, key=lambda item: item.span_index)
        text = "".join(item.text for item in ordered).strip()
        y0 = min(item.bbox[1] for item in ordered)
        y1 = max(item.bbox[3] for item in ordered)
        x0 = min(item.bbox[0] for item in ordered)
        x1 = max(item.bbox[2] for item in ordered)
        style = "italic" if any("italic" in item.font_name.lower() for item in ordered) else "normal"
        lines.append((line_index, ordered, text, y0, y1, y1 - y0, x0, x1, style))

    groups: list[list[TextObjectFact]] = []
    current: list[TextObjectFact] = []
    previous: tuple[int, list[TextObjectFact], str, float, float, float, float, float, str] | None = None
    current_x0 = 0.0
    current_text = ""
    for line in lines:
        line_index, spans, text, y0, y1, height, x0, x1, style = line
        boundary = False
        if previous is not None:
            previous_index, _, previous_text, previous_y0, previous_y1, previous_height, previous_x0, previous_x1, previous_style = previous
            gap = y0 - previous_y1
            paragraph_gap = gap > max(3.0, min(height, previous_height) * 0.45)
            skipped_pdf_line = line_index - previous_index > 1 and gap > 1.0
            vertical_overlap = min(y1, previous_y1) - max(y0, previous_y0)
            horizontal_gap = max(x0 - previous_x1, previous_x0 - x1)
            spatially_separate = (
                vertical_overlap > max(0.5, min(height, previous_height) * 0.30)
                and horizontal_gap > max(4.0, min(height, previous_height) * 0.80)
            )
            new_bullet = (text.strip() in {"\uf0b7", "•", "●", "▪"} or bool(LIST_PREFIX.match(text))) and bool(current)
            current_starts_with_bullet = current_text.lstrip().startswith(("\uf0b7", "•", "●", "▪"))
            indented_body = y0 > previous_y0 + 1.0 and not current_starts_with_bullet and len(current_text) <= 100 and x0 - current_x0 > 15.0
            style_change = previous_style != style and (len(previous_text) <= 100 or len(text) <= 100)
            boundary = paragraph_gap or skipped_pdf_line or spatially_separate or new_bullet or indented_body or style_change
        if boundary:
            groups.append(current)
            current = []
            current_text = ""
        current.extend(spans)
        current_text = (current_text + " " + text).strip()
        if len(current) == len(spans):
            current_x0 = x0
        previous = line
    if current:
        groups.append(current)
    return groups


def _merge_text(objects: list[TextObjectFact]) -> str:
    lines: dict[int, list[TextObjectFact]] = defaultdict(list)
    for item in objects:
        lines[item.line_index].append(item)
    merged_lines: list[tuple[str, tuple[float, float, float, float]]] = []
    for _, items in sorted(lines.items()):
        ordered = sorted(items, key=lambda row: row.span_index)
        merged_lines.append(
            (
                "".join(item.text for item in ordered).strip(),
                (
                    min(item.bbox[0] for item in ordered),
                    min(item.bbox[1] for item in ordered),
                    max(item.bbox[2] for item in ordered),
                    max(item.bbox[3] for item in ordered),
                ),
            )
        )
    output: list[str] = []
    previous_line: tuple[str, tuple[float, float, float, float]] | None = None
    for line, bbox in merged_lines:
        if not line:
            continue
        if previous_line is not None and _overlaid_duplicate_line(previous_line, (line, bbox)):
            previous_line = (line, bbox)
            continue
        if output and LIST_PREFIX.match(line):
            output.append("\n" + line)
        elif output and output[-1].endswith("-") and line[:1].islower():
            output[-1] = output[-1][:-1] + line
        elif output:
            output.append(" " + line)
        else:
            output.append(line)
        previous_line = (line, bbox)
    return "".join(output).strip()


def _overlaid_duplicate_line(
    left: tuple[str, tuple[float, float, float, float]],
    right: tuple[str, tuple[float, float, float, float]],
) -> bool:
    left_text, left_bbox = left
    right_text, right_bbox = right
    if re.sub(r"\s+", " ", left_text).strip() != re.sub(r"\s+", " ", right_text).strip():
        return False
    overlap_width = max(0.0, min(left_bbox[2], right_bbox[2]) - max(left_bbox[0], right_bbox[0]))
    overlap_height = max(0.0, min(left_bbox[3], right_bbox[3]) - max(left_bbox[1], right_bbox[1]))
    overlap_area = overlap_width * overlap_height
    left_area = max(0.0, left_bbox[2] - left_bbox[0]) * max(0.0, left_bbox[3] - left_bbox[1])
    right_area = max(0.0, right_bbox[2] - right_bbox[0]) * max(0.0, right_bbox[3] - right_bbox[1])
    return overlap_area >= min(left_area, right_area) * 0.90


def _role(
    text: str,
    bbox: tuple[float, float, float, float],
    page_height: float,
    max_font_size: float,
    page_font_median: float,
    objects: list[TextObjectFact],
) -> str:
    if bbox[1] <= page_height * 0.06 or bbox[3] >= page_height * 0.94:
        return "margin"
    if LIST_PREFIX.match(text) or "\n" in text:
        return "list"
    font_names = " ".join(item.font_name.lower() for item in objects)
    # 只有拉丁字母才存在此处需要识别的“全大写标题”语义；汉字的 upper() 不变，不能因此把正文误判为标题。
    latin_letters = [character for character in text if "A" <= character.upper() <= "Z"]
    uppercase = len(text) <= 180 and bool(latin_letters) and all(character == character.upper() for character in latin_letters)
    if max_font_size >= page_font_median * 1.25 or "bold" in font_names or ("italic" in font_names and len(text) <= 100) or uppercase:
        return "heading"
    return "body"
