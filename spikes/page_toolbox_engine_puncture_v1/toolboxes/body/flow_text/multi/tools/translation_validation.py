"""多栏工具箱自有的译文规范化与单容器定向重试。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
)
from page_toolbox_puncture.translation import ProviderError, TranslationProvider

from .models import MultiColumnTemplate


def canonicalize_with_targeted_retry(
    *,
    request: PageTranslationRequest,
    translation: PageTranslationBundle,
    template: MultiColumnTemplate,
    provider: TranslationProvider,
) -> tuple[PageTranslationBundle, tuple[dict[str, object], ...]]:
    """一次只重译一个具有直接错误证据的容器。"""

    current = translation
    retry_trace: list[dict[str, object]] = []
    attempted: set[tuple[str, str]] = set()
    retryable_prefixes = (
        "P5_TRANSLATION_SUSPICIOUS_DUPLICATE:",
        "P5_TRANSLATION_META_COMMENTARY:",
        "P5_TRANSLATION_LIST_MARKER_COUNT_MISMATCH:",
        "P5_TRANSLATION_STRUCTURALLY_INCOMPLETE:",
    )
    while True:
        try:
            return _canonicalize(request, current, template), tuple(retry_trace)
        except ProviderError as exc:
            if not exc.code.startswith(retryable_prefixes):
                raise
            parts = exc.code.split(":")
            targets = parts[-2:] if exc.code.startswith("P5_TRANSLATION_SUSPICIOUS_DUPLICATE:") else parts[-1:]
            for container_id in targets:
                attempt_key = (exc.code, container_id)
                if attempt_key in attempted:
                    raise
                attempted.add(attempt_key)
                unit = next((item for item in request.units if item.container_id == container_id), None)
                if unit is None:
                    raise
                retry_index = len(retry_trace) + 1
                retry_request = PageTranslationRequest(
                    f"{request.request_id}-retry-{retry_index}-{container_id}",
                    request.page_id,
                    request.source_language,
                    request.target_language,
                    (unit,),
                )
                retry_bundle = provider.translate(retry_request)
                retry_bundle.validate_against(retry_request)
                replacement = retry_bundle.translations[0]
                current = PageTranslationBundle(
                    request_id=request.request_id,
                    page_id=request.page_id,
                    provider=current.provider,
                    model=current.model,
                    translations=tuple(
                        replacement if item.container_id == container_id else item
                        for item in current.translations
                    ),
                    provider_request_id=_join_optional(current.provider_request_id, retry_bundle.provider_request_id),
                    latency_ms=_sum_optional(current.latency_ms, retry_bundle.latency_ms),
                    response_sha256=_combine_hashes(current.response_sha256, retry_bundle.response_sha256),
                )
                current.validate_against(request)
                retry_trace.append(
                    {
                        "retry_index": retry_index,
                        "trigger": exc.code,
                        "target_container_id": container_id,
                        "retry_request_id": retry_request.request_id,
                        "provider_request_id": retry_bundle.provider_request_id,
                        "latency_ms": retry_bundle.latency_ms,
                        "response_sha256": retry_bundle.response_sha256,
                    }
                )


def _canonicalize(
    request: PageTranslationRequest,
    translation: PageTranslationBundle,
    template: MultiColumnTemplate,
) -> PageTranslationBundle:
    source_by_id = {unit.container_id: unit.source_text for unit in request.units}
    prefix_by_id = {item.container_id: item.preserved_prefix for item in template.containers}
    forbidden_meta = (
        "the original text has a line break",
        "i will preserve it",
        "the translation is complete",
        "原文在此处有换行",
        "翻译完成",
    )
    normalized: list[TranslationResult] = []
    long_translations: dict[str, tuple[str, str]] = {}
    for item in translation.translations:
        source_text = source_by_id[item.container_id]
        source_count = source_text.count("•")
        text = item.translated_text.replace("\uf0b7", "•")
        prefix = prefix_by_id[item.container_id]
        if prefix:
            text = re.sub(rf"^\s*{re.escape(prefix)}\s*", "", text, count=1)
        if any(phrase in text.casefold() for phrase in forbidden_meta):
            raise ProviderError(f"P5_TRANSLATION_META_COMMENTARY:{item.container_id}")
        if _is_structurally_incomplete_translation(
            source_text=source_text,
            translated_text=text,
            source_language=request.source_language,
            target_language=request.target_language,
        ):
            raise ProviderError(f"P5_TRANSLATION_STRUCTURALLY_INCOMPLETE:{item.container_id}")
        if source_count:
            text = text.replace("□", "•")
            text = re.sub(r"\s*•\s*", "\n• ", text).strip()
            if text.count("•") != source_count:
                raise ProviderError(f"P5_TRANSLATION_LIST_MARKER_COUNT_MISMATCH:{item.container_id}")
        duplicate_key = re.sub(r"\s+", "", text).casefold()
        source_key = re.sub(r"\s+", "", source_text).casefold()
        if len(duplicate_key) >= 120 and duplicate_key in long_translations:
            previous_id, previous_source = long_translations[duplicate_key]
            if previous_source != source_key:
                raise ProviderError(f"P5_TRANSLATION_SUSPICIOUS_DUPLICATE:{previous_id}:{item.container_id}")
        long_translations[duplicate_key] = (item.container_id, source_key)
        normalized.append(TranslationResult(item.container_id, text))
    return replace(translation, translations=tuple(normalized))


def _is_structurally_incomplete_translation(
    *,
    source_text: str,
    translated_text: str,
    source_language: str,
    target_language: str,
) -> bool:
    """只拦截明确截断，不使用公司名、数字、样本坐标或样本 ID。"""

    text = translated_text.strip()
    if not text:
        return True
    for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
        if text.count(left) != text.count(right):
            return True
    dangling_english_word = re.search(
        r"\b(?:a|an|the|of|to|and|or|for|with|in|on|by|as)\s*$",
        text,
        flags=re.IGNORECASE,
    ) if target_language.casefold().startswith("en") else None
    if dangling_english_word:
        terminal_word = dangling_english_word.group(0).strip().casefold()
        # 列表倒数第二项可以合法地以“；及 / ; and”或“；或 / ; or”结束，不能误判为截断。
        source_has_terminal_list_conjunction = bool(
            source_language.casefold().startswith("zh")
            and re.search(r"(?:[；;]\s*)?(?:以及|及|和|或)\s*$", source_text)
        )
        if terminal_word not in {"and", "or"} or not source_has_terminal_list_conjunction:
            return True
    if source_language.casefold().startswith("zh") and target_language.casefold().startswith("en"):
        source_compact = re.sub(r"\s+", "", source_text)
        target_compact = re.sub(r"\s+", "", text)
        source_is_sentence = bool(re.search(r"[。！？；]$", source_compact))
        target_has_ending = bool(re.search(r"[.!?;:)\]\}\"']$", target_compact))
        if (
            source_is_sentence
            and len(source_compact) >= 24
            and len(target_compact) < len(source_compact) * 0.65
            and not target_has_ending
        ):
            return True
    return False


def _join_optional(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return ",".join(values) or None


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _combine_hashes(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    if not values:
        return None
    return hashlib.sha256("".join(values).encode("ascii")).hexdigest()
