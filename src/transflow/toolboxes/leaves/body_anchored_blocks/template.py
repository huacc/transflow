"""Build anchored owners from Kernel geometry without sample identity checks."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from transflow.domain.common import content_sha256
from transflow.domain.text_inventory import InventoryDisposition
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.toolboxes.leaves.body_anchored_blocks.models import (
    AnchoredBlockOwner,
    AnchoredBlocksTemplate,
    AnchoredContainer,
    Rect,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

TOOLBOX_KEY = "body.anchored_blocks"


@dataclass(frozen=True, slots=True)
class _SourceBlock:
    spans: tuple[KernelTextFact, ...]
    bbox: Rect
    source_text: str
    font_name: str
    font_size: float
    color_srgb: int
    role: str


@dataclass(frozen=True, slots=True)
class _VisualRegion:
    bbox: Rect
    object_ids: tuple[str, ...]


@dataclass(slots=True)
class _OwnerDraft:
    blocks: list[_SourceBlock]
    visual: _VisualRegion | None
    margin_owner: str | None = None
    ambiguous: bool = False


def build_anchored_blocks_template(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> AnchoredBlocksTemplate:
    """Lift the Spike owner/slot model onto production Kernel facts."""

    inventory = {
        item.object_id: item
        for item in freeze_page_text_inventory(
            facts,
            target_language=policy.target_language,
        ).items
    }
    blocks = _source_blocks(facts, policy)
    visuals = _visual_regions(facts)
    drafts = _owner_drafts(blocks, visuals, facts)
    drafts.sort(key=lambda item: _position(_draft_bbox(item)))

    owners: list[AnchoredBlockOwner] = []
    containers: list[AnchoredContainer] = []
    ambiguous: list[str] = []
    visual_bboxes = {
        object_id: region.bbox
        for region in visuals
        for object_id in region.object_ids
    }
    for owner_order, draft in enumerate(drafts):
        owner_id = draft.margin_owner or f"anchored-owner-{owner_order:03d}"
        boundary = _owner_boundary(draft, drafts, facts)
        groups = _container_groups(draft.blocks)
        owner_containers: list[AnchoredContainer] = []
        for local_order, group in enumerate(groups):
            spans = tuple(
                sorted(
                    (span for block in group for span in block.spans),
                    key=lambda item: (
                        item.block_index,
                        item.line_index,
                        item.span_index,
                        item.bbox[0],
                    ),
                )
            )
            source_bbox = _union(tuple(item.bbox for item in spans))
            peer_bboxes = tuple(
                _union(
                    tuple(
                        item.bbox
                        for block in peer
                        for item in block.spans
                    )
                )
                for peer in groups
                if peer is not group
            )
            background_ids = draft.visual.object_ids if draft.visual else ()
            immutable = tuple(
                bbox
                for object_id, bbox in visual_bboxes.items()
                if object_id not in background_ids
            )
            object_ids = tuple(item.object_id for item in spans)
            translation_ids = tuple(
                object_id
                for object_id in object_ids
                if inventory[object_id].disposition
                is InventoryDisposition.TRANSLATE
            )
            inline_keep_ids = tuple(
                object_id
                for object_id in object_ids
                if inventory[object_id].disposition
                is InventoryDisposition.KEEP_SOURCE
            )
            representative = _dominant_span(spans)
            container_id = f"{owner_id}/container-{local_order:02d}"
            container = AnchoredContainer(
                container_id=container_id,
                block_owner_id=owner_id,
                source_object_ids=object_ids,
                translation_object_ids=translation_ids,
                inline_keep_source_object_ids=inline_keep_ids,
                source_text=_merge_text(spans),
                source_bbox=source_bbox,
                slot_bbox=boundary,
                allowed_bbox=_allowed_bbox(
                    source_bbox,
                    boundary,
                    peer_bboxes,
                    immutable,
                ),
                reading_order=len(containers) + len(owner_containers),
                role=group[0].role,
                font_name=representative.font_name,
                font_size=max(item.font_size for item in spans),
                color_srgb=representative.color_srgb,
                alignment=_alignment(source_bbox, boundary),
            )
            owner_containers.append(container)
            if draft.ambiguous:
                ambiguous.append(container_id)
        containers.extend(owner_containers)
        owner_source_ids = tuple(
            item.object_id
            for block in sorted(
                draft.blocks,
                key=lambda item: _position(item.bbox),
            )
            for item in block.spans
        )
        protected_ids = tuple(
            object_id
            for object_id in owner_source_ids
            if inventory[object_id].disposition
            is not InventoryDisposition.TRANSLATE
        )
        source_bbox = _draft_bbox(draft)
        owners.append(
            AnchoredBlockOwner(
                owner_id=owner_id,
                boundary_bbox=boundary,
                anchor=(source_bbox[0], source_bbox[1]),
                reading_order=owner_order,
                source_object_ids=owner_source_ids,
                container_ids=tuple(
                    item.container_id for item in owner_containers
                ),
                protected_object_ids=protected_ids,
                background_object_ids=background_ids,
                boundary_source=(
                    "shared_margin"
                    if draft.margin_owner
                    else "kernel_visual_bbox"
                    if draft.visual
                    else "derived_safe_region"
                ),
            )
        )

    structure_sha256 = content_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "owners": tuple(owners),
            "containers": tuple(containers),
        }
    )
    return AnchoredBlocksTemplate(
        page_id=facts.page_identity,
        toolbox_key=TOOLBOX_KEY,
        width=facts.page.width_points,
        height=facts.page.height_points,
        block_owners=tuple(owners),
        containers=tuple(containers),
        protected_object_ids=tuple(
            item.object_id
            for item in inventory.values()
            if item.disposition is not InventoryDisposition.TRANSLATE
        ),
        structure_sha256=structure_sha256,
        ambiguous_container_ids=tuple(ambiguous),
    )


def _source_blocks(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> list[_SourceBlock]:
    grouped: dict[int, list[KernelTextFact]] = {}
    for span in facts.text_spans:
        if span.text.strip():
            grouped.setdefault(span.block_index, []).append(span)
    if not grouped:
        return []
    page_font_median = median(item.font_size for item in facts.text_spans)
    top_margin = facts.page.height_points * policy.body_margin_top_ratio
    bottom_margin = facts.page.height_points * policy.body_margin_bottom_ratio
    output: list[_SourceBlock] = []
    for values in grouped.values():
        spans = tuple(
            sorted(
                values,
                key=lambda item: (
                    item.line_index,
                    item.span_index,
                    item.bbox[0],
                ),
            )
        )
        bbox = _union(tuple(item.bbox for item in spans))
        representative = _dominant_span(spans)
        role = (
            "margin"
            if bbox[3] < top_margin or bbox[1] > bottom_margin
            else "heading"
            if max(item.font_size for item in spans) >= page_font_median * 1.25
            or "bold" in representative.font_name.casefold()
            else "body"
        )
        output.append(
            _SourceBlock(
                spans=spans,
                bbox=bbox,
                source_text=_merge_text(spans),
                font_name=representative.font_name,
                font_size=max(item.font_size for item in spans),
                color_srgb=representative.color_srgb,
                role=role,
            )
        )
    return sorted(output, key=lambda item: _position(item.bbox))


def _visual_regions(facts: ExtractedPageFacts) -> list[_VisualRegion]:
    page_area = facts.page.width_points * facts.page.height_points
    raw = (
        *((item.object_id, item.bbox) for item in facts.drawing_objects),
        *((item.object_id, item.bbox) for item in facts.image_objects),
    )
    grouped: list[tuple[Rect, list[str]]] = []
    for object_id, bbox in raw:
        clipped = _clip(
            bbox,
            facts.page.width_points,
            facts.page.height_points,
        )
        width = clipped[2] - clipped[0]
        height = clipped[3] - clipped[1]
        area = width * height
        if (
            width < 36.0
            or height < 20.0
            or area < page_area * 0.003
            or area > page_area * 0.36
        ):
            continue
        existing = next(
            (
                item
                for item in grouped
                if _rect_distance(item[0], clipped) <= 3.0
            ),
            None,
        )
        if existing is None:
            grouped.append((clipped, [object_id]))
        else:
            existing[1].append(object_id)
    return [
        _VisualRegion(_round_rect(bbox), tuple(object_ids))
        for bbox, object_ids in grouped
    ]


def _owner_drafts(
    blocks: list[_SourceBlock],
    visuals: list[_VisualRegion],
    facts: ExtractedPageFacts,
) -> list[_OwnerDraft]:
    drafts: list[_OwnerDraft] = []
    margins: dict[str, list[_SourceBlock]] = {}
    for block in blocks:
        if block.role == "margin":
            owner = (
                "shared.margin.header"
                if block.bbox[1] < facts.page.height_points * 0.5
                else "shared.margin.footer"
            )
            margins.setdefault(owner, []).append(block)
            continue
        candidates = [
            region
            for region in visuals
            if _coverage(block.bbox, region.bbox) >= 0.35
            and _center_inside(block.bbox, region.bbox, tolerance=2.0)
        ]
        candidates.sort(key=lambda item: (_area(item.bbox), item.object_ids))
        visual = candidates[0] if candidates else None
        ambiguous = (
            len(candidates) > 1
            and _area(candidates[1].bbox)
            <= _area(candidates[0].bbox) * 1.05
            and candidates[1].object_ids != candidates[0].object_ids
        )
        if visual is not None:
            existing = next(
                (item for item in drafts if item.visual == visual),
                None,
            )
            if existing is None:
                drafts.append(_OwnerDraft([block], visual, ambiguous=ambiguous))
            else:
                existing.blocks.append(block)
                existing.ambiguous = existing.ambiguous or ambiguous
            continue
        compatible = [
            item
            for item in drafts
            if item.visual is None
            and item.margin_owner is None
            and _can_merge_owner(item.blocks, block)
        ]
        if compatible:
            min(
                compatible,
                key=lambda item: _rect_gap(_draft_bbox(item), block.bbox),
            ).blocks.append(block)
        else:
            drafts.append(_OwnerDraft([block], None))
    drafts.extend(
        _OwnerDraft(values, None, margin_owner=owner)
        for owner, values in sorted(margins.items())
    )
    return drafts


def _can_merge_owner(
    existing: list[_SourceBlock],
    candidate: _SourceBlock,
) -> bool:
    bbox = _union(tuple(item.bbox for item in existing))
    vertical_gap = max(
        0.0,
        candidate.bbox[1] - bbox[3],
        bbox[1] - candidate.bbox[3],
    )
    overlap = _axis_overlap(
        (bbox[0], bbox[2]),
        (candidate.bbox[0], candidate.bbox[2]),
    )
    smaller_width = min(
        bbox[2] - bbox[0],
        candidate.bbox[2] - candidate.bbox[0],
    )
    anchor_gap = abs(bbox[0] - candidate.bbox[0])
    size = max(
        [candidate.font_size, *(item.font_size for item in existing)]
    )
    return vertical_gap <= max(5.0, size * 1.25) and (
        overlap >= smaller_width * 0.45
        or anchor_gap <= max(8.0, size)
    )


def _owner_boundary(
    draft: _OwnerDraft,
    drafts: list[_OwnerDraft],
    facts: ExtractedPageFacts,
) -> Rect:
    source = _draft_bbox(draft)
    if draft.visual is not None:
        return _clip(
            _union((source, draft.visual.bbox)),
            facts.page.width_points,
            facts.page.height_points,
        )
    right = facts.page.width_points - 12.0
    bottom = min(
        facts.page.height_points - 12.0,
        max(
            source[3] + 8.0,
            source[1] + max(28.0, (source[3] - source[1]) * 1.8),
        ),
    )
    for obstacle in (
        _draft_bbox(item) for item in drafts if item is not draft
    ):
        if obstacle[0] >= source[2] and _axis_overlap(
            (source[1], source[3]),
            (obstacle[1], obstacle[3]),
        ) > 1.0:
            right = min(right, obstacle[0] - 3.0)
        if obstacle[1] >= source[3] and _axis_overlap(
            (source[0], source[2]),
            (obstacle[0], obstacle[2]),
        ) > 1.0:
            bottom = min(bottom, obstacle[1] - 3.0)
    return _clip(
        (
            source[0],
            source[1],
            max(source[2], right),
            max(source[3], bottom),
        ),
        facts.page.width_points,
        facts.page.height_points,
    )


def _container_groups(
    blocks: list[_SourceBlock],
) -> list[list[_SourceBlock]]:
    groups: list[list[_SourceBlock]] = []
    for block in sorted(blocks, key=lambda item: _position(item.bbox)):
        if groups and _can_merge_container(groups[-1], block):
            groups[-1].append(block)
        else:
            groups.append([block])
    return groups


def _can_merge_container(
    group: list[_SourceBlock],
    candidate: _SourceBlock,
) -> bool:
    previous = group[-1]
    if previous.role != candidate.role or previous.role == "margin":
        return False
    if (
        previous.font_name != candidate.font_name
        or abs(previous.font_size - candidate.font_size) > 0.35
        or previous.color_srgb != candidate.color_srgb
    ):
        return False
    vertical_gap = candidate.bbox[1] - previous.bbox[3]
    overlap = _axis_overlap(
        (previous.bbox[0], previous.bbox[2]),
        (candidate.bbox[0], candidate.bbox[2]),
    )
    smaller_width = min(
        previous.bbox[2] - previous.bbox[0],
        candidate.bbox[2] - candidate.bbox[0],
    )
    anchor_gap = abs(previous.bbox[0] - candidate.bbox[0])
    size = max(previous.font_size, candidate.font_size)
    return (
        -max(2.0, size * 0.3)
        <= vertical_gap
        <= max(6.0, size * 0.85)
        and (
            overlap >= smaller_width * 0.55
            or anchor_gap <= max(7.0, size * 0.8)
        )
    )


def _allowed_bbox(
    source: Rect,
    boundary: Rect,
    peers: tuple[Rect, ...],
    immutable: tuple[Rect, ...],
) -> Rect:
    right = boundary[2]
    bottom = boundary[3]
    for obstacle in (*peers, *immutable):
        vertical_overlap = _axis_overlap(
            (source[1], source[3]),
            (obstacle[1], obstacle[3]),
        )
        horizontal_overlap = _axis_overlap(
            (source[0], source[2]),
            (obstacle[0], obstacle[2]),
        )
        if obstacle[0] >= source[2] - 0.1 and vertical_overlap > 0.5:
            right = min(right, max(source[2], obstacle[0] - 1.0))
        if obstacle[1] >= source[3] - 0.1 and horizontal_overlap > 0.5:
            bottom = min(bottom, max(source[3], obstacle[1] - 1.0))
    return _round_rect(
        (
            source[0],
            source[1],
            max(source[2], right),
            max(source[3], bottom),
        )
    )


def _alignment(source: Rect, slot: Rect) -> str:
    slot_width = max(slot[2] - slot[0], 1.0)
    left_gap = source[0] - slot[0]
    right_gap = slot[2] - source[2]
    if source[2] - source[0] >= slot_width * 0.75:
        return "LEFT"
    if abs(left_gap - right_gap) <= max(5.0, slot_width * 0.08):
        return "CENTER"
    if (
        right_gap <= max(4.0, slot_width * 0.05)
        and left_gap > slot_width * 0.20
    ):
        return "RIGHT"
    return "LEFT"


def _merge_text(spans: tuple[KernelTextFact, ...]) -> str:
    if len(spans) == 1:
        return spans[0].text
    output: list[str] = []
    previous: KernelTextFact | None = None
    for span in spans:
        text = span.text.strip()
        if not text:
            continue
        separator = ""
        if previous is not None:
            separator = (
                "\n"
                if span.block_index != previous.block_index
                or span.line_index != previous.line_index
                else " "
            )
        output.append(separator + text)
        previous = span
    return "".join(output).strip()


def _dominant_span(
    spans: tuple[KernelTextFact, ...],
) -> KernelTextFact:
    return max(
        spans,
        key=lambda item: (
            sum(character.isalpha() for character in item.text),
            len(item.text.strip()),
            item.font_size,
        ),
    )


def _draft_bbox(draft: _OwnerDraft) -> Rect:
    return _union(tuple(item.bbox for item in draft.blocks))


def _union(rects: tuple[Rect, ...]) -> Rect:
    return _round_rect(
        (
            min(item[0] for item in rects),
            min(item[1] for item in rects),
            max(item[2] for item in rects),
            max(item[3] for item in rects),
        )
    )


def _clip(rect: Rect, width: float, height: float) -> Rect:
    return _round_rect(
        (
            max(0.0, rect[0]),
            max(0.0, rect[1]),
            min(width, rect[2]),
            min(height, rect[3]),
        )
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(value, 4) for value in rect)  # type: ignore[return-value]


def _position(rect: Rect) -> tuple[float, float]:
    return rect[1], rect[0]


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _coverage(inner: Rect, outer: Rect) -> float:
    intersection = _axis_overlap(
        (inner[0], inner[2]),
        (outer[0], outer[2]),
    ) * _axis_overlap((inner[1], inner[3]), (outer[1], outer[3]))
    return intersection / max(_area(inner), 1.0)


def _center_inside(
    inner: Rect,
    outer: Rect,
    *,
    tolerance: float,
) -> bool:
    center_x = (inner[0] + inner[2]) / 2.0
    center_y = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - tolerance <= center_x <= outer[2] + tolerance
        and outer[1] - tolerance <= center_y <= outer[3] + tolerance
    )


def _axis_overlap(
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _rect_distance(left: Rect, right: Rect) -> float:
    return max(
        0.0,
        left[0] - right[2],
        right[0] - left[2],
        left[1] - right[3],
        right[1] - left[3],
    )


def _rect_gap(left: Rect, right: Rect) -> float:
    return (
        max(0.0, left[0] - right[2], right[0] - left[2])
        + max(0.0, left[1] - right[3], right[1] - left[3])
    )
