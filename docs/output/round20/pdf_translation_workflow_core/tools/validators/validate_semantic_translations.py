"""Validate semantic translation JSON against extracted PDF text units.

tool_name: validate_semantic_translations
category: validators
input_contract: source extraction JSON and semantic translations JSON
output_contract: JSON verdict with missing/invalid unit evidence
failure_signals: missing units, placeholder text, provider missing, token preservation failure
fallback: mark S_FAIL_CAPABILITY or S_FAIL_QUALITY; do not generate a product candidate
anti_overfit_statement: validates current-run unit ids/text only and never branches on sample filename, page number, coordinates, or known document identity
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, resolve_workspace_path, write_json  # noqa: E402
from planners.build_translation_batch_manifest import line_is_translatable as manifest_line_is_translatable  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_OR_DIGIT_RE = re.compile(r"[A-Za-z0-9]")
TOKEN_PATTERNS = [
    ("footnote_marker", re.compile(r"\(\d+\)")),
    ("year", re.compile(r"\b(?:19|20)\d{2}\b")),
    ("percentage", re.compile(r"\(?\d+(?:\.\d+)?%\)?")),
    ("currency_amount", re.compile(r"\b(?:US\$|HK\$|RMB|USD|HKD)?\d[\d,]*(?:\.\d+)?\s*(?:million|billion|trillion)?\b", re.IGNORECASE)),
    ("number", re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")),
]
FORBIDDEN_PROVIDERS = {"", "deterministic_placeholder", "placeholder", "manual_placeholder", None}
FORBIDDEN_TRANSLATIONS = {
    "中文回填",
    "中文标题",
    "中文标签",
    "待翻译",
    "占位",
    "placeholder",
    "tbd",
}
FORBIDDEN_TRANSLATION_PATTERNS = [
    (
        "meta_line_description_zh",
        re.compile(r"^本行(?:说明|列示|报告|描述|展示|表示)"),
    ),
    (
        "meta_line_description_en",
        re.compile(r"^this line (?:reports|describes|lists|shows|states|explains)\b", re.IGNORECASE),
    ),
    (
        "preservation_instruction_leaked_zh",
        re.compile(r"保留(?:数值|数字|标记|符号)"),
    ),
    (
        "preservation_instruction_leaked_en",
        re.compile(r"\bpreserv(?:e|ed|ing)\s+(?:figures|numbers|markers|tokens)\b", re.IGNORECASE),
    ),
    (
        "generic_page_description_zh",
        re.compile(r"当前页的(?:财务报告|治理|业务信息)"),
    ),
    (
        "generic_page_description_en",
        re.compile(r"\bcurrent page'?s? (?:financial report|governance|business information)\b", re.IGNORECASE),
    ),
]


def normalize_language(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "中文"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english", "英文"}:
        return "en"
    return text or default


def target_text_field(translations: dict[str, Any], target_language: str) -> str:
    explicit = str(translations.get("target_text_field") or "").strip()
    if explicit:
        return explicit
    if target_language == "zh":
        return "translation_zh"
    if target_language == "en":
        return "translation_en"
    return "translation_target_text"


def line_is_translatable(line: dict[str, Any], source_language: str) -> bool:
    return manifest_line_is_translatable(line, source_language)


def infer_source_language(extraction: dict[str, Any]) -> str:
    cjk_count = 0
    latin_count = 0
    for page in extraction.get("pages", []):
        for line in page.get("text_lines", []):
            text = str(line.get("text", ""))
            cjk_count += len(CJK_RE.findall(text))
            latin_count += sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    if cjk_count == 0 and latin_count == 0:
        return "unknown"
    if cjk_count > max(1, latin_count) * 0.5:
        return "zh"
    if latin_count > 0:
        return "en"
    return "unknown"


def source_units(extraction: dict[str, Any], source_language: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for page in extraction.get("pages", []):
        page_index = int(page.get("page_index", 0))
        for line in page.get("text_lines", []):
            if not line_is_translatable(line, source_language):
                continue
            units.append(
                {
                    "unit_id": line["line_id"],
                    "page_index": page_index,
                    "source_text": str(line.get("text", "")),
                    "bbox": line.get("bbox"),
                    "font_size": line.get("font_size"),
                }
            )
    return units


def has_forbidden_text(text: str) -> bool:
    lowered = text.strip().lower()
    return any(token in lowered for token in FORBIDDEN_TRANSLATIONS)


def forbidden_pattern_reasons(text: str) -> list[str]:
    stripped = text.strip()
    return [reason for reason, pattern in FORBIDDEN_TRANSLATION_PATTERNS if pattern.search(stripped)]


def source_preserve_tokens(source_text: str) -> list[dict[str, str]]:
    tokens: list[dict[str, str]] = []
    occupied: list[range] = []
    for token_type, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(source_text):
            span = range(match.start(), match.end())
            if any(match.start() < item.stop and match.end() > item.start for item in occupied):
                continue
            value = match.group(0)
            occupied.append(span)
            tokens.append({"type": token_type, "value": value})
    return tokens


def number_core(value: str) -> str:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    return match.group(0) if match else value


def token_is_present(token: dict[str, str], translation_text: str) -> bool:
    value = token["value"]
    if value in translation_text:
        return True
    if token["type"] == "currency_amount":
        return number_core(value) in translation_text
    return False


def missing_preserve_tokens(source_text: str, translation_text: str) -> list[dict[str, str]]:
    return [token for token in source_preserve_tokens(source_text) if not token_is_present(token, translation_text)]


def validate_target_text(unit_id: str, source_text: str, translation_text: str, target_language: str) -> list[str]:
    reasons: list[str] = []
    if not translation_text:
        reasons.append("empty_translation")
        return reasons
    if target_language == "zh" and not CJK_RE.search(translation_text):
        reasons.append("target_text_no_cjk_characters")
    if target_language == "en":
        if not ASCII_OR_DIGIT_RE.search(translation_text):
            reasons.append("target_text_no_ascii_or_digits")
        if CJK_RE.search(translation_text):
            reasons.append("target_text_has_cjk_residue")
    if translation_text.lower() == source_text.lower():
        reasons.append("translation_equals_source")
    if has_forbidden_text(translation_text):
        reasons.append("placeholder_translation_text")
    reasons.extend(forbidden_pattern_reasons(translation_text))
    missing_tokens = missing_preserve_tokens(source_text, translation_text)
    if missing_tokens:
        reasons.append(
            "preserve_tokens_missing:"
            + ",".join(f"{item['type']}={item['value']}" for item in missing_tokens[:8])
        )
    return reasons


def validate_layout_variants(unit_id: str, translated: dict[str, Any], target_language: str) -> list[str]:
    reasons: list[str] = []
    variants = translated.get("layout_variants")
    if variants is None:
        return reasons
    if not isinstance(variants, dict):
        return ["layout_variants_not_object"]
    for key, value in variants.items():
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"layout_variant_invalid:{key}")
            continue
        if target_language == "zh" and not CJK_RE.search(value):
            reasons.append(f"layout_variant_no_cjk:{key}")
        if target_language == "en" and CJK_RE.search(value):
            reasons.append(f"layout_variant_has_cjk_residue:{key}")
        if has_forbidden_text(value):
            reasons.append(f"layout_variant_placeholder:{key}")
        for reason in forbidden_pattern_reasons(value):
            reasons.append(f"layout_variant_{reason}:{key}")
    return reasons


def validate(extraction_path: Path, translations_path: Path) -> dict[str, Any]:
    extraction = read_json(extraction_path)
    translations = read_json(translations_path)
    source_language = normalize_language(translations.get("source_language"), "en")
    target_language = normalize_language(translations.get("target_language"), "zh")
    translation_field = target_text_field(translations, target_language)
    inferred_source_language = infer_source_language(extraction)
    units = source_units(extraction, source_language)
    translated_units = translations.get("units", [])
    by_id = {item.get("unit_id"): item for item in translated_units if isinstance(item, dict)}
    invalid_units: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    provider = translations.get("translation_provider")
    translation_quality = translations.get("translation_quality")
    semantic_coverage = translations.get("semantic_coverage")

    top_level_errors: list[str] = []
    if provider in FORBIDDEN_PROVIDERS:
        top_level_errors.append("translation_provider_missing_or_placeholder")
    if translation_quality != "semantic_translation":
        top_level_errors.append("translation_quality_must_be_semantic_translation")
    if semantic_coverage != "full_semantic_translation":
        top_level_errors.append("semantic_coverage_must_be_full_semantic_translation")
    if inferred_source_language != "unknown" and inferred_source_language != source_language:
        top_level_errors.append(
            f"source_language_mismatch:declared={source_language};inferred_from_current_extraction={inferred_source_language}"
        )
    if not units:
        top_level_errors.append("no_required_source_units_for_declared_source_language")

    missing_unit_ids = []
    for unit in units:
        unit_id = unit["unit_id"]
        translated = by_id.get(unit_id)
        if translated is None:
            missing_unit_ids.append(unit_id)
            continue
        source_text = unit["source_text"].strip()
        translation_text = str(
            translated.get(translation_field)
            or translated.get("translation_target_text")
            or translated.get("translation_zh")
            or ""
        ).strip()
        reasons = []
        if str(translated.get("source_text", "")).strip() != source_text:
            reasons.append("source_text_mismatch")
        reasons.extend(validate_target_text(unit_id, source_text, translation_text, target_language))
        reasons.extend(validate_layout_variants(unit_id, translated, target_language))
        if reasons:
            invalid_units.append(
                {
                    "unit_id": unit_id,
                    "page_index": unit["page_index"],
                    "source_text": source_text,
                    translation_field: translation_text,
                    "reasons": reasons,
                }
            )

    source_ids = {unit["unit_id"] for unit in units}
    extra_unit_ids = sorted(str(unit_id) for unit_id in by_id if unit_id not in source_ids)
    if extra_unit_ids:
        warnings.append({"warning": "extra_translation_units_ignored", "unit_ids": extra_unit_ids[:50]})

    coverage = {
        "source_unit_count": len(units),
        "translated_unit_count": len(translated_units),
        "matched_unit_count": len(units) - len(missing_unit_ids),
        "missing_unit_ids": missing_unit_ids,
        "invalid_unit_count": len(invalid_units),
    }
    verdict = "PASS" if not top_level_errors and not missing_unit_ids and not invalid_units else "FAIL"
    return {
        "tool": "validate_semantic_translations",
        "translation_validation_verdict": verdict,
        "translation_provider": provider,
        "source_language": source_language,
        "inferred_source_language": inferred_source_language,
        "target_language": target_language,
        "target_text_field": translation_field,
        "translation_quality": translation_quality,
        "semantic_coverage": semantic_coverage,
        "top_level_errors": top_level_errors,
        "coverage": coverage,
        "invalid_units": invalid_units[:200],
        "warnings": warnings,
        "source_extraction": str(extraction_path),
        "translations_json": str(translations_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = validate(resolve_workspace_path(args.source_extraction), resolve_workspace_path(args.translations))
    write_json(Path(args.out), result)
    print(args.out)
    return 0 if result["translation_validation_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
