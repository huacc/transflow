from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import Path

from page_toolbox_puncture.contracts import PageFacts
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.diagram.tools.models import DiagramConnector, DiagramTemplate
from toolboxes.body.diagram.tools.template_builder import (
    DiagramCapabilityError,
    build_diagram_template,
)
from toolboxes.body.flow_text.multi.tools.models import ColumnAssignment, MultiColumnTemplate
from toolboxes.body.flow_text.multi.tools.template_builder import (
    _translatable_margin_containers,
    build_multi_column_template_with_repairs,
)
from toolboxes.body.flow_text.single.tools.models import SingleColumnTemplate, TextContainer
from toolboxes.body.flow_text.single.tools.template_builder import (
    build_p4_page_template,
    build_page_template,
)

from .. import TOOLBOX_KEY
from .models import CompositeContainer, CompositePageTemplate, ObjectOwnership, Rect


class CompositeCapabilityError(ValueError):
    pass


_CODE_LITERAL = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2,6}(?=\s*[\u3400-\u9fff])"
    r"|(?<![A-Za-z0-9])(?=[A-Z0-9/-]*\d)(?=[A-Z0-9/-]*[A-Z])[A-Z][A-Z0-9/-]{1,}(?![A-Za-z0-9])"
    r"|(?<!\d)[+\-$¥￥€£]?\d(?:[\d,]*\d)?(?:\.\d+)?\+?(?:%|％)?(?!\d)"
)
_PAGE_MARKER = re.compile(r"^(?:(?:page|p\.?)[ ]*)?\d+(?:[ /.-](?:of[ ])?\d+)*$", re.IGNORECASE)
_URL = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
_ENUMERATION_MARKER = re.compile(
    r"^(?:\(?[IVXLCDM]+\)?|\(?[A-Z]\)?|\(?\d+(?:\.\d+)*\)?)[.)、．]?$",
    re.IGNORECASE,
)
_BULLET_MARKERS = {"\uf0b7", "\uf0d8", "→", "•", "●", "▪", "◦", "‣", "·", "‧", "∙"}


def build_composite_template(
    source_pdf: Path,
    facts: PageFacts,
    *,
    target_language: str,
) -> CompositePageTemplate:
    """Build one immutable ownership map before any translation or layout work."""

    try:
        diagram_full = build_diagram_template(facts, source_pdf)
    except DiagramCapabilityError as exc:
        raise CompositeCapabilityError(f"P14_CAPABILITY:{exc}") from exc

    diagram_region, structural_connectors = _structural_diagram_region(diagram_full, facts)
    diagram_rows = tuple(
        container
        for container in diagram_full.containers
        if _diagram_owned(container, diagram_region, facts)
    )
    if not diagram_rows:
        raise CompositeCapabilityError("DIAGRAM_REGION_HAS_NO_NATIVE_TEXT")

    diagram_object_ids = {
        object_id
        for container in diagram_rows
        for object_id in container.source_object_ids
    }
    flow_objects = tuple(
        item for item in facts.text_objects if item.object_id not in diagram_object_ids
    )
    if not flow_objects:
        raise CompositeCapabilityError("FLOW_REGION_HAS_NO_NATIVE_TEXT")
    flow_facts = replace(
        facts,
        native_text_object_count=len(flow_objects),
        text_objects=flow_objects,
        text_objects_sha256=None,
    )
    flow_mode, flow_template = _build_flow_template(flow_facts)
    if not flow_template.containers:
        raise CompositeCapabilityError("FLOW_REGION_HAS_NO_TRANSLATABLE_CONTAINER")

    filtered_diagram = _filtered_diagram_template(
        diagram_full,
        diagram_rows,
        structural_connectors,
        facts,
    )
    rows: list[tuple[str, str, tuple[str, ...], str, Rect, Rect, tuple[str, ...], str]] = []
    for container in flow_template.containers:
        owner = _flow_owner(container, diagram_region, facts)
        allowed = _flow_allowed_bbox(
            container,
            flow_template,
            diagram_region,
            facts,
        )
        rows.append(
            (
                owner,
                container.container_id,
                container.source_object_ids,
                container.source_text,
                container.source_bbox,
                allowed,
                _required_literals_with_marker(container.source_text),
                container.role,
            )
        )
    for container in filtered_diagram.containers:
        rows.append(
            (
                "diagram",
                container.container_id,
                container.source_object_ids,
                container.source_text,
                container.source_bbox,
                container.allowed_bbox,
                container.required_literals,
                container.role,
            )
        )
    rows.sort(key=lambda item: (item[4][1], item[4][0], _owner_order(item[0]), item[1]))

    containers = tuple(
        CompositeContainer(
            composite_id=f"{owner}::{base_id}",
            owner=owner,
            base_container_id=base_id,
            source_object_ids=source_ids,
            source_text=source_text,
            source_bbox=_round_rect(source_bbox),
            allowed_bbox=_round_rect(allowed_bbox),
            reading_order=index,
            required_literals=required_literals,
            role=role,
        )
        for index, (
            owner,
            base_id,
            source_ids,
            source_text,
            source_bbox,
            allowed_bbox,
            required_literals,
            role,
        ) in enumerate(rows)
    )
    claimed: dict[str, CompositeContainer] = {}
    for container in containers:
        for object_id in container.source_object_ids:
            if object_id in claimed:
                raise CompositeCapabilityError(f"DUPLICATE_SOURCE_OBJECT_OWNER:{object_id}")
            claimed[object_id] = container

    all_ids = {item.object_id for item in facts.text_objects}
    unknown_ids = set(claimed) - all_ids
    if unknown_ids:
        raise CompositeCapabilityError(f"UNKNOWN_SOURCE_OBJECT:{sorted(unknown_ids)[0]}")
    protected_ids = tuple(item.object_id for item in facts.text_objects if item.object_id not in claimed)
    ownerships = tuple(
        ObjectOwnership(
            object_id=item.object_id,
            owner=claimed[item.object_id].owner if item.object_id in claimed else "protected",
            container_id=claimed[item.object_id].composite_id if item.object_id in claimed else None,
        )
        for item in facts.text_objects
    )
    if {item.object_id for item in ownerships} != all_ids or len(ownerships) != len(all_ids):
        raise CompositeCapabilityError("OBJECT_OWNERSHIP_NOT_EXHAUSTIVE")

    topology_sha256 = canonical_sha256(
        {
            "diagram_geometry_sha256": filtered_diagram.diagram_geometry_sha256,
            "nodes": filtered_diagram.nodes,
            "connectors": filtered_diagram.connectors,
            "diagram_region": diagram_region,
        }
    )
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "flow_mode": flow_mode,
            "topology_sha256": topology_sha256,
            "containers": [
                (
                    item.composite_id,
                    item.owner,
                    item.source_object_ids,
                    item.source_bbox,
                    item.allowed_bbox,
                )
                for item in containers
            ],
            "protected_object_ids": protected_ids,
        }
    )
    return CompositePageTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        flow_mode=flow_mode,
        flow_template=flow_template,
        diagram_template=filtered_diagram,
        diagram_region=_round_rect(diagram_region),
        containers=containers,
        ownerships=ownerships,
        protected_object_ids=protected_ids,
        topology_sha256=topology_sha256,
        structure_sha256=structure_sha256,
    )


