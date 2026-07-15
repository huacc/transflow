from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageFacts, TextObjectFact
from shared_pdf_kernel.facts import canonical_sha256

from . import TOOLBOX_KEY
from .models import CoverContainer, CoverTemplate, Rect


class CoverCapabilityError(RuntimeError):
    pass


_NONSEMANTIC_LITERAL = re.compile(
    r"^(?:(?:https?|ftp)://\S+|www\.\S+|[^\s@]+@[^\s@]+\.[^\s@]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Line:
    object_ids: tuple[str, ...]
    text: str
    bbox: Rect
    font_name: str
    font_size: float
    color_srgb: int

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


def build_cover_template(facts: PageFacts, source_pdf: Path | None = None) -> CoverTemplate:
    occluded_ids = _occluded_text_object_ids(source_pdf, facts) if source_pdf else ()
    visible_objects = tuple(item for item in facts.text_objects if item.object_id not in occluded_ids)
    lines = _logical_lines(visible_objects)
    semantic = tuple(line for line in lines if _is_semantic(line.text))
    nonsemantic = tuple(line for line in lines if line not in semantic)
    literal_repaints = tuple(
        line
        for line in nonsemantic
        if any(_coverage(line.bbox, candidate.bbox) >= 0.92 for candidate in semantic)
    )
    protected = tuple(line for line in nonsemantic if line not in literal_repaints)
    visual_only = not semantic

    containers: list[CoverContainer] = []
    if semantic:
        materialized = tuple(line for line in lines if line in semantic or line in literal_repaints)
        sizes = [line.font_size for line in materialized]
        median_size = statistics.median(sizes)
        distinct_sizes = sorted({round(size, 1) for size in sizes}, reverse=True)
        for order, line in enumerate(materialized):
            translatable = line in semantic
            hierarchy = min(3, distinct_sizes.index(round(line.font_size, 1)) + 1)
            role = _role(line.font_size, median_size, hierarchy) if translatable else "literal_repaint"
            anchor = _anchor(line, lines, facts.width)
            allowed = (
                line.bbox
                if not translatable
                else _allowed_bbox(line, lines, anchor, facts.width, facts.height)
            )
            containers.append(
                CoverContainer(
                    container_id=f"cover-text-{order:03d}",
                    source_object_ids=line.object_ids,
                    source_text=line.text,
                    source_bbox=_round_rect(line.bbox),
                    allowed_bbox=_round_rect(allowed),
                    reading_order=order,
                    translatable=translatable,
                    role=role,
                    hierarchy_level=hierarchy,
                    anchor=anchor,
                    font_name=line.font_name,
                    font_size=round(line.font_size, 4),
                    color_srgb=line.color_srgb,
                )
            )

    protected_ids = tuple(object_id for line in protected for object_id in line.object_ids)
    owned_ids = [object_id for item in containers for object_id in item.source_object_ids] + [
        *protected_ids,
        *occluded_ids,
    ]
    expected_ids = [item.object_id for item in facts.text_objects]
    if sorted(owned_ids) != sorted(expected_ids):
        raise CoverCapabilityError("COVER_NATIVE_TEXT_OWNERSHIP_INCOMPLETE")

    signature = canonical_sha256(
        {
            "toolbox_key": TOOLBOX_KEY,
            "containers": containers,
            "protected_object_ids": protected_ids,
            "occluded_object_ids": occluded_ids,
            "visual_only": visual_only,
        }
    )
    return CoverTemplate(
        page_id=facts.page_id,
        toolbox_key=TOOLBOX_KEY,
        width=facts.width,
        height=facts.height,
        containers=tuple(containers),
        protected_object_ids=protected_ids,
        visual_only=visual_only,
        visual_only_reason=(
            "NO_VISIBLE_SEMANTIC_NATIVE_TEXT" if visual_only and occluded_ids else "NO_SEMANTIC_NATIVE_TEXT"
        )
        if visual_only
        else None,
        structure_sha256=signature,
        occluded_object_ids=occluded_ids,
    )


def _logical_lines(objects: tuple[TextObjectFact, ...]) -> tuple[_Line, ...]:
    grouped: dict[tuple[int, int], list[TextObjectFact]] = {}
    for item in objects:
        grouped.setdefault((item.block_index, item.line_index), []).append(item)

    fragments: list[_Line] = []
    for items in grouped.values():
        for run in _style_runs(items):
            text = _join_text([(item.text, item.bbox, item.font_size) for item in run])
            if not text:
                continue
            style = max(run, key=lambda item: item.font_size)
            fragments.append(
                _Line(
                    tuple(item.object_id for item in run),
                    text,
                    _union([item.bbox for item in run]),
                    style.font_name,
                    max(item.font_size for item in run),
                    style.color_srgb,
                )
            )

    fragments = _combine_duplicate_overdraw(fragments)
    rows: list[list[_Line]] = []
    for fragment in sorted(fragments, key=lambda item: (item.cy, item.bbox[0])):
        compatible = [row for row in rows if _same_row(fragment, row)]
        if compatible:
            min(compatible, key=lambda row: abs(fragment.cy - statistics.mean(item.cy for item in row))).append(fragment)
        else:
            rows.append([fragment])

    lines: list[_Line] = []
    for row in rows:
        current: list[_Line] = []
        for fragment in sorted(row, key=lambda item: item.bbox[0]):
            if current and not _nearby(current[-1], fragment):
                lines.append(_merge_fragments(current))
                current = []
            current.append(fragment)
        if current:
            lines.append(_merge_fragments(current))
    return tuple(sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])))


def _style_runs(items: list[TextObjectFact]) -> tuple[tuple[TextObjectFact, ...], ...]:
    runs: list[list[TextObjectFact]] = []
    for item in sorted(items, key=lambda candidate: (candidate.bbox[0], candidate.span_index)):
        compatible = next(
            (
                run
                for run in runs
                if run[0].color_srgb == item.color_srgb
                and min(run[0].font_size, item.font_size) / max(run[0].font_size, item.font_size) >= 0.62
            ),
            None,
        )
        if compatible is None:
            runs.append([item])
        else:
            compatible.append(item)
    return tuple(tuple(run) for run in runs)


def _occluded_text_object_ids(source_pdf: Path, facts: PageFacts) -> tuple[str, ...]:
    with fitz.open(source_pdf) as document:
        page = document[facts.page_index]
        paint_log = page.get_bboxlog()
        traces = [
            (
                int(trace["seqno"]),
                "".join(chr(character[0]) for character in trace.get("chars", ())),
                tuple(float(value) for value in trace["bbox"]),
                tuple(
                    tuple(float(value) for value in character[3])
                    for character in trace.get("chars", ())
                    if chr(character[0]).strip()
                ),
            )
            for trace in page.get_texttrace()
        ]
    occlusion_candidates: list[tuple[TextObjectFact, tuple[Rect, ...]]] = []
    for item in facts.text_objects:
        item_text = _normalized(item.text)
        trace_candidates = [
            (seqno, bbox, glyphs)
            for seqno, text, bbox, glyphs in traces
            if item_text
            and item_text in _normalized(text)
            and _intersection_coverage(bbox, item.bbox) >= 0.20
        ]
        if not trace_candidates:
            continue
        text_trace = max(trace_candidates, key=lambda candidate: _intersection_coverage(candidate[1], item.bbox))
        text_seqno = text_trace[0]
        if any(
            index > text_seqno
            and entry[0] == "fill-image"
            and _intersection_coverage(tuple(float(value) for value in entry[1]), item.bbox) >= 0.98
            for index, entry in enumerate(paint_log)
        ):
            occlusion_candidates.append((item, text_trace[2]))
    if not occlusion_candidates:
        return ()

    with fitz.open(source_pdf) as document:
        original_samples = document[facts.page_index].get_pixmap(
            matrix=fitz.Matrix(2.0, 2.0),
            colorspace=fitz.csRGB,
            alpha=False,
        ).samples
    source_bytes = source_pdf.read_bytes()
    return tuple(
        item.object_id
        for item, glyphs in occlusion_candidates
        if _redaction_is_visually_inert(
            source_bytes,
            facts.page_index,
            glyphs or (item.bbox,),
            original_samples,
        )
    )


