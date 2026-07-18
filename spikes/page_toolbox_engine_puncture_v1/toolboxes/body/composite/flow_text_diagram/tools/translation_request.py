from __future__ import annotations

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
    TranslationUnit,
)

from .models import CompositePageTemplate


def build_translation_request(
    template: CompositePageTemplate,
    *,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest:
    units = tuple(
        TranslationUnit(
            container_id=container.composite_id,
            source_text=container.source_text,
            reading_order=container.reading_order,
            required_literals=container.required_literals,
        )
        for container in template.containers
    )
    if not units:
        raise ValueError("P18_TRANSLATION_REQUEST_EMPTY")
    if len({item.container_id for item in units}) != len(units):
        raise ValueError("P18_DUPLICATE_TRANSLATION_CONTAINER_ID")
    return PageTranslationRequest(
        request_id=f"p18-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def split_translation_bundle(
    template: CompositePageTemplate,
    bundle: PageTranslationBundle,
) -> tuple[PageTranslationBundle, PageTranslationBundle]:
    translated = {item.container_id: item for item in bundle.translations}
    expected = {item.composite_id for item in template.containers}
    if set(translated) != expected:
        raise ValueError("P18_TRANSLATION_IDS_DO_NOT_MATCH_TEMPLATE")

    flow_rows = []
    diagram_rows = []
    for container in template.containers:
        result = TranslationResult(
            container_id=container.base_container_id,
            translated_text=translated[container.composite_id].translated_text,
        )
        if container.owner == "diagram":
            diagram_rows.append(result)
        else:
            flow_rows.append(result)
    flow_order = [item.container_id for item in template.flow_template.containers]
    diagram_order = [item.container_id for item in template.diagram_template.containers]
    flow_by_id = {item.container_id: item for item in flow_rows}
    diagram_by_id = {item.container_id: item for item in diagram_rows}
    if set(flow_by_id) != set(flow_order) or set(diagram_by_id) != set(diagram_order):
        raise ValueError("P18_CHILD_TRANSLATION_IDS_DO_NOT_MATCH")
    return (
        _bundle(bundle, tuple(flow_by_id[item] for item in flow_order)),
        _bundle(bundle, tuple(diagram_by_id[item] for item in diagram_order)),
    )


def _bundle(
    source: PageTranslationBundle,
    translations: tuple[TranslationResult, ...],
) -> PageTranslationBundle:
    return PageTranslationBundle(
        request_id=source.request_id,
        page_id=source.page_id,
        provider=source.provider,
        model=source.model,
        translations=translations,
        provider_request_id=source.provider_request_id,
        latency_ms=source.latency_ms,
        response_sha256=source.response_sha256,
    )
