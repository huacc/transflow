from __future__ import annotations

import re

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
    ordered = sorted(
        template.containers,
        key=lambda item: (item.reading_order, item.source_bbox[1], item.source_bbox[0]),
    )
    units = tuple(
        TranslationUnit(
            container_id=container.composite_id,
            source_text=container.source_text,
            reading_order=index,
            required_literals=_required_literals(
                container.source_text,
                container.required_literals,
            ),
        )
        for index, container in enumerate(ordered)
    )
    return PageTranslationRequest(
        request_id=f"p15-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def _required_literals(text: str, existing: tuple[str, ...]) -> tuple[str, ...]:
    structural = (
        *re.findall(r"\bVS\b", text, flags=re.IGNORECASE),
        *re.findall(r"(?<=\d)[KMBT]\b", text),
    )
    return tuple(dict.fromkeys((*existing, *structural)))


def slice_translation_bundle(
    template: CompositePageTemplate,
    bundle: PageTranslationBundle,
) -> tuple[PageTranslationBundle, PageTranslationBundle]:
    by_composite_id = {item.container_id: item.translated_text for item in bundle.translations}
    composite_by_base = {
        (item.owner, item.base_container_id): item
        for item in template.containers
    }

    if template.anchored_template is None:
        anchored_ids = [
            item.base_container_id for item in template.containers if item.owner == "anchored"
        ]
    else:
        anchored_ids = [item.container_id for item in template.anchored_template.containers]
    if template.chart_template is None:
        chart_ids = [
            item.base_container_id
            for item in template.containers
            if item.owner in {"chart", "shared"}
        ]
    else:
        chart_ids = [item.container_id for item in template.chart_template.containers]

    anchored = tuple(
        TranslationResult(
            base_id,
            by_composite_id[composite_by_base[("anchored", base_id)].composite_id],
        )
        for base_id in anchored_ids
    )
    chart = tuple(
        TranslationResult(
            base_id,
            by_composite_id[
                next(
                    item.composite_id
                    for owner in ("chart", "shared")
                    if (item := composite_by_base.get((owner, base_id))) is not None
                )
            ],
        )
        for base_id in chart_ids
    )
    metadata = {
        "request_id": bundle.request_id,
        "page_id": bundle.page_id,
        "provider": bundle.provider,
        "model": bundle.model,
        "provider_request_id": bundle.provider_request_id,
        "latency_ms": bundle.latency_ms,
        "response_sha256": bundle.response_sha256,
    }
    return (
        PageTranslationBundle(translations=anchored, **metadata),
        PageTranslationBundle(translations=chart, **metadata),
    )
