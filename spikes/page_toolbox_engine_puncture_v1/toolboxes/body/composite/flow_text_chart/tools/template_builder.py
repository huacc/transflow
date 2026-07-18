from __future__ import annotations

import re
from dataclasses import replace
from statistics import median

from page_toolbox_puncture.contracts import PageFacts
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.chart.tools.models import ChartTemplate, ChartTextContainer, Rect
from toolboxes.body.chart.tools.template_builder import build_chart_template
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate, TextContainer
from toolboxes.body.flow_text.single.tools.template_builder import build_p4_page_template, merge_flow_containers

from .. import TOOLBOX_KEY
from .models import (
    ContainerOwnership,
    FlowRegionTemplate,
    FlowTextChartTemplate,
    ObjectOwnership,
)


class FlowTextChartCapabilityError(RuntimeError):
    pass


_SOURCE_RE = re.compile(
    r"^(?:source|sources|资料来源|資料來源|来源|來源)\s*[:：]",
    flags=re.IGNORECASE,
)
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:(?:page|p\.?)[ ]*)?\d+(?:\s*(?:of|/)\s*\d+)?\s*$",
    flags=re.IGNORECASE,
)


def build_flow_text_chart_template(facts: PageFacts) -> FlowTextChartTemplate:
    full_flow = build_p4_page_template(facts)
    full_chart = build_chart_template(facts)
    selected_flow = _select_flow_containers(full_flow)
    if not selected_flow:
        raise FlowTextChartCapabilityError("P17_FLOW_REGION_NOT_FOUND")

    flow_ids = {
        object_id
        for container in selected_flow
        for object_id in container.source_object_ids
    }
    flow_ids = _close_mixed_chart_groups(flow_ids, full_chart, full_flow)
    selected_flow = tuple(
        container
        for container in full_flow.containers
        if set(container.source_object_ids).issubset(flow_ids)
    )
    selected_flow = merge_flow_containers(_split_leading_flow_headings(facts, selected_flow))
    if not selected_flow:
        raise FlowTextChartCapabilityError("P17_FLOW_REGION_NOT_FOUND_AFTER_OWNERSHIP_CLOSE")

    chart_containers: list[ChartTextContainer] = []
    chart_owners: dict[str, str] = {}
    for container in full_chart.containers:
        source_ids = set(container.source_object_ids)
        overlap = source_ids & flow_ids
        if overlap:
            if overlap != source_ids:
                raise FlowTextChartCapabilityError(
                    f"P17_MIXED_FLOW_CHART_CONTAINER:{container.container_id}"
                )
            continue
        owner = _chart_container_owner(container, facts)
        chart_containers.append(container)
        chart_owners[container.container_id] = owner
    if not any(owner == "chart" for owner in chart_owners.values()):
        raise FlowTextChartCapabilityError("P17_CHART_TEXT_OWNER_NOT_FOUND")

    chart_guard_regions = _chart_guard_regions(
        facts,
        full_chart,
        tuple(
            container
            for container in chart_containers
            if chart_owners[container.container_id] == "chart"
        ),
    )
    flow_regions = _build_flow_regions(
        facts,
        selected_flow,
        chart_guard_regions,
    )
    flow_container_by_id = {
        container.container_id: (region.region_id, container)
        for region in flow_regions
        for container in region.template.containers
    }
    flow_ids = {
        object_id
        for _, container in flow_container_by_id.values()
        for object_id in container.source_object_ids
    }

    chart_containers = [
        container
        for container in chart_containers
        if not set(container.source_object_ids) & flow_ids
    ]
    chart_ids = {
        object_id
        for container in chart_containers
        for object_id in container.source_object_ids
    }
    if flow_ids & chart_ids:
        raise FlowTextChartCapabilityError("P17_FLOW_CHART_OBJECT_OWNERSHIP_OVERLAP")

    source_by_id = {item.object_id: item for item in facts.text_objects}
    owner_by_object: dict[str, tuple[str, str | None]] = {}
    for container_id, (region_id, container) in flow_container_by_id.items():
        del region_id
        for object_id in container.source_object_ids:
            _assign(owner_by_object, object_id, "flow", container_id)
    for container in chart_containers:
        owner = chart_owners[container.container_id]
        for object_id in container.source_object_ids:
            _assign(owner_by_object, object_id, owner, container.container_id)
    for object_id in source_by_id:
        owner_by_object.setdefault(object_id, ("protected", None))
    if set(owner_by_object) != set(source_by_id):
        raise FlowTextChartCapabilityError("P17_OBJECT_OWNERSHIP_NOT_EXHAUSTIVE")

    protected_ids = tuple(
        object_id
        for object_id, (owner, _) in owner_by_object.items()
        if owner == "protected"
    )
    normalized_chart_containers = tuple(
        replace(container, reading_order=index)
        for index, container in enumerate(
            sorted(chart_containers, key=lambda item: _reading_key(item.source_bbox))
        )
    )
    chart_structure = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "kind": "chart-subplan",
            "visual_regions": full_chart.visual_regions,
            "containers": normalized_chart_containers,
            "protected_object_ids": tuple(
                object_id
                for object_id in source_by_id
                if object_id not in chart_ids
            ),
            "locked_objects_sha256": full_chart.locked_objects_sha256,
        }
    )
    chart_template = ChartTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        full_chart.visual_regions,
        normalized_chart_containers,
        tuple(object_id for object_id in source_by_id if object_id not in chart_ids),
        full_chart.locked_objects_sha256,
        chart_structure,
    )

    render_containers: list[ChartTextContainer] = list(normalized_chart_containers)
    for region in flow_regions:
        for container in region.template.containers:
            render_containers.append(
                _flow_render_container(
                    facts,
                    container,
                    region,
                )
            )
    render_containers.sort(key=lambda item: _reading_key(item.source_bbox))
    render_containers = [
        replace(container, reading_order=index)
        for index, container in enumerate(render_containers)
    ]
    render_ids = {
        object_id
        for container in render_containers
        for object_id in container.source_object_ids
    }
    if len(render_ids) != sum(len(item.source_object_ids) for item in render_containers):
        raise FlowTextChartCapabilityError("P17_RENDER_CONTAINER_OWNERSHIP_NOT_UNIQUE")
    render_structure = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "kind": "single-source-render",
            "visual_regions": full_chart.visual_regions,
            "containers": render_containers,
            "protected_object_ids": protected_ids,
            "chart_guard_regions": chart_guard_regions,
            "locked_objects_sha256": full_chart.locked_objects_sha256,
        }
    )
    render_template = ChartTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        full_chart.visual_regions,
        tuple(render_containers),
        protected_ids,
        full_chart.locked_objects_sha256,
        render_structure,
    )
    ownerships = tuple(
        ObjectOwnership(object_id, owner_by_object[object_id][0], owner_by_object[object_id][1])
        for object_id in source_by_id
    )
    container_ownerships = tuple(
        [
            ContainerOwnership(container_id, "flow", region_id)
            for container_id, (region_id, _) in flow_container_by_id.items()
        ]
        + [
            ContainerOwnership(
                container.container_id,
                chart_owners[container.container_id],
                container.association_id,
            )
            for container in normalized_chart_containers
        ]
    )
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "flow_regions": flow_regions,
            "chart_structure_sha256": chart_structure,
            "render_structure_sha256": render_structure,
            "ownerships": ownerships,
        }
    )
    return FlowTextChartTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        flow_regions,
        chart_template,
        render_template,
        chart_guard_regions,
        ownerships,
        container_ownerships,
        structure_sha256,
    )


