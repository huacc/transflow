from __future__ import annotations

import json
import math
import re
from dataclasses import replace

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
    TranslationUnit,
)
from page_toolbox_puncture.translation import (
    ProviderError,
    QwenPageTranslationProvider,
    TranslationProvider,
)


def translate_with_guard(
    provider: TranslationProvider,
    request: PageTranslationRequest,
) -> tuple[PageTranslationBundle, dict[str, object]]:
    bundle = provider.translate(request)
    bundle.validate_against(request)
    first = translation_validation(request, bundle)
    retry_ids = set(first["failed_container_ids"])
    retries = 0
    provider_error = None
    if retry_ids and getattr(provider, "provider_name", "") not in {"fixed", "recorded"}:
        retry_request = PageTranslationRequest(
            request_id=f"{request.request_id}-guard-retry",
            page_id=request.page_id,
            source_language=request.source_language,
            target_language=request.target_language,
            units=tuple(
                replace(
                    item,
                    source_text=_structured_output_safe_clause(item.source_text),
                )
                for item in request.units
                if item.container_id in retry_ids
            ),
        )
        try:
            current = {item.container_id: item.translated_text for item in bundle.translations}
            retry_provider = _guard_retry_provider(
                provider,
                first,
                failed_outputs={container_id: current[container_id] for container_id in retry_ids},
            )
            repaired = retry_provider.translate(retry_request)
            repaired.validate_against(retry_request)
            bundle = _merge_repaired_bundle(
                bundle,
                {item.container_id: item for item in repaired.translations},
                repaired,
            )
            bundle.validate_against(request)
            retries = 1
        except ProviderError as exc:
            provider_error = exc.code
    final = translation_validation(request, bundle)
    if (
        final["failed_container_ids"]
        and isinstance(provider, QwenPageTranslationProvider)
        and provider_error is None
    ):
        try:
            bundle, recovered = _recover_by_semantic_clauses(
                provider,
                request,
                bundle,
                final,
            )
            if recovered:
                bundle.validate_against(request)
                retries = 2
                final = translation_validation(request, bundle)
        except ProviderError as exc:
            provider_error = exc.code
    return bundle, {
        "schema_version": "p18-translation-guard/v1",
        "initial": first,
        "final": final,
        "retry_count": retries,
        "provider_error": provider_error,
    }


def translation_validation(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
) -> dict[str, object]:
    by_id = {item.container_id: item.translated_text for item in bundle.translations}
    missing_literals: dict[str, list[str]] = {}
    language_mismatches: dict[str, dict[str, int]] = {}
    inadequate_outputs: dict[str, dict[str, int]] = {}
    list_structure_mismatches: dict[str, dict[str, int]] = {}
    terminal_punctuation_mismatches: dict[str, dict[str, str]] = {}
    delimiter_balance_mismatches: dict[str, dict[str, int]] = {}
    for unit in request.units:
        translated = by_id[unit.container_id]
        missing = [
            literal
            for literal in unit.required_literals
            if not _literal_preserved(
                unit.source_text,
                translated,
                literal,
                request.target_language,
            )
        ]
        if missing:
            missing_literals[unit.container_id] = missing
        source_bullets = _bullet_count(unit.source_text)
        target_bullets = _bullet_count(translated)
        if source_bullets != target_bullets:
            list_structure_mismatches[unit.container_id] = {
                "source_bullet_count": source_bullets,
                "target_bullet_count": target_bullets,
            }
        if _requires_terminal_punctuation(unit.source_text) and not _has_terminal_punctuation(translated):
            terminal_punctuation_mismatches[unit.container_id] = {
                "source_ending": unit.source_text.rstrip()[-8:],
                "target_ending": translated.rstrip()[-8:],
            }
        if _has_balanced_parentheses(unit.source_text) and _parenthesis_count(unit.source_text) and not _has_balanced_parentheses(translated):
            delimiter_balance_mismatches[unit.container_id] = {
                "source_parenthesis_count": _parenthesis_count(unit.source_text),
                "target_parenthesis_count": _parenthesis_count(translated),
            }
        source_latin = sum(char.isascii() and char.isalpha() for char in unit.source_text)
        source_cjk = sum("\u3400" <= char <= "\u9fff" for char in unit.source_text)
        target_latin = sum(char.isascii() and char.isalpha() for char in translated)
        target_cjk = sum("\u3400" <= char <= "\u9fff" for char in translated)
        counts = {
            "source_latin_count": source_latin,
            "source_cjk_count": source_cjk,
            "target_latin_count": target_latin,
            "target_cjk_count": target_cjk,
        }
        if request.source_language.casefold().startswith("en") and request.target_language.casefold().startswith("zh"):
            if source_latin >= 8 and target_cjk == 0 and target_latin >= 8:
                language_mismatches[unit.container_id] = counts
            if source_latin >= 8 and target_cjk == 0:
                inadequate_outputs[unit.container_id] = {
                    **counts,
                    "minimum_target_character_count": 1,
                }
            if source_latin >= 40 and target_cjk < source_latin * 0.08:
                inadequate_outputs[unit.container_id] = {
                    **counts,
                    "minimum_target_character_count": math.ceil(source_latin * 0.08),
                }
        elif request.source_language.casefold().startswith("zh") and request.target_language.casefold().startswith("en"):
            if source_cjk >= 4 and target_latin < 4 and target_cjk >= 4:
                language_mismatches[unit.container_id] = counts
            if source_cjk >= 4 and target_latin < 4:
                inadequate_outputs[unit.container_id] = {
                    **counts,
                    "minimum_target_character_count": 4,
                }
            if source_cjk >= 20 and target_latin < source_cjk * 2.5:
                inadequate_outputs[unit.container_id] = {
                    **counts,
                    "minimum_target_character_count": math.ceil(source_cjk * 2.5),
                }
    failed = sorted(
        set(missing_literals)
        | set(language_mismatches)
        | set(inadequate_outputs)
        | set(list_structure_mismatches)
        | set(terminal_punctuation_mismatches)
        | set(delimiter_balance_mismatches)
    )
    return {
        "status": "PASS" if not failed else "FAIL",
        "failed_container_ids": failed,
        "missing_required_literals": missing_literals,
        "target_language_mismatches": language_mismatches,
        "inadequate_outputs": inadequate_outputs,
        "list_structure_mismatches": list_structure_mismatches,
        "terminal_punctuation_mismatches": terminal_punctuation_mismatches,
        "delimiter_balance_mismatches": delimiter_balance_mismatches,
    }


