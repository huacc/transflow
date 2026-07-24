"""Build fixed TBM2 ownership plans from production leaf cores."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import combinations, pairwise
from pathlib import Path
from statistics import median

from transflow.domain.common import content_sha256
from transflow.domain.text_inventory import InventoryDisposition
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.composites.models import OwnedContainer, Rect
from transflow.toolboxes.leaves.body_chart.models import (
    ChartTemplate,
    ChartTextContainer,
)
from transflow.toolboxes.leaves.body_chart.template import (
    ChartCapabilityError,
    build_chart_template,
)
from transflow.toolboxes.leaves.body_diagram.models import (
    DiagramContainer,
    DiagramTemplate,
)
from transflow.toolboxes.leaves.body_diagram.template import (
    DiagramCapabilityError,
    build_diagram_template,
)
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    SingleTextContainer,
)
from transflow.toolboxes.leaves.body_flow_text_single.template import (
    build_containers,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

FREEFORM_COMPONENT_ALLOWLIST = ("diagram", "chart", "flow")


@dataclass(frozen=True, slots=True)
class OwnershipPlan:
    containers: tuple[OwnedContainer, ...]
    flow_containers: tuple[SingleTextContainer, ...]
    chart_template: ChartTemplate | None
    diagram_template: DiagramTemplate | None
    retained_ids: tuple[str, ...]
    force_fallback_reason: str | None


def build_flow_text_chart_plan(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> OwnershipPlan:
    """Port the P17 flow-first arbitration without instantiating child toolboxes."""

    try:
        full_chart = build_chart_template(facts)
    except (ChartCapabilityError, ValueError, RuntimeError) as error:
        return _failed_plan(facts, policy, f"FLOW_TEXT_CHART:{type(error).__name__}")
    full_flow = build_containers(facts, policy)
    selected_flow = _select_narrative_flow(full_flow, facts.page.width_points)
    if not selected_flow:
        return _failed_plan(facts, policy, "FLOW_TEXT_CHART:FLOW_REGION_NOT_FOUND")

    flow_ids = {
        object_id
        for container in selected_flow
        for object_id in container.source_object_ids
    }
    flow_ids = _close_mixed_groups(
        flow_ids,
        tuple(item.source_object_ids for item in full_chart.containers),
        tuple(item.source_object_ids for item in full_flow),
    )
    selected_flow = tuple(
        item
        for item in full_flow
        if set(item.source_object_ids).issubset(flow_ids)
    )
    selected_chart = tuple(
        item
        for item in full_chart.containers
        if not set(item.source_object_ids).intersection(flow_ids)
    )
    return _materialize_plan(
        facts,
        policy,
        flow=selected_flow,
        chart_template=_filter_chart_template(full_chart, selected_chart),
        diagram_template=None,
        required_components=("flow", "chart"),
    )


def build_flow_text_diagram_plan(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
    source_pdf: Path,
) -> OwnershipPlan:
    """Port the P18 flow/diagram split while keeping one composite root."""

    try:
        full_diagram = build_diagram_template(facts, source_pdf)
    except (DiagramCapabilityError, ValueError, RuntimeError) as error:
        return _failed_plan(facts, policy, f"FLOW_TEXT_DIAGRAM:{type(error).__name__}")
    full_flow = build_containers(facts, policy)
    selected_flow = _select_narrative_flow(full_flow, facts.page.width_points)
    if not selected_flow:
        return _failed_plan(facts, policy, "FLOW_TEXT_DIAGRAM:FLOW_REGION_NOT_FOUND")

    flow_ids = {
        object_id
        for container in selected_flow
        for object_id in container.source_object_ids
    }
    flow_ids = _close_mixed_groups(
        flow_ids,
        tuple(item.source_object_ids for item in full_diagram.containers),
        tuple(item.source_object_ids for item in full_flow),
    )
    selected_flow = tuple(
        item
        for item in full_flow
        if set(item.source_object_ids).issubset(flow_ids)
    )
    selected_diagram = tuple(
        item
        for item in full_diagram.containers
        if not set(item.source_object_ids).intersection(flow_ids)
    )
    return _materialize_plan(
        facts,
        policy,
        flow=selected_flow,
        chart_template=None,
        diagram_template=_filter_diagram_template(
            full_diagram,
            selected_diagram,
        ),
        required_components=("flow", "diagram"),
    )


def build_freeform_plan(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
    source_pdf: Path | None,
) -> OwnershipPlan:
    """Decompose once through a fixed ready-only allow-list, then stop."""

    claimed: set[str] = set()
    diagram_template: DiagramTemplate | None = None
    chart_template: ChartTemplate | None = None

    if source_pdf is not None:
        try:
            full_diagram = build_diagram_template(facts, source_pdf)
            diagram_containers = _high_confidence_diagram(full_diagram)
            if diagram_containers:
                diagram_template = _filter_diagram_template(
                    full_diagram,
                    diagram_containers,
                )
                claimed.update(
                    object_id
                    for item in diagram_containers
                    for object_id in item.source_object_ids
                )
        except (DiagramCapabilityError, ValueError, RuntimeError):
            diagram_template = None

    try:
        full_chart = build_chart_template(facts)
        chart_containers = tuple(
            item
            for item in full_chart.containers
            if not set(item.source_object_ids).intersection(claimed)
            and _high_confidence_chart(item, full_chart, facts)
        )
        if chart_containers:
            chart_template = _filter_chart_template(full_chart, chart_containers)
            claimed.update(
                object_id
                for item in chart_containers
                for object_id in item.source_object_ids
            )
    except (ChartCapabilityError, ValueError, RuntimeError):
        chart_template = None

    flow = tuple(
        item
        for item in build_containers(facts, policy)
        if not set(item.source_object_ids).intersection(claimed)
    )
    return _materialize_plan(
        facts,
        policy,
        flow=flow,
        chart_template=chart_template,
        diagram_template=diagram_template,
        required_components=(),
    )


def _materialize_plan(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
    *,
    flow: tuple[SingleTextContainer, ...],
    chart_template: ChartTemplate | None,
    diagram_template: DiagramTemplate | None,
    required_components: tuple[str, ...],
) -> OwnershipPlan:
    inventory = {
        item.object_id: item
        for item in freeze_page_text_inventory(
            facts,
            target_language=policy.target_language,
        ).items
    }
    spans = {item.object_id: item for item in facts.text_spans}
    claimed: set[str] = set()
    projected_flow: list[SingleTextContainer] = []
    projected_chart: list[ChartTextContainer] = []
    projected_diagram: list[DiagramContainer] = []
    shared_retained: list[OwnedContainer] = []
    bounded_retained: list[OwnedContainer] = []

    for container in flow:
        projected = _project_flow(container, spans, inventory)
        if projected is not None:
            _claim(projected.source_object_ids, claimed)
            if _is_shared_margin(projected.source_bbox, facts, policy):
                shared_retained.append(
                    _retained_container("flow", projected)
                )
            elif (
                not required_components
                and _has_disjoint_inline_text(projected, spans)
            ):
                bounded_retained.append(
                    _retained_container("flow", projected)
                )
            else:
                projected_flow.append(projected)
    if chart_template is not None:
        for container in chart_template.containers:
            projected = _project_chart(container, spans, inventory)
            if projected is not None:
                _claim(projected.source_object_ids, claimed)
                if _is_shared_margin(projected.source_bbox, facts, policy):
                    shared_retained.append(
                        _retained_container("chart", projected)
                    )
                else:
                    projected_chart.append(projected)
        chart_template = _filter_chart_template(
            chart_template,
            tuple(projected_chart),
        )
    if diagram_template is not None:
        for container in diagram_template.containers:
            projected = _project_diagram(container, spans, inventory)
            if projected is not None:
                _claim(projected.source_object_ids, claimed)
                if _is_shared_margin(projected.source_bbox, facts, policy):
                    shared_retained.append(
                        _retained_container("diagram", projected)
                    )
                elif _has_disjoint_local_label(projected, spans):
                    bounded_retained.append(
                        _retained_container("diagram", projected)
                    )
                else:
                    projected_diagram.append(projected)
        diagram_template = _filter_diagram_template(
            diagram_template,
            tuple(projected_diagram),
        )

    translatable = {
        object_id
        for object_id, item in inventory.items()
        if item.disposition is InventoryDisposition.TRANSLATE
    }
    unsafe_retained_ids = tuple(
        item.object_id
        for item in facts.text_spans
        if item.object_id in translatable and item.object_id not in claimed
    )
    owned: list[OwnedContainer] = []
    for component, items in (
        ("flow", tuple(projected_flow)),
        ("chart", tuple(projected_chart)),
        ("diagram", tuple(projected_diagram)),
    ):
        for item in items:
            internal_id = item.container_id
            owned.append(
                OwnedContainer(
                    composite_id=f"{component}/{internal_id}",
                    component=component,
                    internal_id=internal_id,
                    source_object_ids=item.source_object_ids,
                    source_text=item.source_text,
                    source_bbox=item.source_bbox,
                    reading_order=0,
                )
            )
    owned.extend(shared_retained)
    owned.extend(bounded_retained)
    for object_id in unsafe_retained_ids:
        span = spans[object_id]
        owned.append(
            OwnedContainer(
                composite_id=f"retained/{object_id}",
                component="retained",
                internal_id=object_id,
                source_object_ids=(object_id,),
                source_text=span.text,
                source_bbox=span.bbox,
                reading_order=0,
            )
        )
    owned.sort(key=lambda item: (*_reading_key(item.source_bbox), item.composite_id))
    owned = [
        replace(item, reading_order=index)
        for index, item in enumerate(owned)
    ]
    present = {item.component for item in owned}
    missing = tuple(item for item in required_components if item not in present)
    retained_ids = tuple(
        object_id
        for item in (*shared_retained, *bounded_retained)
        for object_id in item.source_object_ids
    ) + unsafe_retained_ids
    reason = (
        f"REQUIRED_COMPONENT_MISSING:{','.join(missing)}"
        if missing
        else "OWNERSHIP_INCOMPLETE"
        if unsafe_retained_ids and required_components
        else None
    )
    return OwnershipPlan(
        tuple(owned),
        tuple(projected_flow),
        chart_template,
        diagram_template,
        retained_ids,
        reason,
    )


def _retained_container(
    source_component: str,
    container: SingleTextContainer | ChartTextContainer | DiagramContainer,
) -> OwnedContainer:
    return OwnedContainer(
        composite_id=f"retained/{source_component}/{container.container_id}",
        component="retained",
        internal_id=f"{source_component}/{container.container_id}",
        source_object_ids=container.source_object_ids,
        source_text=container.source_text,
        source_bbox=container.source_bbox,
        reading_order=0,
    )


def _is_shared_margin(
    bbox: Rect,
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> bool:
    return (
        bbox[3] <= facts.page.height_points * policy.body_margin_top_ratio
        or bbox[1]
        >= facts.page.height_points * policy.body_margin_bottom_ratio
    )


def _has_disjoint_local_label(
    container: DiagramContainer,
    spans: dict[str, KernelTextFact],
) -> bool:
    if container.owner_kind != "local_label" or container.node_id is not None:
        return False
    selected = sorted(
        (spans[object_id] for object_id in container.source_object_ids),
        key=lambda item: (item.bbox[1], item.bbox[0]),
    )
    if len(selected) < 2:
        return False
    line_height = median(
        item.bbox[3] - item.bbox[1]
        for item in selected
    )
    separation = max(2.0, line_height * 0.5)
    return any(
        following.bbox[1] - current.bbox[3] > separation
        for current, following in pairwise(selected)
    )


def _has_disjoint_inline_text(
    container: SingleTextContainer,
    spans: dict[str, KernelTextFact],
) -> bool:
    selected = tuple(
        spans[object_id]
        for object_id in container.source_object_ids
    )
    if len(selected) < 2:
        return False
    line_height = median(
        item.bbox[3] - item.bbox[1]
        for item in selected
    )
    separation = max(12.0, line_height * 2.0)
    for left, right in combinations(selected, 2):
        if (
            left.block_index == right.block_index
            and left.line_index == right.line_index
        ):
            continue
        vertical_overlap = max(
            0.0,
            min(left.bbox[3], right.bbox[3])
            - max(left.bbox[1], right.bbox[1]),
        )
        if vertical_overlap < min(
            left.bbox[3] - left.bbox[1],
            right.bbox[3] - right.bbox[1],
        ) * 0.8:
            continue
        horizontal_gap = max(
            left.bbox[0],
            right.bbox[0],
        ) - min(
            left.bbox[2],
            right.bbox[2],
        )
        if horizontal_gap > separation:
            return True
    return False


def _failed_plan(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
    reason: str,
) -> OwnershipPlan:
    plan = _materialize_plan(
        facts,
        policy,
        flow=(),
        chart_template=None,
        diagram_template=None,
        required_components=(),
    )
    return replace(plan, force_fallback_reason=reason)


def _select_narrative_flow(
    containers: tuple[SingleTextContainer, ...],
    page_width: float,
) -> tuple[SingleTextContainer, ...]:
    candidates = [
        item
        for item in containers
        if item.role != "margin"
        and _width(item.source_bbox) >= page_width * 0.22
        and _translatable_text(item.source_text)
    ]
    lanes: list[list[SingleTextContainer]] = []
    for item in sorted(candidates, key=lambda row: (row.source_bbox[0], row.source_bbox[1])):
        target = next(
            (
                lane
                for lane in lanes
                if abs(item.source_bbox[0] - median(row.source_bbox[0] for row in lane))
                <= 18.0
                and _horizontal_overlap_ratio(
                    item.source_bbox,
                    _union(tuple(row.source_bbox for row in lane)),
                )
                >= 0.55
            ),
            None,
        )
        if target is None:
            lanes.append([item])
        else:
            target.append(item)
    selected: set[str] = set()
    for lane in lanes:
        text = " ".join(item.source_text for item in lane)
        if not _narrative_text(text):
            continue
        widest = max(_width(item.source_bbox) for item in lane)
        selected.update(
            item.container_id
            for item in lane
            if _width(item.source_bbox) >= max(page_width * 0.22, widest * 0.55)
        )
    return tuple(item for item in containers if item.container_id in selected)


def _high_confidence_diagram(
    template: DiagramTemplate,
) -> tuple[DiagramContainer, ...]:
    if not template.nodes or not template.connectors:
        return ()
    node_ids = {node.node_id for node in template.nodes}
    node_bbox = _union(tuple(node.boundary_bbox for node in template.nodes))
    return tuple(
        item
        for item in template.containers
        if (
            item.owner_kind == "node"
            or (
                item.owner_kind == "local_label"
                and item.source_bbox[1] >= node_bbox[1] - 8.0
                and _intersection_area(item.source_bbox, node_bbox) > 0.0
            )
        )
        and (item.node_id is None or item.node_id in node_ids)
    )


def _high_confidence_chart(
    container: ChartTextContainer,
    template: ChartTemplate,
    facts: ExtractedPageFacts,
) -> bool:
    if container.role in {"PAGE_HEADER", "PAGE_FOOTER"}:
        return False
    region = next(
        (
            item
            for item in template.visual_regions
            if item.region_id == container.association_id
        ),
        None,
    )
    if region is None:
        return False
    page_area = facts.page.width_points * facts.page.height_points
    if _area(region.bbox) >= page_area * 0.65:
        return False
    words = len(re.findall(r"[A-Za-z]+", container.source_text))
    return bool(container.anchor_object_ids) and (
        container.role != "ANNOTATION" or words < 18
    )


def _project_flow(
    container: SingleTextContainer,
    spans: dict[str, KernelTextFact],
    inventory: dict[str, object],
) -> SingleTextContainer | None:
    ids = _translation_ids(container.source_object_ids, inventory)
    if not ids:
        return None
    selected = tuple(spans[object_id] for object_id in ids)
    bbox = _union(tuple(item.bbox for item in selected))
    return replace(
        container,
        semantic_object_id=ids[0],
        source_object_ids=ids,
        source_rects=tuple(item.bbox for item in selected),
        source_text=_source_text(selected),
        source_bbox=bbox,
        anchor=(bbox[0], bbox[1]),
        preserved_prefix=None,
        preserved_page_numbers=(),
    )


def _project_chart(
    container: ChartTextContainer,
    spans: dict[str, KernelTextFact],
    inventory: dict[str, object],
) -> ChartTextContainer | None:
    ids = _translation_ids(container.source_object_ids, inventory)
    if not ids:
        return None
    selected = tuple(spans[object_id] for object_id in ids)
    bbox = _union(tuple(item.bbox for item in selected))
    return replace(
        container,
        source_object_ids=ids,
        semantic_object_id=ids[0],
        source_text=_source_text(selected),
        source_bbox=bbox,
        allowed_bbox=_clip_to_kept_text(
            container.allowed_bbox,
            bbox,
            tuple(
                spans[object_id].bbox
                for object_id in container.source_object_ids
                if object_id not in ids
            ),
        ),
        required_literals=(),
    )


def _project_diagram(
    container: DiagramContainer,
    spans: dict[str, KernelTextFact],
    inventory: dict[str, object],
) -> DiagramContainer | None:
    ids = _translation_ids(container.source_object_ids, inventory)
    if not ids:
        return None
    selected = tuple(spans[object_id] for object_id in ids)
    bbox = _union(tuple(item.bbox for item in selected))
    return replace(
        container,
        source_object_ids=ids,
        source_text=_source_text(selected),
        source_bbox=bbox,
        allowed_bbox=_clip_to_kept_text(
            container.allowed_bbox,
            bbox,
            tuple(
                spans[object_id].bbox
                for object_id in container.source_object_ids
                if object_id not in ids
            ),
        ),
        required_literals=(),
        recomposed_object_ids=(),
    )


def _translation_ids(
    object_ids: tuple[str, ...],
    inventory: dict[str, object],
) -> tuple[str, ...]:
    return tuple(
        object_id
        for object_id in object_ids
        if inventory[object_id].disposition is InventoryDisposition.TRANSLATE
    )


def _filter_chart_template(
    template: ChartTemplate,
    containers: tuple[ChartTextContainer, ...],
) -> ChartTemplate:
    selected = {
        object_id
        for item in containers
        for object_id in item.source_object_ids
    }
    return replace(
        template,
        containers=containers,
        protected_object_ids=tuple(
            item
            for item in template.protected_object_ids
            if item not in selected
        ),
        structure_hash=content_sha256(
            {
                "source": template.structure_hash,
                "containers": containers,
                "scope": "tbm2-composite",
            }
        ),
    )


def _filter_diagram_template(
    template: DiagramTemplate,
    containers: tuple[DiagramContainer, ...],
) -> DiagramTemplate:
    selected_ids = {item.container_id for item in containers}
    selected_objects = {
        object_id
        for item in containers
        for object_id in item.source_object_ids
    }
    return replace(
        template,
        nodes=tuple(
            replace(
                node,
                container_ids=tuple(
                    item for item in node.container_ids if item in selected_ids
                ),
            )
            for node in template.nodes
        ),
        containers=containers,
        protected_object_ids=tuple(
            item
            for item in template.protected_object_ids
            if item not in selected_objects
        ),
        structure_sha256=content_sha256(
            {
                "source": template.structure_sha256,
                "containers": containers,
                "scope": "tbm2-composite",
            }
        ),
    )


def _close_mixed_groups(
    initial: set[str],
    *groups: tuple[tuple[str, ...], ...],
) -> set[str]:
    closed = set(initial)
    changed = True
    while changed:
        changed = False
        for collection in groups:
            for group in collection:
                ids = set(group)
                if ids.intersection(closed) and not ids.issubset(closed):
                    closed.update(ids)
                    changed = True
    return closed


def _claim(ids: tuple[str, ...], claimed: set[str]) -> None:
    overlap = claimed.intersection(ids)
    if overlap:
        raise ValueError(f"TBM2_DUPLICATE_OBJECT_OWNER:{sorted(overlap)[0]}")
    claimed.update(ids)


def _join_text(items: tuple[KernelTextFact, ...]) -> str:
    ordered = sorted(
        items,
        key=lambda item: (
            item.block_index,
            item.line_index,
            item.span_index,
            item.bbox[0],
        ),
    )
    result = ""
    previous: KernelTextFact | None = None
    for item in ordered:
        text = item.text.strip()
        if not text:
            continue
        if previous is not None and (
            item.block_index != previous.block_index
            or item.line_index != previous.line_index
            or (
                result
                and text
                and result[-1].isalnum()
                and text[0].isalnum()
            )
        ):
            result += " "
        result += text
        previous = item
    return re.sub(r"[ \t]+", " ", result).strip()


def _source_text(items: tuple[KernelTextFact, ...]) -> str:
    """Preserve the exact Kernel hash for a one-span semantic unit."""

    return items[0].text if len(items) == 1 else _join_text(items)


def _clip_to_kept_text(
    allowed: Rect,
    source: Rect,
    kept: tuple[Rect, ...],
) -> Rect:
    left, top, right, bottom = allowed
    for rect in kept:
        if rect[2] <= source[0]:
            left = max(left, source[0])
        elif rect[0] >= source[2]:
            right = min(right, source[2])
        elif rect[3] <= source[1]:
            top = max(top, source[1])
        elif rect[1] >= source[3]:
            bottom = min(bottom, source[3])
        else:
            return source
    if right <= left or bottom <= top:
        return source
    return tuple(round(value, 4) for value in (left, top, right, bottom))


def _narrative_text(text: str) -> bool:
    words = len(re.findall(r"[A-Za-z]+", text))
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    punctuation = len(re.findall(r"[.!?;。！？；]", text))
    digits = len(re.findall(r"\d", text))
    letters = len(re.findall(r"[A-Za-z\u3400-\u9fff]", text))
    if digits / max(1, letters + digits) >= 0.35:
        return False
    return (
        words >= 18 and (punctuation >= 1 or len(text) >= 180)
    ) or (
        cjk >= 40 and (punctuation >= 1 or cjk >= 75)
    )


def _translatable_text(text: str) -> bool:
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", text))


def _reading_key(rect: Rect) -> tuple[float, float]:
    return (round(rect[1], 3), round(rect[0], 3))


def _width(rect: Rect) -> float:
    return rect[2] - rect[0]


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _horizontal_overlap_ratio(left: Rect, right: Rect) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(0.1, min(_width(left), _width(right)))


def _union(rectangles: tuple[Rect, ...]) -> Rect:
    if not rectangles:
        raise ValueError("TBM2_EMPTY_RECTANGLE_SET")
    return tuple(
        round(value, 4)
        for value in (
            min(item[0] for item in rectangles),
            min(item[1] for item in rectangles),
            max(item[2] for item in rectangles),
            max(item[3] for item in rectangles),
        )
    )
