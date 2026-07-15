from __future__ import annotations

import hashlib
import re

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
)
from page_toolbox_puncture.translation import TranslationProvider


_PLACEHOLDER_PATTERN = re.compile(
    r"\[(?:missing\s+)?(?:value|data|text)\]|\b(?:TODO|TBD)\b",
    re.IGNORECASE,
)
_DANGLING_ENGLISH_FUNCTION_WORD = re.compile(
    r"\b(?:for|of|to|in|on|at|with|by)\s+(?:the|a|an)?\s*$",
    re.IGNORECASE,
)
_DANGLING_ENGLISH_OUTPUT = re.compile(
    r"\b(?:a|an|and|at|by|for|from|in|of|on|or|the|to|with)\s*$",
    re.IGNORECASE,
)
_LEGAL_NAME = re.compile(
    r"\b(?:[A-Z][A-Za-z'’&.-]*\s+){1,5}(?:Limited|Incorporated|Corporation|PLC)\b"
)


class RequiredLiteralRetryProvider:
    provider_name = "qwen_guarded"

    def __init__(self, primary: TranslationProvider, retry: TranslationProvider) -> None:
        self.primary = primary
        self.retry = retry
        self.model_name = primary.model_name
        self.last_audit: dict[str, object] = {"status": "NOT_RUN"}

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle:
        shielded_request, literal_map = _shield_request(request)
        primary_bundle = self.primary.translate(shielded_request)
        primary_bundle.validate_against(shielded_request)
        primary_bundle, compacted_fragment_ids = _compact_boundary_fragments(
            shielded_request,
            primary_bundle,
        )
        missing = _missing_literals(shielded_request, primary_bundle)
        residue = source_language_residue(shielded_request, primary_bundle)
        placeholders = invented_placeholders(shielded_request, primary_bundle)
        incomplete = incomplete_translations(shielded_request, primary_bundle)
        retry_ids = set(missing) | set(residue) | set(placeholders) | set(incomplete)
        if not retry_ids:
            final = _restore_bundle(request, primary_bundle, literal_map, self.provider_name, self.model_name)
            self.last_audit = {
                "status": "NOT_NEEDED",
                "retried_container_ids": [],
                "compacted_fragment_ids": compacted_fragment_ids,
                "confirmed_proper_name_ids": [],
                "primary_incomplete_translations": {},
                "remaining_incomplete_translations": {},
                "shielded_literal_count": sum(len(items) for items in literal_map.values()),
            }
            return final

        retry_units = tuple(unit for unit in shielded_request.units if unit.container_id in retry_ids)
        retry_request = PageTranslationRequest(
            request_id=f"{shielded_request.request_id}-required-literal-retry",
            page_id=shielded_request.page_id,
            source_language=shielded_request.source_language,
            target_language=shielded_request.target_language,
            units=retry_units,
        )
        retry_bundle = self.retry.translate(retry_request)
        retry_bundle.validate_against(retry_request)
        retry_bundle, retry_compacted_ids = _compact_boundary_fragments(retry_request, retry_bundle)
        retry_by_id = {item.container_id: item for item in retry_bundle.translations}
        merged = tuple(
            retry_by_id.get(item.container_id, item)
            for item in primary_bundle.translations
        )
        shielded_final = PageTranslationBundle(
            request_id=shielded_request.request_id,
            page_id=shielded_request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(TranslationResult(item.container_id, item.translated_text) for item in merged),
            provider_request_id=",".join(
                value
                for value in (primary_bundle.provider_request_id, retry_bundle.provider_request_id)
                if value
            )
            or None,
            latency_ms=(primary_bundle.latency_ms or 0) + (retry_bundle.latency_ms or 0),
            response_sha256=_combined_hash(primary_bundle.response_sha256, retry_bundle.response_sha256),
        )
        shielded_final.validate_against(shielded_request)
        remaining = _missing_literals(shielded_request, shielded_final)
        remaining_residue = source_language_residue(shielded_request, shielded_final)
        remaining_placeholders = invented_placeholders(shielded_request, shielded_final)
        remaining_incomplete = incomplete_translations(shielded_request, shielded_final)
        confirmed_proper_names = _confirmed_retained_proper_names(
            retry_request,
            primary_bundle,
            retry_bundle,
            remaining_residue,
        )
        remaining_residue = {
            container_id: values
            for container_id, values in remaining_residue.items()
            if container_id not in confirmed_proper_names
        }
        self.last_audit = {
            "status": (
                "PASS"
                if not remaining and not remaining_residue and not remaining_placeholders and not remaining_incomplete
                else "FAIL"
            ),
            "retried_container_ids": sorted(retry_ids),
            "compacted_fragment_ids": sorted(set(compacted_fragment_ids) | set(retry_compacted_ids)),
            "confirmed_proper_name_ids": confirmed_proper_names,
            "primary_missing_required_literals": missing,
            "primary_source_language_residue": residue,
            "primary_invented_placeholders": placeholders,
            "primary_incomplete_translations": incomplete,
            "remaining_missing_required_literals": remaining,
            "remaining_source_language_residue": remaining_residue,
            "remaining_invented_placeholders": remaining_placeholders,
            "remaining_incomplete_translations": remaining_incomplete,
            "shielded_literal_count": sum(len(items) for items in literal_map.values()),
        }
        return _restore_bundle(request, shielded_final, literal_map, self.provider_name, self.model_name)


