from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import ContentsColumnBand, ContentsContainer, ContentsEntry, ContentsTemplate, Rect


class ContentsCapabilityError(RuntimeError):
    pass


_PAGE_TOKEN = re.compile(
    r"^\s*(?:(?:p(?:age|ages)?|頁(?:次)?|页(?:次)?)\s*)?"
    r"(?:\d+|[ivxlcdm]+)(?:\s*[-–—]\s*(?:\d+|[ivxlcdm]+))?\s*$",
    re.IGNORECASE,
)
_NONSEMANTIC_LITERAL = re.compile(
    r"^(?:(?:https?|ftp)://\S+|www\.\S+|[^\s@]+@[^\s@]+\.[^\s@]+)$",
    re.IGNORECASE,
)
_TITLE_TEXTS = {"contents", "tableofcontents", "目录", "目錄", "目录表", "目錄表"}
_COLUMN_HEADERS = {"page", "pages", "页", "頁", "页次", "頁次"}


@dataclass(frozen=True)
class _Line:
    object_ids: tuple[str, ...]
    text: str
    bbox: Rect
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class _EntryDraft:
    entry_id: str
    column_index: int
    page_anchor: _Line
    primary_lines: tuple[_Line, ...]
    companion_lines: list[_Line]
    hierarchy_level: int = 1

    @property
    def source_bbox(self) -> Rect:
        return _union([line.bbox for line in (*self.primary_lines, *self.companion_lines)])

    @property
    def primary_bbox(self) -> Rect:
        return _union([line.bbox for line in self.primary_lines])

    @property
    def source_text(self) -> str:
        return "\n".join(line.text for line in self.primary_lines)


@dataclass
class _ContainerDraft:
    source_lines: tuple[_Line, ...]
    role: str
    column_index: int
    hierarchy_level: int
    entry_id: str | None

    @property
    def bbox(self) -> Rect:
        return _union([line.bbox for line in self.source_lines])

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.source_lines)


def is_protected_page_text(text: str) -> bool:
    return bool(_PAGE_TOKEN.fullmatch(text.replace("\ufeff", "").strip()))


def is_protected_literal_text(text: str) -> bool:
    normalized = text.replace("\ufeff", "").strip()
    return is_protected_page_text(normalized) or bool(_NONSEMANTIC_LITERAL.fullmatch(normalized))


