from __future__ import annotations

from collections import Counter
import re
import statistics
from dataclasses import dataclass, replace
from pathlib import Path

import fitz
from pdfminer.cmapdb import CMapDB

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import DiagramConnector, DiagramContainer, DiagramNode, DiagramTemplate, Point, Rect


class DiagramCapabilityError(RuntimeError):
    pass


_PURE_NUMBER = re.compile(r"^[\s+\-−–—$¥￥€£HKDUSDRMB,CNY()（）]*\d[\d\s,.]*\+?(?:%|％|‰)?$", re.IGNORECASE)
_PURE_MARK = re.compile(r"^[•·|｜/\\:：;；,，.。()（）\-–—]+$")
_IDENTIFIER = re.compile(
    r"^(?:(?i:(?:https?|ftp)://\S+|www\.\S+)|[^\s@]+@[^\s@]+\.[^\s@]+|(?=[A-Z0-9/-]*\d)[A-Z][A-Z0-9/-]{2,}|\d{4}[./-]\d{1,2}[./-]\d{1,2})$"
)
_REQUIRED_LITERAL = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2,6}(?=\s*[\u3400-\u9fff])"
    r"|(?<![A-Za-z0-9])(?=[A-Z0-9/-]*\d)(?=[A-Z0-9/-]*[A-Z])[A-Z][A-Z0-9/-]{1,}(?![A-Za-z0-9])"
    r"|(?<!\d)(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|[+\-−–—]?[€£¥￥$]?\d(?:[\d,]*\d)?(?:\.\d+)?\+?(?:%|％)?)(?!\d)"
)
_ROMAN_ENUMERATION = re.compile(r"^\(?[IVXLCDM]+\)?$")


@dataclass(frozen=True)
class _TextGroup:
    objects: tuple[TextObjectFact, ...]
    bbox: Rect
    font_size: float
    color_srgb: int


@dataclass(frozen=True)
class _DrawingDetail:
    object_id: str
    bbox: Rect
    fill: object
    closed: bool
    item_kinds: tuple[str, ...]
    lines: tuple[tuple[Point, Point], ...]


@dataclass(frozen=True)
class _NodeShape:
    bbox: Rect
    drawing_ids: tuple[str, ...]


def build_diagram_template(facts: PageFacts, source_pdf: Path) -> DiagramTemplate:
    details = _drawing_details(source_pdf, facts.page_index)
    diagram_geometry_sha256 = _diagram_geometry_sha256(facts, details)
    if not facts.text_objects:
        return _empty_template(facts, details, diagram_geometry_sha256)

    text_objects = _recover_source_text_objects(source_pdf, facts.page_index, facts.text_objects)
    groups = _text_groups(text_objects, details, facts.width, facts.height)
    shapes = _body_node_shapes(_node_shapes(details, groups, facts.width, facts.height), facts.height)
    node_ids = {index: f"node-{index:03d}" for index in range(len(shapes))}
    group_nodes = {
        index: _containing_shape(group.bbox, shapes)
        for index, group in enumerate(groups)
    }
    diagram_bbox = _diagram_bbox(details, facts.width, facts.height)
    median_size = statistics.median(item.font_size for item in text_objects)
    dense_unowned = len(groups) >= 60 or (
        len(details) >= 80 and median_size < 6.5
    )

    protected_ids: list[str] = []
    protected_groups: list[_TextGroup] = []
    containers: list[DiagramContainer] = []
    node_container_ids: dict[int, list[str]] = {index: [] for index in range(len(shapes))}
    owner_counter = 0
    ordered_group_indices = sorted(range(len(groups)), key=lambda index: (groups[index].bbox[1], groups[index].bbox[0]))
    for group_index in ordered_group_indices:
        group = groups[group_index]
        shape_index = group_nodes[group_index]
        text = _joined_text(group.objects)
        if _is_protected_group(
            text,
            group,
            shape_index,
            dense_unowned=dense_unowned,
            diagram_bbox=diagram_bbox,
            page_height=facts.height,
        ):
            protected_ids.extend(item.object_id for item in group.objects)
            protected_groups.append(group)
            continue

        role = _role(group, shape_index, diagram_bbox, median_size, facts.width, facts.height)
        if shape_index is not None:
            owner_id = node_ids[shape_index]
            owner_kind = "node"
            node_id = owner_id
            source_bbox = _union([item.bbox for item in group.objects])
            allowed = _node_safe_bbox(shapes[shape_index].bbox, source_bbox)
            alignment = "CENTER"
            local_index = len(node_container_ids[shape_index])
            container_id = f"{owner_id}/text-{local_index:02d}"
        else:
            owner_id = f"label-{owner_counter:03d}"
            owner_counter += 1
            owner_kind = "local_label"
            node_id = None
            source_bbox = _union([item.bbox for item in group.objects])
            allowed = _derived_allowed_bbox(
                source_bbox,
                role,
                group_index,
                groups,
                shapes,
                facts.width,
                facts.height,
            )
            alignment = _alignment(source_bbox, facts.width, role)
            if (
                role == "independent_label"
                and allowed[0] < source_bbox[0] - 2.0
                and allowed[2] > source_bbox[2] + 2.0
            ):
                alignment = "CENTER"
            container_id = f"{owner_id}/text-00"

        style = max(group.objects, key=lambda item: item.font_size)
        container = DiagramContainer(
            container_id=container_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            node_id=node_id,
            source_object_ids=tuple(item.object_id for item in group.objects),
            source_text=text,
            source_bbox=_round_rect(source_bbox),
            allowed_bbox=_round_rect(allowed),
            reading_order=len(containers),
            required_literals=_required_literals(text),
            role=role,
            font_name=style.font_name,
            font_size=round(max(item.font_size for item in group.objects), 4),
            color_srgb=style.color_srgb,
            alignment=alignment,
        )
        containers.append(container)
        if shape_index is not None:
            node_container_ids[shape_index].append(container_id)

    nodes = tuple(
        DiagramNode(
            node_id=node_ids[index],
            boundary_bbox=_round_rect(shape.bbox),
            safe_text_bbox=_round_rect(_node_safe_bbox(shape.bbox, shape.bbox)),
            source_drawing_ids=shape.drawing_ids,
            container_ids=tuple(node_container_ids[index]),
        )
        for index, shape in enumerate(shapes)
    )
    connectors = _connectors(details, nodes)
    containers = list(_constrain_connector_corridors(tuple(containers), connectors))
    layout_strategy = "OWNER_FIT"
    if _is_coordinate_locked_map(nodes, connectors, tuple(containers)):
        layout_strategy = "MAP_COORDINATE_LOCKED"
        coordinate_region = _coordinate_locked_region(nodes, facts.width, facts.height)
        containers = list(
            _constrain_map_image_titles(
                tuple(containers),
                facts.image_objects,
                coordinate_region=coordinate_region,
                page_width=facts.width,
                page_height=facts.height,
            )
        )
        promoted = _coordinate_locked_protected_containers(
            tuple(protected_groups),
            diagram_bbox=coordinate_region,
            page_height=facts.height,
        )
        promoted_ids = {
            object_id
            for container in promoted
            for object_id in container.source_object_ids
        }
        protected_ids = [object_id for object_id in protected_ids if object_id not in promoted_ids]
        containers.extend(promoted)
        containers = list(
            _merge_coordinate_locked_fragments(
                tuple(containers),
                text_objects,
                coordinate_region=coordinate_region,
                page_height=facts.height,
            )
        )
        containers = list(
            _split_coordinate_locked_containers(
                tuple(containers),
                text_objects,
                coordinate_region=coordinate_region,
                page_height=facts.height,
            )
        )
        containers = list(_expand_tiny_map_anchor_labels(tuple(containers), facts.width, facts.height))
        nodes = tuple(
            replace(
                node,
                container_ids=tuple(
                    container.container_id
                    for container in containers
                    if container.node_id == node.node_id
                ),
            )
            for node in nodes
        )
        connectors = _connectors(details, nodes)
    assigned = [object_id for container in containers for object_id in container.source_object_ids] + protected_ids
    expected = [item.object_id for item in facts.text_objects]
    if sorted(assigned) != sorted(expected) or len(assigned) != len(set(assigned)):
        raise DiagramCapabilityError("DIAGRAM_TEXT_OWNERSHIP_INCOMPLETE")
    topology_sha256 = canonical_sha256(
        {
            "locked_objects_sha256": facts.locked_objects_sha256,
            "diagram_geometry_sha256": diagram_geometry_sha256,
            "nodes": nodes,
            "connectors": connectors,
        }
    )
    mode = "translated" if containers else "passthrough"
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "mode": mode,
            "topology_sha256": topology_sha256,
            "containers": containers,
            "protected_object_ids": tuple(protected_ids),
        }
    )
    return DiagramTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        mode=mode,
        nodes=nodes,
        connectors=connectors,
        containers=tuple(containers),
        protected_object_ids=tuple(protected_ids),
        diagram_geometry_sha256=diagram_geometry_sha256,
        topology_sha256=topology_sha256,
        structure_sha256=structure_sha256,
        layout_strategy=layout_strategy,
    )