def _split_leading_flow_headings(
    facts: PageFacts,
    containers: tuple[TextContainer, ...],
) -> tuple[TextContainer, ...]:
    source_by_id = {item.object_id: item for item in facts.text_objects}
    refined: list[TextContainer] = []
    for container in containers:
        objects = [source_by_id[object_id] for object_id in container.source_object_ids]
        if len(objects) < 2:
            refined.append(container)
            continue
        first = objects[0]
        cut = next(
            (
                index
                for index, item in enumerate(objects[1:], start=1)
                if _material_style_change(first, item)
            ),
            None,
        )
        if cut is None:
            refined.append(container)
            continue
        leading = objects[:cut]
        remainder = objects[cut:]
        leading_text = _merge_flow_text(leading)
        remainder_text = _merge_flow_text(remainder)
        leading_size = max(item.font_size for item in leading)
        remainder_size = median(item.font_size for item in remainder)
        heading_evidence = (
            len(leading_text) <= 180
            and len(leading) <= 2
            and (
                leading_size >= remainder_size * 1.12
                or leading[0].color_srgb != remainder[0].color_srgb
                or _objects_bold(leading) != _objects_bold(remainder)
            )
        )
        if not heading_evidence or not remainder_text:
            refined.append(container)
            continue
        refined.extend(
            (
                _flow_style_container(container, leading, leading_text, "heading", 0),
                _flow_style_container(container, remainder, remainder_text, "body", 1),
            )
        )
    body_sizes = [
        item.font_size
        for item in refined
        if item.role == "body" and len(item.source_text) >= 120
    ]
    baseline = median(body_sizes or [item.font_size for item in refined])
    return tuple(
        replace(item, role="heading")
        if len(item.source_text) <= 180
        and (item.font_size >= baseline * 1.18 or item.font_weight == "bold")
        else item
        for item in refined
    )