def _confirmed_retained_proper_names(
    request: PageTranslationRequest,
    primary: PageTranslationBundle,
    retry: PageTranslationBundle,
    residue: dict[str, list[str]],
) -> list[str]:
    units = {unit.container_id: unit for unit in request.units}
    primary_text = {item.container_id: item.translated_text.strip() for item in primary.translations}
    retry_text = {item.container_id: item.translated_text.strip() for item in retry.translations}
    confirmed = []
    for container_id in residue:
        unit = units.get(container_id)
        if unit is None:
            continue
        source = unit.source_text.strip()
        words = re.findall(r"[A-Za-z][A-Za-z'’&.-]*", source)
        title_cased = words and all(
            word.casefold() in {"a", "an", "and", "of", "the"} or word[0].isupper()
            for word in words
        )
        if (
            1 <= len(words) <= 4
            and title_cased
            and primary_text.get(container_id) == source
            and retry_text.get(container_id) == source
        ):
            confirmed.append(container_id)
            continue
        retained_names = [
            name
            for name in _LEGAL_NAME.findall(source)
            if name in primary_text.get(container_id, "") and name in retry_text.get(container_id, "")
        ]
        retained_words = {
            word.casefold()
            for name in retained_names
            for word in re.findall(r"[A-Za-z][A-Za-z'’&.-]*", name)
        }
        residue_words = {word for word in residue[container_id] if word != "NO_HAN_TRANSLATION"}
        if (
            retained_names
            and "NO_HAN_TRANSLATION" not in residue[container_id]
            and residue_words
            and residue_words <= retained_words
            and re.search(r"[\u3400-\u9fff]", primary_text.get(container_id, ""))
            and re.search(r"[\u3400-\u9fff]", retry_text.get(container_id, ""))
        ):
            confirmed.append(container_id)
    return sorted(confirmed)


def _compact_boundary_fragments(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
) -> tuple[PageTranslationBundle, list[str]]:
    if not (
        request.source_language.casefold().startswith("en")
        and request.target_language.casefold().startswith("zh")
    ):
        return bundle, []
    units = {unit.container_id: unit for unit in request.units}
    compacted: list[str] = []
    translations = []
    for item in bundle.translations:
        unit = units[item.container_id]
        text = item.translated_text
        if not unit.required_literals and _DANGLING_ENGLISH_FUNCTION_WORD.search(unit.source_text.strip()):
            suffix = text.rsplit("的", 1)[-1].strip(" \t\r\n\"'“”‘’：:")
            if 1 <= len(re.findall(r"[\u3400-\u9fff]", suffix)) <= 8:
                text = suffix.rstrip("，,；;。.:：") + "："
        if text != item.translated_text:
            compacted.append(item.container_id)
        translations.append(TranslationResult(item.container_id, text))
    return (
        PageTranslationBundle(
            bundle.request_id,
            bundle.page_id,
            bundle.provider,
            bundle.model,
            tuple(translations),
            bundle.provider_request_id,
            bundle.latency_ms,
            bundle.response_sha256,
        ),
        compacted,
    )


def _missing_literals(request: PageTranslationRequest, bundle: PageTranslationBundle) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    return {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated[unit.container_id]]
        for unit in request.units
        if any(literal not in translated[unit.container_id] for literal in unit.required_literals)
    }


