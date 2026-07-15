from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import AnchoredBlocksTemplate, AnchoredContainer, BlockOwner, Rect


class AnchoredBlocksCapabilityError(RuntimeError):
    pass


_PURE_NUMBER = re.compile(r"^[\s+\-−–—$¥￥€£HKDUSDRMB,CNY()（）]*\d[\d\s,.]*\+?(?:%|％|‰)?$", re.IGNORECASE)
_PURE_MARK = re.compile(r"^(?:%|％|‰|[|｜]+)$")
_BULLET_MARK = re.compile(r"^[•·]+$")
_IDENTIFIER = re.compile(
    r"^(?:(?i:(?:https?|ftp)://\S+|www\.\S+)|[^\s@]+@[^\s@]+\.[^\s@]+|(?=[A-Z0-9/-]*\d)[A-Z][A-Z0-9/-]{2,}|\d{4}[./-]\d{1,2}[./-]\d{1,2})$"
)
_REQUIRED_LITERAL = re.compile(
    r"(?<![\w])(?:[+\-−–—]?[€£¥￥$]?\d(?:[\d,]*\d)?(?:\.\d+)?\+?(?:%|％|‰)?|(?=[A-Za-z0-9/-]*\d)(?=[A-Za-z0-9/-]*[A-Za-z])[A-Za-z][A-Za-z0-9/-]{2,})(?![\w])"
)


@dataclass(frozen=True)
class _TextBlock:
    block_index: int
    objects: tuple[TextObjectFact, ...]
    bbox: Rect
    font_size: float


@dataclass(frozen=True)
class _VisualRegion:
    bbox: Rect
    object_ids: tuple[str, ...]


@dataclass
class _OwnerDraft:
    blocks: list[_TextBlock]
    visual: _VisualRegion | None