def _flow_style_container(
    parent: TextContainer,
    objects,
    text: str,
    role: str,
    index: int,
) -> TextContainer:
    bbox = _union(tuple(item.bbox for item in objects))
    representative = max(objects, key=lambda item: (item.font_size, len(item.text)))
    return replace(
        parent,
        container_id=f"{parent.container_id}-style-{index:02d}",
        source_object_ids=tuple(item.object_id for item in objects),
        source_text=text,
        role=role,
        source_bbox=tuple(round(value, 4) for value in bbox),
        anchor=(round(bbox[0], 4), round(bbox[1], 4)),
        font_size=round(max(item.font_size for item in objects), 4),
        color_srgb=representative.color_srgb,
        font_weight="bold" if _objects_bold(objects) else "regular",
        preserved_prefix=parent.preserved_prefix if index == 0 else None,
    )


def _material_style_change(left, right) -> bool:
    return (
        abs(left.font_size - right.font_size) > 0.75
        or left.color_srgb != right.color_srgb
        or ("bold" in left.font_name.casefold()) != ("bold" in right.font_name.casefold())
    )


def _objects_bold(objects) -> bool:
    total = sum(max(1, len(item.text.strip())) for item in objects)
    bold = sum(
        max(1, len(item.text.strip()))
        for item in objects
        if "bold" in item.font_name.casefold()
    )
    return bold * 2 >= total


def _merge_flow_text(objects) -> str:
    ordered = sorted(objects, key=lambda item: (item.block_index, item.line_index, item.span_index))
    output = ""
    previous_line: tuple[int, int] | None = None
    for item in ordered:
        text = item.text.strip()
        if not text:
            continue
        current_line = (item.block_index, item.line_index)
        if not output:
            output = text
        elif current_line == previous_line:
            output += text
        elif output.endswith("-") and text[:1].islower():
            output = output[:-1] + text
        elif _is_han(output[-1:]) and _is_han(text[:1]):
            output += text
        else:
            output += " " + text
        previous_line = current_line
    return output.strip()


def _is_han(value: str) -> bool:
    return bool(value and "\u3400" <= value <= "\u9fff")


def _required_literals(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            re.findall(
                r"(?:https?://\S+|www\.\S+|\b[A-Z]{1,4}\d+[A-Z0-9.-]*\b|"
                r"(?<![\d'\u2018\u2019])\d+(?:[.,:/-]\d+)*%?)",
                text,
            )
        )
    )


def _select_flow_containers(template: SingleColumnTemplate) -> tuple[TextContainer, ...]:
    candidates = [
        container
        for container in template.containers
        if container.role != "margin"
        and container.source_bbox[1] >= template.height * 0.10
        and not _is_page_footer(container, template.height)
    ]
    lanes = _narrative_lanes(candidates, template.width)
    selected: set[str] = set()
    for lane in lanes:
        max_width = max(_width(item) for item in lane)
        base = [
            item
            for item in lane
            if _width(item) >= max(template.width * 0.22, max_width * 0.55)
            and _translatable_text(item.source_text)
            and not _is_source_text(item.source_text)
        ]
        selected.update(item.container_id for item in base)
        changed = True
        while changed:
            changed = False
            selected_items = [item for item in candidates if item.container_id in selected]
            lane_left = median(item.source_bbox[0] for item in lane)
            for item in candidates:
                if item.container_id in selected:
                    continue
                if abs(item.source_bbox[0] - lane_left) > 18.0:
                    continue
                if not _translatable_text(item.source_text) or _is_source_text(item.source_text):
                    continue
                below = any(
                    other.source_bbox[1] >= item.source_bbox[3]
                    and other.source_bbox[1] - item.source_bbox[3] <= 42.0
                    for other in selected_items
                )
                above = any(
                    item.source_bbox[1] >= other.source_bbox[3]
                    and item.source_bbox[1] - other.source_bbox[3] <= 24.0
                    for other in selected_items
                )
                _, _, _, cjk_count, word_count, _ = _text_stats(item.source_text)
                continuation = word_count >= 6 or cjk_count >= 12 or len(item.source_text.strip()) >= 45
                if below or (above and continuation):
                    selected.add(item.container_id)
                    changed = True
    return tuple(item for item in template.containers if item.container_id in selected)