def _combined_hash(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return hashlib.sha256("".join(values).encode("ascii")).hexdigest() if values else None


def _shield_request(
    request: PageTranslationRequest,
) -> tuple[PageTranslationRequest, dict[str, dict[str, str]]]:
    literal_map: dict[str, dict[str, str]] = {}
    shielded_units = []
    for unit_index, unit in enumerate(request.units):
        markers = {
            f"P11LIT{unit_index:03d}X{literal_index:02d}P11": literal
            for literal_index, literal in enumerate(unit.required_literals)
        }
        marker_by_literal = {literal: marker for marker, literal in markers.items()}
        if marker_by_literal:
            alternatives = "|".join(
                re.escape(literal)
                for literal in sorted(marker_by_literal, key=len, reverse=True)
            )
            pattern = re.compile(rf"(?<![\w])(?:{alternatives})(?![\w])")
            text = pattern.sub(lambda match: marker_by_literal[match.group(0)], unit.source_text)
        else:
            text = unit.source_text
        literal_map[unit.container_id] = markers
        shielded_units.append(
            type(unit)(unit.container_id, text, unit.reading_order, tuple(markers))
        )
    return (
        PageTranslationRequest(
            request.request_id,
            request.page_id,
            request.source_language,
            request.target_language,
            tuple(shielded_units),
        ),
        literal_map,
    )


def _restore_bundle(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
    literal_map: dict[str, dict[str, str]],
    provider_name: str,
    model_name: str,
) -> PageTranslationBundle:
    restored = []
    for item in bundle.translations:
        text = item.translated_text
        for marker, literal in literal_map[item.container_id].items():
            text = text.replace(marker, literal)
        restored.append(TranslationResult(item.container_id, text))
    result = PageTranslationBundle(
        request.request_id,
        request.page_id,
        provider_name,
        model_name,
        tuple(restored),
        bundle.provider_request_id,
        bundle.latency_ms,
        bundle.response_sha256,
    )
    result.validate_against(request)
    return result


def source_language_residue(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    if request.target_language.casefold().startswith("en"):
        return {
            unit.container_id: sorted(set(re.findall(r"[\u3400-\u9fff]", translated[unit.container_id])))
            for unit in request.units
            if re.search(r"[\u3400-\u9fff]", translated[unit.container_id])
        }
    if request.target_language.casefold().startswith("zh"):
        result = {}
        for unit in request.units:
            output = translated[unit.container_id]
            source_without_literals = unit.source_text
            output_without_literals = output
            for literal in unit.required_literals:
                source_without_literals = source_without_literals.replace(literal, "")
                output_without_literals = output_without_literals.replace(literal, "")
            source_tokens = [
                word
                for word in re.findall(r"\b[A-Za-z]{4,}\b", source_without_literals)
                if not (word.isupper() and len(word) <= 5)
            ]
            output_tokens = re.findall(r"\b[A-Za-z]{4,}\b", output_without_literals)
            source_words = {word.casefold() for word in source_tokens}
            output_by_fold = {word.casefold(): word for word in output_tokens}
            residue = sorted(source_words & set(output_by_fold))
            has_han = bool(re.search(r"[\u3400-\u9fff]", output_without_literals))
            if has_han and len(residue) == 1:
                retained = output_by_fold[residue[0]]
                if len(retained) >= 10 and retained[0].isupper() and retained[1:].islower():
                    residue = []
            legal_name_suffix = bool(
                re.search(r"\b(?:LIMITED|INCORPORATED|CORPORATION|PLC)\b", source_without_literals)
            )
            if has_han and residue and legal_name_suffix and all(
                output_by_fold[word].isupper() for word in residue
            ):
                residue = []
            if source_words and not has_han:
                residue = sorted(set(residue) | {"NO_HAN_TRANSLATION"})
            if residue:
                result[unit.container_id] = residue
        return result
    return {}


def incomplete_translations(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
) -> dict[str, list[str]]:
    if not (
        request.source_language.casefold().startswith("zh")
        and request.target_language.casefold().startswith("en")
    ):
        return {}
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    result: dict[str, list[str]] = {}
    for unit in request.units:
        source = unit.source_text
        output = translated[unit.container_id]
        for literal in unit.required_literals:
            source = source.replace(literal, "")
            output = output.replace(literal, "")
        source_han_count = len(re.findall(r"[\u3400-\u9fff]", source))
        if source_han_count < 24:
            continue
        output_letter_count = len(re.findall(r"[A-Za-z]", output))
        reasons = []
        too_short = output_letter_count < source_han_count * 0.8
        dangling = bool(_DANGLING_ENGLISH_OUTPUT.search(output.strip()))
        if too_short and dangling:
            reasons.append("TARGET_TEXT_TOO_SHORT")
            reasons.append("DANGLING_ENGLISH_OUTPUT")
        if reasons:
            result[unit.container_id] = reasons
    return result


def invented_placeholders(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    result = {}
    for unit in request.units:
        source_values = {match.casefold() for match in _PLACEHOLDER_PATTERN.findall(unit.source_text)}
        output_values = {
            match
            for match in _PLACEHOLDER_PATTERN.findall(translated[unit.container_id])
            if match.casefold() not in source_values
        }
        if output_values:
            result[unit.container_id] = sorted(output_values, key=str.casefold)
    return result
