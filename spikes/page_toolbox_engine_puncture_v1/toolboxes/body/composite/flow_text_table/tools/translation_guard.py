from __future__ import annotations

import hashlib
import re

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
)
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from toolboxes.body.table.tools.layout_planner import _missing_protected_tokens
from toolboxes.body.table.tools.template_builder import is_currency_literal


_BULLET_RE = re.compile(r"[•\uf0b2◆◇▪◦□]")
_BULLET_RENDER = {"\uf0b2": "◇"}
_STRUCTURAL_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[\(\uff08](?:[A-Za-z]|\d+|[ivxlcdm]+|[一二三四五六七八九十百]+)[\)\uff09]"
    r"|(?:\d+|[ivxlcdm]+)[\.\)\u3001]"
    r"|\d+|[ivxlcdm]+"
    r"|[\u2022\u25cf\u25aa\uf0b7\uf0b2]"
    r")\s*$",
    flags=re.IGNORECASE,
)


def translate_with_targeted_guard_retry(
    provider: TranslationProvider,
    request: PageTranslationRequest,
) -> tuple[PageTranslationBundle, tuple[dict[str, object], ...]]:
    """Retry only a container with a literal violation or clear surface truncation."""

    current = provider.translate(request)
    current.validate_against(request)
    trace: list[dict[str, object]] = []
    current = _canonicalize_structural_markers(request, current, trace)
    current = _canonicalize_bullet_layout(request, current, trace)
    retry_count = 0
    for unit in request.units:
        result = next(item for item in current.translations if item.container_id == unit.container_id)
        missing = _missing_literals(unit.source_text, result.translated_text, unit.required_literals)
        surface_violations = _surface_violations(unit.source_text, result.translated_text)
        if not missing and not surface_violations:
            continue
        retry_count += 1
        retry_index = retry_count
        retry_request = PageTranslationRequest(
            f"{request.request_id}-literal-retry-{retry_index}-{unit.container_id}",
            request.page_id,
            request.source_language,
            request.target_language,
            (unit,),
        )
        retry_bundle = provider.translate(retry_request)
        retry_bundle.validate_against(retry_request)
        retry_bundle = _canonicalize_structural_markers(retry_request, retry_bundle, trace)
        retry_bundle = _canonicalize_bullet_layout(retry_request, retry_bundle, trace)
        replacement = retry_bundle.translations[0]
        still_missing = _missing_literals(unit.source_text, replacement.translated_text, unit.required_literals)
        if still_missing and any(is_currency_literal(literal) for literal in still_missing):
            replacement = TranslationResult(
                replacement.container_id,
                f"{replacement.translated_text.rstrip()} ({' '.join(still_missing)})",
            )
            trace.append(
                {
                    "kind": "REQUIRED_LITERAL_RESTORED",
                    "container_id": unit.container_id,
                    "literals": still_missing,
                    "verdict": "PASS",
                }
            )
            still_missing = _missing_literals(unit.source_text, replacement.translated_text, unit.required_literals)
        remaining_surface_violations = _surface_violations(unit.source_text, replacement.translated_text)
        trace.append(
            {
                "retry_index": retry_index,
                "container_id": unit.container_id,
                "missing_literals": missing,
                "surface_violations": surface_violations,
                "retry_request_id": retry_request.request_id,
                "provider_request_id": retry_bundle.provider_request_id,
                "latency_ms": retry_bundle.latency_ms,
                "response_sha256": retry_bundle.response_sha256,
                "verdict": "PASS" if not still_missing and not remaining_surface_violations else "FAIL",
            }
        )
        if still_missing or remaining_surface_violations:
            raise ProviderError(
                f"TRANSLATION_GUARD_RETRY_EXHAUSTED:{unit.container_id}:"
                f"{','.join(still_missing + remaining_surface_violations)}"
            )
        current = PageTranslationBundle(
            request.request_id,
            request.page_id,
            current.provider,
            current.model,
            tuple(
                replacement if item.container_id == unit.container_id else item
                for item in current.translations
            ),
            _join(current.provider_request_id, retry_bundle.provider_request_id),
            _sum(current.latency_ms, retry_bundle.latency_ms),
            _combine_hashes(current.response_sha256, retry_bundle.response_sha256),
        )
        current.validate_against(request)
    return current, tuple(trace)


def _missing_literals(
    source_text: str,
    translated_text: str,
    literals: tuple[str, ...],
) -> tuple[str, ...]:
    return _missing_protected_tokens(source_text, translated_text, literals)