def _narrative_lanes(
    containers: list[TextContainer],
    page_width: float,
) -> tuple[tuple[TextContainer, ...], ...]:
    wide = [item for item in containers if _width(item) >= page_width * 0.28]
    lanes: list[list[TextContainer]] = []
    for item in sorted(wide, key=lambda value: (value.source_bbox[0], value.source_bbox[1])):
        target = next(
            (
                lane
                for lane in lanes
                if abs(item.source_bbox[0] - median(value.source_bbox[0] for value in lane)) <= 18.0
                and _horizontal_overlap_ratio(
                    item.source_bbox,
                    _union(tuple(value.source_bbox for value in lane)),
                )
                >= 0.65
            ),
            None,
        )
        if target is None:
            lanes.append([item])
        else:
            target.append(item)
    return tuple(
        tuple(lane)
        for lane in lanes
        if _narrative_text(" ".join(item.source_text for item in lane))
    )


def _close_mixed_chart_groups(
    flow_ids: set[str],
    chart_template: ChartTemplate,
    flow_template: SingleColumnTemplate,
) -> set[str]:
    closed = set(flow_ids)
    changed = True
    while changed:
        changed = False
        for container in chart_template.containers:
            source_ids = set(container.source_object_ids)
            if source_ids & closed and not source_ids.issubset(closed):
                closed.update(source_ids)
                changed = True
        for container in flow_template.containers:
            source_ids = set(container.source_object_ids)
            if source_ids & closed and not source_ids.issubset(closed):
                closed.update(source_ids)
                changed = True
    return closed


def _build_flow_regions(
    facts: PageFacts,
    containers: tuple[TextContainer, ...],
    chart_guard_regions: tuple[Rect, ...],
) -> tuple[FlowRegionTemplate, ...]:
    lanes: list[list[TextContainer]] = []
    for container in sorted(containers, key=lambda item: (item.source_bbox[0], item.source_bbox[1])):
        target = next(
            (
                lane
                for lane in lanes
                if _same_flow_lane(container, lane)
            ),
            None,
        )
        if target is None:
            lanes.append([container])
        else:
            target.append(container)

    segments: list[list[TextContainer]] = []
    for lane in lanes:
        current: list[TextContainer] = []
        for container in sorted(lane, key=lambda item: (item.source_bbox[1], item.source_bbox[0])):
            if current and _separates_flow(current[-1], container, chart_guard_regions):
                segments.append(current)
                current = []
            current.append(container)
        if current:
            segments.append(current)
    segments.sort(key=lambda group: _reading_key(_union(tuple(item.source_bbox for item in group))))
    if not segments:
        raise FlowTextChartCapabilityError("P17_FLOW_REGION_NOT_SEPARABLE")

    footer_top = min(
        (
            item.bbox[1]
            for item in facts.text_objects
            if item.bbox[1] >= facts.height * 0.90
        ),
        default=facts.height - 16.0,
    )
    regions: list[FlowRegionTemplate] = []
    for index, segment in enumerate(segments):
        ordered = tuple(
            replace(container, reading_order=order)
            for order, container in enumerate(
                sorted(segment, key=lambda item: _reading_key(item.source_bbox))
            )
        )
        source_bbox = _union(tuple(item.source_bbox for item in ordered))
        next_top = min(
            (
                _union(tuple(item.source_bbox for item in other))[1] - 4.0
                for other in segments
                if other is not segment
                and _same_lane(source_bbox, _union(tuple(item.source_bbox for item in other)))
                and _union(tuple(item.source_bbox for item in other))[1] >= source_bbox[3]
            ),
            default=footer_top - 4.0,
        )
        chart_top = min(
            (
                region[1] - 4.0
                for region in chart_guard_regions
                if region[1] >= source_bbox[3] - 1.0
                and _horizontal_overlap_ratio(source_bbox, region) >= 0.10
            ),
            default=next_top,
        )
        bottom = max(source_bbox[3] + 1.0, min(next_top, chart_top, footer_top - 4.0))
        allowed_bbox = (
            max(0.0, source_bbox[0] - 2.0),
            max(0.0, source_bbox[1] - 2.0),
            min(facts.width, source_bbox[2] + 2.0),
            min(facts.height, bottom),
        )
        region_id = f"flow-single-{index:03d}"
        regions.append(
            FlowRegionTemplate(
                region_id,
                "single",
                tuple(round(value, 4) for value in allowed_bbox),
                SingleColumnTemplate(
                    facts.page_id,
                    TOOLBOX_KEY,
                    facts.width,
                    facts.height,
                    ordered,
                ),
            )
        )
    return tuple(regions)


