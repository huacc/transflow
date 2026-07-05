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


CJK_RE = re.compile(r"[\u3400-\u9fff]")
TOKEN_PATTERNS = [
    ("percentage", re.compile(r"\(?\d+(?:\.\d+)?%\)?")),
    ("currency_amount", re.compile(r"\b(?:US\$|HK\$|RMB|USD|HKD)?\d[\d,]*(?:\.\d+)?\s*(?:million|billion|trillion)?\b", re.IGNORECASE)),
    ("footnote_marker", re.compile(r"\(\d+\)")),
    ("year", re.compile(r"\b(?:19|20)\d{2}\b")),
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


def source_units(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for page in extraction.get("pages", []):
        page_index = int(page.get("page_index", 0))
        for line in page.get("text_lines", []):
            if not line.get("ascii_tokens"):
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


def token_is_present(token: dict[str, str], translation_zh: str) -> bool:
    value = token["value"]
    if value in translation_zh:
        return True
    if token["type"] == "currency_amount":
        return number_core(value) in translation_zh
    return False


def missing_preserve_tokens(source_text: str, translation_zh: str) -> list[dict[str, str]]:
    return [token for token in source_preserve_tokens(source_text) if not token_is_present(token, translation_zh)]


def validate_layout_variants(unit_id: str, translated: dict[str, Any]) -> list[str]:
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
        if not CJK_RE.search(value):
            reasons.append(f"layout_variant_no_cjk:{key}")
        if has_forbidden_text(value):
            reasons.append(f"layout_variant_placeholder:{key}")
    return reasons


def validate(extraction_path: Path, translations_path: Path) -> dict[str, Any]:
    extraction = read_json(extraction_path)
    translations = read_json(translations_path)
    units = source_units(extraction)
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

    missing_unit_ids = []
    for unit in units:
        unit_id = unit["unit_id"]
        translated = by_id.get(unit_id)
        if translated is None:
            missing_unit_ids.append(unit_id)
            continue
        source_text = unit["source_text"].strip()
        translation_zh = str(translated.get("translation_zh", "")).strip()
        reasons = []
        if str(translated.get("source_text", "")).strip() != source_text:
            reasons.append("source_text_mismatch")
        if not translation_zh:
            reasons.append("empty_translation")
        if not CJK_RE.search(translation_zh):
            reasons.append("no_cjk_characters")
        if translation_zh.lower() == source_text.lower():
            reasons.append("translation_equals_source")
        if has_forbidden_text(translation_zh):
            reasons.append("placeholder_translation_text")
        missing_tokens = missing_preserve_tokens(source_text, translation_zh)
        if missing_tokens:
            reasons.append(
                "preserve_tokens_missing:"
                + ",".join(f"{item['type']}={item['value']}" for item in missing_tokens[:8])
            )
        reasons.extend(validate_layout_variants(unit_id, translated))
        if reasons:
            invalid_units.append(
                {
                    "unit_id": unit_id,
                    "page_index": unit["page_index"],
                    "source_text": source_text,
                    "translation_zh": translation_zh,
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