def build_contents_template(facts: PageFacts) -> ContentsTemplate:
    lines = _logical_lines(facts.text_objects)
    if not lines:
        raise ContentsCapabilityError("CONTENTS_NATIVE_TEXT_REQUIRED")
    protected_lines = tuple(line for line in lines if is_protected_literal_text(line.text))
    nonnumeric_lines = tuple(line for line in lines if line not in protected_lines)
    title = _find_title(nonnumeric_lines)
    if title is None:
        raise ContentsCapabilityError("CONTENTS_TITLE_NOT_FOUND")

    anchor_clusters = _page_anchor_clusters(protected_lines, nonnumeric_lines, facts.height)
    if not anchor_clusters:
        raise ContentsCapabilityError("CONTENTS_REPEATED_PAGE_ANCHORS_NOT_FOUND")

    used_primary: set[tuple[int, int]] = set()
    matched: list[tuple[int, _Line, _Line]] = []
    for column_index, cluster in enumerate(anchor_clusters):
        for anchor in sorted(cluster, key=lambda item: (item.bbox[1], item.bbox[0])):
            primary = _match_label(anchor, nonnumeric_lines, title, used_primary, facts.height)
            if primary is None:
                continue
            used_primary.add((primary.block_index, primary.line_index))
            matched.append((column_index, anchor, primary))
    if len(matched) < 3:
        raise ContentsCapabilityError("CONTENTS_ENTRY_RELATIONS_INSUFFICIENT")

    matched_keys = {(line.block_index, line.line_index) for _, _, line in matched}
    entries: list[_EntryDraft] = []
    consumed_lines: set[tuple[int, int]] = set()
    for order, (column_index, anchor, primary) in enumerate(
        sorted(matched, key=lambda item: (item[0], item[2].bbox[1], item[2].bbox[0]))
    ):
        continuation = _continuation_lines(primary, nonnumeric_lines, matched_keys, title)
        primary_lines = (primary, *continuation)
        consumed_lines.update((line.block_index, line.line_index) for line in primary_lines)
        entries.append(
            _EntryDraft(
                entry_id=f"contents-entry-{order:03d}",
                column_index=column_index,
                page_anchor=anchor,
                primary_lines=primary_lines,
                companion_lines=[],
            )
        )

    column_bands = _column_bands(entries, facts.width, facts.height)
    column_by_index = {band.column_index: band for band in column_bands}
    structural: list[_ContainerDraft] = []
    entry_font = statistics.median(line.font_size for entry in entries for line in entry.primary_lines)
    content_bottom = max(entry.page_anchor.bbox[3] for entry in entries)
    for line in nonnumeric_lines:
        key = (line.block_index, line.line_index)
        if line == title or key in consumed_lines:
            continue
        if line.bbox[1] < title.bbox[1] - 0.5 or line.bbox[1] > content_bottom + max(25.0, entry_font * 3.0):
            continue
        column_index = _nearest_column(line, entries, column_bands)
        same_row = _nearest_same_row_entry(line, entries, column_index)
        if same_row is not None:
            same_row.companion_lines.append(line)
            consumed_lines.add(key)
            continue
        normalized = _normalized(line.text)
        is_heading = (
            normalized in _COLUMN_HEADERS
            or line.font_size >= entry_font * 1.18
            or (_is_upper_heading(line.text) and _has_following_entry(line, entries, column_index, entry_font))
        )
        if not is_heading:
            continue
        role = "column_header" if normalized in _COLUMN_HEADERS else "group_heading"
        if role == "group_heading" and not _has_following_entry(line, entries, column_index, entry_font):
            following = [
                entry
                for entry in entries
                if 0.0 < entry.primary_bbox[1] - line.bbox[3] <= max(65.0, entry_font * 6.0)
            ]
            if following:
                column_index = min(following, key=lambda entry: entry.primary_bbox[1] - line.bbox[3]).column_index
        structural.append(_ContainerDraft((line,), role, column_index, 1, None))
        consumed_lines.add(key)

    _assign_hierarchy(entries, structural)
    auxiliary = _auxiliary_drafts(
        tuple(
            line
            for line in nonnumeric_lines
            if line != title
            and (line.block_index, line.line_index) not in consumed_lines
            and any(character.isalpha() for character in line.text)
        )
    )
    drafts = _container_drafts(title, entries, structural, auxiliary)
    containers = _materialize_containers(
        drafts,
        entries,
        column_by_index,
        facts.width,
        facts.height,
        lines,
    )
    ids_by_entry: dict[str, list[str]] = {entry.entry_id: [] for entry in entries}
    for container in containers:
        if container.entry_id:
            ids_by_entry[container.entry_id].append(container.container_id)

    final_entries = tuple(
        ContentsEntry(
            entry_id=entry.entry_id,
            order=index,
            column_index=entry.column_index,
            hierarchy_level=entry.hierarchy_level,
            container_ids=tuple(ids_by_entry[entry.entry_id]),
            page_anchor_object_ids=entry.page_anchor.object_ids,
            page_number_text=entry.page_anchor.text.strip(),
            page_anchor_bbox=_round_rect(entry.page_anchor.bbox),
            row_bbox=_round_rect(_union([entry.source_bbox, entry.page_anchor.bbox])),
        )
        for index, entry in enumerate(entries)
    )
    protected_ids = tuple(sorted(object_id for line in protected_lines for object_id in line.object_ids))
    structure_sha256 = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "columns": column_bands,
            "containers": containers,
            "entries": final_entries,
            "protected_object_ids": protected_ids,
        }
    )
    return ContentsTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        column_bands=column_bands,
        containers=containers,
        entries=final_entries,
        protected_object_ids=protected_ids,
        structure_sha256=structure_sha256,
    )