def _build_flow_template(facts: PageFacts) -> tuple[str, SingleColumnTemplate | MultiColumnTemplate]:
    try:
        multi, _ = build_multi_column_template_with_repairs(facts)
        multi = _normalize_flow_template(multi, facts)
        return "multi", _filter_protected_flow_containers(multi)
    except (ValueError, IndexError, ZeroDivisionError):
        filtered = build_p4_page_template(facts)
        if filtered.containers:
            filtered = _detach_mixed_margin_markers(filtered, facts)
            filtered = _normalize_flow_template(filtered, facts)
            return "single", _filter_protected_flow_containers(filtered)
        fallback = build_page_template(facts)
        if fallback.containers:
            fallback = _detach_mixed_margin_markers(fallback, facts)
            fallback = _normalize_flow_template(fallback, facts)
            return "single", _filter_protected_flow_containers(fallback)
        raise CompositeCapabilityError("FLOW_TEMPLATE_EMPTY")


def _detach_mixed_margin_markers(
    template: SingleColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate:
    source_by_id = {item.object_id: item for item in facts.text_objects}
    output = []
    for container in template.containers:
        objects = [
            source_by_id[object_id]
            for object_id in container.source_object_ids
            if object_id in source_by_id
        ]
        protected = [
            item for item in objects if _detachable_mixed_margin_marker(item.text)
        ]
        if container.role != "margin" or not protected or len(protected) == len(objects):
            output.append(container)
            continue
        output.extend(_translatable_margin_containers(container, facts))
    return replace(template, containers=tuple(output))


def _normalize_flow_template(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    template = _restore_bottom_body_continuations(template)
    template = _split_protected_heading_fragments(template, facts)
    template = _merge_single_body_block_fragments(template, facts)
    template = _restore_semantic_edge_text(template, facts)
    template = _merge_edge_margin_fragments(template)
    template = _reattach_margin_years(template, facts)
    template = _attach_adjacent_heading_markers(template, facts)
    return replace(
        template,
        containers=tuple(
            replace(item, source_text=_normalize_leading_bullet(item.source_text))
            for item in template.containers
        ),
    )


def _split_protected_heading_fragments(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    source_by_id = {item.object_id: item for item in facts.text_objects}
    containers: list[TextContainer] = []
    split_ids: dict[str, tuple[str, ...]] = {}
    for container in template.containers:
        objects = sorted(
            (
                source_by_id[object_id]
                for object_id in container.source_object_ids
                if object_id in source_by_id
            ),
            key=lambda item: (item.block_index, item.line_index, item.span_index),
        )
        if len(objects) < 2:
            containers.append(container)
            continue
        roles: list[str]
        if container.role == "body":
            leading = _leading_heading_body_groups(objects)
            if leading is None:
                containers.append(container)
                continue
            groups = [list(group) for group in leading]
            roles = ["heading", "body"]
        elif container.role in {"heading", "title"}:
            groups = [[objects[0]]]
            for item in objects[1:]:
                previous = groups[-1][-1]
                protected_gap = (
                    item.block_index == previous.block_index
                    and item.line_index == previous.line_index
                    and item.span_index > previous.span_index + 1
                    and item.bbox[0] - previous.bbox[2]
                    >= min(item.font_size, previous.font_size) * 0.5
                )
                if protected_gap:
                    groups.append([item])
                else:
                    groups[-1].append(item)
            roles = [container.role] * len(groups)
        else:
            containers.append(container)
            continue
        if len(groups) < 2:
            containers.append(container)
            continue
        ids = []
        for index, group in enumerate(groups):
            container_id = (
                container.container_id
                if index == 0
                else f"{container.container_id}-segment-{index:02d}"
            )
            ids.append(container_id)
            bbox = _union(tuple(item.bbox for item in group))
            representative = max(
                group,
                key=lambda item: (item.font_size, len(item.text), -item.span_index),
            )
            containers.append(
                replace(
                    container,
                    container_id=container_id,
                    source_object_ids=tuple(item.object_id for item in group),
                    source_text=_merge_semantic_objects(group),
                    source_bbox=_round_rect(bbox),
                    anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                    font_size=round(max(item.font_size for item in group), 4),
                    color_srgb=representative.color_srgb,
                    font_weight=_object_font_weight(group),
                    role=roles[index],
                )
            )
        split_ids[container.container_id] = tuple(ids)
    if not split_ids:
        return template
    reindexed = _reindex_flow_containers(tuple(containers))
    if not isinstance(template, MultiColumnTemplate):
        return replace(template, containers=reindexed)
    assignments = []
    for assignment in template.assignments:
        for container_id in split_ids.get(
            assignment.container_id,
            (assignment.container_id,),
        ):
            assignments.append(
                replace(
                    assignment,
                    container_id=container_id,
                    column_reading_order=len(assignments),
                )
            )
    ambiguous = tuple(
        container_id
        for original in template.ambiguous_spanning_container_ids
        for container_id in split_ids.get(original, (original,))
    )
    return replace(
        template,
        containers=reindexed,
        assignments=tuple(assignments),
        ambiguous_spanning_container_ids=ambiguous,
    )


def _leading_heading_body_groups(objects):
    if len({item.block_index for item in objects}) != 1:
        return None
    first_line_index = min(item.line_index for item in objects)
    heading = [item for item in objects if item.line_index == first_line_index]
    body = [item for item in objects if item.line_index != first_line_index]
    if not heading or len({item.line_index for item in body}) < 2:
        return None
    heading_bbox = _union(tuple(item.bbox for item in heading))
    body_bbox = _union(tuple(item.bbox for item in body))
    total_bbox = _union((heading_bbox, body_bbox))
    heading_text = _merge_semantic_objects(heading).strip()
    heading_fonts = {item.font_name for item in heading}
    body_fonts = {item.font_name for item in body}
    gap = body_bbox[1] - heading_bbox[3]
    if (
        not heading_text
        or len(heading_text) > 80
        or heading_bbox[2] - heading_bbox[0]
        > (total_bbox[2] - total_bbox[0]) * 0.55
        or heading_fonts == body_fonts
        or min(item.font_size for item in heading)
        < max(item.font_size for item in body) * 0.9
        or gap > max(item.font_size for item in heading) * 0.75
    ):
        return None
    return tuple(heading), tuple(body)


def _merge_single_body_block_fragments(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    if not isinstance(template, SingleColumnTemplate):
        return template
    source_by_id = {item.object_id: item for item in facts.text_objects}
    by_family: dict[str, list[TextContainer]] = {}
    for container in template.containers:
        family = _block_family(container.container_id)
        if container.role in {"body", "heading"} and family is not None:
            by_family.setdefault(family, []).append(container)

    replacements: dict[str, TextContainer] = {}
    removed: set[str] = set()
    for members in by_family.values():
        if any("-segment-" in item.container_id for item in members):
            continue
        heading_fragments = _same_style_heading_fragments(members, source_by_id)
        has_heading = any(item.role == "heading" for item in members)
        if (
            len(members) < 2
            or (has_heading and not heading_fragments)
            or (not has_heading and not _vertically_connected_fragments(members))
        ):
            continue
        objects = [
            source_by_id[object_id]
            for member in members
            for object_id in member.source_object_ids
            if object_id in source_by_id
        ]
        if not objects:
            continue
        bullets = [item for item in objects if _is_bullet_marker(item.text)]
        if len(bullets) > 1:
            continue
        representative = min(
            members,
            key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id),
        )
        ordered_objects = sorted(
            {item.object_id: item for item in objects}.values(),
            key=lambda item: (item.bbox[1], item.bbox[0], item.object_id),
        )
        if bullets:
            first_text = min(
                (item for item in ordered_objects if item.object_id != bullets[0].object_id),
                key=lambda item: (item.bbox[1], item.bbox[0]),
            )
            if not (
                bullets[0].bbox[0] < first_text.bbox[0]
                and _vertical_overlap(bullets[0].bbox, first_text.bbox) > 0.5
            ):
                continue
        bbox = _union(tuple(item.bbox for item in ordered_objects))
        replacements[representative.container_id] = replace(
            representative,
            source_object_ids=tuple(item.object_id for item in ordered_objects),
            source_text=_merge_semantic_objects(ordered_objects),
            source_bbox=_round_rect(bbox),
            anchor=(round(bbox[0], 4), round(bbox[1], 4)),
            font_size=round(max(item.font_size for item in objects), 4),
            font_weight=_object_font_weight(objects),
            role="heading" if heading_fragments else representative.role,
        )
        removed.update(
            item.container_id
            for item in members
            if item.container_id != representative.container_id
        )

    merged = tuple(
        replacements.get(item.container_id, item)
        for item in template.containers
        if item.container_id not in removed
    )
    return replace(template, containers=_reindex_flow_containers(merged))


def _same_style_heading_fragments(
    members: list[TextContainer],
    source_by_id: dict[str, object],
) -> bool:
    if (
        len(members) < 2
        or not any(item.role == "heading" for item in members)
        or any(item.role not in {"body", "heading"} for item in members)
    ):
        return False
    objects = [
        source_by_id[object_id]
        for member in members
        for object_id in member.source_object_ids
        if object_id in source_by_id
    ]
    if not objects or any(_is_bullet_marker(item.text) for item in objects):
        return False
    font_sizes = [item.font_size for item in objects]
    if (
        max(font_sizes) - min(font_sizes) > max(font_sizes) * 0.05
        or len({item.font_name for item in objects}) != 1
        or len({item.color_srgb for item in objects}) != 1
        or len({item.block_index for item in objects}) != 1
    ):
        return False
    ordered = sorted(members, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
    for previous, current in zip(ordered, ordered[1:]):
        gap = current.source_bbox[1] - previous.source_bbox[3]
        overlap = _horizontal_overlap(previous.source_bbox, current.source_bbox)
        minimum_width = min(
            previous.source_bbox[2] - previous.source_bbox[0],
            current.source_bbox[2] - current.source_bbox[0],
        )
        if gap > max(font_sizes) * 0.5 or overlap < minimum_width * 0.5:
            return False
    return True


def _vertically_connected_fragments(members: list[TextContainer]) -> bool:
    ordered = sorted(members, key=lambda item: (item.source_bbox[1], item.source_bbox[0]))
    bottom = ordered[0].source_bbox[3]
    overlap_found = False
    for member in ordered[1:]:
        if member.source_bbox[1] > bottom + 0.5:
            return False
        overlap_found = overlap_found or member.source_bbox[1] < bottom - 0.5
        bottom = max(bottom, member.source_bbox[3])
    return overlap_found


def _restore_semantic_edge_text(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    claimed = {
        object_id for container in template.containers for object_id in container.source_object_ids
    }
    candidates = [
        item
        for item in facts.text_objects
        if item.object_id not in claimed
        and (item.bbox[1] <= facts.height * 0.15 or item.bbox[3] >= facts.height * 0.85)
        and item.text.strip()
        and any(character.isprintable() for character in item.text)
    ]
    rows: list[list] = []
    for item in sorted(candidates, key=lambda value: (value.bbox[1], value.bbox[0])):
        if rows and _same_edge_object_row(rows[-1], item):
            rows[-1].append(item)
        else:
            rows.append([item])

    additions: list[TextContainer] = []
    for row in rows:
        segments: list[list] = []
        for item in sorted(row, key=lambda value: value.bbox[0]):
            if not segments:
                segments.append([item])
                continue
            previous = segments[-1][-1]
            gap = item.bbox[0] - previous.bbox[2]
            threshold = max(8.0, min(previous.font_size, item.font_size) * 1.6)
            if gap <= threshold:
                segments[-1].append(item)
            else:
                segments.append([item])
        for segment in segments:
            semantic_segment = [
                item for item in segment if not _detachable_mixed_margin_marker(item.text)
            ]
            if semantic_segment:
                segment = semantic_segment
            text = "".join(item.text for item in segment).strip()
            compact = re.sub(r"\s+", "", text)
            if (
                not _semantic_shared_text(text)
                or _PAGE_MARKER.fullmatch(compact)
                or _URL.fullmatch(text)
                or _ENUMERATION_MARKER.fullmatch(compact)
            ):
                continue
            bbox = _union(tuple(item.bbox for item in segment))
            representative = max(
                segment,
                key=lambda item: (item.font_size, len(item.text), -item.span_index),
            )
            additions.append(
                TextContainer(
                    container_id=f"p18-edge-margin-{len(additions):03d}",
                    source_object_ids=tuple(item.object_id for item in segment),
                    source_text=text,
                    reading_order=len(template.containers) + len(additions),
                    role="margin",
                    source_bbox=_round_rect(bbox),
                    anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                    font_size=round(max(item.font_size for item in segment), 4),
                    color_srgb=representative.color_srgb,
                    font_weight=_object_font_weight(segment),
                )
            )
    if not additions:
        return template
    containers = _reindex_flow_containers((*template.containers, *additions))
    if not isinstance(template, MultiColumnTemplate):
        return replace(template, containers=containers)
    assignments = [*template.assignments]
    for addition in additions:
        assignments.append(
            ColumnAssignment(
                container_id=addition.container_id,
                column_id="margin",
                column_reading_order=len(assignments),
            )
        )
    return replace(
        template,
        containers=containers,
        assignments=tuple(assignments),
    )


def _merge_edge_margin_fragments(
    template: SingleColumnTemplate | MultiColumnTemplate,
) -> SingleColumnTemplate | MultiColumnTemplate:
    containers = list(template.containers)
    removed: set[str] = set()
    for index, first in enumerate(containers):
        if first.container_id in removed or not _edge_margin(first, template.height):
            continue
        current = first
        while _continues_edge_phrase(current.source_text):
            candidates = [
                (other_index, other)
                for other_index, other in enumerate(containers)
                if other_index != index
                and other.container_id not in removed
                and _edge_margin(other, template.height)
                and _same_page_edge(current, other, template.height)
                and other.source_bbox[1] > current.source_bbox[1]
                and _horizontal_overlap(current.source_bbox, other.source_bbox)
                >= min(
                    current.source_bbox[2] - current.source_bbox[0],
                    other.source_bbox[2] - other.source_bbox[0],
                )
                * 0.5
                and (
                    _vertical_overlap(current.source_bbox, other.source_bbox) > 0.5
                    or abs(
                        _vertical_center(current.source_bbox)
                        - _vertical_center(other.source_bbox)
                    )
                    <= max(current.font_size, other.font_size) * 2.0
                )
            ]
            if not candidates:
                break
            other_index, other = min(
                candidates,
                key=lambda row: (
                    abs(_vertical_center(current.source_bbox) - _vertical_center(row[1].source_bbox)),
                    row[1].source_bbox[0],
                ),
            )
            bbox = _union((current.source_bbox, other.source_bbox))
            current = replace(
                current,
                source_object_ids=current.source_object_ids + other.source_object_ids,
                source_text=current.source_text.rstrip() + " " + other.source_text.lstrip(),
                source_bbox=_round_rect(bbox),
                anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                font_size=round(max(current.font_size, other.font_size), 4),
            )
            containers[index] = current
            removed.add(other.container_id)
    if not removed:
        return template
    kept = _reindex_flow_containers(
        tuple(item for item in containers if item.container_id not in removed)
    )
    if not isinstance(template, MultiColumnTemplate):
        return replace(template, containers=kept)
    return replace(
        template,
        containers=kept,
        assignments=tuple(
            item for item in template.assignments if item.container_id not in removed
        ),
        ambiguous_spanning_container_ids=tuple(
            item for item in template.ambiguous_spanning_container_ids if item not in removed
        ),
    )


def _restore_bottom_body_continuations(
    template: SingleColumnTemplate | MultiColumnTemplate,
) -> SingleColumnTemplate | MultiColumnTemplate:
    containers = list(template.containers)
    removed: set[str] = set()
    for margin in sorted(containers, key=lambda item: item.source_bbox[1]):
        if margin.role != "margin" or margin.source_bbox[1] < template.height * 0.88:
            continue
        family = _block_family(margin.container_id)
        if family is None:
            continue
        candidates = [
            (index, item)
            for index, item in enumerate(containers)
            if item.container_id not in removed
            and item.role == "body"
            and _block_family(item.container_id) == family
            and item.source_bbox[3] <= margin.source_bbox[1] + 0.01
            and margin.source_bbox[1] - item.source_bbox[3]
            <= max(6.0, item.font_size * 0.75)
            and abs(margin.source_bbox[0] - item.source_bbox[0])
            <= max(2.0, item.font_size * 0.75)
            and not item.source_text.rstrip().endswith(("。", "！", "？", ".", "!", "?", ";", "；"))
        ]
        if not candidates:
            continue
        index, previous = max(candidates, key=lambda item: item[1].source_bbox[3])
        separator = (
            " "
            if previous.source_text[-1:].isascii()
            and previous.source_text[-1:].isalnum()
            and margin.source_text[:1].isascii()
            and margin.source_text[:1].isalnum()
            else ""
        )
        bbox = _union((previous.source_bbox, margin.source_bbox))
        containers[index] = replace(
            previous,
            source_object_ids=previous.source_object_ids + margin.source_object_ids,
            source_text=previous.source_text.rstrip() + separator + margin.source_text.lstrip(),
            source_bbox=_round_rect(bbox),
            anchor=(round(bbox[0], 4), round(bbox[1], 4)),
        )
        removed.add(margin.container_id)
    return replace(
        template,
        containers=tuple(item for item in containers if item.container_id not in removed),
    )


def _reattach_margin_years(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    containers = list(template.containers)
    claimed = {
        object_id for container in containers for object_id in container.source_object_ids
    }
    years = [
        item
        for item in facts.text_objects
        if item.object_id not in claimed
        and re.fullmatch(r"(?:19|20)\d{2}", item.text.strip())
    ]
    for year in years:
        candidates = [
            (index, container)
            for index, container in enumerate(containers)
            if container.role == "margin"
            and (
                container.source_bbox[1] <= template.height * 0.15
                or container.source_bbox[3] >= template.height * 0.85
            )
            and _vertical_overlap(container.source_bbox, year.bbox) > 0.5
            and max(
                0.0,
                max(container.source_bbox[0], year.bbox[0])
                - min(container.source_bbox[2], year.bbox[2]),
            )
            <= max(container.font_size, year.font_size)
        ]
        if not candidates:
            continue
        index, container = min(
            candidates,
            key=lambda item: abs(
                (item[1].source_bbox[0] + item[1].source_bbox[2]) / 2.0
                - (year.bbox[0] + year.bbox[2]) / 2.0
            ),
        )
        year_first = year.bbox[0] <= container.source_bbox[0]
        source_ids = (
            (year.object_id,) + container.source_object_ids
            if year_first
            else container.source_object_ids + (year.object_id,)
        )
        source_text = (
            f"{year.text.strip()} {container.source_text}"
            if year_first
            else f"{container.source_text} {year.text.strip()}"
        )
        bbox = _union((container.source_bbox, year.bbox))
        containers[index] = replace(
            container,
            source_object_ids=source_ids,
            source_text=source_text,
            source_bbox=_round_rect(bbox),
            anchor=(round(bbox[0], 4), round(bbox[1], 4)),
            font_size=round(max(container.font_size, year.font_size), 4),
        )
    return replace(template, containers=tuple(containers))


def _attach_adjacent_heading_markers(
    template: SingleColumnTemplate | MultiColumnTemplate,
    facts: PageFacts,
) -> SingleColumnTemplate | MultiColumnTemplate:
    already_attached = {
        object_id
        for container in template.containers
        if container.preserved_prefix
        for object_id in container.source_object_ids
    }
    markers = [
        item
        for item in facts.text_objects
        if item.object_id not in already_attached
        and _ENUMERATION_MARKER.fullmatch(item.text.strip())
    ]
    used: set[str] = set()
    containers = []
    for container in template.containers:
        if container.role not in {"heading", "title"} or container.preserved_prefix:
            containers.append(container)
            continue
        candidates = []
        for marker in markers:
            if marker.object_id in used or marker.bbox[2] > container.source_bbox[0] + 1.0:
                continue
            gap = container.source_bbox[0] - marker.bbox[2]
            if gap > max(36.0, container.font_size * 2.5):
                continue
            overlap = _vertical_overlap(marker.bbox, container.source_bbox)
            center_gap = abs(_vertical_center(marker.bbox) - _vertical_center(container.source_bbox))
            if overlap <= 0.5 and center_gap > max(marker.font_size, container.font_size) * 0.75:
                continue
            candidates.append((gap, center_gap, marker))
        if not candidates:
            containers.append(container)
            continue
        marker = min(candidates, key=lambda item: (item[0], item[1]))[2]
        used.add(marker.object_id)
        containers.append(
            replace(
                container,
                source_object_ids=(marker.object_id,) + container.source_object_ids,
                preserved_prefix=marker.text.strip(),
            )
        )
    return replace(template, containers=tuple(containers))


def _block_family(container_id: str) -> str | None:
    match = re.search(r"block-\d+", container_id)
    return match.group(0) if match else None


def _same_edge_object_row(row, item) -> bool:
    reference = row[0]
    overlap = _vertical_overlap(reference.bbox, item.bbox)
    minimum_height = min(
        reference.bbox[3] - reference.bbox[1],
        item.bbox[3] - item.bbox[1],
    )
    return overlap >= max(0.5, minimum_height * 0.35) or abs(
        _vertical_center(reference.bbox) - _vertical_center(item.bbox)
    ) <= max(reference.font_size, item.font_size) * 0.45


def _object_font_weight(objects) -> str:
    names = " ".join(item.font_name.casefold() for item in objects)
    return (
        "bold"
        if any(token in names for token in ("bold", "black", "heavy", "semibold", "demi"))
        else "regular"
    )


def _reindex_flow_containers(containers) -> tuple[TextContainer, ...]:
    ordered = sorted(containers, key=lambda item: (item.source_bbox[1], item.source_bbox[0], item.container_id))
    return tuple(replace(item, reading_order=index) for index, item in enumerate(ordered))


def _edge_margin(container: TextContainer, height: float) -> bool:
    return container.role == "margin" and (
        container.source_bbox[1] <= height * 0.15
        or container.source_bbox[3] >= height * 0.85
    )


def _same_page_edge(first: TextContainer, second: TextContainer, height: float) -> bool:
    return (
        first.source_bbox[1] <= height * 0.15
        and second.source_bbox[1] <= height * 0.15
    ) or (
        first.source_bbox[3] >= height * 0.85
        and second.source_bbox[3] >= height * 0.85
    )


def _continues_edge_phrase(text: str) -> bool:
    return bool(
        re.search(r"(?:\band|\bor|\bof|\bthe|&|[-–—/])\s*$", text, re.IGNORECASE)
    )


def _vertical_center(rect: Rect) -> float:
    return (rect[1] + rect[3]) / 2.0


def _filter_protected_flow_containers(
    template: SingleColumnTemplate | MultiColumnTemplate,
) -> SingleColumnTemplate | MultiColumnTemplate:
    kept = tuple(
        container
        for container in template.containers
        if not _protected_flow_text(container.source_text)
    )
    kept = tuple(replace(container, reading_order=index) for index, container in enumerate(kept))
    if isinstance(template, MultiColumnTemplate):
        kept_ids = {item.container_id for item in kept}
        assignments = tuple(item for item in template.assignments if item.container_id in kept_ids)
        active_columns = {
            item.column_id
            for item in assignments
            if item.column_id not in {"span", "fixed", "margin"}
        }
        if any(column.column_id not in active_columns for column in template.columns):
            raise ValueError("p18_protected_marker_left_empty_column")
        return replace(
            template,
            containers=kept,
            assignments=assignments,
            ambiguous_spanning_container_ids=tuple(
                item for item in template.ambiguous_spanning_container_ids if item in kept_ids
            ),
        )
    return replace(template, containers=kept)


def _protected_flow_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    return bool(
        not compact
        or not any(char.isprintable() for char in compact)
        or _PAGE_MARKER.fullmatch(compact)
        or _URL.fullmatch(text.strip())
        or _ENUMERATION_MARKER.fullmatch(compact)
        or (len(compact) >= 3 and all(not char.isalnum() for char in compact))
        or compact in {"•", "·", "▪", "■", "◆", "-", "–", "—"}
    )


def _detachable_mixed_margin_marker(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    digits = "".join(char for char in compact if char.isdigit())
    return bool(
        (_PAGE_MARKER.fullmatch(compact) and 0 < len(digits) <= 3)
        or (len(compact) >= 3 and all(not char.isalnum() for char in compact))
    )


def _structural_diagram_region(
    template: DiagramTemplate,
    facts: PageFacts,
) -> tuple[Rect, tuple[DiagramConnector, ...]]:
    connectors = tuple(
        connector
        for connector in template.connectors
        if _interior_connector(connector, facts.width, facts.height)
    )
    if template.nodes:
        node_region = _union(tuple(item.boundary_bbox for item in template.nodes))
        expanded = _expand(node_region, max(12.0, facts.height * 0.015), facts.width, facts.height)
        selected = tuple(
            connector
            for connector in connectors
            if connector.start_node_id
            or connector.end_node_id
            or _intersection_area(_connector_bbox(connector), expanded) > 0
        )
        rects = (*tuple(item.boundary_bbox for item in template.nodes), *tuple(_connector_bbox(item) for item in selected))
        return _clip(_union(rects), facts.width, facts.height), selected

    clusters: list[list[DiagramConnector]] = []
    vertical_gap = max(32.0, facts.height * 0.075)
    for connector in sorted(connectors, key=lambda item: (_connector_bbox(item)[1], _connector_bbox(item)[0])):
        bbox = _connector_bbox(connector)
        if clusters and bbox[1] <= max(_connector_bbox(item)[3] for item in clusters[-1]) + vertical_gap:
            clusters[-1].append(connector)
        else:
            clusters.append([connector])
    candidates = [
        cluster
        for cluster in clusters
        if len(cluster) >= 3
        and max(_connector_bbox(item)[3] for item in cluster)
        - min(_connector_bbox(item)[1] for item in cluster)
        >= max(15.0, facts.height * 0.02)
    ]
    if not candidates:
        raise CompositeCapabilityError("DIAGRAM_STRUCTURAL_REGION_NOT_FOUND")
    selected_list = max(
        candidates,
        key=lambda cluster: (
            len(cluster),
            sum(math.dist(item.start, item.end) for item in cluster),
        ),
    )
    selected = tuple(selected_list)
    region = _union(tuple(_connector_bbox(item) for item in selected))
    return _clip(region, facts.width, facts.height), selected


def _diagram_owned(container, region: Rect, facts: PageFacts) -> bool:
    if container.node_id:
        return True
    expanded = _expand(region, max(8.0, facts.height * 0.01), facts.width, facts.height)
    area = max(_area(container.source_bbox), 1.0)
    return _intersection_area(container.source_bbox, expanded) / area >= 0.20


def _filtered_diagram_template(
    template: DiagramTemplate,
    containers,
    connectors: tuple[DiagramConnector, ...],
    facts: PageFacts,
) -> DiagramTemplate:
    selected_ids = {item.container_id for item in containers}
    nodes = tuple(
        replace(
            node,
            container_ids=tuple(item for item in node.container_ids if item in selected_ids),
        )
        for node in template.nodes
    )
    normalized_containers, nodes = _normalize_diagram_containers(
        template,
        tuple(containers),
        nodes,
        facts,
    )
    diagram_ids = {
        object_id
        for container in normalized_containers
        for object_id in container.source_object_ids
    }
    protected = tuple(item.object_id for item in facts.text_objects if item.object_id not in diagram_ids)
    topology = canonical_sha256(
        {
            "diagram_geometry_sha256": template.diagram_geometry_sha256,
            "nodes": nodes,
            "connectors": connectors,
        }
    )
    structure = canonical_sha256(
        {
            "toolbox_key": "body.diagram",
            "topology_sha256": topology,
            "containers": normalized_containers,
            "protected_object_ids": protected,
        }
    )
    return replace(
        template,
        mode="translated",
        nodes=nodes,
        connectors=connectors,
        containers=normalized_containers,
        protected_object_ids=protected,
        topology_sha256=topology,
        structure_sha256=structure,
    )


def _normalize_diagram_containers(
    template: DiagramTemplate,
    containers: tuple,
    nodes: tuple,
    facts: PageFacts,
):
    source_by_id = {item.object_id: item for item in facts.text_objects}
    claimed_source_ids = {
        object_id
        for container in containers
        for object_id in container.source_object_ids
    }
    available_bullets = [
        item
        for item in facts.text_objects
        if item.object_id not in claimed_source_ids and _is_bullet_marker(item.text)
    ]
    used_bullets: set[str] = set()
    node_by_id = {item.node_id: item for item in nodes}
    by_node: dict[str, list] = {}
    for container in containers:
        if container.node_id:
            by_node.setdefault(container.node_id, []).append(container)

    normalized_by_node: dict[str, tuple] = {}
    updated_nodes = []
    for node in nodes:
        members = by_node.get(node.node_id, [])
        objects = [
            source_by_id[object_id]
            for member in members
            for object_id in member.source_object_ids
            if object_id in source_by_id
        ]
        if any(_is_bullet_marker(item.text) for item in objects):
            normalized = _semantic_list_containers(node, members, objects, facts)
        else:
            normalized = tuple(
                _normalize_node_container(member, node, facts)
                for member in members
            )
        normalized_by_node[node.node_id] = normalized
        updated_nodes.append(
            replace(node, container_ids=tuple(item.container_id for item in normalized))
        )

    output = []
    emitted_nodes: set[str] = set()
    for container in containers:
        if container.node_id:
            if container.node_id not in emitted_nodes:
                output.extend(normalized_by_node.get(container.node_id, ()))
                emitted_nodes.add(container.node_id)
            continue
        normalized = _normalize_independent_diagram_container(
            container,
            facts,
            source_by_id,
            tuple(
                item
                for item in available_bullets
                if item.object_id not in used_bullets
            ),
        )
        output.extend(normalized)
        used_bullets.update(
            object_id
            for item in normalized
            for object_id in item.source_object_ids
            if object_id in {bullet.object_id for bullet in available_bullets}
        )
    return (
        tuple(replace(item, reading_order=index) for index, item in enumerate(output)),
        tuple(updated_nodes),
    )


def _normalize_independent_diagram_container(
    container,
    facts,
    source_by_id,
    available_bullets,
):
    objects = [
        source_by_id[object_id]
        for object_id in container.source_object_ids
        if object_id in source_by_id
    ]
    expanded = _expand_structural_label(container, facts)
    bullets = [
        item
        for item in available_bullets
        if _contains_rect(expanded.allowed_bbox, item.bbox, tolerance=1.0)
        and item.bbox[1] >= container.source_bbox[1] - container.font_size
        and item.bbox[3] <= container.source_bbox[3] + container.font_size
    ]
    if bullets:
        semantic = _semantic_list_containers_in_frame(
            container.container_id,
            expanded.allowed_bbox,
            (expanded,),
            (*objects, *bullets),
        )
        if semantic:
            return semantic
    by_block: dict[int, list] = {}
    for item in objects:
        by_block.setdefault(item.block_index, []).append(item)
    groups = sorted(
        by_block.values(),
        key=lambda group: (_union(tuple(item.bbox for item in group))[1], group[0].block_index),
    )
    heading_bands = _heading_led_semantic_bands(expanded, groups)
    if heading_bands:
        return heading_bands
    if (
        container.role not in {"independent_label", "independent_paragraph", "connector_label"}
        or len(groups) < 2
        or not _stacked_semantic_groups(groups)
    ):
        return (expanded,)

    parts = []
    for index, group in enumerate(groups):
        ordered = sorted(group, key=lambda item: (item.bbox[1], item.bbox[0], item.object_id))
        bbox = _union(tuple(item.bbox for item in ordered))
        representative = max(
            ordered,
            key=lambda item: (item.font_size, len(item.text.strip()), -item.span_index),
        )
        text = _merge_semantic_objects(ordered)
        part = replace(
            container,
            container_id=f"{container.container_id}/part-{index:02d}",
            source_object_ids=tuple(item.object_id for item in ordered),
            source_text=text,
            source_bbox=_round_rect(bbox),
            allowed_bbox=_round_rect(bbox),
            required_literals=_required_literals(text),
            font_name=representative.font_name,
            font_size=round(max(item.font_size for item in ordered), 4),
            color_srgb=representative.color_srgb,
        )
        parts.append(_expand_structural_label(part, facts))

    adjusted = []
    for index, part in enumerate(parts):
        top = part.allowed_bbox[1]
        bottom = part.allowed_bbox[3]
        if index > 0:
            top = max(
                top,
                (parts[index - 1].source_bbox[3] + part.source_bbox[1]) / 2.0,
            )
        if index + 1 < len(parts):
            bottom = min(
                bottom,
                (part.source_bbox[3] + parts[index + 1].source_bbox[1]) / 2.0,
            )
        adjusted.append(
            replace(
                part,
                allowed_bbox=_round_rect(
                    (part.allowed_bbox[0], top, part.allowed_bbox[2], bottom)
                ),
            )
        )
    return tuple(adjusted)


def _heading_led_semantic_bands(container, groups):
    if container.role not in {"independent_label", "independent_paragraph"}:
        return ()
    heading_indexes = [
        index
        for index, group in enumerate(groups)
        if _merge_semantic_objects(group).strip().endswith((":", "："))
        and len(group) <= 2
    ]
    if len(heading_indexes) < 2 or heading_indexes[0] != 0:
        return ()

    parts = []
    for part_index, start in enumerate(heading_indexes):
        end = heading_indexes[part_index + 1] if part_index + 1 < len(heading_indexes) else len(groups)
        objects = sorted(
            (item for group in groups[start:end] for item in group),
            key=lambda item: (item.bbox[1], item.bbox[0], item.object_id),
        )
        if not objects:
            continue
        bbox = _union(tuple(item.bbox for item in objects))
        text = _merge_semantic_objects(objects)
        representative = max(
            objects,
            key=lambda item: (item.font_size, len(item.text.strip()), -item.span_index),
        )
        literals = list(_required_literals(text))
        if any(_is_bullet_marker(item.text) for item in objects):
            literals.append("•")
        parts.append(
            replace(
                container,
                container_id=f"{container.container_id}/band-{part_index:02d}",
                source_object_ids=tuple(item.object_id for item in objects),
                source_text=text,
                source_bbox=_round_rect(bbox),
                required_literals=tuple(dict.fromkeys(literals)),
                role="independent_paragraph",
                font_name=representative.font_name,
                font_size=round(max(item.font_size for item in objects), 4),
                color_srgb=representative.color_srgb,
                alignment="LEFT",
            )
        )
    if len(parts) < 2:
        return ()

    adjusted = []
    for index, part in enumerate(parts):
        top = container.allowed_bbox[1]
        bottom = container.allowed_bbox[3]
        if index > 0:
            top = (parts[index - 1].source_bbox[3] + part.source_bbox[1]) / 2.0
        if index + 1 < len(parts):
            bottom = (part.source_bbox[3] + parts[index + 1].source_bbox[1]) / 2.0
        adjusted.append(
            replace(
                part,
                allowed_bbox=_round_rect(
                    (container.allowed_bbox[0], top, container.allowed_bbox[2], bottom)
                ),
            )
        )
    return tuple(adjusted)


def _stacked_semantic_groups(groups) -> bool:
    if len(groups) != 2 or any(len(group) < 2 for group in groups):
        return False
    bboxes = [_union(tuple(item.bbox for item in group)) for group in groups]
    for index in range(1, len(bboxes)):
        previous = bboxes[index - 1]
        current = bboxes[index]
        minimum_width = min(previous[2] - previous[0], current[2] - current[0])
        font_size = min(
            max(item.font_size for item in groups[index - 1]),
            max(item.font_size for item in groups[index]),
        )
        if (
            _horizontal_overlap(previous, current) < minimum_width * 0.5
            or current[1] - previous[3] < max(1.0, font_size * 0.35)
        ):
            return False
    return True


def _semantic_list_containers(node, members, objects, facts: PageFacts):
    result = _semantic_list_containers_in_frame(
        node.node_id,
        node.safe_text_bbox,
        members,
        objects,
    )
    if not result:
        return tuple(_normalize_node_container(member, node, facts) for member in members)
    return result


def _semantic_list_containers_in_frame(prefix, safe, members, objects):
    ordered = sorted(objects, key=lambda item: (item.bbox[1], item.bbox[0], item.object_id))
    markers = [item for item in ordered if _is_bullet_marker(item.text)]
    non_markers = [item for item in ordered if item not in markers]
    first_marker_center = _vertical_center(markers[0].bbox)
    heading = [
        item
        for item in non_markers
        if _vertical_center(item.bbox)
        < first_marker_center - max(1.0, markers[0].font_size * 0.45)
    ]
    assigned = {item.object_id for item in heading}
    owner_kind = members[0].owner_kind
    owner_id = members[0].owner_id
    result = []
    if heading:
        bbox = _union(tuple(item.bbox for item in heading))
        representative = max(
            heading,
            key=lambda item: (item.font_size, len(item.text), -item.span_index),
        )
        result.append(
            replace(
                members[0],
                container_id=f"{prefix}/list-heading",
                owner_kind=owner_kind,
                owner_id=owner_id,
                source_object_ids=tuple(item.object_id for item in heading),
                source_text=_merge_semantic_objects(heading),
                source_bbox=_round_rect(bbox),
                allowed_bbox=safe,
                required_literals=_required_literals(_merge_semantic_objects(heading)),
                role="list_heading",
                font_name=representative.font_name,
                font_size=round(max(item.font_size for item in heading), 4),
                color_srgb=representative.color_srgb,
                alignment="CENTER",
            )
        )
    for index, marker in enumerate(markers):
        lower = _vertical_center(marker.bbox) - max(1.0, marker.font_size * 0.5)
        upper = (
            _vertical_center(markers[index + 1].bbox)
            - max(1.0, markers[index + 1].font_size * 0.5)
            if index + 1 < len(markers)
            else float("inf")
        )
        item_objects = [
            item
            for item in non_markers
            if item.object_id not in assigned
            and lower <= _vertical_center(item.bbox) < upper
        ]
        if not item_objects:
            continue
        assigned.update(item.object_id for item in item_objects)
        bbox = _union((marker.bbox, *tuple(item.bbox for item in item_objects)))
        text = "• " + _merge_semantic_objects(item_objects)
        representative = max(
            item_objects,
            key=lambda item: (len(item.text.strip()), item.font_size, -item.span_index),
        )
        result.append(
            replace(
                members[0],
                container_id=f"{prefix}/list-item-{index:02d}",
                owner_kind=owner_kind,
                owner_id=owner_id,
                source_object_ids=(marker.object_id, *tuple(item.object_id for item in item_objects)),
                source_text=text,
                source_bbox=_round_rect(bbox),
                allowed_bbox=safe,
                required_literals=tuple(dict.fromkeys((*_required_literals(text), "•"))),
                role="list_item",
                font_name=representative.font_name,
                font_size=round(max(item.font_size for item in item_objects), 4),
                color_srgb=representative.color_srgb,
                alignment="LEFT",
            )
        )
    return tuple(result)


def _normalize_node_container(container, node, facts: PageFacts):
    node_width = node.boundary_bbox[2] - node.boundary_bbox[0]
    node_height = node.boundary_bbox[3] - node.boundary_bbox[1]
    narrow_vertical_node = node_width <= 50.0 and node_height >= node_width * 2.0
    source_vertical = _vertical_source_container(container)
    if not (source_vertical or narrow_vertical_node):
        source_text = _normalize_leading_bullet(container.source_text)
        marker = _leading_marker(source_text)
        list_item = marker in {"•", "→"}
        return replace(
            container,
            source_text=source_text,
            allowed_bbox=node.safe_text_bbox,
            required_literals=tuple(
                dict.fromkeys(
                    (*_required_literals(source_text), *((marker,) if list_item else ()))
                )
            ),
            role="list_item" if list_item else container.role,
            alignment="LEFT" if list_item else container.alignment,
        )
    lines = [line.strip() for line in container.source_text.splitlines() if line.strip()]
    source_text = (
        "".join(lines)
        if all(any("\u3400" <= char <= "\u9fff" for char in line) for line in lines)
        else " ".join(lines)
    )
    normalized = replace(
        container,
        source_text=source_text,
        allowed_bbox=node.safe_text_bbox,
        required_literals=_required_literals(source_text),
        role="vertical_node_text",
        alignment="CENTER",
    )
    return (
        _expand_structural_label(normalized, facts)
        if source_vertical and not narrow_vertical_node
        else normalized
    )


def _vertical_source_container(container) -> bool:
    lines = [line.strip() for line in container.source_text.splitlines() if line.strip()]
    width = container.source_bbox[2] - container.source_bbox[0]
    height = container.source_bbox[3] - container.source_bbox[1]
    visual_lines_vertical = (
        len(lines) >= 3
        and max(len(re.sub(r"\s+", "", line)) for line in lines) <= 2
        and height >= width * 1.5
    )
    source_bbox_vertical = (
        height >= width * 2.5
        and height >= container.font_size * 3.0
    )
    return visual_lines_vertical or source_bbox_vertical


def _expand_structural_label(container, facts: PageFacts):
    container = replace(
        container,
        required_literals=_required_literals(container.source_text),
    )
    source = container.source_bbox
    page_area = facts.width * facts.height
    containment_tolerance = max(1.0, container.font_size * 0.75)
    candidates = [
        item.bbox
        for item in facts.drawing_objects
        if _contains_rect(item.bbox, source, tolerance=containment_tolerance)
        and _area(item.bbox) <= page_area * 0.20
        and item.bbox[2] - item.bbox[0] >= source[2] - source[0] + 1.0
        and item.bbox[3] - item.bbox[1] >= source[3] - source[1] + 1.0
    ]
    if not candidates:
        return container
    frame = min(candidates, key=_area)
    inset = min(2.5, max(1.0, min(frame[2] - frame[0], frame[3] - frame[1]) * 0.05))
    allowed = (
        min(source[0], frame[0] + inset),
        min(source[1], frame[1] + inset),
        max(source[2], frame[2] - inset),
        max(source[3], frame[3] - inset),
    )
    return replace(container, allowed_bbox=_round_rect(allowed))


def _is_bullet_marker(text: str) -> bool:
    return text.strip() in _BULLET_MARKERS


def _normalize_leading_bullet(text: str) -> str:
    stripped = text.lstrip()
    marker = next((item for item in _BULLET_MARKERS if stripped.startswith(item)), None)
    if marker is None:
        return text
    normalized = "→" if marker in {"\uf0d8", "→"} else "•"
    return normalized + " " + stripped[len(marker) :].lstrip()


def _leading_marker(text: str) -> str | None:
    stripped = text.lstrip()
    return next((item for item in ("→", "•") if stripped.startswith(item)), None)


def _required_literals_with_marker(text: str) -> tuple[str, ...]:
    marker = _leading_marker(text)
    return tuple(
        dict.fromkeys((*_required_literals(text), *((marker,) if marker else ())))
    )


def _merge_semantic_objects(objects) -> str:
    rows: list[list] = []
    for item in sorted(objects, key=lambda value: (value.bbox[1], value.bbox[0], value.object_id)):
        if rows and _same_edge_object_row(rows[-1], item):
            rows[-1].append(item)
        else:
            rows.append([item])
    lines = []
    for row in rows:
        parts = [item.text.strip() for item in sorted(row, key=lambda value: value.bbox[0])]
        lines.append(_join_semantic_parts(parts))
    return _join_semantic_parts(lines)


def _join_semantic_parts(parts) -> str:
    output = ""
    for part in (value.strip() for value in parts if value.strip()):
        if (
            output
            and output[-1:].isascii()
            and output[-1:].isalnum()
            and part[:1].isascii()
            and part[:1].isalnum()
        ):
            output += " "
        output += part
    return output


def _contains_rect(outer: Rect, inner: Rect, tolerance: float = 0.0) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _flow_owner(container: TextContainer, diagram_region: Rect, facts: PageFacts) -> str:
    text = container.source_text.strip()
    if container.role == "margin" and _semantic_shared_text(text):
        return "shared"
    horizontal = _horizontal_overlap(container.source_bbox, diagram_region)
    if horizontal <= 0:
        return "flow"
    above_gap = diagram_region[1] - container.source_bbox[3]
    below_gap = container.source_bbox[1] - diagram_region[3]
    nearby = max(28.0, container.font_size * 5.0)
    short = len(text) <= 180
    heading_like = container.role in {"heading", "title", "anchored"}
    if short and heading_like and (-2.0 <= above_gap <= nearby or -2.0 <= below_gap <= nearby * 0.6):
        return "shared"
    if container.source_bbox[1] <= facts.height * 0.12 and short and heading_like:
        return "shared"
    return "flow"


def _flow_allowed_bbox(
    container: TextContainer,
    flow_template: SingleColumnTemplate | MultiColumnTemplate,
    diagram_region: Rect,
    facts: PageFacts,
) -> Rect:
    if container.role == "margin":
        return _margin_allowed_bbox(container, diagram_region, facts)

    margin_x = max(8.0, facts.width * 0.012)
    left = max(margin_x, container.source_bbox[0])
    right = min(facts.width - margin_x, container.source_bbox[2])
    if isinstance(flow_template, MultiColumnTemplate):
        assignment = next(item for item in flow_template.assignments if item.container_id == container.container_id)
        column = next((item for item in flow_template.columns if item.column_id == assignment.column_id), None)
        if column is not None:
            left, right = column.left, column.right
        elif assignment.column_id == "span":
            left = min(item.source_bbox[0] for item in flow_template.containers)
            right = max(item.source_bbox[2] for item in flow_template.containers)
    else:
        non_margin = [item for item in flow_template.containers if item.role != "margin"] or list(flow_template.containers)
        left = min(item.source_bbox[0] for item in non_margin)
        right = max(item.source_bbox[2] for item in non_margin)
    if container.preserved_prefix:
        source_by_id = {item.object_id: item for item in facts.text_objects}
        marker_lefts = [
            source_by_id[object_id].bbox[0]
            for object_id in container.source_object_ids
            if object_id in source_by_id
            and source_by_id[object_id].text.strip() == container.preserved_prefix
        ]
        if marker_lefts:
            left = min(left, min(marker_lefts))
    flow_evidence = [item for item in flow_template.containers if item.role != "margin"] or list(flow_template.containers)
    if container.role in {"heading", "title"}:
        family = container.container_id.split("-segment-", 1)[0]
        siblings = [
            item
            for item in flow_evidence
            if item.container_id != container.container_id
            and item.container_id.split("-segment-", 1)[0] == family
            and _vertical_overlap(item.source_bbox, container.source_bbox) > 0.5
        ]
        has_left_sibling = any(
            sibling.source_bbox[2] <= container.source_bbox[0] + 0.01
            for sibling in siblings
        )
        has_right_sibling = any(
            sibling.source_bbox[0] >= container.source_bbox[2] - 0.01
            for sibling in siblings
        )
        if has_left_sibling and not has_right_sibling:
            right = max(right, facts.width - margin_x)
        divider_padding = max(1.5, container.font_size * 0.25)
        for sibling in siblings:
            if sibling.source_bbox[0] >= container.source_bbox[2] - 0.01:
                divider = (container.source_bbox[2] + sibling.source_bbox[0]) / 2.0
                right = min(right, divider - divider_padding)
            elif sibling.source_bbox[2] <= container.source_bbox[0] + 0.01:
                divider = (sibling.source_bbox[2] + container.source_bbox[0]) / 2.0
                left = max(left, divider + divider_padding)
    source_height = container.source_bbox[3] - container.source_bbox[1]
    short_post_diagram_line = (
        container.source_bbox[1] >= diagram_region[3] - 2.0
        and source_height <= max(2.0, container.font_size * 1.8)
        and len(container.source_text.strip()) <= 60
    )
    if short_post_diagram_line:
        safe_left = min(item.source_bbox[0] for item in flow_evidence)
        safe_right = max(item.source_bbox[2] for item in flow_evidence)
        for sibling in flow_evidence:
            if sibling.container_id == container.container_id:
                continue
            if _vertical_overlap(sibling.source_bbox, container.source_bbox) <= 0.5:
                continue
            if sibling.source_bbox[2] <= container.source_bbox[0] + 0.01:
                divider = (sibling.source_bbox[2] + container.source_bbox[0]) / 2.0
                safe_left = max(safe_left, divider + 0.5)
            elif sibling.source_bbox[0] >= container.source_bbox[2] - 0.01:
                divider = (container.source_bbox[2] + sibling.source_bbox[0]) / 2.0
                safe_right = min(safe_right, divider - 0.5)
        left = min(left, safe_left)
        right = max(right, safe_right)
    top = max(8.0, min(item.source_bbox[1] for item in flow_evidence))
    bottom = facts.height - max(10.0, facts.height * 0.015)
    if _horizontal_overlap((left, top, right, bottom), diagram_region) > 0:
        if container.source_bbox[3] <= diagram_region[1] + 2.0:
            bottom = min(bottom, diagram_region[1] - 0.75)
        elif container.source_bbox[1] >= diagram_region[3] - 2.0:
            top = max(top, diagram_region[3] + 0.75)
    if _vertical_overlap((left, top, right, bottom), diagram_region) > 0:
        if container.source_bbox[2] <= diagram_region[0] + 2.0:
            right = min(right, diagram_region[0] - 0.75)
        elif container.source_bbox[0] >= diagram_region[2] - 2.0:
            left = max(left, diagram_region[2] + 0.75)
    if right <= left + 1.0 or bottom <= top + 1.0:
        return _expand(container.source_bbox, 1.0, facts.width, facts.height)
    return _clip((left, top, right, bottom), facts.width, facts.height)


def _margin_allowed_bbox(
    container: TextContainer,
    diagram_region: Rect,
    facts: PageFacts,
) -> Rect:
    page_margin = max(8.0, facts.width * 0.012)
    left = min(container.source_bbox[0], page_margin)
    right = max(container.source_bbox[2], facts.width - page_margin)
    top = 0.0
    bottom = facts.height
    if _horizontal_overlap(container.source_bbox, diagram_region) > 0:
        if container.source_bbox[3] <= diagram_region[1] + 2.0:
            bottom = diagram_region[1] - 0.75
        elif container.source_bbox[1] >= diagram_region[3] - 2.0:
            top = diagram_region[3] + 0.75
    if _vertical_overlap(container.source_bbox, diagram_region) > 0:
        if container.source_bbox[2] <= diagram_region[0] + 2.0:
            right = diagram_region[0] - 0.75
        elif container.source_bbox[0] >= diagram_region[2] - 2.0:
            left = diagram_region[2] + 0.75
    return _clip((left, top, right, bottom), facts.width, facts.height)


def _semantic_shared_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact or _PAGE_MARKER.fullmatch(compact) or _URL.fullmatch(text):
        return False
    return any(character.isalpha() for character in text)


def _required_literals(text: str) -> tuple[str, ...]:
    literals = []
    for match in _CODE_LITERAL.finditer(text):
        literal = match.group(0)
        if (
            literal.startswith("-")
            and match.start() > 0
            and text[match.start() - 1].isalnum()
        ):
            literal = literal[1:]
        literals.append(literal)
    return tuple(dict.fromkeys(literals))


def _interior_connector(connector: DiagramConnector, width: float, height: float) -> bool:
    values = (*connector.start, *connector.end)
    if not (
        -1.0 <= values[0] <= width + 1.0
        and -1.0 <= values[2] <= width + 1.0
        and -1.0 <= values[1] <= height + 1.0
        and -1.0 <= values[3] <= height + 1.0
    ):
        return False
    length = math.dist(connector.start, connector.end)
    return 1.0 <= length <= width * 0.92


def _connector_bbox(connector: DiagramConnector) -> Rect:
    return (
        min(connector.start[0], connector.end[0]),
        min(connector.start[1], connector.end[1]),
        max(connector.start[0], connector.end[0]),
        max(connector.start[1], connector.end[1]),
    )


def _owner_order(owner: str) -> int:
    return {"shared": 0, "flow": 1, "diagram": 2}.get(owner, 9)


def _union(rects: tuple[Rect, ...]) -> Rect:
    if not rects:
        raise CompositeCapabilityError("EMPTY_GEOMETRY_UNION")
    return (
        min(item[0] for item in rects),
        min(item[1] for item in rects),
        max(item[2] for item in rects),
        max(item[3] for item in rects),
    )


def _expand(rect: Rect, padding: float, width: float, height: float) -> Rect:
    return _clip(
        (rect[0] - padding, rect[1] - padding, rect[2] + padding, rect[3] + padding),
        width,
        height,
    )


def _clip(rect: Rect, width: float, height: float) -> Rect:
    return (
        max(0.0, min(width, rect[0])),
        max(0.0, min(height, rect[1])),
        max(0.0, min(width, rect[2])),
        max(0.0, min(height, rect[3])),
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _horizontal_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _vertical_overlap(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