def _same_flow_lane(container: TextContainer, lane: list[TextContainer]) -> bool:
    parent = _style_parent_id(container.container_id)
    if parent is not None and any(_style_parent_id(item.container_id) == parent for item in lane):
        return True
    return (
        abs(container.source_bbox[0] - median(item.source_bbox[0] for item in lane)) <= 20.0
        and _horizontal_overlap_ratio(
            container.source_bbox,
            _union(tuple(item.source_bbox for item in lane)),
        )
        >= 0.50
    )


def _style_parent_id(container_id: str) -> str | None:
    match = re.fullmatch(r"(.+)-style-\d+", container_id)
    return match.group(1) if match else None


def _chart_guard_regions(
    facts: PageFacts,
    full_chart: ChartTemplate,
    chart_containers: tuple[ChartTextContainer, ...],
) -> tuple[Rect, ...]:
    page_area = facts.width * facts.height
    visual_by_id = {
        item.object_id: item.bbox
        for item in (*facts.image_objects, *facts.drawing_objects)
    }
    region_by_id = {item.region_id: item for item in full_chart.visual_regions}
    boxes: list[Rect] = [item.source_bbox for item in chart_containers]
    for container in chart_containers:
        region = region_by_id.get(container.association_id)
        if region is not None and _area(region.bbox) < page_area * 0.65:
            boxes.append(region.bbox)
        boxes.extend(
            visual_by_id[object_id]
            for object_id in container.anchor_object_ids
            if object_id in visual_by_id and _area(visual_by_id[object_id]) < page_area * 0.65
        )
    if not boxes:
        raise FlowTextChartCapabilityError("P17_CHART_GUARD_REGION_NOT_FOUND")
    merge_gap = facts.width * 0.047
    groups: list[list[Rect]] = []
    for box in sorted(boxes, key=_reading_key):
        target = next(
            (
                group
                for group in groups
                if _rect_gap(box, _union(tuple(group))) <= merge_gap
                or _intersection_area(box, _union(tuple(group))) > 0.01
            ),
            None,
        )
        if target is None:
            groups.append([box])
        else:
            target.append(box)
    return tuple(
        tuple(round(value, 4) for value in _union(tuple(group)))
        for group in groups
    )


def _flow_render_container(
    facts: PageFacts,
    container: TextContainer,
    region: FlowRegionTemplate,
) -> ChartTextContainer:
    source_by_id = {item.object_id: item for item in facts.text_objects}
    source_objects = [source_by_id[object_id] for object_id in container.source_object_ids]
    visual_objects = (*facts.image_objects, *facts.drawing_objects)
    page_area = facts.width * facts.height
    anchors = tuple(
        item.object_id
        for item in visual_objects
        if _area(item.bbox) < page_area * 0.80
        and _intersection_area(item.bbox, container.source_bbox) > 0.01
    )
    role = {
        "heading": "TITLE",
        "body": "BODY_TEXT",
        "list": "BODY_TEXT",
    }.get(container.role, "BODY_TEXT")
    return ChartTextContainer(
        container.container_id,
        role,
        region.region_id,
        container.source_object_ids,
        container.source_text,
        container.source_bbox,
        region.allowed_bbox,
        anchors,
        "FLOW_BAND",
        container.reading_order,
        _required_literals(container.source_text),
        source_objects[0].font_name,
        container.font_size,
        container.color_srgb,
        "LEFT",
        0,
    )