def _logical_lines(objects: tuple[TextObjectFact, ...]) -> tuple[_Line, ...]:
    grouped: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        grouped.setdefault((item.block_index, item.line_index), []).append(item)
    lines: list[_Line] = []
    for (block_index, line_index), items in grouped.items():
        items.sort(key=lambda item: (item.bbox[0], item.span_index))
        pieces: list[str] = []
        previous: TextObjectFact | None = None
        for item in items:
            if previous is not None and item.bbox[0] - previous.bbox[2] > max(previous.font_size, item.font_size) * 0.45:
                pieces.append(" ")
            pieces.append(item.text)
            previous = item
        text = "".join(pieces).replace("\ufeff", "").strip()
        if not text:
            continue
        style = max(items, key=lambda item: (item.font_size, item.span_index))
        lines.append(
            _Line(
                tuple(item.object_id for item in items),
                text,
                _round_rect(_union([item.bbox for item in items])),
                style.font_name,
                max(item.font_size for item in items),
                style.color_srgb,
                block_index,
                line_index,
            )
        )
    return tuple(sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0], line.block_index, line.line_index)))


def _find_title(lines: tuple[_Line, ...]) -> _Line | None:
    candidates = [line for line in lines if _normalized(line.text) in _TITLE_TEXTS]
    return max(candidates, key=lambda line: (line.font_size, -line.bbox[1])) if candidates else None


def _page_anchor_clusters(
    protected: tuple[_Line, ...],
    nonnumeric: tuple[_Line, ...],
    page_height: float,
) -> tuple[tuple[_Line, ...], ...]:
    reference_sizes = [line.font_size for line in nonnumeric if line.font_size <= 20.0]
    reference = statistics.median(reference_sizes) if reference_sizes else 10.0
    candidates = [
        line
        for line in protected
        if page_height * 0.07 <= line.cy <= page_height * 0.92
        and (line.font_size <= max(18.0, reference * 1.6) or line.text.strip().casefold().startswith("p"))
    ]
    clusters: list[list[_Line]] = []
    for line in sorted(candidates, key=lambda item: item.cx):
        if clusters and abs(line.cx - statistics.mean(item.cx for item in clusters[-1])) <= 24.0:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    repeated = [cluster for cluster in clusters if len(cluster) >= 3]
    return tuple(tuple(sorted(cluster, key=lambda line: line.cy)) for cluster in repeated)


def _match_label(
    anchor: _Line,
    candidates: tuple[_Line, ...],
    title: _Line,
    used: set[tuple[int, int]],
    page_height: float,
) -> _Line | None:
    scored: list[tuple[float, _Line]] = []
    for line in candidates:
        key = (line.block_index, line.line_index)
        if line == title or key in used or line.cy < title.cy or line.cy > page_height * 0.92:
            continue
        vertical = abs(line.cy - anchor.cy)
        if vertical > max(18.0, line.height * 1.65, anchor.height * 1.65):
            continue
        same_block = line.block_index == anchor.block_index
        side_penalty = 0.0
        if line.bbox[0] > anchor.bbox[2] + 2.0 and not same_block:
            side_penalty = 100.0
        horizontal_gap = max(0.0, anchor.bbox[0] - line.bbox[2], line.bbox[0] - anchor.bbox[2])
        score = vertical * 8.0 + horizontal_gap * 0.03 + side_penalty - (80.0 if same_block else 0.0)
        scored.append((score, line))
    return min(scored, key=lambda item: item[0])[1] if scored else None