def _bullet_count(text: str) -> int:
    equivalents = {"\uf0b7", "\uf0d8", "→", "•", "●", "▪", "◦", "‣", "·", "‧", "∙"}
    return sum(character in equivalents for character in text)


def _requires_terminal_punctuation(text: str) -> bool:
    if _terminal_character(text) not in {"。", "！", "？", "；", ".", "!", "?", ";"}:
        return False
    latin = sum(character.isascii() and character.isalpha() for character in text)
    cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
    return latin >= 20 or cjk >= 10


def _has_terminal_punctuation(text: str) -> bool:
    return _terminal_character(text) in {"。", "！", "？", "；", ".", "!", "?", ";"}


def _terminal_character(text: str) -> str:
    stripped = text.rstrip()
    closers = set("\"'”’）》】」』")
    while stripped and stripped[-1] in closers:
        stripped = stripped[:-1].rstrip()
    return stripped[-1:]


def _parenthesis_count(text: str) -> int:
    normalized = text.translate(str.maketrans({"（": "(", "）": ")"}))
    return normalized.count("(") + normalized.count(")")


def _has_balanced_parentheses(text: str) -> bool:
    normalized = text.translate(str.maketrans({"（": "(", "）": ")"}))
    depth = 0
    for character in normalized:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _literal_preserved(
    source_text: str,
    translated_text: str,
    literal: str,
    target_language: str,
) -> bool:
    if literal in translated_text:
        return True
    if not target_language.casefold().startswith("en") or not literal.isdigit():
        return False
    month = int(literal)
    if not 1 <= month <= 12 or f"{literal}月" not in source_text:
        return False
    names = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    return names[month - 1] in translated_text.casefold()


def _guard_retry_provider(
    provider: TranslationProvider,
    validation: dict[str, object],
    *,
    failed_outputs: dict[str, str] | None = None,
):
    if not isinstance(provider, QwenPageTranslationProvider):
        return provider
    failure_context = {
        key: validation.get(key, {})
        for key in (
            "missing_required_literals",
            "target_language_mismatches",
            "inadequate_outputs",
            "list_structure_mismatches",
            "terminal_punctuation_mismatches",
            "delimiter_balance_mismatches",
        )
        if validation.get(key)
    }
    suffix = (
        "\n\n这是质量门定向重试。上一次输出未通过完整性校验："
        + json.dumps(failure_context, ensure_ascii=False, sort_keys=True)
        + "。上一次不合格译文："
        + json.dumps(failed_outputs or {}, ensure_ascii=False, sort_keys=True)
        + "。本次必须覆盖原文从开头到最后一个分句的全部内容，逐句完整翻译，"
        "输出不得与上一次不合格译文相同，且字符覆盖量必须达到 "
        "minimum_target_character_count；禁止摘要、截断或只返回句首；"
        "必须使用请求指定的目标语言，并逐字保留 required_literals 和每个 •/→ 标记。"
    )
    return QwenPageTranslationProvider(
        provider.config,
        provider.prompt_text + suffix,
    )