def _empty_template(facts: PageFacts, details: list[_DrawingDetail], diagram_geometry_sha256: str) -> DiagramTemplate:
    topology_sha256 = canonical_sha256(
        {
            "locked_objects_sha256": facts.locked_objects_sha256,
            "diagram_geometry_sha256": diagram_geometry_sha256,
            "drawing_ids": [item.object_id for item in details],
        }
    )
    return DiagramTemplate(
        facts.page_id,
        TOOLBOX_KEY,
        facts.width,
        facts.height,
        "passthrough",
        (),
        (),
        (),
        (),
        diagram_geometry_sha256,
        topology_sha256,
        canonical_sha256({"toolbox_key": TOOLBOX_KEY, "mode": "passthrough", "topology_sha256": topology_sha256}),
    )


def _text_groups(
    objects: tuple[TextObjectFact, ...],
    details: list[_DrawingDetail],
    page_width: float,
    page_height: float,
) -> list[_TextGroup]:
    rows: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        rows.setdefault((item.block_index, item.line_index), []).append(item)

    fragments: list[_TextGroup] = []
    for items in rows.values():
        ordered = sorted(items, key=lambda item: (item.bbox[0], item.span_index))
        current: list[TextObjectFact] = []
        for item in ordered:
            if current:
                previous = current[-1]
                gap = item.bbox[0] - previous.bbox[2]
                threshold = max(12.0, max(item.font_size, previous.font_size) * 2.5)
                if gap > threshold:
                    fragments.append(_make_group(current))
                    current = []
            current.append(item)
        if current:
            fragments.append(_make_group(current))

    shape_hints = _node_shapes(details, fragments, page_width, page_height)
    groups: list[list[TextObjectFact]] = []
    for fragment in sorted(fragments, key=lambda item: (item.bbox[1], item.bbox[0])):
        fragment_owner = _containing_shape(fragment.bbox, shape_hints)
        compatible = [
            group
            for group in groups
            if _containing_shape(_make_group(group).bbox, shape_hints) == fragment_owner
            and _can_merge_group(_make_group(group), fragment)
        ]
        if compatible:
            target = min(compatible, key=lambda group: _rect_gap(_make_group(group).bbox, fragment.bbox))
            target.extend(fragment.objects)
            for extra in compatible:
                if extra is target:
                    continue
                target.extend(extra)
                groups.remove(extra)
        else:
            groups.append(list(fragment.objects))
    return [_make_group(group) for group in groups]


def _is_coordinate_locked_map(
    nodes: tuple[DiagramNode, ...],
    connectors: tuple[DiagramConnector, ...],
    containers: tuple[DiagramContainer, ...],
) -> bool:
    if not nodes or not containers or len(connectors) >= len(nodes):
        return False
    node_owner_counts: dict[str, int] = {}
    for container in containers:
        if container.owner_kind == "node":
            node_owner_counts[container.owner_id] = node_owner_counts.get(container.owner_id, 0) + 1
    repeated_labels_per_node = bool(node_owner_counts) and statistics.median(node_owner_counts.values()) > 1
    overlapping_node_pairs = sum(
        _intersection_area(left.boundary_bbox, right.boundary_bbox)
        > min(_area(left.boundary_bbox), _area(right.boundary_bbox)) * 0.08
        for index, left in enumerate(nodes)
        for right in nodes[index + 1 :]
    )
    overlapping_regions = overlapping_node_pairs >= len(nodes)
    return repeated_labels_per_node or overlapping_regions


