"""Adapt production Kernel facts to the diagram core's read-only source view."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from transflow.pdf_kernel.facts import ExtractedPageFacts

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class DiagramTextFact:
    object_id: str
    text: str
    bbox: Rect
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int
    span_index: int


@dataclass(frozen=True, slots=True)
class DiagramImageFact:
    object_id: str
    bbox: Rect
    width: int
    height: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class DiagramPageFacts:
    page_id: str
    width: float
    height: float
    page_index: int
    rotation: int
    text_objects: tuple[DiagramTextFact, ...]
    image_objects: tuple[DiagramImageFact, ...]
    locked_objects_sha256: str


def adapt_diagram_page_facts(
    facts: ExtractedPageFacts,
    source_pdf: Path,
) -> DiagramPageFacts:
    """Build the private diagram view without widening the shared facts contract."""

    with pymupdf.open(source_pdf) as document:
        page_index = 0 if document.page_count == 1 else facts.page.page_no - 1
        if page_index < 0 or page_index >= document.page_count:
            raise ValueError("DIAGRAM_SOURCE_PAGE_OUT_OF_RANGE")
    return DiagramPageFacts(
        page_id=facts.page_identity,
        width=facts.page.width_points,
        height=facts.page.height_points,
        page_index=page_index,
        rotation=facts.rotation,
        text_objects=tuple(
            DiagramTextFact(
                item.object_id,
                item.text,
                item.bbox,
                item.font_name,
                item.font_size,
                item.color_srgb,
                item.block_index,
                item.line_index,
                item.span_index,
            )
            for item in facts.text_spans
        ),
        image_objects=tuple(
            DiagramImageFact(
                item.object_id,
                item.bbox,
                item.width,
                item.height,
                item.content_hash,
            )
            for item in facts.image_objects
        ),
        locked_objects_sha256=facts.locked_objects_hash,
    )