def _recover_by_semantic_clauses(
    provider: QwenPageTranslationProvider,
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
    validation: dict[str, object],
) -> tuple[PageTranslationBundle, bool]:
    failed = set(validation["failed_container_ids"])
    clause_units = []
    clause_ids_by_container: dict[str, list[str]] = {}
    for unit in request.units:
        if unit.container_id not in failed:
            continue
        clauses = _semantic_clauses(unit.source_text)
        if len(clauses) < 2:
            continue
        clause_ids_by_container[unit.container_id] = []
        for index, clause in enumerate(clauses):
            clause_id = f"{unit.container_id}/guard-clause-{index:02d}"
            clause_ids_by_container[unit.container_id].append(clause_id)
            clause_units.append(
                TranslationUnit(
                    clause_id,
                    _structured_output_safe_clause(clause),
                    len(clause_units),
                    tuple(literal for literal in unit.required_literals if literal in clause),
                )
            )
    if not clause_units:
        return bundle, False
    current = {item.container_id: item.translated_text for item in bundle.translations}
    clause_provider = QwenPageTranslationProvider(
        provider.config,
        provider.prompt_text
        + "\n\n这是最终分句恢复。当前请求中的每个 unit 都是原容器的一个连续分句；"
        "逐个完整翻译该 unit，保留其标点和 required_literals，不得摘要、续写、合并或遗漏。"
        "为兼容结构化 JSON，目标文本不要使用引号装饰简称；括号内简称可使用无引号形式，"
        "但必须保留简称文字并闭合所有括号，绝不能停在左括号之后。"
        "此前不合格译文仅用于识别失败，禁止复制："
        + json.dumps(
            {container_id: current[container_id] for container_id in failed},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    translations = []
    provider_request_ids = []
    latency_ms = 0
    for index, unit in enumerate(clause_units):
        single_request = PageTranslationRequest(
            request_id=f"{request.request_id}-clause-retry-{index:02d}",
            page_id=request.page_id,
            source_language=request.source_language,
            target_language=request.target_language,
            units=(replace(unit, reading_order=0),),
        )
        single = clause_provider.translate(single_request)
        single.validate_against(single_request)
        clause_audit = translation_validation(single_request, single)
        if clause_audit["status"] != "PASS":
            retry_provider = _guard_retry_provider(
                clause_provider,
                clause_audit,
                failed_outputs={
                    item.container_id: item.translated_text
                    for item in single.translations
                },
            )
            retried = retry_provider.translate(single_request)
            retried.validate_against(single_request)
            if single.provider_request_id:
                provider_request_ids.append(single.provider_request_id)
            if single.latency_ms is not None:
                latency_ms += single.latency_ms
            single = retried
        translations.extend(single.translations)
        if single.provider_request_id:
            provider_request_ids.append(single.provider_request_id)
        if single.latency_ms is not None:
            latency_ms += single.latency_ms
    repaired = PageTranslationBundle(
        request_id=f"{request.request_id}-clause-retry",
        page_id=request.page_id,
        provider=provider.provider_name,
        model=provider.model_name,
        translations=tuple(translations),
        provider_request_id=",".join(provider_request_ids) or None,
        latency_ms=latency_ms or None,
    )
    by_clause = {item.container_id: item.translated_text.strip() for item in repaired.translations}
    replacement = {
        container_id: TranslationResult(
            container_id,
            " ".join(by_clause[clause_id] for clause_id in clause_ids),
        )
        for container_id, clause_ids in clause_ids_by_container.items()
    }
    return _merge_repaired_bundle(bundle, replacement, repaired), True


def _semantic_clauses(text: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in re.findall(r".+?(?:[，。；！？,.;!?]+|$)", text, flags=re.DOTALL)
        if part.strip()
    )


def _structured_output_safe_clause(text: str) -> str:
    return text.translate(str.maketrans("", "", "「」『』《》“”"))


def _merge_repaired_bundle(
    bundle: PageTranslationBundle,
    replacement: dict[str, TranslationResult],
    repaired: PageTranslationBundle,
) -> PageTranslationBundle:
    return PageTranslationBundle(
        request_id=bundle.request_id,
        page_id=bundle.page_id,
        provider=bundle.provider,
        model=bundle.model,
        translations=tuple(
            replacement.get(item.container_id, item) for item in bundle.translations
        ),
        provider_request_id=",".join(
            value
            for value in (bundle.provider_request_id, repaired.provider_request_id)
            if value
        )
        or None,
        latency_ms=sum(
            value for value in (bundle.latency_ms, repaired.latency_ms) if value is not None
        )
        or None,
        response_sha256=None,
    )