def _split_coordinate_locked_containers(
    containers: tuple[DiagramContainer, ...],
    source_objects: tuple[TextObjectFact, ...],
    *,
    coordinate_region: Rect | None = None,
    page_height: float | None = None,
) -> tuple[DiagramContainer, ...]:
    source_by_id = {item.object_id: item for item in source_objects}
    split: list[DiagramContainer] = []
    for container in containers:
        if not _container_uses_map_coordinates(container, coordinate_region, page_height):
            split.append(container)
            continue
        blocks: dict[int, list[TextObjectFact]] = {}
        for object_id in container.source_object_ids:
            item = source_by_id[object_id]
            blocks.setdefault(item.block_index, []).append(item)
        ordered_blocks = _coalesced_coordinate_blocks(tuple(tuple(items) for items in blocks.values()))
        for block_index, items in enumerate(ordered_blocks):
            ordered = tuple(sorted(items, key=lambda item: (item.line_index, item.bbox[0], item.span_index)))
            source_bbox = _round_rect(_union([item.bbox for item in ordered]))
            source_text = _coordinate_block_text(ordered)
            style = max(ordered, key=lambda item: item.font_size)
            container_id = container.container_id
            if len(ordered_blocks) > 1:
                container_id = f"{container_id}/block-{block_index:02d}"
            split.append(
                replace(
                    container,
                    container_id=container_id,
                    source_object_ids=tuple(item.object_id for item in ordered),
                    source_text=source_text,
                    source_bbox=source_bbox,
                    allowed_bbox=source_bbox,
                    required_literals=_required_literals(source_text),
                    font_name=style.font_name,
                    font_size=round(max(item.font_size for item in ordered), 4),
                    color_srgb=style.color_srgb,
                    alignment="LEFT",
                )
            )
    ordered_split = sorted(split, key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    return tuple(replace(item, reading_order=index) for index, item in enumerate(ordered_split))


def _merge_coordinate_locked_fragments(
    containers: tuple[DiagramContainer, ...],
    source_objects: tuple[TextObjectFact, ...],
    *,
    coordinate_region: Rect | None = None,
    page_height: float | None = None,
) -> tuple[DiagramContainer, ...]:
    source_by_id = {item.object_id: item for item in source_objects}
    groups: list[list[DiagramContainer]] = []
    for container in sorted(containers, key=lambda item: item.reading_order):
        if not _container_uses_map_coordinates(container, coordinate_region, page_height):
            groups.append([container])
            continue
        compatible = [
            group
            for group in groups
            if _container_uses_map_coordinates(group[0], coordinate_region, page_height)
            and group[0].owner_id == container.owner_id
            and (
                _coordinate_blocks_are_duplicate_overlay(
                    tuple(
                        source_by_id[object_id]
                        for existing in group
                        for object_id in existing.source_object_ids
                    ),
                    tuple(source_by_id[object_id] for object_id in container.source_object_ids),
                )
                or any(
                    _coordinate_fragments_are_word_continuation(source_by_id[left_id], source_by_id[right_id])
                    for existing in group
                    for left_id in existing.source_object_ids
                    for right_id in container.source_object_ids
                )
            )
        ]
        if compatible:
            target = compatible[0]
            target.append(container)
            for extra in compatible[1:]:
                target.extend(extra)
                groups.remove(extra)
        else:
            groups.append([container])

    merged = []
    for group in groups:
        base = min(group, key=lambda item: item.reading_order)
        if len(group) == 1 and not _container_uses_map_coordinates(base, coordinate_region, page_height):
            merged.append(base)
            continue
        object_ids = tuple(
            dict.fromkeys(
                object_id
                for container in group
                for object_id in container.source_object_ids
            )
        )
        objects = tuple(source_by_id[object_id] for object_id in object_ids)
        source_bbox = _round_rect(_union([item.bbox for item in objects]))
        merged.append(
            replace(
                base,
                source_object_ids=object_ids,
                source_text=_joined_visual_text(objects),
                source_bbox=source_bbox,
                allowed_bbox=source_bbox,
            )
        )
    return tuple(sorted(merged, key=lambda item: item.reading_order))


def _coordinate_locked_protected_containers(
    groups: tuple[_TextGroup, ...],
    *,
    diagram_bbox: Rect | None,
    page_height: float,
) -> tuple[DiagramContainer, ...]:
    if diagram_bbox is None:
        return ()
    result = []
    for group in groups:
        text = _joined_text(group.objects)
        stripped = text.strip()
        if (
            group.bbox[1] >= page_height * 0.95
            or not _center_inside(group.bbox, diagram_bbox, tolerance=0.0)
            or not (_has_semantic_text(text) or _PURE_NUMBER.fullmatch(stripped))
            or _PURE_MARK.fullmatch(stripped)
            or _IDENTIFIER.fullmatch(stripped)
            or _ROMAN_ENUMERATION.fullmatch(stripped)
        ):
            continue
        owner_id = f"map-label-{len(result):03d}"
        style = max(group.objects, key=lambda item: item.font_size)
        source_bbox = _round_rect(_union([item.bbox for item in group.objects]))
        result.append(
            DiagramContainer(
                container_id=f"{owner_id}/text-00",
                owner_kind="local_label",
                owner_id=owner_id,
                node_id=None,
                source_object_ids=tuple(item.object_id for item in group.objects),
                source_text=text,
                source_bbox=source_bbox,
                allowed_bbox=source_bbox,
                reading_order=0,
                required_literals=_required_literals(text),
                role="independent_label",
                font_name=style.font_name,
                font_size=round(max(item.font_size for item in group.objects), 4),
                color_srgb=style.color_srgb,
                alignment="LEFT",
            )
        )
    return tuple(result)


def _coalesced_coordinate_blocks(
    blocks: tuple[tuple[TextObjectFact, ...], ...],
) -> list[list[TextObjectFact]]:
    ordered = sorted(blocks, key=lambda items: (_union([item.bbox for item in items])[1], _union([item.bbox for item in items])[0]))
    groups: list[list[TextObjectFact]] = []
    for block in ordered:
        compatible = [
            group
            for group in groups
            if _coordinate_blocks_are_duplicate_overlay(group, block)
            or any(_coordinate_fragments_are_word_continuation(left, right) for left in group for right in block)
        ]
        if compatible:
            target = compatible[0]
            target.extend(block)
            for extra in compatible[1:]:
                target.extend(extra)
                groups.remove(extra)
        else:
            groups.append(list(block))
    return sorted(groups, key=lambda items: (_union([item.bbox for item in items])[1], _union([item.bbox for item in items])[0]))


def _coordinate_blocks_are_duplicate_overlay(
    left_items: list[TextObjectFact] | tuple[TextObjectFact, ...],
    right_items: list[TextObjectFact] | tuple[TextObjectFact, ...],
) -> bool:
    left_bbox = _union([item.bbox for item in left_items])
    right_bbox = _union([item.bbox for item in right_items])
    overlap = _intersection_area(left_bbox, right_bbox)
    if overlap < min(_area(left_bbox), _area(right_bbox)) * 0.55:
        return False
    left_text = "".join(_joined_visual_text(tuple(left_items)).split())
    right_text = "".join(_joined_visual_text(tuple(right_items)).split())
    return bool(left_text and right_text and (left_text in right_text or right_text in left_text))


def _coordinate_block_text(objects: tuple[TextObjectFact, ...]) -> str:
    by_block: dict[int, list[TextObjectFact]] = {}
    for item in objects:
        by_block.setdefault(item.block_index, []).append(item)
    block_texts = [_joined_visual_text(tuple(items)) for items in by_block.values()]
    longest = max(block_texts, key=lambda value: len("".join(value.split())), default="")
    normalized_longest = "".join(longest.split())
    normalized_others = ["".join(value.split()) for value in block_texts if value != longest]
    if (
        normalized_longest
        and all(value in normalized_longest for value in normalized_others)
        and all(len(value) >= 2 or not value.isascii() for value in normalized_others)
    ):
        return longest
    return _joined_visual_text(objects)


def _coordinate_fragments_touch(left: TextObjectFact, right: TextObjectFact) -> bool:
    vertical_overlap = _axis_overlap((left.bbox[1], left.bbox[3]), (right.bbox[1], right.bbox[3]))
    if vertical_overlap < min(_height(left.bbox), _height(right.bbox)) * 0.7:
        return False
    horizontal_gap = max(0.0, max(left.bbox[0], right.bbox[0]) - min(left.bbox[2], right.bbox[2]))
    return horizontal_gap <= max(0.75, max(left.font_size, right.font_size) * 0.35)


def _coordinate_fragments_are_word_continuation(left: TextObjectFact, right: TextObjectFact) -> bool:
    if not _coordinate_fragments_touch(left, right):
        return False
    left_text = _recover_visible_text(left.text, left.font_name).strip()
    right_text = _recover_visible_text(right.text, right.font_name).strip()
    left_token = re.search(r"[A-Za-z]+$", left_text)
    right_token = re.match(r"[A-Za-z]+", right_text)
    if not left_token or not right_token:
        return False
    left_is_fragment = len(left_token.group(0)) <= 2
    right_is_fragment = len(right_token.group(0)) <= 2
    return left_is_fragment != right_is_fragment


def _joined_visual_text(objects: tuple[TextObjectFact, ...]) -> str:
    rows: list[list[TextObjectFact]] = []
    for item in sorted(objects, key=lambda value: (value.bbox[1], value.bbox[0])):
        compatible = [
            row
            for row in rows
            if any(
                _axis_overlap((item.bbox[1], item.bbox[3]), (other.bbox[1], other.bbox[3]))
                >= min(_height(item.bbox), _height(other.bbox)) * 0.7
                for other in row
            )
        ]
        if compatible:
            compatible[0].append(item)
        else:
            rows.append([item])
    lines = []
    for row in sorted(rows, key=lambda items: min(item.bbox[1] for item in items)):
        text = ""
        previous = None
        for item in sorted(row, key=lambda value: (value.bbox[0], value.span_index)):
            value = _recover_visible_text(item.text, item.font_name).strip()
            if not value:
                continue
            gap = item.bbox[0] - previous.bbox[2] if previous is not None else 0.0
            if text and _needs_space(text[-1], value[0]) and gap > max(0.5, item.font_size * 0.25):
                text += " "
            text += value
            previous = item
        if text:
            lines.append(text)
    return "\n".join(lines)


def _make_group(items: list[TextObjectFact] | tuple[TextObjectFact, ...]) -> _TextGroup:
    ordered = tuple(sorted(items, key=lambda item: (item.bbox[1], item.bbox[0], item.span_index)))
    style = max(ordered, key=lambda item: item.font_size)
    return _TextGroup(ordered, _union([item.bbox for item in ordered]), max(item.font_size for item in ordered), style.color_srgb)


def _can_merge_group(left: _TextGroup, right: _TextGroup) -> bool:
    size_ratio = max(left.font_size, right.font_size) / max(0.1, min(left.font_size, right.font_size))
    if size_ratio > 1.35 or left.color_srgb != right.color_srgb:
        return False
    same_baseline = (
        abs(left.bbox[1] - right.bbox[1]) <= max(0.75, max(left.font_size, right.font_size) * 0.15)
        and abs(left.bbox[3] - right.bbox[3]) <= max(0.75, max(left.font_size, right.font_size) * 0.15)
    )
    horizontal_gap = max(0.0, max(left.bbox[0], right.bbox[0]) - min(left.bbox[2], right.bbox[2]))
    if same_baseline and horizontal_gap <= max(8.0, max(left.font_size, right.font_size) * 1.5):
        return True
    overlap = _axis_overlap((left.bbox[0], left.bbox[2]), (right.bbox[0], right.bbox[2]))
    minimum_width = min(_width(left.bbox), _width(right.bbox))
    anchor_gap = abs(left.bbox[0] - right.bbox[0])
    top_delta = right.bbox[1] - left.bbox[1]
    same_source_block = len({item.block_index for item in (*left.objects, *right.objects)}) == 1
    left_lines = {item.line_index for item in left.objects}
    right_lines = {item.line_index for item in right.objects}
    sequential_source_lines = (
        same_source_block
        and left_lines
        and right_lines
        and min(right_lines) == max(left_lines) + 1
        and right.bbox[1] > left.bbox[1]
        and (
            overlap >= minimum_width * 0.35
            or anchor_gap <= max(8.0, max(left.font_size, right.font_size) * 1.5)
        )
    )
    if sequential_source_lines:
        return True
    if (
        same_source_block
        and max(left.font_size, right.font_size) * 0.6 <= top_delta <= max(left.font_size, right.font_size) * 1.8
        and (overlap >= minimum_width * 0.35 or anchor_gap <= max(8.0, max(left.font_size, right.font_size) * 1.5))
    ):
        return True
    if right.bbox[1] < left.bbox[1] - 0.5:
        return False
    vertical_gap = right.bbox[1] - left.bbox[3]
    if vertical_gap < -max(left.font_size, right.font_size) * 0.45:
        return False
    if vertical_gap > max(6.0, max(left.font_size, right.font_size)):
        return False
    return overlap >= minimum_width * 0.35 or anchor_gap <= max(8.0, max(left.font_size, right.font_size) * 1.5)


def _drawing_details(source_pdf: Path, page_index: int) -> list[_DrawingDetail]:
    result: list[_DrawingDetail] = []
    with fitz.open(source_pdf) as document:
        page = document[page_index]
        for drawing_index, drawing in enumerate(page.get_drawings()):
            bbox = fitz.Rect(drawing.get("rect", (0, 0, 0, 0)))
            lines: list[tuple[Point, Point]] = []
            kinds: list[str] = []
            for item in drawing.get("items", []):
                kind = str(item[0])
                kinds.append(kind)
                if kind == "l":
                    lines.append((_point(item[1]), _point(item[2])))
            if bbox.is_empty and lines:
                points = [point for line in lines for point in line]
                bbox = fitz.Rect(
                    min(point[0] for point in points),
                    min(point[1] for point in points),
                    max(point[0] for point in points),
                    max(point[1] for point in points),
                )
                padding = max(0.25, float(drawing.get("width") or 0.5) / 2)
                bbox = fitz.Rect(bbox.x0 - padding, bbox.y0 - padding, bbox.x1 + padding, bbox.y1 + padding)
            if bbox.is_empty:
                continue
            result.append(
                _DrawingDetail(
                    object_id=f"p{page_index}-diagram-drawing-{drawing_index:04d}",
                    bbox=_round_rect(tuple(bbox)),
                    fill=drawing.get("fill"),
                    closed=bool(drawing.get("closePath")),
                    item_kinds=tuple(kinds),
                    lines=tuple(lines),
                )
            )
    return result


def diagram_geometry_sha256(source_pdf: Path, page_index: int, facts: PageFacts) -> str:
    return _diagram_geometry_sha256(facts, _drawing_details(source_pdf, page_index))


def _diagram_geometry_sha256(facts: PageFacts, details: list[_DrawingDetail]) -> str:
    return canonical_sha256(
        {
            "page": _geometry_value((facts.width, facts.height, facts.rotation)),
            "images": [
                {
                    "object_id": item.object_id,
                    "bbox": _geometry_value(item.bbox),
                    "width": item.width,
                    "height": item.height,
                    "content_sha256": item.content_sha256,
                }
                for item in facts.image_objects
            ],
            "drawings": [
                {
                    "object_id": item.object_id,
                    "bbox": _geometry_value(item.bbox),
                    "fill": _geometry_value(item.fill),
                    "closed": item.closed,
                    "item_kinds": item.item_kinds,
                    "lines": _geometry_value(item.lines),
                }
                for item in details
            ],
        }
    )


def _geometry_value(value):
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (list, tuple)):
        return tuple(_geometry_value(item) for item in value)
    return value


