from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256
from toolboxes.body.anchored_blocks.tools.models import AnchoredBlocksTemplate
from toolboxes.body.anchored_blocks.tools.template_builder import (
    AnchoredBlocksCapabilityError,
    build_anchored_blocks_template,
)
from toolboxes.body.chart.tools.models import ChartTemplate, ChartTextContainer
from toolboxes.body.chart.tools.template_builder import (
    ChartCapabilityError,
    build_chart_template,
)

from . import TOOLBOX_KEY
from .models import CompositeContainer, CompositePageTemplate, ObjectOwnership, Rect


class CompositeCapabilityError(ValueError):
    pass


_STRONG_CHART_ROLES = {
    "AXIS_OR_CATEGORY_LABEL",
    "LEGEND_LABEL",
    "TABLE_HEADER",
    "TABLE_SECTION",
    "TABLE_CELL",
    "TABLE_TOTAL",
}


def build_composite_template(
    source_pdf: Path,
    facts: PageFacts,
    *,
    target_language: str,
) -> CompositePageTemplate:
    """Partition native text once, then reuse the frozen P11 and P13 leaf rules."""

    try:
        anchored_full = build_anchored_blocks_template(facts, source_pdf)
    except AnchoredBlocksCapabilityError as exc:
        raise CompositeCapabilityError(f"P11_CAPABILITY:{exc}") from exc
    try:
        chart_full = build_chart_template(facts)
    except ChartCapabilityError as exc:
        raise CompositeCapabilityError(f"P13_CAPABILITY:{exc}") from exc

    object_by_id = {item.object_id: item for item in facts.text_objects}
    all_ids = set(object_by_id)
    region_by_id = {item.region_id: item for item in chart_full.visual_regions}
    card_unit_ids = _anchored_card_unit_object_ids(anchored_full)

    chart_rows: list[tuple[str, ChartTextContainer]] = []
    claimed: dict[str, tuple[str, str]] = {}
    claimed_boundary_by_object: dict[str, Rect] = {}
    for container in chart_full.containers:
        if not _requires_translation(container.source_text, target_language):
            continue
        if any(object_id in card_unit_ids for object_id in container.source_object_ids):
            continue
        owner = _chart_owner(container, region_by_id, facts.height)
        if owner is None:
            continue
        composite_id = f"{owner}::{container.container_id}"
        for object_id in container.source_object_ids:
            _claim(claimed, object_id, owner, composite_id)
            claimed_boundary_by_object[object_id] = container.allowed_bbox
        chart_rows.append((owner, container))

    anchored_containers = []
    for container in anchored_full.containers:
        remaining = tuple(
            object_by_id[object_id]
            for object_id in container.source_object_ids
            if object_id not in claimed
        )
        if not remaining:
            continue
        source_text = _joined_text(remaining)
        if not _requires_translation(source_text, target_language):
            continue
        adapted = container
        if tuple(item.object_id for item in remaining) != container.source_object_ids:
            source_bbox = _union(tuple(item.bbox for item in remaining))
            claimed_bboxes = tuple(
                claimed_boundary_by_object.get(object_id, object_by_id[object_id].bbox)
                for object_id in container.source_object_ids
                if object_id in claimed
            )
            partial_bbox = _safe_partial_bbox(
                source_bbox,
                container.allowed_bbox,
                claimed_bboxes,
            )
            adapted = replace(
                container,
                container_id=f"{container.container_id}-partial",
                source_object_ids=tuple(item.object_id for item in remaining),
                source_text=source_text,
                source_bbox=source_bbox,
                slot_bbox=partial_bbox,
                allowed_bbox=partial_bbox,
                required_literals=tuple(
                    literal for literal in container.required_literals if literal in source_text
                ),
            )
        composite_id = f"anchored::{adapted.container_id}"
        for object_id in adapted.source_object_ids:
            _claim(claimed, object_id, "anchored", composite_id)
        anchored_containers.append(adapted)

    anchored_template = _filtered_anchored_template(
        anchored_full,
        tuple(anchored_containers),
        all_ids,
    )
    chart_template = _filtered_chart_template(chart_full, chart_rows, all_ids)

    rows: list[tuple[str, object]] = [
        *(('anchored', item) for item in anchored_template.containers),
        *chart_rows,
    ]
    rows.sort(key=lambda row: (row[1].source_bbox[1], row[1].source_bbox[0], row[1].reading_order))
    containers = tuple(
        CompositeContainer(
            composite_id=f"{owner}::{container.container_id}",
            owner=owner,
            base_container_id=container.container_id,
            source_object_ids=container.source_object_ids,
            source_text=container.source_text,
            source_bbox=container.source_bbox,
            allowed_bbox=container.allowed_bbox,
            reading_order=index,
            required_literals=container.required_literals,
        )
        for index, (owner, container) in enumerate(rows)
    )

    protected_ids = tuple(item.object_id for item in facts.text_objects if item.object_id not in claimed)
    ownerships = tuple(
        ObjectOwnership(
            item.object_id,
            claimed[item.object_id][0] if item.object_id in claimed else "protected",
            claimed[item.object_id][1] if item.object_id in claimed else None,
        )
        for item in facts.text_objects
    )
    _validate_total_ownership(ownerships, all_ids)
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "anchored_structure_sha256": anchored_template.structure_sha256,
            "chart_structure_sha256": chart_template.structure_sha256,
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
        anchored_template=anchored_template,
        chart_template=chart_template,
        containers=containers,
        ownerships=ownerships,
        protected_object_ids=protected_ids,
        structure_sha256=structure_sha256,
    )


def _chart_owner(container, region_by_id, page_height: float) -> str | None:
    if container.role == "PAGE_HEADER" and container.source_bbox[3] <= page_height * 0.14:
        return "shared"
    if container.role == "PAGE_FOOTER" and container.source_bbox[1] >= page_height * 0.88:
        return "shared"
    if container.role in _STRONG_CHART_ROLES:
        return "chart"
    region = region_by_id.get(container.association_id)
    near_chart = region is not None and _rect_gap(container.source_bbox, region.bbox) <= max(
        4.0,
        container.font_size * 1.5,
    )
    if near_chart:
        return "chart"
    if container.role == "TITLE" and container.source_bbox[1] <= page_height * 0.18:
        return "shared"
    return None


def _filtered_anchored_template(
    template: AnchoredBlocksTemplate,
    containers,
    all_ids: set[str],
) -> AnchoredBlocksTemplate:
    container_ids = {item.container_id for item in containers}
    owners = tuple(
        replace(
            owner,
            container_ids=tuple(
                item.container_id
                for item in containers
                if item.block_owner_id == owner.owner_id
            ),
        )
        for owner in template.block_owners
        if any(item.block_owner_id == owner.owner_id for item in containers)
    )
    editable_ids = {
        object_id for container in containers for object_id in container.source_object_ids
    }
    protected = tuple(sorted(all_ids - editable_ids))
    structure = canonical_sha256(
        {
            "owners": [
                (item.owner_id, item.boundary_bbox, item.container_ids) for item in owners
            ],
            "containers": [
                (
                    item.container_id,
                    item.block_owner_id,
                    item.source_object_ids,
                    item.allowed_bbox,
                )
                for item in containers
                if item.container_id in container_ids
            ],
            "protected": protected,
        }
    )
    return replace(
        template,
        block_owners=owners,
        containers=tuple(containers),
        protected_object_ids=protected,
        structure_sha256=structure,
    )


def _filtered_chart_template(
    template: ChartTemplate,
    rows: list[tuple[str, ChartTextContainer]],
    all_ids: set[str],
) -> ChartTemplate:
    containers = tuple(container for _, container in rows)
    editable_ids = {
        object_id for container in containers for object_id in container.source_object_ids
    }
    protected = tuple(sorted(all_ids - editable_ids))
    structure = canonical_sha256(
        {
            "visual_regions": [
                (item.region_id, item.kind, item.bbox, item.object_ids)
                for item in template.visual_regions
            ],
            "containers": [
                (
                    item.container_id,
                    item.role,
                    item.association_id,
                    item.source_object_ids,
                    item.allowed_bbox,
                )
                for item in containers
            ],
            "protected": protected,
        }
    )
    return replace(
        template,
        containers=containers,
        protected_object_ids=protected,
        structure_sha256=structure,
    )