def _continuation_lines(
    primary: _Line,
    candidates: tuple[_Line, ...],
    matched_keys: set[tuple[int, int]],
    title: _Line,
) -> tuple[_Line, ...]:
    same_block = sorted(
        (
            line
            for line in candidates
            if line.block_index == primary.block_index
            and line.line_index > primary.line_index
            and line != title
        ),
        key=lambda line: line.line_index,
    )
    result: list[_Line] = []
    previous = primary
    for line in same_block:
        key = (line.block_index, line.line_index)
        if line.line_index != previous.line_index + 1 or key in matched_keys:
            break
        gap = line.bbox[1] - previous.bbox[3]
        if gap > max(4.0, primary.font_size * 0.75):
            break
        result.append(line)
        previous = line

    used_keys = {(line.block_index, line.line_index) for line in result}
    left_tolerance = max(2.0, primary.font_size * 0.8)
    vertical_tolerance = max(2.0, primary.font_size * 0.8)
    font_tolerance = max(0.5, primary.font_size * 0.2)
    aligned = sorted(
        (
            line
            for line in candidates
            if line != primary
            and line != title
            and (line.block_index, line.line_index) not in used_keys
            and abs(line.bbox[0] - primary.bbox[0]) <= left_tolerance
        ),
        key=lambda line: (line.bbox[1], line.bbox[0]),
    )
    for line in aligned:
        if line.bbox[1] < previous.bbox[1] - 1.0:
            continue
        gap = line.bbox[1] - previous.bbox[3]
        if gap < -1.0:
            continue
        if gap > vertical_tolerance:
            break
        key = (line.block_index, line.line_index)
        if key in matched_keys:
            break
        if abs(line.font_size - primary.font_size) > font_tolerance or line.color_srgb != primary.color_srgb:
            break
        result.append(line)
        previous = line
    return tuple(result)


def _column_bands(entries: list[_EntryDraft], width: float, height: float) -> tuple[ContentsColumnBand, ...]:
    groups: dict[int, list[_EntryDraft]] = {}
    for entry in entries:
        groups.setdefault(entry.column_index, []).append(entry)
    ordered = [groups[index] for index in sorted(groups)]
    extents = [
        (
            statistics.median(min(entry.source_bbox[0], entry.page_anchor.bbox[0]) for entry in group),
            statistics.median(max(entry.source_bbox[2], entry.page_anchor.bbox[2]) for entry in group),
        )
        for group in ordered
    ]
    boundaries = [0.0]
    for left, right in zip(extents, extents[1:]):
        boundaries.append((left[1] + right[0]) / 2.0 if left[1] < right[0] else (left[1] + right[1]) / 2.0)
    boundaries.append(width)
    bands: list[ContentsColumnBand] = []
    for index, group in enumerate(ordered):
        relations = []
        for entry in group:
            label = entry.primary_bbox
            anchor = entry.page_anchor.bbox
            if anchor[0] >= label[2] - 1.0:
                relations.append("right")
            elif anchor[2] <= label[0] + 1.0:
                relations.append("left")
            else:
                relations.append("stacked")
        side = max(set(relations), key=relations.count)
        bands.append(
            ContentsColumnBand(
                column_index=index,
                bbox=_round_rect((boundaries[index], 0.0, boundaries[index + 1], height)),
                page_anchor_x=round(statistics.median(entry.page_anchor.bbox[0] for entry in group), 4),
                anchor_side=side,
            )
        )
        for entry in group:
            entry.column_index = index
    return tuple(bands)


def _nearest_column(line: _Line, entries: list[_EntryDraft], bands: tuple[ContentsColumnBand, ...]) -> int:
    containing = [band for band in bands if band.bbox[0] - 1.0 <= line.cx <= band.bbox[2] + 1.0]
    if containing:
        return containing[0].column_index
    return min(
        {entry.column_index for entry in entries},
        key=lambda index: min(abs(line.cx - entry.primary_bbox[0]) for entry in entries if entry.column_index == index),
    )


def _nearest_same_row_entry(line: _Line, entries: list[_EntryDraft], column_index: int) -> _EntryDraft | None:
    candidates = [entry for entry in entries if entry.column_index == column_index]
    if not candidates:
        return None
    entry = min(candidates, key=lambda item: abs(item.primary_bbox[1] + item.primary_bbox[3] - line.bbox[1] - line.bbox[3]))
    entry_cy = (entry.primary_bbox[1] + entry.primary_bbox[3]) / 2.0
    if abs(entry_cy - line.cy) <= max(3.0, line.height * 0.45):
        return entry
    return None


def _has_following_entry(line: _Line, entries: list[_EntryDraft], column_index: int, entry_font: float) -> bool:
    return any(
        entry.column_index == column_index
        and 0.0 < entry.primary_bbox[1] - line.bbox[3] <= max(65.0, entry_font * 6.0)
        for entry in entries
    )