def _chart_container_owner(container: ChartTextContainer, facts: PageFacts) -> str:
    if container.role in {"PAGE_HEADER", "PAGE_FOOTER"}:
        return "shared"
    if _is_source_text(container.source_text):
        return "shared"
    if container.role == "TITLE" and _width_rect(container.source_bbox) >= facts.width * 0.45:
        return "shared"
    return "chart"


def _assign(
    owners: dict[str, tuple[str, str | None]],
    object_id: str,
    owner: str,
    container_id: str | None,
) -> None:
    previous = owners.get(object_id)
    if previous is not None and previous != (owner, container_id):
        raise FlowTextChartCapabilityError(f"P17_DUPLICATE_OBJECT_OWNER:{object_id}")
    owners[object_id] = (owner, container_id)


def _narrative_text(text: str) -> bool:
    _, letters, digits, cjk_count, word_count, punctuation = _text_stats(text)
    if digits / max(1, letters + digits) >= 0.35:
        return False
    return (
        word_count >= 18 and (punctuation >= 1 or len(text) >= 180)
    ) or (
        cjk_count >= 40 and (punctuation >= 1 or cjk_count >= 75)
    )


def _translatable_text(text: str) -> bool:
    normalized, letters, digits, _, _, _ = _text_stats(text)
    if not normalized or _PAGE_NUMBER_RE.fullmatch(normalized):
        return False
    if letters < 3 or digits / max(1, letters + digits) >= 0.35:
        return False
    return not bool(re.fullmatch(r"[\d\s%.,()+\-–—/]+", normalized))


def _text_stats(text: str) -> tuple[str, int, int, int, int, int]:
    normalized = " ".join(text.split())
    letters = sum(character.isalpha() for character in normalized)
    digits = sum(character.isdigit() for character in normalized)
    cjk_count = sum("\u3400" <= character <= "\u9fff" for character in normalized)
    word_count = len(re.findall(r"[A-Za-z][A-Za-z'’-]*", normalized))
    punctuation = sum(normalized.count(character) for character in ".!?;:。！？；：")
    return normalized, letters, digits, cjk_count, word_count, punctuation


def _is_source_text(text: str) -> bool:
    return bool(_SOURCE_RE.search(" ".join(text.split())))


def _is_page_footer(container: TextContainer, page_height: float) -> bool:
    return container.source_bbox[1] >= page_height * 0.88 and len(container.source_text.strip()) < 100


def _separates_flow(
    previous: TextContainer,
    current: TextContainer,
    chart_regions: tuple[Rect, ...],
) -> bool:
    gap = current.source_bbox[1] - previous.source_bbox[3]
    if gap > 72.0:
        return True
    return any(
        region[1] >= previous.source_bbox[3] - 1.0
        and region[3] <= current.source_bbox[1] + 1.0
        and _horizontal_overlap_ratio(
            _union((previous.source_bbox, current.source_bbox)),
            region,
        )
        >= 0.10
        for region in chart_regions
    )


def _same_lane(left: Rect, right: Rect) -> bool:
    return abs(left[0] - right[0]) <= 22.0 and _horizontal_overlap_ratio(left, right) >= 0.50


def _reading_key(rect: Rect) -> tuple[float, float]:
    return rect[1], rect[0]


def _horizontal_overlap_ratio(left: Rect, right: Rect) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(_width_rect(left), _width_rect(right)))


def _width(container: TextContainer) -> float:
    return _width_rect(container.source_bbox)


def _width_rect(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0])


def _area(rect: Rect) -> float:
    return _width_rect(rect) * max(0.0, rect[3] - rect[1])


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _rect_gap(left: Rect, right: Rect) -> float:
    horizontal = max(0.0, max(left[0], right[0]) - min(left[2], right[2]))
    vertical = max(0.0, max(left[1], right[1]) - min(left[3], right[3]))
    return max(horizontal, vertical)


def _union(rects: tuple[Rect, ...]) -> Rect:
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )
