"""实现 single 布局计划和真实候选的机械裁决。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pymupdf

from transflow.domain.toolbox import Finding, PagePatch
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageFactsExtractor
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    MINIMUM_LINE_HEIGHT,
    SinglePlacement,
    SingleTextContainer,
)

Rect = tuple[float, float, float, float]
ExtractedLine = tuple[float, float, Rect, str]
PAGE_NUMBER = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?\s*$", re.IGNORECASE)
IMAGE_UNDERLAY_PAGE_AREA_RATIO = 0.45
MATERIALIZED_COLLISION_TOLERANCE = 1.0


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _rect_area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _materially_underlays(target: Rect, visual: Rect) -> bool:
    target_width = max(target[2] - target[0], 1.0)
    target_height = max(target[3] - target[1], 1.0)
    horizontal_overlap = max(
        0.0,
        min(target[2], visual[2]) - max(target[0], visual[0]),
    )
    vertical_overlap = max(
        0.0,
        min(target[3], visual[3]) - max(target[1], visual[1]),
    )
    return (
        horizontal_overlap >= target_width * 0.60
        and vertical_overlap >= target_height * 0.25
    )


def _is_image_obstacle(image: Rect, source: Rect, page: Rect | None) -> bool:
    if page is not None and _rect_area(image) / max(_rect_area(page), 1.0) >= (
        IMAGE_UNDERLAY_PAGE_AREA_RATIO
    ):
        return False
    return not _materially_underlays(source, image)


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.01) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"):
        normalized = normalized.replace(dash, "-")
    for alias in ("•", "・", "∙", "●", "▪", "\uf0b7"):
        normalized = normalized.replace(alias, "·")
    return "".join(normalized.split()).casefold()


def _matching_line_window(
    lines: tuple[ExtractedLine, ...],
    expected_text: str,
) -> tuple[ExtractedLine, ...]:
    """从邻近全页行中选择包含当前译文的最小连续窗口。"""

    expected = _normalized(expected_text)
    best: tuple[tuple[int, int], tuple[ExtractedLine, ...]] | None = None
    for start in range(len(lines)):
        observed = ""
        for end in range(start, len(lines)):
            observed += _normalized(lines[end][3])
            if expected in observed:
                window = lines[start : end + 1]
                key = (len(observed) - len(expected), len(window))
                if best is None or key < best[0]:
                    best = (key, window)
                break
            if len(observed) > len(expected) * 2 + 64:
                break
    return () if best is None else best[1]


@dataclass(frozen=True, slots=True)
class SingleMaterializedJudgement:
    """汇总真实候选的 RV5 硬指标，不把存在 PDF 等同于产品通过。"""

    expected_operation_count: int
    materialized_operation_count: int
    overflow_count: int
    collision_count: int
    owner_clip_violation_count: int
    protected_modification_count: int
    line_spacing_violation_count: int

    @property
    def materialization_rate(self) -> float:
        return self.materialized_operation_count / max(1, self.expected_operation_count)

    @property
    def passed(self) -> bool:
        return (
            self.materialization_rate == 1.0
            and not self.overflow_count
            and not self.collision_count
            and not self.owner_clip_violation_count
            and not self.protected_modification_count
            and not self.line_spacing_violation_count
        )


def judge_placements(
    plan_id: str,
    containers: tuple[SingleTextContainer, ...],
    placements: tuple[SinglePlacement, ...],
    *,
    clip_box: Rect | None = None,
    protected_rects: tuple[Rect, ...] = (),
    image_rects: tuple[Rect, ...] = (),
    non_target_text_rects: tuple[Rect, ...] = (),
) -> tuple[Finding, ...]:
    """返回可由一次有界 Repair 处理的稳定 Finding。"""

    findings: list[Finding] = []
    if tuple(item.container_id for item in placements) != tuple(
        item.container_id for item in sorted(containers, key=lambda item: item.reading_order)
    ):
        findings.append(
            Finding(f"{plan_id}-order", "READING_ORDER_CHANGED", "HARD", (plan_id,))
        )
    by_id = {item.container_id: item for item in containers}
    for index, left in enumerate(placements):
        if clip_box is not None and not _contains(clip_box, left.output_bbox):
            findings.append(
                Finding(
                    f"{plan_id}-{left.container_id}-clip",
                    "OWNER_CLIP_EXCEEDED",
                    "HARD",
                    (left.container_id,),
                )
            )
        if any(_intersection_area(left.output_bbox, rect) > 0.01 for rect in protected_rects):
            findings.append(
                Finding(
                    f"{plan_id}-{left.container_id}-protected",
                    "PROTECTED_OBJECT_COLLISION",
                    "HARD",
                    (left.container_id,),
                )
            )
        if any(
            _intersection_area(left.output_bbox, rect) > 0.01
            and _is_image_obstacle(rect, by_id[left.container_id].source_bbox, clip_box)
            for rect in image_rects
        ):
            findings.append(
                Finding(
                    f"{plan_id}-{left.container_id}-image",
                    "PROTECTED_OBJECT_COLLISION",
                    "HARD",
                    (left.container_id,),
                )
            )
        if any(
            _intersection_area(left.output_bbox, rect) > 0.01
            for rect in non_target_text_rects
        ):
            findings.append(
                Finding(
                    f"{plan_id}-{left.container_id}-non-target",
                    "NON_TARGET_TEXT_COLLISION",
                    "HARD",
                    (left.container_id,),
                )
            )
        for right in placements[index + 1 :]:
            if _intersection_area(left.output_bbox, right.output_bbox) > 0.01:
                findings.append(
                    Finding(
                        f"{plan_id}-{left.container_id}-{right.container_id}-collision",
                        "TEXT_PLACEMENT_COLLISION",
                        "HARD",
                        (left.container_id, right.container_id),
                    )
                )
    previous_body_bottom: float | None = None
    for placement in placements:
        container = by_id[placement.container_id]
        x_changed = abs(placement.output_bbox[0] - container.anchor[0]) > 0.01
        fixed_margin_y_changed = (
            container.role == "margin"
            and abs(placement.output_bbox[1] - container.anchor[1]) > 0.01
        )
        if x_changed or fixed_margin_y_changed:
            findings.append(
                Finding(
                    f"{plan_id}-{placement.container_id}-anchor",
                    "ANCHOR_CHANGED",
                    "HARD",
                    (placement.container_id,),
                )
            )
        if container.role != "margin":
            if (
                previous_body_bottom is not None
                and placement.output_bbox[1] < previous_body_bottom - 0.01
            ):
                findings.append(
                    Finding(
                        f"{plan_id}-{placement.container_id}-flow-overlap",
                        "FLOW_TEXT_OVERLAP",
                        "HARD",
                        (placement.container_id,),
                    )
                )
            previous_body_bottom = placement.output_bbox[3]
        if not placement.fit:
            findings.append(
                Finding(
                    f"{plan_id}-{placement.container_id}-overflow",
                    "TEXT_LAYOUT_OVERFLOW",
                    "HARD",
                    (placement.container_id,),
                )
            )
        if placement.line_height < MINIMUM_LINE_HEIGHT:
            findings.append(
                Finding(
                    f"{plan_id}-{placement.container_id}-line-height",
                    "TEXT_LINE_SPACING_TOO_TIGHT",
                    "HARD",
                    (placement.container_id,),
                )
            )
        if container.preserved_prefix and not placement.translated_text.lstrip().startswith(
            container.preserved_prefix
        ):
            findings.append(
                Finding(
                    f"{plan_id}-{placement.container_id}-marker",
                    "LIST_MARKER_LOST",
                    "HARD",
                    (placement.container_id,),
                )
            )
    return tuple(findings)


def inspect_materialized_candidate(
    candidate_path: Path,
    facts: ExtractedPageFacts,
    patch: PagePatch,
) -> SingleMaterializedJudgement:
    """重新打开候选，核对译文物化、实际行距、碰撞与锁定对象。"""

    candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    candidate_facts = PageFactsExtractor().extract_page(
        candidate_path, candidate_hash, patch.page_no
    )
    target_ids = {
        object_id for operation in patch.operations for object_id in operation.target_object_ids
    }
    materialized = 0
    overflow = 0
    clip_violations = 0
    line_violations = 0
    glyph_boxes: list[Rect] = []
    with pymupdf.open(candidate_path) as document:
        page = document[patch.page_no - 1]
        page_lines: list[ExtractedLine] = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = tuple(
                    span for span in line.get("spans", []) if str(span.get("text", "")).strip()
                )
                if not spans:
                    continue
                raw_bbox = line["bbox"]
                bbox = (
                    float(raw_bbox[0]),
                    float(raw_bbox[1]),
                    float(raw_bbox[2]),
                    float(raw_bbox[3]),
                )
                page_lines.append(
                    (
                        bbox[1],
                        max(float(span["size"]) for span in spans),
                        bbox,
                        "".join(str(span["text"]) for span in spans),
                    )
                )
        page_lines.sort(key=lambda item: (item[0], item[2][0]))
        for operation in patch.operations:
            if operation.rect is None or operation.replacement_text is None:
                overflow += 1
                continue
            rect = pymupdf.Rect(operation.rect)
            font_metric_tolerance = max(
                1.0,
                (operation.font_size or 0.0) * ((operation.line_height or 1.0) + 0.3),
            )
            nearby = tuple(
                line
                for line in page_lines
                if rect.x0 - 0.5 <= (line[2][0] + line[2][2]) / 2 <= rect.x1 + 0.5
                and rect.y0 - font_metric_tolerance
                <= (line[2][1] + line[2][3]) / 2
                <= rect.y1 + font_metric_tolerance
            )
            lines = _matching_line_window(nearby, operation.replacement_text)
            if lines:
                materialized += 1
            else:
                overflow += 1
            if lines:
                glyph_box = (
                    min(item[2][0] for item in lines),
                    min(item[2][1] for item in lines),
                    max(item[2][2] for item in lines),
                    max(item[2][3] for item in lines),
                )
                glyph_boxes.append(glyph_box)
                if not _contains(facts.crop_box, glyph_box, 0.5) or not _contains(
                    operation.rect, glyph_box, font_metric_tolerance
                ):
                    clip_violations += 1
            for previous, current in pairwise(lines):
                if current[0] <= previous[0] + 0.1:
                    continue
                if (current[0] - previous[0]) / previous[1] < MINIMUM_LINE_HEIGHT - 0.02:
                    line_violations += 1
        page_number_violations = 0
        for span in facts.text_spans:
            if span.object_id in target_ids or not PAGE_NUMBER.fullmatch(span.text):
                continue
            matches = page.search_for(span.text)
            if not any(
                sum(abs(left - right) for left, right in zip(match, span.bbox, strict=True))
                <= 4.0
                for match in matches
            ):
                page_number_violations += 1
    collisions = sum(
        min(left[2], right[2]) - max(left[0], right[0])
        > MATERIALIZED_COLLISION_TOLERANCE
        and min(left[3], right[3]) - max(left[1], right[1])
        > MATERIALIZED_COLLISION_TOLERANCE
        for index, left in enumerate(glyph_boxes)
        for right in glyph_boxes[index + 1 :]
    )
    return SingleMaterializedJudgement(
        expected_operation_count=len(patch.operations),
        materialized_operation_count=materialized,
        overflow_count=overflow,
        collision_count=collisions,
        owner_clip_violation_count=clip_violations,
        protected_modification_count=(
            int(candidate_facts.locked_objects_hash != facts.locked_objects_hash)
            + page_number_violations
        ),
        line_spacing_violation_count=line_violations,
    )