def _node_shapes(details: list[_DrawingDetail], groups: list[_TextGroup], width: float, height: float) -> list[_NodeShape]:
    page_area = width * height
    candidates = [
        detail
        for detail in details
        if width * 0.03 <= _width(detail.bbox)
        and height * 0.011 <= _height(detail.bbox)
        and page_area * 0.0002 <= _area(detail.bbox) <= page_area * 0.12
        and _width(detail.bbox) / max(width * 1e-9, _height(detail.bbox)) <= 18.0
        and (detail.fill is not None or detail.closed or "re" in detail.item_kinds)
    ]
    selected: list[_DrawingDetail] = []
    for group in groups:
        matches = [
            detail
            for detail in candidates
            if _center_inside(
                group.bbox,
                detail.bbox,
                tolerance=min(_width(detail.bbox), _height(detail.bbox)) * 0.08,
            )
            and _coverage(group.bbox, detail.bbox) >= 0.68
            and _width(detail.bbox) >= group.font_size * 1.5
            and _height(detail.bbox) >= group.font_size * 1.5
        ]
        if matches:
            selected.append(min(matches, key=lambda detail: _area(detail.bbox)))

    clusters: list[list[_DrawingDetail]] = []
    for detail in selected:
        cluster = next((row for row in clusters if any(_iou(detail.bbox, item.bbox) >= 0.65 for item in row)), None)
        if cluster is None:
            clusters.append([detail])
        elif detail.object_id not in {item.object_id for item in cluster}:
            cluster.append(detail)

    shapes: list[_NodeShape] = []
    for cluster in clusters:
        intersection = _intersection([item.bbox for item in cluster])
        smallest_area = min(_area(item.bbox) for item in cluster)
        bbox = intersection if _area(intersection) >= smallest_area * 0.5 else min(cluster, key=lambda item: _area(item.bbox)).bbox
        shapes.append(_NodeShape(_round_rect(bbox), tuple(sorted(item.object_id for item in cluster))))
    shapes.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    return shapes