def build_anchored_blocks_template(facts: PageFacts, source_pdf: Path | None = None) -> AnchoredBlocksTemplate:
    if not facts.text_objects:
        raise AnchoredBlocksCapabilityError("ANCHORED_BLOCKS_NATIVE_TEXT_REQUIRED")

    vertical_separators, horizontal_separators = _separator_lines(source_pdf, facts.page_index)
    median_size = statistics.median(item.font_size for item in facts.text_objects)
    blocks = _text_blocks(facts.text_objects)
    visual_regions = _visual_regions(facts)
    immutable_obstacles = [item.bbox for item in (*facts.image_objects, *facts.drawing_objects)]
    drafts = _owner_drafts(blocks, visual_regions, facts.width, facts.height)
    drafts.sort(key=lambda draft: (_union([block.bbox for block in draft.blocks])[1], _union([block.bbox for block in draft.blocks])[0]))

    owners: list[BlockOwner] = []
    containers: list[AnchoredContainer] = []
    protected_ids: list[str] = []
    for owner_order, draft in enumerate(drafts):
        owner_id = f"block-{owner_order:03d}"
        source_bbox = _union([block.bbox for block in draft.blocks])
        boundary = _owner_boundary(draft, drafts, facts.width, facts.height)
        boundary = _structural_owner_boundary(
            source_bbox,
            boundary,
            facts.width,
            facts.height,
            vertical_separators,
            horizontal_separators,
        )
        owner_objects = tuple(
            item
            for block in sorted(draft.blocks, key=lambda item: (item.bbox[1], item.bbox[0]))
            for item in sorted(block.objects, key=lambda item: (item.line_index, item.span_index, item.bbox[0]))
        )
        date_numeric_ids = _date_numeric_object_ids(owner_objects)
        owner_protected = tuple(
            item.object_id
            for item in owner_objects
            if item.object_id in date_numeric_ids
            or _is_protected(item, median_size, facts.height, visual_backed=draft.visual is not None)
        )
        protected_ids.extend(owner_protected)
        translatable_by_block = [
            (block, segment)
            for block in sorted(draft.blocks, key=lambda item: (item.bbox[1], item.bbox[0]))
            for segment in _translatable_segments(block, set(owner_protected))
        ]
        protected_bboxes = [item.bbox for item in owner_objects if item.object_id in owner_protected]
        translatable_groups = _container_groups(translatable_by_block)

        owner_containers: list[AnchoredContainer] = []
        for local_order, group in enumerate(translatable_groups):
            items = tuple(item for _block, block_items in group for item in block_items)
            source_text = _joined_text(items)
            if not _has_semantic_text(source_text):
                owner_protected += tuple(item.object_id for item in items)
                protected_ids.extend(item.object_id for item in items)
                continue
            source = _visible_source_bbox(items)
            slot = _cell_slot(source, boundary, vertical_separators, horizontal_separators)
            container_id = f"{owner_id}/container-{local_order:02d}"
            peer_bboxes = [
                _union([item.bbox for _other, items in other_group for item in items])
                for other_group in translatable_groups
                if other_group is not group
            ]
            immutable_bboxes = [
                obstacle
                for obstacle in immutable_obstacles
                if _coverage(source, obstacle) < 0.45 and not _center_inside(source, obstacle, tolerance=1.0)
            ]
            allowed = _allowed_bbox(
                source,
                slot,
                peer_bboxes,
                protected_bboxes,
                immutable_bboxes,
            )
            style = max(items, key=lambda item: item.font_size)
            alignment = _alignment(items, source, slot, owner_objects)
            if alignment in {"CENTER", "RIGHT"}:
                allowed = (
                    _expanded_left_edge(source, slot, allowed, peer_bboxes + protected_bboxes + immutable_bboxes),
                    allowed[1],
                    allowed[2],
                    allowed[3],
                )
            owner_containers.append(
                AnchoredContainer(
                    container_id=container_id,
                    block_owner_id=owner_id,
                    source_object_ids=tuple(item.object_id for item in items),
                    source_text=source_text,
                    source_bbox=_round_rect(source),
                    slot_bbox=_round_rect(slot),
                    allowed_bbox=_round_rect(allowed),
                    reading_order=len(containers) + len(owner_containers),
                    required_literals=_required_literals(source_text),
                    role=_role(items, median_size),
                    font_name=style.font_name,
                    font_size=round(max(item.font_size for item in items), 4),
                    color_srgb=style.color_srgb,
                    alignment=alignment,
                )
            )
        containers.extend(owner_containers)
        owners.append(
            BlockOwner(
                owner_id=owner_id,
                boundary_bbox=_round_rect(boundary),
                anchor=(round(source_bbox[0], 4), round(source_bbox[1], 4)),
                reading_order=owner_order,
                source_object_ids=tuple(item.object_id for item in owner_objects),
                container_ids=tuple(item.container_id for item in owner_containers),
                protected_object_ids=tuple(dict.fromkeys(owner_protected)),
                background_object_ids=draft.visual.object_ids if draft.visual else (),
                boundary_source="visual_region" if draft.visual else "derived_safe_region",
            )
        )

    if not containers:
        raise AnchoredBlocksCapabilityError("ANCHORED_BLOCKS_TRANSLATABLE_TEXT_REQUIRED")
    assigned = [object_id for item in containers for object_id in item.source_object_ids] + protected_ids
    expected = [item.object_id for item in facts.text_objects]
    if sorted(assigned) != sorted(expected) or len(assigned) != len(set(assigned)):
        raise AnchoredBlocksCapabilityError("ANCHORED_BLOCKS_TEXT_OWNERSHIP_INCOMPLETE")

    signature = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "block_owners": owners,
            "containers": containers,
            "protected_object_ids": tuple(protected_ids),
        }
    )
    return AnchoredBlocksTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        block_owners=tuple(owners),
        containers=tuple(containers),
        protected_object_ids=tuple(protected_ids),
        structure_sha256=signature,
    )


def _text_blocks(objects: tuple[TextObjectFact, ...]) -> list[_TextBlock]:
    grouped: dict[int, list[TextObjectFact]] = {}
    for item in objects:
        grouped.setdefault(item.block_index, []).append(item)
    return [
        _TextBlock(
            block_index,
            tuple(sorted(items, key=lambda item: (item.line_index, item.span_index, item.bbox[0]))),
            _union([item.bbox for item in items]),
            max(item.font_size for item in items),
        )
        for block_index, items in sorted(grouped.items())
    ]


