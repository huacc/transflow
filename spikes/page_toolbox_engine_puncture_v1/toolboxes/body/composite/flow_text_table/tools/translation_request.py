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
    """Create one page-level request while retaining leaf container identities."""

    rows: list[tuple[tuple[float, float, int, str], str, str, tuple[str, ...]]] = []
    for region in template.flow_regions:
        for container in region.template.containers:
            rows.append(
                (
                    (container.source_bbox[1], container.source_bbox[0], 0, container.container_id),
                    container.container_id,
                    container.source_text,
                    (),
                )
            )
    for cell in template.table_template.translatable_cells:
        rows.append(
            (
                (cell.source_bbox[1], cell.source_bbox[0], 1, cell.container_id),
                cell.container_id,
                cell.source_text,
                cell.protected_tokens,
            )
        )
    rows.sort(key=lambda item: item[0])
    units = tuple(
        TranslationUnit(container_id, source_text, reading_order, required_literals)
        for reading_order, (_, container_id, source_text, required_literals) in enumerate(rows)
    )
    return PageTranslationRequest(
        f"p7-{template.page_id}-{source_language}-{target_language}",
        template.page_id,
        source_language,
        target_language,
        units,
    )


def split_translation_bundle(
    template: CompositePageTemplate,
    bundle: PageTranslationBundle,
) -> tuple[tuple[PageTranslationBundle, ...], PageTranslationBundle]:
    translated_by_id = {item.container_id: item for item in bundle.translations}
    expected_ids = {item.container_id for item in template.container_ownerships}
    if set(translated_by_id) != expected_ids:
        raise ValueError("translation_ids_do_not_match_composite_template")

    flow_bundles = tuple(
        _slice(bundle, tuple(container.container_id for container in region.template.containers))
        for region in template.flow_regions
    )
    table_bundle = _slice(
        bundle,
        tuple(cell.container_id for cell in template.table_template.translatable_cells),
    )
    return flow_bundles, table_bundle


def _slice(bundle: PageTranslationBundle, container_ids: tuple[str, ...]) -> PageTranslationBundle:
    by_id = {item.container_id: item for item in bundle.translations}
    return PageTranslationBundle(
        bundle.request_id,
        bundle.page_id,
        bundle.provider,
        bundle.model,
        tuple(TranslationResult(container_id, by_id[container_id].translated_text) for container_id in container_ids),
        bundle.provider_request_id,
        bundle.latency_ms,
        bundle.response_sha256,
    )