def _body_node_shapes(shapes: list[_NodeShape], page_height: float) -> list[_NodeShape]:
    body = [
        shape
        for shape in shapes
        if shape.bbox[3] > page_height * 0.1 and shape.bbox[1] < page_height * 0.92
    ]
    return body if len(body) >= 2 else shapes


def _containing_shape(bbox: Rect, shapes: list[_NodeShape]) -> int | None:
    matches = [
        (index, shape)
        for index, shape in enumerate(shapes)
        if _center_inside(bbox, shape.bbox, tolerance=2.0) and _coverage(bbox, shape.bbox) >= 0.68
    ]
    return min(matches, key=lambda row: _area(row[1].bbox), default=(None, None))[0]


def _connectors(details: list[_DrawingDetail], nodes: tuple[DiagramNode, ...]) -> tuple[DiagramConnector, ...]:
    node_by_drawing = {drawing_id for node in nodes for drawing_id in node.source_drawing_ids}
    arrow_shapes = [
        item
        for item in details
        if item.object_id not in node_by_drawing and item.fill is not None and 2.0 <= _area(item.bbox) <= 140.0
    ]
    segments: list[tuple[Point, Point, str]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for detail in details:
        if len(detail.lines) > 200:
            continue
        for start, end in detail.lines:
            if _distance(start, end) < 3.0:
                continue
            same_node = next((node for node in nodes if _point_in_rect(start, node.boundary_bbox, 1.0) and _point_in_rect(end, node.boundary_bbox, 1.0)), None)
            if same_node:
                continue
            key = tuple(round(value, 2) for value in (*start, *end))
            reverse = tuple(round(value, 2) for value in (*end, *start))
            if key in seen or reverse in seen:
                continue
            seen.add(key)
            segments.append((start, end, detail.object_id))

    result: list[DiagramConnector] = []
    for index, (start, end, drawing_id) in enumerate(segments):
        start_node = _nearest_node(start, nodes)
        end_node = _nearest_node(end, nodes)
        if nodes and start_node is None and end_node is None and not _line_near_nodes(start, end, nodes):
            continue
        start_arrow = any(_point_rect_distance(start, item.bbox) <= 5.0 for item in arrow_shapes)
        end_arrow = any(_point_rect_distance(end, item.bbox) <= 5.0 for item in arrow_shapes)
        if start_arrow and end_arrow:
            direction = "bidirectional"
        elif start_arrow:
            direction = "end_to_start"
        elif end_arrow:
            direction = "start_to_end"
        else:
            direction = "undirected"
        result.append(
            DiagramConnector(
                connector_id=f"connector-{index:04d}",
                start=_round_point(start),
                end=_round_point(end),
                source_drawing_id=drawing_id,
                start_node_id=start_node,
                end_node_id=end_node,
                direction=direction,
            )
        )
    return tuple(result)


def _constrain_connector_corridors(
    containers: tuple[DiagramContainer, ...],
    connectors: tuple[DiagramConnector, ...],
) -> tuple[DiagramContainer, ...]:
    result = []
    for container in containers:
        if container.owner_kind != "local_label":
            result.append(container)
            continue
        interfering = [
            connector
            for connector in connectors
            if _segment_bounds_hit_rect(connector.start, connector.end, container.allowed_bbox)
            and not _segment_bounds_hit_rect(connector.start, connector.end, container.source_bbox)
        ]
        if not interfering:
            result.append(container)
            continue

        gutter = container.font_size * 0.10
        horizontal_bands = [
            connector
            for connector in interfering
            if abs(connector.start[1] - connector.end[1]) <= gutter
            and min(connector.start[1], connector.end[1]) >= container.source_bbox[3]
            and min(connector.start[1], connector.end[1]) - container.source_bbox[3]
            <= container.font_size * 0.50
            and min(connector.start[0], connector.end[0]) <= container.source_bbox[0]
            and max(connector.start[0], connector.end[0]) >= container.source_bbox[2]
        ]
        if horizontal_bands:
            boundary = min(horizontal_bands, key=lambda item: min(item.start[1], item.end[1]))
            boundary_y = min(boundary.start[1], boundary.end[1])
            left = min(container.allowed_bbox[0], min(boundary.start[0], boundary.end[0]) + gutter)
            right = max(
                container.source_bbox[2],
                min(container.allowed_bbox[2], max(boundary.start[0], boundary.end[0]) - gutter),
            )
            bottom = max(container.source_bbox[3], min(container.allowed_bbox[3], boundary_y - gutter))
            result.append(
                replace(
                    container,
                    allowed_bbox=_round_rect((left, container.allowed_bbox[1], right, bottom)),
                    alignment="LEFT",
                )
            )
            continue

        right_boundaries = [
            min(connector.start[0], connector.end[0]) - gutter
            for connector in interfering
            if min(connector.start[0], connector.end[0]) >= container.source_bbox[2]
        ]
        bottom_boundaries = [
            min(connector.start[1], connector.end[1]) - gutter
            for connector in interfering
            if min(connector.start[1], connector.end[1]) >= container.source_bbox[3]
        ]
        right = min(container.allowed_bbox[2], min(right_boundaries, default=container.allowed_bbox[2]))
        right = max(right, container.source_bbox[2])
        bottom = min(container.allowed_bbox[3], min(bottom_boundaries, default=container.allowed_bbox[3]))
        bottom = max(bottom, container.source_bbox[3])
        candidates = (
            (container.allowed_bbox[0], container.allowed_bbox[1], right, container.allowed_bbox[3]),
            (container.allowed_bbox[0], container.allowed_bbox[1], container.allowed_bbox[2], bottom),
            (container.allowed_bbox[0], container.allowed_bbox[1], right, bottom),
        )
        collision_free = [
            candidate
            for candidate in candidates
            if not any(
                _segment_bounds_hit_rect(connector.start, connector.end, candidate)
                for connector in interfering
            )
        ]
        allowed = max(collision_free, key=_area) if collision_free else candidates[-1]
        result.append(
            replace(
                container,
                allowed_bbox=_round_rect(allowed),
            )
        )
    return tuple(result)


def _segment_bounds_hit_rect(start: Point, end: Point, rect: Rect, tolerance: float = 0.4) -> bool:
    return not (
        max(start[0], end[0]) < rect[0] - tolerance
        or min(start[0], end[0]) > rect[2] + tolerance
        or max(start[1], end[1]) < rect[1] - tolerance
        or min(start[1], end[1]) > rect[3] + tolerance
    )


def _line_near_nodes(start: Point, end: Point, nodes: tuple[DiagramNode, ...]) -> bool:
    envelope = _union([node.boundary_bbox for node in nodes])
    padding = max(12.0, min(32.0, min(_width(envelope), _height(envelope)) * 0.15))
    neighborhood = (
        envelope[0] - padding,
        envelope[1] - padding,
        envelope[2] + padding,
        envelope[3] + padding,
    )
    line_bbox = (
        min(start[0], end[0]),
        min(start[1], end[1]),
        max(start[0], end[0]),
        max(start[1], end[1]),
    )
    return not (
        line_bbox[2] < neighborhood[0]
        or line_bbox[0] > neighborhood[2]
        or line_bbox[3] < neighborhood[1]
        or line_bbox[1] > neighborhood[3]
    )


def _is_protected_group(
    text: str,
    group: _TextGroup,
    shape_index: int | None,
    *,
    dense_unowned: bool,
    diagram_bbox: Rect | None,
    page_height: float,
) -> bool:
    stripped = text.strip()
    if not _has_semantic_text(stripped):
        return True
    if (
        _PURE_NUMBER.fullmatch(stripped)
        or _PURE_MARK.fullmatch(stripped)
        or _IDENTIFIER.fullmatch(stripped)
        or _ROMAN_ENUMERATION.fullmatch(stripped)
    ):
        return True
    if shape_index is not None:
        return False
    if group.bbox[1] > page_height * 0.95 and len(stripped) <= 32:
        return True
    if dense_unowned and group.font_size < 6.0 and diagram_bbox and _center_inside(group.bbox, diagram_bbox, tolerance=0.0):
        return True
    return False


def _role(group: _TextGroup, shape_index: int | None, diagram_bbox: Rect | None, median: float, width: float, height: float) -> str:
    if shape_index is not None:
        return "node_text"
    text = _joined_text(group.objects)
    if group.font_size >= median * 1.18:
        return "title"
    if _width(group.bbox) >= width * 0.35 or len(text) >= 120 or _height(group.bbox) >= group.font_size * 2.2:
        return "independent_paragraph"
    return "independent_label"


def _derived_allowed_bbox(source: Rect, role: str, index: int, groups: list[_TextGroup], shapes: list[_NodeShape], width: float, height: float) -> Rect:
    x0, y0, x1, y1 = source
    current_group = groups[index]
    horizontal_gutter = current_group.font_size * 0.25
    page_right = width * 0.925
    horizontal_peers = [
        group
        for other_index, group in enumerate(groups)
        if other_index != index
        and min(current_group.font_size, group.font_size) >= max(current_group.font_size, group.font_size) * 0.70
        and _axis_overlap((y0, y1), (group.bbox[1], group.bbox[3])) >= min(_height(source), _height(group.bbox)) * 0.35
        and (group.bbox[0] >= x1 or group.bbox[2] <= x0)
    ]
    obstacles = [group.bbox for other_index, group in enumerate(groups) if other_index != index] + [shape.bbox for shape in shapes]
    right_blockers = [
        obstacle[0]
        for obstacle in obstacles
        if obstacle[0] >= x1
        and _axis_overlap((y0, y1), (obstacle[1], obstacle[3]))
        >= min(_height(source), _height(obstacle)) * 0.35
    ]
    safe_right = min([page_right, *(value - horizontal_gutter for value in right_blockers)])
    peer_right_boundaries = [
        group.bbox[0] - horizontal_gutter
        for group in horizontal_peers
        if group.bbox[0] >= x1
    ]
    if peer_right_boundaries:
        safe_right = min(safe_right, *peer_right_boundaries)
    safe_right = max(x1, safe_right)
    desired_left = x0
    desired_right = safe_right
    vertical_gutter = current_group.font_size * 0.45
    desired_top = y0
    if role == "independent_paragraph":
        above = [
            obstacle[3]
            for obstacle in obstacles
            if obstacle[3] <= y0
            and _axis_overlap((desired_left, desired_right), (obstacle[0], obstacle[2]))
            >= min(_width(source), _width(obstacle)) * 0.2
        ]
        if above:
            desired_top = max(max(above) + vertical_gutter, height * 0.015)

    if role == "connector_label":
        desired_right = max(x1, min(page_right, x1 + current_group.font_size * 0.5))

    below = [
        obstacle[1]
        for obstacle in obstacles
        if obstacle[1] >= y1
        and _axis_overlap((desired_left, desired_right), (obstacle[0], obstacle[2]))
        >= min(_width(source), _width(obstacle)) * 0.2
    ]
    desired_bottom = min(below) - vertical_gutter if below else height * 0.985
    desired_bottom = max(y1, desired_bottom)
    return _clip_rect((desired_left, desired_top, desired_right, desired_bottom), width, height)


def _coordinate_locked_region(nodes: tuple[DiagramNode, ...], width: float, height: float) -> Rect | None:
    if not nodes:
        return None
    union = _union([node.boundary_bbox for node in nodes])
    padding = 18.0
    return _clip_rect(
        (union[0] - padding, union[1] - padding, union[2] + padding, union[3] + padding),
        width,
        height,
    )


def _constrain_map_image_titles(
    containers: tuple[DiagramContainer, ...],
    image_objects,
    *,
    coordinate_region: Rect | None,
    page_width: float,
    page_height: float,
) -> tuple[DiagramContainer, ...]:
    result = []
    for container in containers:
        if container.owner_kind != "local_label" or container.role != "title":
            result.append(container)
            continue
        frame = _map_image_title_frame(
            container.source_bbox,
            image_objects,
            coordinate_region=coordinate_region,
            page_width=page_width,
            page_height=page_height,
        )
        if frame is None:
            result.append(container)
            continue
        safe = _node_safe_bbox(frame, container.source_bbox)
        allowed = (
            safe[0],
            max(safe[1], container.source_bbox[1] - max(1.5, container.font_size * 0.20)),
            safe[2],
            min(safe[3], container.source_bbox[3] + max(3.0, container.font_size * 0.55)),
        )
        result.append(
            replace(
                container,
                allowed_bbox=_round_rect(allowed),
                role="image_framed_label",
                alignment="CENTER",
            )
        )
    return tuple(result)


def _expand_tiny_map_anchor_labels(
    containers: tuple[DiagramContainer, ...],
    page_width: float,
    page_height: float,
) -> tuple[DiagramContainer, ...]:
    result = []
    for container in containers:
        stripped = container.source_text.strip()
        if not (
            container.owner_kind == "node"
            and len(stripped) == 1
            and "\u3400" <= stripped <= "\u9fff"
            and container.font_size <= 3.6
            and _width(container.source_bbox) <= max(4.5, container.font_size * 1.4)
            and _height(container.source_bbox) <= container.font_size * 1.8
        ):
            result.append(container)
            continue
        center_x = (container.source_bbox[0] + container.source_bbox[2]) / 2.0
        center_y = (container.source_bbox[1] + container.source_bbox[3]) / 2.0
        allowed_width = max(24.0, container.font_size * 10.0)
        allowed_height = max(_height(container.source_bbox) + 2.0, container.font_size * 2.0)
        allowed = _clip_rect(
            (
                center_x - allowed_width / 2.0,
                center_y - allowed_height / 2.0,
                center_x + allowed_width / 2.0,
                center_y + allowed_height / 2.0,
            ),
            page_width,
            page_height,
        )
        result.append(
            replace(
                container,
                allowed_bbox=_round_rect(allowed),
                role="map_anchor_label",
                alignment="CENTER",
            )
        )
    return tuple(result)


def _map_image_title_frame(
    source_bbox: Rect,
    image_objects,
    *,
    coordinate_region: Rect | None,
    page_width: float,
    page_height: float,
) -> Rect | None:
    if coordinate_region is None or not _center_inside(source_bbox, coordinate_region, tolerance=8.0):
        return None
    page_area = page_width * page_height
    candidates = []
    for image in image_objects:
        frame = tuple(float(value) for value in image.bbox)
        if (
            20.0 <= _width(frame) <= page_width * 0.40
            and 10.0 <= _height(frame) <= page_height * 0.16
            and _area(frame) <= page_area * 0.05
            and _width(frame) >= _width(source_bbox) + 2.0
            and _height(frame) >= _height(source_bbox) + 2.0
            and _coverage(source_bbox, frame) >= 0.92
            and _center_inside(source_bbox, frame, tolerance=0.0)
        ):
            candidates.append(frame)
    return min(candidates, key=_area, default=None)


def _container_uses_map_coordinates(
    container: DiagramContainer,
    coordinate_region: Rect | None,
    page_height: float | None,
) -> bool:
    if container.owner_kind == "node":
        return True
    if coordinate_region is None:
        return container.owner_id.startswith("map-label-") or container.role == "connector_label"
    center = (
        (container.source_bbox[0] + container.source_bbox[2]) / 2.0,
        (container.source_bbox[1] + container.source_bbox[3]) / 2.0,
    )
    if page_height is not None and center[1] >= page_height * 0.90:
        return False
    if not _point_in_rect(center, coordinate_region, tolerance=0.0):
        return False
    if container.role in {"image_framed_label", "map_anchor_label"}:
        return False
    if container.role == "title":
        return _width(container.source_bbox) <= _width(coordinate_region) * 0.35
    if container.role == "independent_paragraph":
        return _width(container.source_bbox) <= _width(coordinate_region) * 0.35
    return True


def is_coordinate_locked_container(template: DiagramTemplate, container: DiagramContainer) -> bool:
    if template.layout_strategy != "MAP_COORDINATE_LOCKED":
        return False
    if not template.nodes:
        return True
    region = _coordinate_locked_region(template.nodes, template.width, template.height)
    return _container_uses_map_coordinates(container, region, template.height)


def _node_safe_bbox(boundary: Rect, source: Rect) -> Rect:
    inset = min(2.5, max(1.0, min(_width(boundary), _height(boundary)) * 0.055))
    safe = (boundary[0] + inset, boundary[1] + inset, boundary[2] - inset, boundary[3] - inset)
    if _area(safe) <= 1:
        safe = boundary
    if source != boundary:
        safe = (
            max(boundary[0] + 0.5, min(safe[0], source[0] - 0.5)),
            max(boundary[1] + 0.5, min(safe[1], source[1] - 0.5)),
            min(boundary[2] - 0.5, max(safe[2], source[2] + 0.5)),
            min(boundary[3] - 0.5, max(safe[3], source[3] + 0.5)),
        )
    return safe


def _diagram_bbox(details: list[_DrawingDetail], width: float, height: float) -> Rect | None:
    page_area = width * height
    rects = [item.bbox for item in details if 4.0 <= _area(item.bbox) <= page_area * 0.75]
    return _union(rects) if rects else None


def _required_literals(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            match.group(0)
            for match in _REQUIRED_LITERAL.finditer(text)
            if not _ROMAN_ENUMERATION.fullmatch(match.group(0))
        )
    )


