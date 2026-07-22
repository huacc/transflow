"""把 SharedPdfKernel span 事实适配为 single 私有文字容器。"""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import pairwise
from statistics import median

from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact, PageObjectFact
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    DEFAULT_LINE_HEIGHT,
    MAXIMUM_LINE_HEIGHT,
    MINIMUM_LINE_HEIGHT,
    SingleTextContainer,
)
from transflow.toolboxes.leaves.policy import P8ToolboxPolicy

LIST_PREFIX = re.compile(
    r"^\s*(?:[\uf0b7\u2022\u25cf\u25aa\-]|\(?[0-9]+[.)]|\(?[A-Za-z][.)])\s+"
)
PAGE_NUMBER = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?\s*$", re.IGNORECASE)


def _merge_lines(spans: tuple[KernelTextFact, ...]) -> str:
    """按 PDF 行、span 顺序合并正文，并保留明确换段与项目符号。"""

    by_line: dict[int, list[KernelTextFact]] = defaultdict(list)
    for span in spans:
        by_line[span.line_index].append(span)
    lines = [
        "".join(item.text for item in sorted(items, key=lambda row: row.span_index)).strip()
        for _, items in sorted(by_line.items())
    ]
    output: list[str] = []
    for line in (item for item in lines if item):
        if output and LIST_PREFIX.match(line):
            output.append("\n" + line)
        elif output and output[-1].endswith("-") and line[:1].islower():
            output[-1] = output[-1][:-1] + line
        elif output:
            output.append(" " + line)
        else:
            output.append(line)
    return "".join(output).strip()


def _role(
    text: str,
    spans: tuple[KernelTextFact, ...],
    page_font_median: float,
) -> str:
    if LIST_PREFIX.match(text) or "\n" in text:
        return "list"
    max_font = max(item.font_size for item in spans)
    font_names = " ".join(item.font_name.casefold() for item in spans)
    latin = [character for character in text if "A" <= character.upper() <= "Z"]
    uppercase = len(text) <= 180 and bool(latin) and all(
        character == character.upper() for character in latin
    )
    if max_font >= page_font_median * 1.25 or "bold" in font_names or uppercase:
        return "heading"
    return "body"


def _source_line_height(spans: tuple[KernelTextFact, ...]) -> float | None:
    """按当前原生 block 的相邻行顶距推导行高比例。"""

    by_line: dict[int, list[KernelTextFact]] = defaultdict(list)
    for span in spans:
        by_line[span.line_index].append(span)
    lines = [
        (
            min(item.bbox[1] for item in items),
            max(item.font_size for item in items),
        )
        for _, items in sorted(by_line.items())
    ]
    ratios = [
        (current_y - previous_y) / previous_font_size
        for (previous_y, previous_font_size), (current_y, _) in pairwise(lines)
        if current_y > previous_y + 0.1 and previous_font_size > 0
    ]
    return median(ratios) if ratios else None


def _bounded_line_height(value: float) -> float:
    """把源页节奏限制在统一可读范围内。"""

    return round(min(MAXIMUM_LINE_HEIGHT, max(MINIMUM_LINE_HEIGHT, value)), 4)


def _native_text_block(
    facts: ExtractedPageFacts,
    spans: tuple[KernelTextFact, ...],
) -> PageObjectFact:
    """按文字和几何匹配 rawdict span 与原生 text block，不依赖两套 API 的索引顺序。"""

    normalized = " ".join(_merge_lines(spans).split())
    candidates = tuple(
        item
        for item in facts.objects
        if item.kind == "text"
        and not item.protected
        and item.text.strip()
        and all(
            item.bbox[0] - 0.5 <= (span.bbox[0] + span.bbox[2]) / 2 <= item.bbox[2] + 0.5
            and item.bbox[1] - 0.5 <= (span.bbox[1] + span.bbox[3]) / 2 <= item.bbox[3] + 0.5
            for span in spans
        )
    )
    exact = tuple(
        item for item in candidates if " ".join(item.text.split()) == normalized
    )
    pool = exact or candidates
    if not pool:
        raise ValueError("single_block_identity_missing")
    span_bbox = (
        min(item.bbox[0] for item in spans),
        min(item.bbox[1] for item in spans),
        max(item.bbox[2] for item in spans),
        max(item.bbox[3] for item in spans),
    )
    return min(
        pool,
        key=lambda item: (
            sum(abs(left - right) for left, right in zip(item.bbox, span_bbox, strict=True)),
            item.object_id,
        ),
    )