def _redaction_is_visually_inert(
    source_bytes: bytes,
    page_index: int,
    glyphs: tuple[Rect, ...],
    original_samples: bytes,
) -> bool:
    with fitz.open(stream=source_bytes, filetype="pdf") as document:
        page = document[page_index]
        for glyph in glyphs:
            page.add_redact_annot(fitz.Rect(glyph), fill=None)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        candidate_samples = page.get_pixmap(
            matrix=fitz.Matrix(2.0, 2.0),
            colorspace=fitz.csRGB,
            alpha=False,
        ).samples
    return candidate_samples == original_samples


def _combine_duplicate_overdraw(fragments: list[_Line]) -> list[_Line]:
    combined: list[_Line] = []
    for fragment in fragments:
        duplicate = next(
            (
                item
                for item in combined
                if _normalized(item.text) == _normalized(fragment.text)
                and max(abs(item.bbox[index] - fragment.bbox[index]) for index in range(4)) <= 0.75
            ),
            None,
        )
        if duplicate is None:
            combined.append(fragment)
            continue
        combined[combined.index(duplicate)] = _Line(
            tuple((*duplicate.object_ids, *fragment.object_ids)),
            duplicate.text,
            _union([duplicate.bbox, fragment.bbox]),
            duplicate.font_name,
            max(duplicate.font_size, fragment.font_size),
            duplicate.color_srgb,
        )
    return combined


def _same_row(fragment: _Line, row: list[_Line]) -> bool:
    reference = min(row, key=lambda item: abs(fragment.cy - item.cy))
    overlap = max(0.0, min(fragment.bbox[3], reference.bbox[3]) - max(fragment.bbox[1], reference.bbox[1]))
    overlap_ratio = overlap / max(0.1, min(fragment.height, reference.height))
    size_ratio = min(fragment.font_size, reference.font_size) / max(fragment.font_size, reference.font_size)
    return overlap_ratio >= 0.55 and size_ratio >= 0.62 and fragment.color_srgb == reference.color_srgb


def _nearby(left: _Line, right: _Line) -> bool:
    gap = right.bbox[0] - left.bbox[2]
    overlap = max(0.0, min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0]))
    narrower_width = min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0])
    if overlap > max(2.0, narrower_width * 0.25):
        return False
    return gap <= max(18.0, max(left.font_size, right.font_size) * 1.8)


def _merge_fragments(fragments: list[_Line]) -> _Line:
    style = max(fragments, key=lambda item: item.font_size)
    pieces = [(item.text, item.bbox, item.font_size) for item in fragments]
    return _Line(
        tuple(object_id for item in fragments for object_id in item.object_ids),
        _join_text(pieces),
        _union([item.bbox for item in fragments]),
        style.font_name,
        style.font_size,
        style.color_srgb,
    )


def _join_text(items: list[tuple[str, Rect, float]]) -> str:
    pieces: list[str] = []
    previous: tuple[str, Rect, float] | None = None
    for raw_text, bbox, size in items:
        text = raw_text.replace("\ufeff", "").strip()
        if not text:
            continue
        if previous is not None:
            previous_text, previous_bbox, previous_size = previous
            gap = bbox[0] - previous_bbox[2]
            if gap > max(previous_size, size) * 0.18 and not (_ends_cjk(previous_text) and _starts_cjk(text)):
                pieces.append(" ")
        pieces.append(text)
        previous = (text, bbox, size)
    return "".join(pieces).strip()


def _is_semantic(text: str) -> bool:
    value = text.strip()
    has_supported_script = bool(re.search(r"[A-Za-z\u3400-\u9fff]", value))
    return bool(value) and not _NONSEMANTIC_LITERAL.fullmatch(value) and has_supported_script


def _role(font_size: float, median_size: float, hierarchy: int) -> str:
    if hierarchy == 1 or font_size >= median_size * 1.2:
        return "title"
    if hierarchy == 2 or font_size >= median_size:
        return "subtitle"
    return "metadata"