def _recover_source_text_objects(
    source_pdf: Path,
    page_index: int,
    text_objects: tuple[TextObjectFact, ...],
) -> tuple[TextObjectFact, ...]:
    contexts: list[tuple[str, str]] = []
    with fitz.open(source_pdf) as document:
        for font in document.get_page_fonts(page_index, full=True):
            xref = int(font[0])
            basefont = str(font[3])
            encoding = str(font[5])
            collection = _standard_cid_collection(basefont, encoding)
            if not collection or xref <= 0:
                continue
            key_type, _ = document.xref_get_key(xref, "ToUnicode")
            if key_type != "null":
                continue
            contexts.append((_strip_pdf_subset_prefix(basefont), collection))

    trusted_counts = Counter(
        token
        for item in text_objects
        if not any(
            _pdf_font_names_match(item.font_name, basefont)
            for basefont, _ in contexts
        )
        for token in re.findall(r"(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])", item.text)
    )
    trusted_ascii_tokens = {
        token for token, count in trusted_counts.items() if count >= 2
    }

    recovered = []
    for item in text_objects:
        collection = next(
            (
                value
                for basefont, value in contexts
                if _pdf_font_names_match(item.font_name, basefont)
            ),
            None,
        )
        text = _recover_visible_text(
            item.text,
            item.font_name,
            collection,
            trusted_ascii_tokens,
        )
        recovered.append(replace(item, text=text) if text != item.text else item)
    return tuple(recovered)


