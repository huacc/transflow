"""Adapt production Kernel facts to the immutable contracts used by lifted leaf cores."""

from __future__ import annotations

from dataclasses import dataclass

from transflow.domain.common import content_sha256
from transflow.pdf_kernel.facts import ExtractedPageFacts, RectTuple


@dataclass(frozen=True, slots=True)
class TextObjectFact:
    object_id: str
    text: str
    bbox: RectTuple
    font_name: str
    font_size: float
    color_srgb: int
    block_index: int
    line_index: int
    span_index: int


@dataclass(frozen=True, slots=True)
class ImageObjectFact:
    object_id: str
    bbox: RectTuple
    width: int
    height: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class DrawingObjectFact:
    object_id: str
    bbox: RectTuple
    content_sha256: str


@dataclass(frozen=True, slots=True)
class PageFacts:
    page_id: str
    source_pdf_sha256: str
    width: float
    height: float
    native_text_object_count: int
    origin: str
    page_index: int = 0
    rotation: int = 0
    text_objects: tuple[TextObjectFact, ...] = ()
    image_objects: tuple[ImageObjectFact, ...] = ()
    drawing_objects: tuple[DrawingObjectFact, ...] = ()
    geometry_sha256: str | None = None
    text_objects_sha256: str | None = None
    locked_objects_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationResult:
    container_id: str
    translated_text: str


@dataclass(frozen=True, slots=True)
class PageTranslationBundle:
    request_id: str
    page_id: str
    provider: str
    model: str
    translations: tuple[TranslationResult, ...]
    provider_request_id: str | None = None
    latency_ms: int | None = None
    response_sha256: str | None = None


def canonical_sha256(value: object) -> str:
    """Keep lifted source hashing equivalent through the production serializer."""

    return content_sha256(value)


def lift_page_facts(facts: ExtractedPageFacts) -> PageFacts:
    """Project production facts without paths, sample IDs, gold labels, or OCR."""

    text_objects = tuple(
        TextObjectFact(
            object_id=item.object_id,
            text=item.text,
            bbox=item.bbox,
            font_name=item.font_name,
            font_size=item.font_size,
            color_srgb=item.color_srgb,
            block_index=item.block_index,
            line_index=item.line_index,
            span_index=item.span_index,
        )
        for item in facts.text_spans
    )
    return PageFacts(
        page_id=facts.page_identity,
        source_pdf_sha256=facts.page.source_hash,
        width=facts.page.width_points,
        height=facts.page.height_points,
        native_text_object_count=len(text_objects),
        origin="production-kernel-facts",
        page_index=facts.page.page_no - 1,
        rotation=facts.rotation,
        text_objects=text_objects,
        image_objects=tuple(
            ImageObjectFact(
                object_id=item.object_id,
                bbox=item.bbox,
                width=item.width,
                height=item.height,
                content_sha256=item.content_hash,
            )
            for item in facts.image_objects
        ),
        drawing_objects=tuple(
            DrawingObjectFact(
                object_id=item.object_id,
                bbox=item.bbox,
                content_sha256=item.content_hash,
            )
            for item in facts.drawing_objects
        ),
        geometry_sha256=facts.page.geometry_hash,
        text_objects_sha256=content_sha256(text_objects),
        locked_objects_sha256=facts.locked_objects_hash,
    )


def lift_translation_bundle(
    *,
    request_id: str,
    page_id: str,
    translations: tuple[tuple[str, str], ...],
) -> PageTranslationBundle:
    """Project a validated production bundle into the leaf-core layout input."""

    return PageTranslationBundle(
        request_id=request_id,
        page_id=page_id,
        provider="production-translation-port",
        model="opaque",
        translations=tuple(
            TranslationResult(container_id=container_id, translated_text=text)
            for container_id, text in translations
        ),
        response_sha256=content_sha256(translations),
    )
