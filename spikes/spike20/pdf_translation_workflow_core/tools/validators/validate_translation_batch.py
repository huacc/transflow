"""Validate one D2 translation batch output before assembly.

tool_name: validate_translation_batch
category: validators
input_contract: batch slot_values JSON and D2 batch model_output JSON
output_contract: JSON verdict with missing/invalid/extra unit evidence
failure_signals: missing units, placeholder text, pseudo translation, token preservation failure
fallback: repair only the failed batch or route to S_FAIL_CAPABILITY
anti_overfit_statement: validates only current batch slot values and never branches on sample filename, page number, exact text, coordinates, or known document identity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import read_json, resolve_workspace_path, write_json  # noqa: E402
from validate_semantic_translations import (  # noqa: E402
    FORBIDDEN_PROVIDERS,
    normalize_language,
    target_text_field,
    validate_layout_variants,
    validate_target_text,
)


def expected_units(slot_values: dict[str, Any]) -> list[dict[str, Any]]:
    units = slot_values.get("translation_units")
    if not isinstance(units, list) or not units:
        raise ValueError("slot_values.translation_units must be a non-empty array")
    return [unit for unit in units if isinstance(unit, dict)]


def translated_text(translated: dict[str, Any], field: str) -> str:
    return str(
        translated.get(field)
        or translated.get("translation_target_text")
        or translated.get("translation_zh")
        or translated.get("translation_en")
        or ""
    ).strip()


def validate_batch(slot_values_path: Path, model_output_path: Path) -> dict[str, Any]:
    slot_values = read_json(slot_values_path)
    model_output = read_json(model_output_path)
    source_language = normalize_language(slot_values.get("source_language"), "en")
    target_language = normalize_language(slot_values.get("target_language"), "zh")
    field = str(slot_values.get("target_text_field") or target_text_field(model_output, target_language))
    expected = expected_units(slot_values)
    output_units = model_output.get("units", [])
    if not isinstance(output_units, list):
        output_units = []

    provider = model_output.get("translation_provider")
    quality = model_output.get("translation_quality")
    semantic_coverage = model_output.get("semantic_coverage")
    top_level_errors: list[str] = []
    if provider in FORBIDDEN_PROVIDERS:
        top_level_errors.append("translation_provider_missing_or_placeholder")
    if quality != "semantic_translation":
        top_level_errors.append("translation_quality_must_be_semantic_translation")
    if semantic_coverage != "full_semantic_translation":
        top_level_errors.append("semantic_coverage_must_be_full_semantic_translation")

    by_id = {item.get("unit_id"): item for item in output_units if isinstance(item, dict)}
    invalid_units: list[dict[str, Any]] = []
    missing_unit_ids: list[str] = []
    for unit in expected:
        unit_id = str(unit["unit_id"])
        translated = by_id.get(unit_id)
        if translated is None:
            missing_unit_ids.append(unit_id)
            continue
        source_text = str(unit.get("source_text", "")).strip()
        target_text = translated_text(translated, field)
        reasons: list[str] = []
        if str(translated.get("source_text", "")).strip() != source_text:
            reasons.append("source_text_mismatch")
        reasons.extend(validate_target_text(unit_id, source_text, target_text, target_language))
        reasons.extend(validate_layout_variants(unit_id, translated, target_language))
        if reasons:
            invalid_units.append(
                {
                    "unit_id": unit_id,
                    "source_text": source_text,
                    field: target_text,
                    "reasons": reasons,
                }
            )

    expected_ids = {str(unit["unit_id"]) for unit in expected}
    extra_unit_ids = sorted(str(unit_id) for unit_id in by_id if unit_id not in expected_ids)
    coverage = {
        "batch_unit_count": len(expected),
        "translated_unit_count": len(output_units),
        "matched_unit_count": len(expected) - len(missing_unit_ids),
        "missing_unit_ids": missing_unit_ids,
        "extra_unit_ids": extra_unit_ids,
        "invalid_unit_count": len(invalid_units),
    }
    verdict = "PASS" if not top_level_errors and not missing_unit_ids and not invalid_units else "FAIL"
    return {
        "tool": "validate_translation_batch",
        "translation_batch_validation_verdict": verdict,
        "batch_id": slot_values.get("batch_id"),
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": field,
        "translation_provider": provider,
        "translation_quality": quality,
        "semantic_coverage": semantic_coverage,
        "top_level_errors": top_level_errors,
        "coverage": coverage,
        "invalid_units": invalid_units[:200],
        "slot_values": str(slot_values_path),
        "model_output": str(model_output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot-values", required=True)
    parser.add_argument("--model-output", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = validate_batch(
        resolve_workspace_path(args.slot_values),
        resolve_workspace_path(args.model_output),
    )
    write_json(resolve_workspace_path(args.out), result)
    print(resolve_workspace_path(args.out))
    return 0 if result["translation_batch_validation_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