def _standard_cid_collection(basefont: str, encoding: str) -> str | None:
    value = f"{basefont} {encoding}".casefold()
    collections = (
        (("gbk", "unigb", "gbpc", "-gb-", "euc-cn"), "Adobe-GB1"),
        (("unicns", "eten", "big5", "b5pc", "cns1"), "Adobe-CNS1"),
        (("unijis", "rksj", "90ms", "90msp", "japan1"), "Adobe-Japan1"),
        (("uniks", "ksc", "uhc", "korea1"), "Adobe-Korea1"),
    )
    return next((collection for tokens, collection in collections if any(token in value for token in tokens)), None)


def _strip_pdf_subset_prefix(font_name: str) -> str:
    value = font_name.lstrip("/")
    return value.split("+", 1)[1] if "+" in value else value


def _pdf_font_names_match(span_font: str, basefont: str) -> bool:
    span = _strip_pdf_subset_prefix(span_font).casefold().rstrip("-")
    base = _strip_pdf_subset_prefix(basefont).casefold().rstrip("-")
    return bool(span) and (span in base or base in span)


def _recover_visible_text(
    text: str,
    font_name: str,
    cid_collection: str | None = None,
    trusted_ascii_tokens: set[str] | frozenset[str] = frozenset(),
) -> str:
    if not cid_collection:
        return text
    unicode_map = CMapDB.get_unicode_map(cid_collection)
    recovered = []
    mapped = 0
    for part in re.split(r"([A-Za-z][A-Za-z0-9]{1,})", text):
        if part in trusted_ascii_tokens:
            recovered.append(part)
            continue
        for char in part:
            if char.isspace() and char != "\x01":
                recovered.append(char)
                continue
            try:
                value = unicode_map.get_unichr(ord(char))
            except KeyError:
                value = char
            else:
                mapped += 1
            recovered.append(value)
    return "".join(recovered) if mapped else text


