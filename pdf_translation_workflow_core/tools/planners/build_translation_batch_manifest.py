"""Build D2 translation batch slots from source extraction evidence.

tool_name: build_translation_batch_manifest
category: planners
input_contract: source extraction JSON, case/language metadata, batch size
output_contract: translation batch manifest plus per-batch slot_values JSON files
failure_signals: missing extraction, empty source units, invalid language metadata
fallback: route to S_FAIL_CAPABILITY or Ax_AdaptiveChange; do not generate placeholders
anti_overfit_statement: batches current-run extracted units only and never branches on sample filename, page number, exact text, coordinates, or known document identity
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
TOKEN_PATTERNS = [
    ("footnote_marker", re.compile(r"\(\d+\)")),
    ("year", re.compile(r"\b(?:19|20)\d{2}\b")),
    ("percentage", re.compile(r"\(?\d+(?:\.\d+)?%\)?")),
    (
        "currency_amount",
        re.compile(
            r"\b(?:US\$|HK\$|RMB|USD|HKD)?\d[\d,]*(?:\.\d+)?\s*(?:million|billion|trillion)?\b",
            re.IGNORECASE,
        ),
    ),
    ("number", re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")),
]


def normalize_language(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "中文"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english", "英文"}:
        return "en"
    return text


def line_is_translatable(line: dict[str, Any], source_language: str) -> bool:
    text = str(line.get("text", ""))
    if source_language == "zh":
        return bool(CJK_RE.search(text))
    if source_language == "en":
        return bool(line.get("ascii_tokens"))
    return bool(text.strip())


def preserve_tokens(source_text: str) -> list[dict[str, str]]:
    tokens: list[dict[str, str]] = []
    occupied: list[range] = []
    for token_type, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(source_text):
            span = range(match.start(), match.end())
            if any(match.start() < item.stop and match.end() > item.start for item in occupied):
                continue
            occupied.append(span)
            tokens.append({"type": token_type, "value": match.group(0)})
    return tokens


def infer_layout_hint(line: dict[str, Any]) -> str:
    text = str(line.get("text", "")).strip()
    font_size = float(line.get("font_size") or 0)
    ascii_token_count = len(line.get("ascii_tokens") or [])
    cjk_count = int(line.get("cjk_char_count") or 0)
    visible_len = len(text)
    if font_size >= 14:
        return "heading"
    if visible_len <= 24 and (ascii_token_count <= 4 or cjk_count <= 12):
        return "short_label"
    return "body"


def source_units(extraction: dict[str, Any], source_language: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for page in extraction.get("pages", []):
        page_index = int(page.get("page_index", 0))
        page_rect = page.get("rect")
        for line in page.get("text_lines", []):
            if not line_is_translatable(line, source_language):
                continue
            source_text = str(line.get("text", "")).strip()
            units.append(
                {
                    "unit_id": str(line["line_id"]),
                    "page_index": page_index,
                    "block_id": line.get("block_id"),
                    "line_index": line.get("line_index"),
                    "source_text": source_text,
                    "bbox": line.get("bbox"),
                    "page_rect": page_rect,
                    "font_size": line.get("font_size"),
                    "font": line.get("font"),
                    "ascii_tokens": line.get("ascii_tokens") or [],
                    "cjk_char_count": line.get("cjk_char_count") or 0,
                    "preserve_tokens": preserve_tokens(source_text),
                    "layout_hint": infer_layout_hint(line),
                }
            )
    return units


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_manifest(
    *,
    source_extraction: Path,
    case_id: str,
    source_language: str,
    target_language: str,
    target_text_field: str,
    batch_dir: Path,
    out_path: Path,
    max_units: int,
    run_id: str,
) -> dict[str, Any]:
    if not case_id:
        raise ValueError("case_id is required")
    if not source_language or not target_language:
        raise ValueError("source_language and target_language are required")
    if max_units < 1:
        raise ValueError("max_units must be >= 1")

    extraction = read_json(source_extraction)
    units = source_units(extraction, source_language)
    if not units:
        raise ValueError("no translatable source units found for declared source_language")

    batches = []
    batch_dir.mkdir(parents=True, exist_ok=True)
    for index, batch_units in enumerate(chunked(units, max_units), start=1):
        batch_id = f"batch_{index:04d}"
        slot_path = batch_dir / f"{batch_id}.slot_values.json"
        prompt_instance_path = batch_dir / f"{batch_id}.prompt_instance.json"
        model_output_path = batch_dir / f"{batch_id}.model_output.json"
        validation_path = batch_dir / f"{batch_id}.validation.json"
        decision_record_path = batch_dir / f"{batch_id}.decision_record.json"
        workspace_boundary_path = batch_dir / f"{batch_id}.workspace_boundary.json"
        slot_values = {
            "run_id": run_id,
            "case_id": case_id,
            "state_id": "S5_TranslationPlan",
            "decision_id": "D2_translation",
            "batch_id": batch_id,
            "batch_index": index,
            "batch_count": None,
            "source_extraction_ref": rel(source_extraction),
            "source_language": source_language,
            "target_language": target_language,
            "target_text_field": target_text_field,
            "translation_units": batch_units,
            "terminology_policy": {
                "source": "current-run source units only",
                "known_reference_translation_allowed": False,
            },
            "output_contract": {
                "workspace_boundary_ref": rel(workspace_boundary_path),
                "write_model_output_to": rel(model_output_path),
                "required_units": [unit["unit_id"] for unit in batch_units],
                "forbidden_placeholders": [
                    "中文回填",
                    "中文标题",
                    "中文标签",
                    "placeholder",
                    "TBD",
                ],
            },
        }
        write_json(slot_path, slot_values)
        batches.append(
            {
                "batch_id": batch_id,
                "batch_index": index,
                "unit_count": len(batch_units),
                "unit_ids": [unit["unit_id"] for unit in batch_units],
                "slot_values_ref": rel(slot_path),
                "prompt_instance_ref": rel(prompt_instance_path),
                "model_output_ref": rel(model_output_path),
                "batch_validation_ref": rel(validation_path),
                "decision_record_ref": rel(decision_record_path),
                "workspace_boundary_ref": rel(workspace_boundary_path),
            }
        )

    for batch in batches:
        slot_path = resolve_workspace_path(batch["slot_values_ref"])
        slot_values = read_json(slot_path)
        slot_values["batch_count"] = len(batches)
        write_json(slot_path, slot_values)

    manifest = {
        "tool": "build_translation_batch_manifest",
        "contract_version": "2026-07-06",
        "case_id": case_id,
        "run_id": run_id,
        "source_extraction_ref": rel(source_extraction),
        "source_extraction_sha256": sha256_file(source_extraction),
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": target_text_field,
        "batch_dir": rel(batch_dir),
        "max_units_per_batch": max_units,
        "source_unit_count": len(units),
        "batch_count": len(batches),
        "batches": batches,
        "required_d2_loop": [
            "For each batch, run validate_workspace_boundary.py for workspace_boundary_ref before writing prompt/model/decision/validation artifacts.",
            "For each batch, fill D2_translation.prompt.json slots from slot_values_ref.",
            "Persist prompt_instance_ref before judgement.",
            "Persist raw model JSON to model_output_ref.",
            "Run validate_translation_batch.py for that batch.",
            "After all batch validations pass, run assemble_semantic_translations.py.",
            "Run validate_semantic_translations.py on the assembled semantic translation JSON.",
        ],
        "anti_overfit_statement": (
            "Manifest contains only current-run extracted text units and generic language metadata; "
            "it must not be enriched from official bilingual references or previous round outputs."
        ),
    }
    write_json(out_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source-language", required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--target-text-field", required=True)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--max-units", type=int, default=40)
    args = parser.parse_args()

    source_language = normalize_language(args.source_language)
    target_language = normalize_language(args.target_language)
    run_id = args.run_id or args.case_id
    manifest = build_manifest(
        source_extraction=resolve_workspace_path(args.source_extraction),
        case_id=args.case_id,
        source_language=source_language,
        target_language=target_language,
        target_text_field=args.target_text_field,
        batch_dir=resolve_workspace_path(args.batch_dir),
        out_path=resolve_workspace_path(args.out),
        max_units=args.max_units,
        run_id=run_id,
    )
    print(resolve_workspace_path(args.out))
    print(f"source_unit_count={manifest['source_unit_count']} batch_count={manifest['batch_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