def _separator_lines(
    source_pdf: Path | None,
    page_index: int,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    if source_pdf is None or not source_pdf.is_file():
        return [], []
    vertical: list[tuple[float, float, float]] = []
    horizontal: list[tuple[float, float, float]] = []
    with fitz.open(source_pdf) as document:
        for drawing in document[page_index].get_drawings():
            segments = []
            for item in drawing.get("items", []):
                if item[0] == "l":
                    segments.append((item[1], item[2]))
                elif item[0] == "re":
                    rect = fitz.Rect(item[1])
                    segments.extend(
                        (
                            (fitz.Point(rect.x0, rect.y0), fitz.Point(rect.x1, rect.y0)),
                            (fitz.Point(rect.x1, rect.y0), fitz.Point(rect.x1, rect.y1)),
                            (fitz.Point(rect.x1, rect.y1), fitz.Point(rect.x0, rect.y1)),
                            (fitz.Point(rect.x0, rect.y1), fitz.Point(rect.x0, rect.y0)),
                        )
                    )
            for start, end in segments:
                if abs(start.x - end.x) <= 0.75 and abs(start.y - end.y) >= 8.0:
                    vertical.append((round((start.x + end.x) / 2, 4), min(start.y, end.y), max(start.y, end.y)))
                elif abs(start.y - end.y) <= 0.75 and abs(start.x - end.x) >= 8.0:
                    horizontal.append((round((start.y + end.y) / 2, 4), min(start.x, end.x), max(start.x, end.x)))
    return _dedupe_lines(vertical), _dedupe_lines(horizontal)


def _dedupe_lines(lines: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    result: list[tuple[float, float, float]] = []
    for coordinate, start, end in sorted(lines):
        existing = next(
            (
                index
                for index, value in enumerate(result)
                if abs(value[0] - coordinate) <= 0.8
                and min(value[2], end) - max(value[1], start) >= -1.0
            ),
            None,
        )
        if existing is None:
            result.append((coordinate, start, end))
        else:
            previous = result[existing]
            result[existing] = (round((previous[0] + coordinate) / 2, 4), min(previous[1], start), max(previous[2], end))
    return result


def _cell_slot(
    source: Rect,
    boundary: Rect,
    vertical: list[tuple[float, float, float]],
    horizontal: list[tuple[float, float, float]],
) -> Rect:
    center_x = (source[0] + source[2]) / 2
    center_y = (source[1] + source[3]) / 2
    minimum_vertical_length = max(12.0, (source[3] - source[1]) * 1.5)
    x_values = [boundary[0], boundary[2]] + [
        coordinate
        for coordinate, start, end in vertical
        if boundary[0] + 1.0 < coordinate < boundary[2] - 1.0
        and end - start >= minimum_vertical_length
        and start - 2.0 <= center_y <= end + 2.0
        and (coordinate <= source[0] - 1.0 or coordinate >= source[2] + 1.0)
    ]
    minimum_horizontal_length = max(36.0, (source[2] - source[0]) * 0.9)
    y_values = [boundary[1], boundary[3]] + [
        coordinate
        for coordinate, start, end in horizontal
        if boundary[1] + 1.0 < coordinate < boundary[3] - 1.0
        and end - start >= minimum_horizontal_length
        and start - 2.0 <= center_x <= end + 2.0
        and (coordinate <= source[1] - 1.0 or coordinate >= source[3] + 1.0)
    ]
    left = max(value for value in x_values if value <= center_x)
    right = min(value for value in x_values if value >= center_x)
    top = max(value for value in y_values if value <= center_y)
    bottom = min(value for value in y_values if value >= center_y)
    if not (
        left - 1.5 <= source[0]
        and top - 1.5 <= source[1]
        and source[2] <= right + 1.5
        and source[3] <= bottom + 1.5
    ):
        return boundary
    return (left, top, right, bottom)


def _structural_owner_boundary(
    source: Rect,
    fallback: Rect,
    page_width: float,
    page_height: float,
    vertical: list[tuple[float, float, float]],
    horizontal: list[tuple[float, float, float]],
) -> Rect:
    page = (0.0, 0.0, page_width, page_height)
    candidate = _cell_slot(source, page, vertical, horizontal)
    if candidate == page:
        return fallback
    internal_edges = sum(
        (
            candidate[0] > 1.0,
            candidate[1] > 1.0,
            candidate[2] < page_width - 1.0,
            candidate[3] < page_height - 1.0,
        )
    )
    if internal_edges < 3:
        return fallback
    if candidate[2] - candidate[0] > page_width * 0.65:
        return fallback
    if candidate[3] - candidate[1] > page_height * 0.35:
        return fallback
    return candidate


def _date_numeric_object_ids(objects: tuple[TextObjectFact, ...]) -> set[str]:
    rows: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        rows.setdefault((item.block_index, item.line_index), []).append(item)
    result = set()
    for items in rows.values():
        ordered = sorted(items, key=lambda item: (item.bbox[0], item.span_index))
        row_date_ids = set()
        for index, item in enumerate(ordered):
            if not _PURE_NUMBER.fullmatch(item.text.strip()):
                continue
            if any(
                re.match(r"^[年月日]", following.text.strip())
                and following.bbox[0] <= item.bbox[2] + 3.0
                for following in ordered[index + 1 :]
            ):
                row_date_ids.add(item.object_id)
        if len(row_date_ids) >= 2:
            result.update(row_date_ids)
    return result


def _visual_regions(facts: PageFacts) -> list[_VisualRegion]:
    page_area = facts.width * facts.height
    raw = [(item.object_id, item.bbox) for item in (*facts.drawing_objects, *facts.image_objects)]
    grouped: list[tuple[Rect, list[str]]] = []
    for object_id, bbox in raw:
        clipped = _clip(bbox, facts.width, facts.height)
        width = clipped[2] - clipped[0]
        height = clipped[3] - clipped[1]
        area = width * height
        if width < 36 or height < 20 or area < page_area * 0.003 or area > page_area * 0.36:
            continue
        existing = next((row for row in grouped if _rect_distance(row[0], clipped) <= 3.0), None)
        if existing:
            existing[1].append(object_id)
        else:
            grouped.append((clipped, [object_id]))
    return [_VisualRegion(_round_rect(bbox), tuple(ids)) for bbox, ids in grouped]


def _owner_drafts(
    blocks: list[_TextBlock],
    visual_regions: list[_VisualRegion],
    page_width: float,
    page_height: float,
) -> list[_OwnerDraft]:
    del page_width, page_height
    drafts: list[_OwnerDraft] = []
    for block in blocks:
        candidates = [
            region
            for region in visual_regions
            if _coverage(block.bbox, region.bbox) >= 0.45
            and _center_inside(block.bbox, region.bbox, tolerance=2.0)
        ]
        visual = min(candidates, key=lambda item: _area(item.bbox), default=None)
        if visual:
            existing = next((draft for draft in drafts if draft.visual == visual), None)
            if existing:
                existing.blocks.append(block)
            else:
                drafts.append(_OwnerDraft([block], visual))
            continue
        compatible = [draft for draft in drafts if draft.visual is None and _can_merge_derived(draft.blocks, block)]
        if compatible:
            min(compatible, key=lambda draft: _rect_gap(_union([item.bbox for item in draft.blocks]), block.bbox)).blocks.append(block)
        else:
            drafts.append(_OwnerDraft([block], None))
    return drafts


def _can_merge_derived(existing: list[_TextBlock], candidate: _TextBlock) -> bool:
    bbox = _union([item.bbox for item in existing])
    vertical_gap = max(0.0, candidate.bbox[1] - bbox[3], bbox[1] - candidate.bbox[3])
    overlap = _axis_overlap((bbox[0], bbox[2]), (candidate.bbox[0], candidate.bbox[2]))
    smaller_width = min(bbox[2] - bbox[0], candidate.bbox[2] - candidate.bbox[0])
    anchor_gap = abs(bbox[0] - candidate.bbox[0])
    size = max(candidate.font_size, max(item.font_size for item in existing))
    return vertical_gap <= max(5.0, size * 1.25) and (
        overlap >= smaller_width * 0.45 or anchor_gap <= max(8.0, size)
    )


def _owner_boundary(
    draft: _OwnerDraft,
    all_drafts: list[_OwnerDraft],
    page_width: float,
    page_height: float,
) -> Rect:
    source = _union([block.bbox for block in draft.blocks])
    if draft.visual:
        return _clip(_union([source, draft.visual.bbox]), page_width, page_height)
    right = page_width - 12.0
    bottom = min(page_height - 12.0, max(source[3] + 8.0, source[1] + max(28.0, (source[3] - source[1]) * 1.8)))
    obstacles = [
        _union([block.bbox for block in other.blocks])
        for other in all_drafts
        if other is not draft
    ]
    for other in all_drafts:
        if other is draft:
            continue
        obstacle = _union([block.bbox for block in other.blocks])
        repeated_column_anchor = (
            obstacle[0] >= source[2] + max(36.0, page_width * 0.06)
            and sum(abs(peer[0] - obstacle[0]) <= 6.0 for peer in obstacles) >= 2
        )
        if obstacle[0] >= source[2] and (
            _axis_overlap((source[1], source[3]), (obstacle[1], obstacle[3])) > 1.0
            or repeated_column_anchor
        ):
            right = min(right, obstacle[0] - 3.0)
        if obstacle[1] >= source[3] and _axis_overlap((source[0], source[2]), (obstacle[0], obstacle[2])) > 1.0:
            bottom = min(bottom, obstacle[1] - 3.0)
    return _clip((source[0], source[1], max(source[2], right), max(source[3], bottom)), page_width, page_height)


def _allowed_bbox(
    source: Rect,
    boundary: Rect,
    peers: list[Rect],
    protected: list[Rect],
    immutable: list[Rect],
) -> Rect:
    right = boundary[2]
    bottom = boundary[3]
    for obstacle in peers:
        vertical_gap = max(0.0, obstacle[1] - source[3], source[1] - obstacle[3])
        horizontal_gap = max(0.0, obstacle[0] - source[2], source[0] - obstacle[2])
        if obstacle[0] >= source[2] - 0.1 and vertical_gap <= 8.0:
            right = min(right, max(source[2], obstacle[0] - 1.0))
        if obstacle[1] >= source[1] + 4.0 and horizontal_gap <= 8.0:
            bottom = min(bottom, max(source[3], obstacle[1] - 1.0))
    for obstacle in protected:
        can_cut_right = (
            obstacle[0] >= source[0] + 4.0
            and _axis_overlap((source[1], source[3]), (obstacle[1], obstacle[3])) > 0.5
        )
        can_cut_bottom = (
            obstacle[1] >= source[1] + 4.0
            and _axis_overlap((source[0], source[2]), (obstacle[0], obstacle[2])) > 0.5
        )
        if can_cut_right and can_cut_bottom:
            right_limit = obstacle[0] - 1.0
            bottom_limit = obstacle[1] - 1.0
            source_center_y = (source[1] + source[3]) / 2
            obstacle_center_y = (obstacle[1] + obstacle[3]) / 2
            if obstacle_center_y - source_center_y >= max(2.0, (source[3] - source[1]) * 0.45):
                bottom = min(bottom, max(source[3], bottom_limit))
            else:
                right = min(right, right_limit)
        elif can_cut_right:
            right = min(right, obstacle[0] - 1.0)
        elif can_cut_bottom:
            bottom = min(bottom, max(source[3], obstacle[1] - 1.0))
    for obstacle in immutable:
        if obstacle[0] >= source[2] - 0.1 and _axis_overlap((source[1], source[3]), (obstacle[1], obstacle[3])) > 0.5:
            right = min(right, max(source[2], obstacle[0] - 1.0))
        if obstacle[1] >= source[3] - 0.1 and _axis_overlap((source[0], source[2]), (obstacle[0], obstacle[2])) > 0.5:
            bottom = min(bottom, max(source[3], obstacle[1] - 1.0))
    return (
        source[0],
        source[1],
        max(source[0] + 4.0, right),
        max(source[1] + 4.0, bottom),
    )


def _expanded_left_edge(source: Rect, slot: Rect, allowed: Rect, obstacles: list[Rect]) -> float:
    left = slot[0] + 1.0
    for obstacle in obstacles:
        if (
            obstacle[2] <= source[0] + 1.0
            and _axis_overlap((source[1], source[3]), (obstacle[1], obstacle[3])) > 0.5
        ):
            left = max(left, obstacle[2] + 1.0)
    return min(source[0], allowed[2] - 4.0, left)


def _is_protected(
    item: TextObjectFact,
    median_size: float,
    page_height: float,
    *,
    visual_backed: bool,
) -> bool:
    text = item.text.strip()
    if not text:
        return True
    if visual_backed and item.font_size < 5.0:
        return True
    in_page_margin = item.bbox[1] < page_height * 0.035 or item.bbox[1] > page_height * 0.94
    if in_page_margin and _PURE_NUMBER.fullmatch(text):
        return True
    if item.bbox[3] > page_height * 0.925 and not in_page_margin:
        return True
    if _BULLET_MARK.fullmatch(text):
        return item.span_index == 0
    if _IDENTIFIER.fullmatch(text) or _PURE_MARK.fullmatch(text):
        return True
    return bool(_PURE_NUMBER.fullmatch(text)) and item.font_size >= max(11.0, median_size * 1.32)


def _has_semantic_text(text: str) -> bool:
    return any(character.isalpha() or "\u3400" <= character <= "\u9fff" for character in text)


def _container_groups(
    rows: list[tuple[_TextBlock, tuple[TextObjectFact, ...]]],
) -> list[list[tuple[_TextBlock, tuple[TextObjectFact, ...]]]]:
    groups: list[list[tuple[_TextBlock, tuple[TextObjectFact, ...]]]] = []
    for block, items in rows:
        candidate = (block, items)
        compatible = [group for group in groups if _can_merge_container_group(group, candidate)]
        if compatible:
            min(
                compatible,
                key=lambda group: _rect_gap(
                    _union([_union([item.bbox for item in row[1]]) for row in group]),
                    _union([item.bbox for item in items]),
                ),
            ).append(candidate)
        else:
            groups.append([candidate])
    return groups


def _can_merge_container_group(
    group: list[tuple[_TextBlock, tuple[TextObjectFact, ...]]],
    candidate: tuple[_TextBlock, tuple[TextObjectFact, ...]],
) -> bool:
    candidate_block, candidate_items = candidate
    if _has_leading_block_barrier(candidate_block, candidate_items):
        return False
    group_rows = [(_union([item.bbox for item in items]), items) for _block, items in group]
    bbox = _union([row_bbox for row_bbox, _items in group_rows])
    previous_bbox, previous_items = max(group_rows, key=lambda row: row[0][3])
    candidate_bbox = _union([item.bbox for item in candidate_items])
    vertical_gap = candidate_bbox[1] - bbox[3]
    overlap = _axis_overlap((bbox[0], bbox[2]), (candidate_bbox[0], candidate_bbox[2]))
    smaller_width = min(bbox[2] - bbox[0], candidate_bbox[2] - candidate_bbox[0])
    anchor_gap = abs(previous_bbox[0] - candidate_bbox[0])
    previous_size = max(item.font_size for item in previous_items)
    candidate_size = max(item.font_size for item in candidate_items)
    if not _style_compatible(_dominant_style(previous_items), _dominant_style(candidate_items)):
        return False
    size = max(previous_size, candidate_size)
    size_ratio = min(previous_size, candidate_size) / max(previous_size, candidate_size)
    vertical_overlap = _axis_overlap((previous_bbox[1], previous_bbox[3]), (candidate_bbox[1], candidate_bbox[3]))
    smaller_height = min(previous_bbox[3] - previous_bbox[1], candidate_bbox[3] - candidate_bbox[1])
    interleaved = anchor_gap <= 2.0 and vertical_overlap >= smaller_height * 0.5
    sequential = (
        -max(2.0, size * 0.3) <= vertical_gap <= max(6.0, size * 0.85)
        and (overlap >= smaller_width * 0.55 or anchor_gap <= max(6.0, size * 0.7))
    )
    return (
        all(item[0].block_index != candidate_block.block_index for item in group)
        and size_ratio >= 0.72
        and (interleaved or sequential)
    )


def _has_leading_block_barrier(
    block: _TextBlock,
    segment: tuple[TextObjectFact, ...],
) -> bool:
    segment_ids = {item.object_id for item in segment}
    first = min((item.line_index, item.bbox[0]) for item in segment)
    return any(
        (item.line_index, item.bbox[0]) < first
        for item in block.objects
        if item.object_id not in segment_ids
    )


def _translatable_segments(
    block: _TextBlock,
    protected_ids: set[str],
) -> list[tuple[TextObjectFact, ...]]:
    segments: list[list[TextObjectFact]] = []
    generations: list[int] = []
    generation = 0
    for item in sorted(block.objects, key=lambda value: (value.line_index, value.bbox[0], value.span_index)):
        if item.object_id in protected_ids:
            generation += 1
            continue
        compatible = [
            segment
            for segment, segment_generation in zip(segments, generations, strict=True)
            if segment_generation == generation and _can_join_segment(segment, item)
        ]
        if compatible:
            min(compatible, key=lambda segment: _rect_gap(_union([value.bbox for value in segment]), item.bbox)).append(item)
        else:
            segments.append([item])
            generations.append(generation)
    return [tuple(segment) for segment in segments]


def _can_join_segment(segment: list[TextObjectFact], candidate: TextObjectFact) -> bool:
    previous = max(segment, key=lambda item: (item.line_index, item.bbox[0]))
    bbox = _union([item.bbox for item in segment])
    if candidate.line_index == previous.line_index:
        gap = candidate.bbox[0] - previous.bbox[2]
        size = max(previous.font_size, candidate.font_size)
        return -max(5.0, size * 0.4) <= gap <= max(8.0, size * 0.9)
    horizontal_gap = candidate.bbox[0] - previous.bbox[2]
    vertical_overlap = _axis_overlap((previous.bbox[1], previous.bbox[3]), (candidate.bbox[1], candidate.bbox[3]))
    size = max(previous.font_size, candidate.font_size)
    if vertical_overlap > 0.5 and -max(1.0, size * 1.1) <= horizontal_gap <= max(
        8.0,
        size * 0.9,
    ):
        return True
    if not _style_compatible(_dominant_style(tuple(segment)), candidate):
        return False
    vertical_gap = candidate.bbox[1] - bbox[3]
    anchor_gap = abs(candidate.bbox[0] - bbox[0])
    overlap = _axis_overlap((bbox[0], bbox[2]), (candidate.bbox[0], candidate.bbox[2]))
    smaller_width = min(bbox[2] - bbox[0], candidate.bbox[2] - candidate.bbox[0])
    return (
        -max(2.5, size * 0.3) <= vertical_gap <= max(6.0, size * 0.85)
        and (anchor_gap <= max(7.0, size * 0.8) or overlap >= smaller_width * 0.55)
    )


def _style_compatible(left: TextObjectFact, right: TextObjectFact) -> bool:
    return (
        left.font_name == right.font_name
        and abs(left.font_size - right.font_size) <= 0.25
        and left.color_srgb == right.color_srgb
    )


def _dominant_style(items: tuple[TextObjectFact, ...]) -> TextObjectFact:
    return max(
        items,
        key=lambda item: (
            sum(character.isalpha() or "\u3400" <= character <= "\u9fff" for character in item.text),
            len(item.text.strip()),
        ),
    )


def _required_literals(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0) for match in _REQUIRED_LITERAL.finditer(text)))