def _joined_text(objects: tuple[TextObjectFact, ...]) -> str:
    return _joined_visual_text(objects)


def _needs_space(left: str, right: str) -> bool:
    return (left.isascii() and left.isalnum()) and (right.isascii() and right.isalnum())


def _has_semantic_text(text: str) -> bool:
    return any(char.isalpha() or "\u3400" <= char <= "\u9fff" for char in text)


def _alignment(source: Rect, width: float, role: str) -> str:
    return "LEFT"


def _nearest_node(point: Point, nodes: tuple[DiagramNode, ...], tolerance: float = 9.0) -> str | None:
    ranked = sorted((_point_rect_distance(point, node.boundary_bbox), node.node_id) for node in nodes)
    return ranked[0][1] if ranked and ranked[0][0] <= tolerance else None


def _point_rect_distance(point: Point, rect: Rect) -> float:
    dx = max(rect[0] - point[0], 0.0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0.0, point[1] - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def _point_in_rect(point: Point, rect: Rect, tolerance: float = 0.0) -> bool:
    return rect[0] - tolerance <= point[0] <= rect[2] + tolerance and rect[1] - tolerance <= point[1] <= rect[3] + tolerance


def _point(value: object) -> Point:
    point = fitz.Point(value)
    return (float(point.x), float(point.y))


def _distance(left: Point, right: Point) -> float:
    return ((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2) ** 0.5


def _center_inside(inner: Rect, outer: Rect, tolerance: float = 0.0) -> bool:
    center = ((inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2)
    return _point_in_rect(center, outer, tolerance)


def _coverage(inner: Rect, outer: Rect) -> float:
    return _intersection_area(inner, outer) / max(0.001, _area(inner))


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _iou(left: Rect, right: Rect) -> float:
    intersection = _intersection_area(left, right)
    return intersection / max(0.001, _area(left) + _area(right) - intersection)


def _intersection(rects: list[Rect]) -> Rect:
    return (max(item[0] for item in rects), max(item[1] for item in rects), min(item[2] for item in rects), min(item[3] for item in rects))


def _union(rects: list[Rect] | tuple[Rect, ...]) -> Rect:
    return (min(item[0] for item in rects), min(item[1] for item in rects), max(item[2] for item in rects), max(item[3] for item in rects))


def _clip_rect(rect: Rect, width: float, height: float) -> Rect:
    return (max(0.0, rect[0]), max(0.0, rect[1]), min(width, rect[2]), min(height, rect[3]))


def _axis_overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _rect_gap(left: Rect, right: Rect) -> float:
    dx = max(left[0] - right[2], right[0] - left[2], 0.0)
    dy = max(left[1] - right[3], right[1] - left[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _width(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0])


def _height(rect: Rect) -> float:
    return max(0.0, rect[3] - rect[1])


def _area(rect: Rect) -> float:
    return _width(rect) * _height(rect)


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]


def _round_point(point: Point) -> Point:
    return (round(float(point[0]), 4), round(float(point[1]), 4))