def _surface_violations(source_text: str, translated_text: str) -> tuple[str, ...]:
    violations: list[str] = []
    source = source_text.rstrip()
    translated = translated_text.rstrip()
    source_complete = _ends_with_terminal_punctuation(source)
    if source_complete and not _ends_with_terminal_punctuation(translated):
        violations.append("TERMINAL_PUNCTUATION_MISSING")
    if _bracket_balance(source) == 0 and _bracket_balance(translated) != 0:
        violations.append("UNBALANCED_TARGET_BRACKETS")
    if source_complete and re.search(r"\b(?:a|an|the|of|to|for|and|or|with|by|in|on|at)\s*$", translated, flags=re.IGNORECASE):
        violations.append("DANGLING_TARGET_FUNCTION_WORD")
    source_bullets = len(_BULLET_RE.findall(source_text))
    target_matches = tuple(_BULLET_RE.finditer(translated_text))
    target_bullets = len(target_matches)
    if source_bullets != target_bullets:
        violations.append("BULLET_COUNT_MISMATCH")
    elif source_bullets and any(match.start() > 0 and translated_text[match.start() - 1] != "\n" for match in target_matches):
        violations.append("BULLET_LINEBREAK_MISMATCH")
    return tuple(violations)


def _ends_with_terminal_punctuation(value: str) -> bool:
    stripped = re.sub(r"[\s\"'”’）)】》]+$", "", value)
    return bool(stripped) and stripped[-1] in ".!?。！？;；:："


def _bracket_balance(value: str) -> int:
    return sum(value.count(character) for character in "(（[【") - sum(
        value.count(character) for character in ")）]】"
    )


def _canonicalize_bullet_layout(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
    trace: list[dict[str, object]],
) -> PageTranslationBundle:
    units = {unit.container_id: unit for unit in request.units}
    normalized: list[TranslationResult] = []
    changed = False
    for result in bundle.translations:
        unit = units[result.container_id]
        source_markers = _BULLET_RE.findall(unit.source_text)
        target_markers = _BULLET_RE.findall(result.translated_text)
        source_count = len(source_markers)
        target_count = len(target_markers)
        text = result.translated_text
        if source_count and source_count == target_count:
            parts = _BULLET_RE.split(text)
            source_has_label = bool(_BULLET_RE.split(unit.source_text, maxsplit=1)[0].strip())
            target_label = parts[0].strip()
            render_markers = [_BULLET_RENDER.get(marker, marker) for marker in source_markers]
            bullet_lines = [
                f"{marker} {part.strip()}"
                for marker, part in zip(render_markers, parts[1:])
            ]
            candidate = "\n".join(([target_label] if source_has_label and target_label else []) + bullet_lines)
            if candidate and candidate != text:
                text = candidate
                changed = True
                trace.append(
                    {
                        "kind": "BULLET_LINEBREAK_CANONICALIZED",
                        "container_id": unit.container_id,
                        "bullet_count": source_count,
                        "verdict": "PASS",
                    }
                )
        normalized.append(TranslationResult(result.container_id, text))
    if not changed:
        return bundle
    return PageTranslationBundle(
        bundle.request_id,
        bundle.page_id,
        bundle.provider,
        bundle.model,
        tuple(normalized),
        bundle.provider_request_id,
        bundle.latency_ms,
        bundle.response_sha256,
    )


def _canonicalize_structural_markers(
    request: PageTranslationRequest,
    bundle: PageTranslationBundle,
    trace: list[dict[str, object]],
) -> PageTranslationBundle:
    units = {unit.container_id: unit for unit in request.units}
    normalized: list[TranslationResult] = []
    changed = False
    for result in bundle.translations:
        unit = units[result.container_id]
        text = result.translated_text
        if _STRUCTURAL_MARKER_RE.fullmatch(unit.source_text) and text.strip() != unit.source_text.strip():
            text = unit.source_text.strip()
            changed = True
            trace.append(
                {
                    "kind": "STRUCTURAL_MARKER_PRESERVED",
                    "container_id": unit.container_id,
                    "verdict": "PASS",
                }
            )
        normalized.append(TranslationResult(result.container_id, text))
    if not changed:
        return bundle
    return PageTranslationBundle(
        bundle.request_id,
        bundle.page_id,
        bundle.provider,
        bundle.model,
        tuple(normalized),
        bundle.provider_request_id,
        bundle.latency_ms,
        bundle.response_sha256,
    )


def _join(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return ",".join(values) or None


def _sum(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _combine_hashes(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return hashlib.sha256("".join(values).encode("ascii")).hexdigest() if values else None