def _anchor(line: _Line, lines: tuple[_Line, ...], page_width: float) -> str:
    bbox = line.bbox
    center = line.cx
    tolerance = max(2.0, line.font_size * 0.35)
    left_matches = sum(abs(candidate.bbox[0] - bbox[0]) <= tolerance for candidate in lines)
    right_matches = sum(abs(candidate.bbox[2] - bbox[2]) <= tolerance for candidate in lines)
    center_matches = sum(abs(candidate.cx - center) <= tolerance for candidate in lines)
    strongest = max(left_matches, right_matches, center_matches)
    if strongest >= 2:
        if left_matches == strongest and left_matches > max(right_matches, center_matches):
            return "LEFT"
        if right_matches == strongest and right_matches > max(left_matches, center_matches):
            return "RIGHT"
        if center_matches == strongest:
            return "CENTER"
    if abs(center - page_width / 2.0) <= page_width * 0.12:
        return "CENTER"
    left_space = bbox[0]
    right_space = page_width - bbox[2]
    if left_space <= page_width * 0.25 or right_space >= left_space * 1.5:
        return "LEFT"
    if right_space <= page_width * 0.25 or left_space >= right_space * 1.5:
        return "RIGHT"
    return "CENTER"


def _allowed_bbox(line: _Line, lines: tuple[_Line, ...], anchor: str, width: float, height: float) -> Rect:
    margin_x = max(12.0, width * 0.04)
    margin_y = max(8.0, height * 0.02)
    left = margin_x
    right = width - margin_x
    for other in lines:
        if other is line or not _vertical_overlap(line.bbox, other.bbox):
            continue
        if other.bbox[2] <= line.bbox[0]:
            left = max(left, other.bbox[2] + 2.0)
        elif other.bbox[0] >= line.bbox[2]:
            right = min(right, other.bbox[0] - 2.0)

    if anchor == "LEFT":
        left = line.bbox[0]
    elif anchor == "RIGHT":
        right = line.bbox[2]
    else:
        radius = min(line.cx - left, right - line.cx)
        left, right = line.cx - radius, line.cx + radius

    above = [other.bbox[3] for other in lines if other is not line and other.bbox[3] <= line.bbox[1]]
    below = [other.bbox[1] for other in lines if other is not line and other.bbox[1] >= line.bbox[3]]
    top = max(margin_y, max(above, default=margin_y) + 1.0)
    bottom = min(height - margin_y, min(below, default=height - margin_y) - 1.0)
    top = min(top, line.bbox[1])
    bottom = max(bottom, line.bbox[3])
    maximum_height = max(line.height * 3.0, line.font_size * 3.2)
    if bottom - top > maximum_height:
        top = max(top, line.cy - maximum_height / 2.0)
        bottom = min(bottom, line.cy + maximum_height / 2.0)
    if right - left < max(20.0, line.bbox[2] - line.bbox[0]):
        left, right = line.bbox[0], line.bbox[2]
    return left, top, right, bottom


def _vertical_overlap(left: Rect, right: Rect) -> bool:
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap >= min(left[3] - left[1], right[3] - right[1]) * 0.45


def _coverage(cover: Rect, target: Rect) -> float:
    overlap = max(0.0, min(cover[2], target[2]) - max(cover[0], target[0])) * max(
        0.0, min(cover[3], target[3]) - max(cover[1], target[1])
    )
    area = max(0.01, (target[2] - target[0]) * (target[3] - target[1]))
    return overlap / area


def _intersection_coverage(cover: Rect, target: Rect) -> float:
    return _coverage(cover, target)


def _starts_cjk(text: str) -> bool:
    return bool(text) and "\u3400" <= text[0] <= "\u9fff"


def _ends_cjk(text: str) -> bool:
    return bool(text) and "\u3400" <= text[-1] <= "\u9fff"


def _normalized(text: str) -> str:
    return "".join(text.split()).casefold()


def _union(rectangles: list[Rect]) -> Rect:
    return (
        min(rect[0] for rect in rectangles),
        min(rect[1] for rect in rectangles),
        max(rect[2] for rect in rectangles),
        max(rect[3] for rect in rectangles),
    )


def _round_rect(rect: Rect) -> Rect:
    return tuple(round(float(value), 4) for value in rect)  # type: ignore[return-value]