def _claim(
    claimed: dict[str, tuple[str, str]],
    object_id: str,
    owner: str,
    container_id: str,
) -> None:
    previous = claimed.get(object_id)
    if previous is not None and previous != (owner, container_id):
        raise CompositeCapabilityError(f"DUPLICATE_OBJECT_OWNER:{object_id}")
    claimed[object_id] = (owner, container_id)


def _validate_total_ownership(
    ownerships: tuple[ObjectOwnership, ...],
    expected: set[str],
) -> None:
    actual = [item.object_id for item in ownerships]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise CompositeCapabilityError("OBJECT_OWNERSHIP_NOT_EXHAUSTIVE")


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin and not has_cjk
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _joined_text(items: tuple[TextObjectFact, ...]) -> str:
    rows: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in items:
        rows.setdefault((item.block_index, item.line_index), []).append(item)
    values = []
    for row in sorted(rows):
        parts = [
            item.text.strip()
            for item in sorted(rows[row], key=lambda item: (item.bbox[0], item.span_index))
        ]
        values.append(" ".join(part for part in parts if part))
    return " ".join(value for value in values if value).strip()


def _union(rects: tuple[Rect, ...]) -> Rect:
    return tuple(
        round(value, 4)
        for value in (
            min(item[0] for item in rects),
            min(item[1] for item in rects),
            max(item[2] for item in rects),
            max(item[3] for item in rects),
        )
    )


def _rect_gap(left: Rect, right: Rect) -> float:
    dx = max(right[0] - left[2], left[0] - right[2], 0.0)
    dy = max(right[1] - left[3], left[1] - right[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _safe_partial_bbox(
    source_bbox: Rect,
    original_allowed: Rect,
    claimed_bboxes: tuple[Rect, ...],
) -> Rect:
    """Keep a partial P11 owner on its side of objects already claimed by P13."""

    x0, y0, x1, y1 = original_allowed
    source_center = (
        (source_bbox[0] + source_bbox[2]) / 2.0,
        (source_bbox[1] + source_bbox[3]) / 2.0,
    )
    gutter = 2.0
    for claimed in claimed_bboxes:
        claimed_center = (
            (claimed[0] + claimed[2]) / 2.0,
            (claimed[1] + claimed[3]) / 2.0,
        )
        vertical_overlap = min(source_bbox[3], claimed[3]) - max(source_bbox[1], claimed[1])
        horizontal_overlap = min(source_bbox[2], claimed[2]) - max(source_bbox[0], claimed[0])
        if vertical_overlap > 0.0:
            if claimed_center[0] > source_center[0]:
                candidate = claimed[0] - gutter
                if candidate > x0 + 1.0:
                    x1 = min(x1, candidate)
            elif claimed_center[0] < source_center[0]:
                candidate = claimed[2] + gutter
                if candidate < x1 - 1.0:
                    x0 = max(x0, candidate)
        if horizontal_overlap > 0.0:
            if claimed_center[1] > source_center[1]:
                candidate = claimed[1] - gutter
                if candidate > y0 + 1.0:
                    y1 = min(y1, candidate)
            elif claimed_center[1] < source_center[1]:
                candidate = claimed[3] + gutter
                if candidate < y1 - 1.0:
                    y0 = max(y0, candidate)
    return tuple(round(value, 4) for value in (x0, y0, x1, y1))


def _anchored_card_unit_object_ids(template: AnchoredBlocksTemplate) -> set[str]:
    by_owner: dict[str, list[object]] = {}
    for container in template.containers:
        by_owner.setdefault(container.block_owner_id, []).append(container)
    result: set[str] = set()
    for container in template.containers:
        if not _unit_like(container.source_text):
            continue
        has_nearby_metric = any(
            sibling.container_id != container.container_id
            and re.search(r"\d", sibling.source_text)
            and _rect_gap(container.source_bbox, sibling.source_bbox)
            <= max(24.0, container.font_size * 5.0)
            for sibling in by_owner.get(container.block_owner_id, [])
        )
        if has_nearby_metric:
            result.update(container.source_object_ids)
    return result


def _unit_like(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:RMB|HKD|HK\$|USD|US\$|CNY|CN¥|¥|EUR|€|GBP|£)"
            r"(?:\s*(?:million|billion|m|bn))?\s*",
            text,
            flags=re.IGNORECASE,
        )
    )