def _assign_hierarchy(entries: list[_EntryDraft], structural: list[_ContainerDraft]) -> None:
    for column_index in sorted({entry.column_index for entry in entries}):
        column_entries = [entry for entry in entries if entry.column_index == column_index]
        base_x = min(entry.primary_bbox[0] for entry in column_entries)
        headings = sorted(
            (draft for draft in structural if draft.column_index == column_index and draft.role == "group_heading"),
            key=lambda draft: draft.bbox[1],
        )
        for entry in column_entries:
            has_parent = any(heading.bbox[1] < entry.primary_bbox[1] for heading in headings)
            indentation = 1 if entry.primary_bbox[0] - base_x > 8.0 else 0
            entry.hierarchy_level = max(2 if has_parent else 1, 1 + indentation)


def _container_drafts(
    title: _Line,
    entries: list[_EntryDraft],
    structural: list[_ContainerDraft],
    auxiliary: list[_ContainerDraft],
) -> list[_ContainerDraft]:
    drafts = [_ContainerDraft((title,), "title", -1, 0, None)]
    for entry in entries:
        for line in sorted(entry.companion_lines, key=lambda item: item.bbox[0]):
            drafts.append(_ContainerDraft((line,), "entry_prefix", entry.column_index, entry.hierarchy_level, entry.entry_id))
        drafts.append(
            _ContainerDraft(entry.primary_lines, "entry_text", entry.column_index, entry.hierarchy_level, entry.entry_id)
        )
    drafts.extend(structural)
    drafts.extend(auxiliary)
    return drafts


def _auxiliary_drafts(lines: tuple[_Line, ...]) -> list[_ContainerDraft]:
    groups: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (item.block_index, item.line_index, item.bbox[0])):
        if groups:
            previous = groups[-1][-1]
            same_paragraph = (
                line.block_index == previous.block_index
                and line.line_index == previous.line_index + 1
                and abs(line.bbox[0] - groups[-1][0].bbox[0]) <= max(2.0, line.font_size * 0.8)
                and abs(line.font_size - groups[-1][0].font_size) <= max(0.5, line.font_size * 0.2)
                and line.color_srgb == groups[-1][0].color_srgb
                and -1.0 <= line.bbox[1] - previous.bbox[3] <= max(4.0, line.font_size * 0.8)
            )
            if same_paragraph:
                groups[-1].append(line)
                continue
        groups.append([line])
    return [_ContainerDraft(tuple(group), "auxiliary_text", -1, 0, None) for group in groups]