def _joined_text(items: tuple[TextObjectFact, ...]) -> str:
    rows: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in items:
        rows.setdefault((item.block_index, item.line_index), []).append(item)
    values = []
    for row in sorted(rows):
        parts = [item.text.strip() for item in sorted(rows[row], key=lambda item: (item.bbox[0], item.span_index))]
        values.append(" ".join(part for part in parts if part))
    return " ".join(value for value in values if value).strip()


def _visible_source_bbox(items: tuple[TextObjectFact, ...]) -> Rect:
    source = _union([item.bbox for item in items])
    first = min(items, key=lambda item: (item.bbox[1], item.bbox[0], item.span_index))
    leading_spaces = len(first.text) - len(first.text.lstrip())
    if not leading_spaces:
        return source
    leading_advance = min(
        first.font_size * 0.24 * leading_spaces,
        max(0.0, (first.bbox[2] - first.bbox[0]) * 0.4),
    )
    return (round(source[0] + leading_advance, 4), source[1], source[2], source[3])


def _role(items: tuple[TextObjectFact, ...], median_size: float) -> str:
    maximum = max(item.font_size for item in items)
    return "heading" if maximum >= median_size * 1.25 else "body"


def _alignment(
    items: tuple[TextObjectFact, ...],
    source: Rect,
    slot: Rect,
    owner_objects: tuple[TextObjectFact, ...],
) -> str:
    if _joined_text(items).lstrip().startswith(("•", "·", "▪", "◦", "‣", "●")):
        return "LEFT"
    item_ids = {item.object_id for item in items}
    item_rows = {(item.block_index, item.line_index) for item in items}
    foreign_row_items = [
        item
        for item in owner_objects
        if (
        item.object_id not in item_ids
        and (item.block_index, item.line_index) in item_rows
        )
    ]
    if foreign_row_items:
        if all(item.bbox[0] >= source[2] - 1.0 for item in foreign_row_items):
            return "RIGHT"
        return "LEFT"
    rows: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in items:
        rows.setdefault((item.block_index, item.line_index), []).append(item)
    row_bboxes = [_union([item.bbox for item in values]) for values in rows.values()]
    slot_width = slot[2] - slot[0]
    left_tolerance = max(3.0, max(item.font_size for item in items) * 0.4)
    left_rows = sum(abs(bbox[0] - slot[0]) <= left_tolerance for bbox in row_bboxes)
    if left_rows >= max(1, (len(row_bboxes) * 8 + 9) // 10):
        return "LEFT"
    stable_left_tolerance = max(6.0, max(item.font_size for item in items) * 0.6)
    if len(row_bboxes) >= 2 and max(bbox[0] for bbox in row_bboxes) - min(
        bbox[0] for bbox in row_bboxes
    ) <= stable_left_tolerance:
        return "LEFT"
    if len(row_bboxes) == 1 and source[2] - source[0] >= slot_width * 0.75:
        current_style = _dominant_style(items)
        peer_rows: dict[tuple[int, int], list[TextObjectFact]] = {}
        for item in owner_objects:
            if item.object_id not in item_ids:
                peer_rows.setdefault((item.block_index, item.line_index), []).append(item)
        for values in peer_rows.values():
            peer_bbox = _union([item.bbox for item in values])
            vertical_gap = max(0.0, peer_bbox[1] - source[3], source[1] - peer_bbox[3])
            if (
                _style_compatible(current_style, _dominant_style(tuple(values)))
                and abs(peer_bbox[0] - source[0]) <= stable_left_tolerance
                and vertical_gap <= max(12.0, current_style.font_size * 2.2)
            ):
                return "LEFT"
    center = (slot[0] + slot[2]) / 2
    center_tolerance = max(5.0, slot_width * 0.08)
    centered_rows = sum(abs((bbox[0] + bbox[2]) / 2 - center) <= center_tolerance for bbox in row_bboxes)
    if centered_rows >= max(1, (len(row_bboxes) * 7 + 9) // 10):
        return "CENTER"
    right_tolerance = max(4.0, slot_width * 0.05)
    right_rows = sum(abs(slot[2] - bbox[2]) <= right_tolerance for bbox in row_bboxes)
    if (
        len(row_bboxes) <= 3
        and right_rows >= max(1, (len(row_bboxes) * 8 + 9) // 10)
        and source[0] > slot[0] + slot_width * 0.2
    ):
        return "RIGHT"
    return "LEFT"


def _union(rects: list[Rect]) -> Rect:
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def _clip(rect: Rect, width: float, height: float) -> Rect:
    return (max(0.0, rect[0]), max(0.0, rect[1]), min(width, rect[2]), min(height, rect[3]))


def _coverage(inner: Rect, outer: Rect) -> float:
    intersection = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0])) * max(
        0.0, min(inner[3], outer[3]) - max(inner[1], outer[1])
    )
    return intersection / max(_area(inner), 0.001)


def _center_inside(inner: Rect, outer: Rect, *, tolerance: float) -> bool:
    center_x = (inner[0] + inner[2]) / 2.0
    center_y = (inner[1] + inner[3]) / 2.0
    return (
        outer[0] - tolerance <= center_x <= outer[2] + tolerance
        and outer[1] - tolerance <= center_y <= outer[3] + tolerance
    )


def _axis_overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rect_distance(left: Rect, right: Rect) -> float:
    return max(abs(left[index] - right[index]) for index in range(4))


def _rect_gap(left: Rect, right: Rect) -> float:
    return max(0.0, right[0] - left[2], left[0] - right[2]) + max(0.0, right[1] - left[3], left[1] - right[3])


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]