def build_containers(
    facts: ExtractedPageFacts,
    policy: P8ToolboxPolicy,
) -> tuple[SingleTextContainer, ...]:
    """按 block 聚合正文和自然语言边距，并把原生纯页码分离。"""

    grouped: dict[int, list[KernelTextFact]] = defaultdict(list)
    for span in facts.text_spans:
        grouped[span.block_index].append(span)
    if not grouped:
        return ()
    page_font_median = median(item.font_size for item in facts.text_spans)
    top = facts.page.height_points * policy.body_margin_top_ratio
    bottom = facts.page.height_points * policy.body_margin_bottom_ratio
    rows: list[
        tuple[
            float,
            float,
            int,
            tuple[KernelTextFact, ...],
            tuple[KernelTextFact, ...],
            str,
            tuple[str, ...],
            float | None,
        ]
    ] = []
    for block_index, block_spans in grouped.items():
        ordered = tuple(
            sorted(block_spans, key=lambda item: (item.line_index, item.span_index))
        )
        bbox = (
            min(item.bbox[0] for item in ordered),
            min(item.bbox[1] for item in ordered),
            max(item.bbox[2] for item in ordered),
            max(item.bbox[3] for item in ordered),
        )
        text = _merge_lines(ordered)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        page_number_spans = tuple(
            item
            for item in ordered
            if PAGE_NUMBER.fullmatch(item.text)
            and (
                item.bbox[3] <= top
                or item.bbox[1] >= bottom
            )
        )
        editable_spans = tuple(item for item in ordered if item not in page_number_spans)
        is_body = bbox[3] >= top and bbox[1] <= bottom
        is_semantic_header = bbox[3] < top and bool(editable_spans)
        is_semantic_footer = bbox[1] > bottom and bool(editable_spans)
        if (
            not text
            or not (is_body or is_semantic_header or is_semantic_footer)
            or PAGE_NUMBER.fullmatch(text)
            or not editable_spans
            or (height >= facts.page.height_points * 0.10 and height > max(40.0, width * 3.0))
        ):
            continue
        rows.append(
            (
                round(bbox[1], 4),
                round(bbox[0], 4),
                block_index,
                ordered,
                editable_spans,
                "body" if is_body else "margin",
                tuple(item.text.strip() for item in page_number_spans),
                _source_line_height(ordered),
            )
        )
    rows.sort(key=lambda item: (item[0], item[1], item[2]))

    source_line_heights = [item[7] for item in rows if item[5] != "margin" and item[7]]
    page_line_height = _bounded_line_height(
        median(source_line_heights) if source_line_heights else DEFAULT_LINE_HEIGHT
    )

    containers: list[SingleTextContainer] = []
    for reading_order, (
        _,
        _,
        block_index,
        all_spans,
        spans,
        area_role,
        preserved_page_numbers,
        source_line_height,
    ) in enumerate(rows):
        bbox = (
            min(item.bbox[0] for item in spans),
            min(item.bbox[1] for item in spans),
            max(item.bbox[2] for item in spans),
            max(item.bbox[3] for item in spans),
        )
        block_object = _native_text_block(facts, all_spans)
        # 页码等机械保留 span 已从当前容器剥离；翻译请求不得再把它们混回语义正文。
        text = _merge_lines(spans)
        representative = max(
            spans,
            key=lambda item: (item.font_size, len(item.text), -item.line_index, -item.span_index),
        )
        prefix_match = LIST_PREFIX.match(text)
        containers.append(
            SingleTextContainer(
                container_id=f"block-{block_index:04d}",
                semantic_object_id=block_object.object_id,
                source_object_ids=tuple(item.object_id for item in spans),
                source_rects=tuple(item.bbox for item in spans),
                source_text=text,
                reading_order=reading_order,
                role=(
                    "margin"
                    if area_role == "margin"
                    else _role(text, all_spans, page_font_median)
                ),
                source_bbox=(
                    round(bbox[0], 4),
                    round(bbox[1], 4),
                    round(bbox[2], 4),
                    round(bbox[3], 4),
                ),
                anchor=(round(bbox[0], 4), round(bbox[1], 4)),
                font_size=round(max(item.font_size for item in spans), 4),
                color_srgb=representative.color_srgb,
                preferred_line_height=_bounded_line_height(
                    source_line_height if source_line_height is not None else page_line_height
                ),
                preserved_prefix=prefix_match.group(0).strip() if prefix_match else None,
                preserved_page_numbers=preserved_page_numbers,
            )
        )
    selected = tuple(containers)
    if _has_overlapping_semantic_projection(facts, selected):
        # 相互覆盖的 block 通常来自表格复合布局或叠印对象；single 不能在
        # 一个 span 被两个语义容器领取时继续翻译，整页交给结构化 fallback。
        return ()
    return selected


def _has_overlapping_semantic_projection(
    facts: ExtractedPageFacts,
    containers: tuple[SingleTextContainer, ...],
) -> bool:
    """预演完整性门禁的 block→span 投影，拒绝重复 owner。"""

    claimed: set[str] = set()
    claimed_rects: set[tuple[float, float, float, float]] = set()
    for container in containers:
        projected = set(container.source_object_ids)
        projected_rects: set[tuple[float, float, float, float]] = {
            (
                round(rect[0], 4),
                round(rect[1], 4),
                round(rect[2], 4),
                round(rect[3], 4),
            )
            for rect in container.source_rects
        }
        if claimed & projected or claimed_rects & projected_rects:
            return True
        claimed.update(projected)
        claimed_rects.update(projected_rects)
    return False