def _materialize_containers(
    drafts: list[_ContainerDraft],
    entries: list[_EntryDraft],
    bands: dict[int, ContentsColumnBand],
    page_width: float,
    page_height: float,
    all_lines: tuple[_Line, ...],
) -> tuple[ContentsContainer, ...]:
    entry_by_id = {entry.entry_id: entry for entry in entries}
    flow_drafts = [draft for draft in drafts if draft.role not in {"title", "auxiliary_text"}]
    row_tops: dict[int, list[float]] = {}
    for draft in flow_drafts:
        row_tops.setdefault(draft.column_index, []).append(draft.bbox[1])
    for values in row_tops.values():
        values[:] = sorted(set(round(value, 3) for value in values))

    ordered = sorted(drafts, key=lambda draft: (-1 if draft.role == "title" else draft.column_index, draft.bbox[1], draft.bbox[0]))
    containers: list[ContentsContainer] = []
    first_content_top = min(draft.bbox[1] for draft in flow_drafts)
    for reading_order, draft in enumerate(ordered):
        source_bbox = draft.bbox
        y0 = source_bbox[1]
        if draft.role == "title":
            # 目录标题是独立短行；当前高度带直到页右边界没有其他文字时，
            # 可使用已证明的横向空白，避免目标语言单词被拆开。
            x1 = page_width - 12.0
            y1 = max(source_bbox[3], first_content_top - 3.0)
        elif draft.role == "auxiliary_text":
            x1 = _auxiliary_right_boundary(draft, all_lines, page_width)
            y1 = _auxiliary_bottom_boundary(draft, all_lines, x1, page_height)
        else:
            band = bands[draft.column_index]
            entry = entry_by_id.get(draft.entry_id or "")
            if entry is not None and band.anchor_side == "right":
                x1 = entry.page_anchor.bbox[0] - max(0.8, draft.source_lines[0].font_size * 0.12)
            else:
                x1 = band.bbox[2] - 2.0
            if entry is not None:
                peers = sorted(
                    (peer for peer in drafts if peer.entry_id == entry.entry_id),
                    key=lambda peer: peer.bbox[0],
                )
                position = peers.index(draft)
                if position + 1 < len(peers):
                    x1 = min(x1, peers[position + 1].bbox[0] - 1.0)
            x1 = max(source_bbox[2], x1)
            later = [value for value in row_tops[draft.column_index] if value > source_bbox[1] + 0.5]
            y1 = max(source_bbox[3], (min(later) - 0.75) if later else source_bbox[3] + max(4.0, draft.source_lines[0].font_size * 1.2))
            if entry is not None and band.anchor_side == "stacked":
                gap = max(0.8, draft.source_lines[0].font_size * 0.12)
                if entry.page_anchor.bbox[1] <= source_bbox[1]:
                    y0 = max(y0, entry.page_anchor.bbox[3] + gap)
                else:
                    y1 = min(y1, entry.page_anchor.bbox[1] - gap)
                if y0 >= y1:
                    raise ContentsCapabilityError("CONTENTS_STACKED_ANCHOR_CLEARANCE_INSUFFICIENT")
        allowed = _round_rect((source_bbox[0], y0, x1, y1))
        style = max(draft.source_lines, key=lambda line: line.font_size)
        containers.append(
            ContentsContainer(
                container_id=f"contents-container-{reading_order:03d}",
                source_object_ids=tuple(object_id for line in draft.source_lines for object_id in line.object_ids),
                source_text=draft.text,
                source_bbox=_round_rect(source_bbox),
                allowed_bbox=allowed,
                reading_order=reading_order,
                role=draft.role,
                hierarchy_level=draft.hierarchy_level,
                column_index=draft.column_index,
                entry_id=draft.entry_id,
                font_name=style.font_name,
                font_size=round(style.font_size, 4),
                color_srgb=style.color_srgb,
            )
        )
    return tuple(containers)


def _auxiliary_right_boundary(draft: _ContainerDraft, lines: tuple[_Line, ...], page_width: float) -> float:
    source_bbox = draft.bbox
    source_ids = {object_id for line in draft.source_lines for object_id in line.object_ids}
    gap = max(0.8, draft.source_lines[0].font_size * 0.12)
    right = page_width - 12.0
    for line in lines:
        if source_ids.intersection(line.object_ids):
            continue
        vertically_aligned = min(source_bbox[3], line.bbox[3]) - max(source_bbox[1], line.bbox[1]) > -0.5
        if vertically_aligned and line.bbox[0] >= source_bbox[2] - 0.5:
            right = min(right, line.bbox[0] - gap)
    return max(source_bbox[2], right)


def _auxiliary_bottom_boundary(
    draft: _ContainerDraft,
    lines: tuple[_Line, ...],
    right: float,
    page_height: float,
) -> float:
    source_bbox = draft.bbox
    source_ids = {object_id for line in draft.source_lines for object_id in line.object_ids}
    gap = max(0.5, draft.source_lines[0].font_size * 0.08)
    bottom = page_height - 12.0
    for line in lines:
        if source_ids.intersection(line.object_ids) or line.bbox[1] < source_bbox[3] - 0.5:
            continue
        horizontally_aligned = min(right, line.bbox[2]) - max(source_bbox[0], line.bbox[0]) > 0.5
        if horizontally_aligned:
            bottom = min(bottom, line.bbox[1] - gap)
    return max(source_bbox[3], bottom)


def _is_upper_heading(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and len(text) <= 80 and all(not char.isascii() or not char.isalpha() or char.isupper() for char in letters)


def _normalized(text: str) -> str:
    return "".join(char for char in text.casefold() if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _union(rectangles: list[Rect]) -> Rect:
    return (
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]
