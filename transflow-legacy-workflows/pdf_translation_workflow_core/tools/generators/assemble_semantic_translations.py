"""Assemble validated D2 translation batches into semantic translations JSON.

tool_name: assemble_semantic_translations
category: generators
input_contract: translation batch manifest and per-batch D2 model output files
output_contract: docs/input/semantic_translations/<case_id>.translations.json plus assembly evidence
failure_signals: missing batch output, duplicate unit, missing unit, incomplete coverage
fallback: repair failed/missing batches or route to S_FAIL_CAPABILITY
anti_overfit_statement: assembles current-run batch outputs only and never creates translations from sample filename, page number, exact text, coordinates, or known document identity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, write_json  # noqa: E402


def load_batch_output(batch: dict[str, Any], batch_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = []
    if batch.get("model_output_ref"):
        candidates.append(resolve_workspace_path(str(batch["model_output_ref"])))
    batch_id = str(batch["batch_id"])
    candidates.extend(
        [
            batch_dir / f"{batch_id}.model_output.json",
            batch_dir / f"{batch_id}.translations.json",
        ]
    )
    for path in candidates:
        if path.exists():
            return path, read_json(path)
    return None, None


def assemble(manifest_path: Path, out_path: Path, evidence_out: Path | None = None) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    batch_dir = resolve_workspace_path(str(manifest.get("batch_dir") or manifest_path.parent))
    expected_ids: list[str] = []
    for batch in manifest.get("batches", []):
        expected_ids.extend(str(unit_id) for unit_id in batch.get("unit_ids", []))

    providers: set[str] = set()
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_unit_ids: list[str] = []
    missing_batch_outputs: list[str] = []
    batch_evidence: list[dict[str, Any]] = []

    for batch in manifest.get("batches", []):
        output_path, output = load_batch_output(batch, batch_dir)
        if output_path is None or output is None:
            missing_batch_outputs.append(str(batch.get("batch_id")))
            continue
        provider = str(output.get("translation_provider") or "").strip()
        if provider:
            providers.add(provider)
        batch_units = output.get("units", [])
        if not isinstance(batch_units, list):
            batch_units = []
        for unit in batch_units:
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("unit_id"))
            if unit_id in seen:
                duplicate_unit_ids.append(unit_id)
                continue
            seen.add(unit_id)
            units.append(unit)
        batch_evidence.append(
            {
                "batch_id": batch.get("batch_id"),
                "slot_values": batch.get("slot_values_ref"),
                "prompt_instance": batch.get("prompt_instance_ref"),
                "model_output": rel(output_path),
                "batch_validation": batch.get("batch_validation_ref"),
                "decision_record": batch.get("decision_record_ref"),
                "unit_count": len(batch_units),
            }
        )

    expected_set = set(expected_ids)
    missing_unit_ids = [unit_id for unit_id in expected_ids if unit_id not in seen]
    extra_unit_ids = sorted(unit_id for unit_id in seen if unit_id not in expected_set)
    complete = not missing_batch_outputs and not missing_unit_ids and not duplicate_unit_ids
    provider = "+".join(sorted(providers)) if providers else "missing_translation_provider"
    coverage = {
        "source_unit_count": len(expected_ids),
        "translated_unit_count": len(units),
        "matched_unit_count": len(expected_ids) - len(missing_unit_ids),
        "missing_unit_ids": missing_unit_ids,
        "extra_unit_ids": extra_unit_ids,
        "duplicate_unit_ids": sorted(set(duplicate_unit_ids)),
        "missing_batch_outputs": missing_batch_outputs,
    }
    result = {
        "translation_provider": provider,
        "source_language": manifest.get("source_language"),
        "target_language": manifest.get("target_language"),
        "target_text_field": manifest.get("target_text_field"),
        "translation_quality": "semantic_translation" if complete else "partial_semantic_translation",
        "semantic_coverage": "full_semantic_translation" if complete else "partial_semantic_translation",
        "source_extraction_ref": manifest.get("source_extraction_ref"),
        "translation_batch_manifest_ref": rel(manifest_path),
        "prompt_artifacts": batch_evidence,
        "coverage": coverage,
        "units": units,
    }
    write_json(out_path, result)
    evidence = {
        "tool": "assemble_semantic_translations",
        "assembly_verdict": "PASS" if complete else "FAIL",
        "manifest": rel(manifest_path),
        "translations_json": rel(out_path),
        "coverage": coverage,
        "batch_evidence": batch_evidence,
    }
    if evidence_out is not None:
        write_json(evidence_out, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--evidence-out", default="")
    args = parser.parse_args()
    evidence = assemble(
        resolve_workspace_path(args.manifest),
        resolve_workspace_path(args.out),
        resolve_workspace_path(args.evidence_out) if args.evidence_out else None,
    )
    print(resolve_workspace_path(args.out))
    return 0 if evidence["assembly_verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
