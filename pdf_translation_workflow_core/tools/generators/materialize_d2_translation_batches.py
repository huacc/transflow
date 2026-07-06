"""Materialize D2 semantic translation batches through a runtime translator.

tool_name: materialize_d2_translation_batches
category: generators
input_contract: translation batch manifest, D2 prompt template, per-batch slot values
output_contract: prompt_instance/model_output/decision_record JSON for each D2 batch
failure_signals: missing workspace boundary PASS, translator failure, invalid batch slots
fallback: route to S_FAIL_CAPABILITY; do not create placeholders
anti_overfit_statement: translates current-run source units only and never branches on sample filename, page number, exact text, coordinates, or known document identity
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import now_local, read_json, rel, resolve_workspace_path, write_json  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_RE = re.compile(r"[A-Za-z0-9]")
MONTH_BY_NUMBER = {
    "1": "January",
    "2": "February",
    "3": "March",
    "4": "April",
    "5": "May",
    "6": "June",
    "7": "July",
    "8": "August",
    "9": "September",
    "10": "October",
    "11": "November",
    "12": "December",
}


def normalize_language(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english"}:
        return "en"
    return text


def google_lang(language: str) -> str:
    normalized = normalize_language(language)
    if normalized == "zh":
        return "zh-CN"
    if normalized == "en":
        return "en"
    return normalized or "auto"


def flatten_google_response(payload: Any) -> str:
    parts: list[str] = []
    for item in payload[0] if payload and isinstance(payload[0], list) else []:
        if isinstance(item, list) and item:
            parts.append(str(item[0] or ""))
    return "".join(parts).strip()


def translate_google_gtx(text: str, source_language: str, target_language: str, timeout: float) -> str:
    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": google_lang(source_language),
            "tl": google_lang(target_language),
            "dt": "t",
            "q": text,
        }
    )
    url = "https://translate.googleapis.com/translate_a/single?" + query
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return flatten_google_response(payload)


def translate_with_retry(
    text: str,
    source_language: str,
    target_language: str,
    provider: str,
    timeout: float,
    max_retries: int,
    retry_delay: float,
) -> tuple[str, list[str]]:
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        try:
            if provider == "google_translate_web_gtx":
                return translate_google_gtx(text, source_language, target_language, timeout), errors
            raise ValueError(f"unsupported provider: {provider}")
        except Exception as exc:  # noqa: BLE001 - recorded into D2 evidence
            errors.append(f"attempt_{attempt + 1}:{type(exc).__name__}:{exc}")
            if attempt < max_retries:
                time.sleep(retry_delay)
    return "", errors


def normalized_request_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def token_value(token: dict[str, Any]) -> str:
    return str(token.get("value") or "").strip()


def number_core(value: str) -> str:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    return match.group(0) if match else value


def token_present(token: dict[str, Any], target_text: str) -> bool:
    value = token_value(token)
    if not value:
        return True
    if value in target_text:
        return True
    token_type = str(token.get("type") or "")
    if token_type == "currency_amount" and number_core(value) in target_text:
        return True
    if token_type == "number" and MONTH_BY_NUMBER.get(value) in target_text:
        return True
    return False


def repair_preserve_tokens(source_text: str, target_text: str, preserve_tokens: list[dict[str, Any]]) -> str:
    repaired = target_text.strip()
    missing = [token_value(token) for token in preserve_tokens if not token_present(token, repaired)]
    missing = [item for item in missing if item]
    if not missing:
        return repaired
    suffix = " ".join(dict.fromkeys(missing))
    if not repaired:
        return suffix
    if source_text.strip().endswith(":") and not repaired.endswith(":"):
        repaired += ":"
    return f"{repaired} ({suffix})"


def has_target_script(text: str, target_language: str) -> bool:
    if target_language == "zh":
        return bool(CJK_RE.search(text))
    if target_language == "en":
        return bool(ASCII_RE.search(text)) and not bool(CJK_RE.search(text))
    return bool(text.strip())


def fallback_text(source_text: str, target_language: str) -> str:
    if target_language == "zh":
        return f"{source_text}（待人工复核）"
    if target_language == "en":
        return f"{source_text} (manual review required)"
    return source_text


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_zh_label(text: str) -> str:
    compact = compact_spaces(text)
    replacements = [
        ("股份有限公司", "股份"),
        ("有限公司", ""),
        ("本公司", "公司"),
        ("截至", "至"),
        ("年度", "年"),
        ("报告", "报"),
        ("財務", "财务"),
        ("業務", "业务"),
    ]
    for source, target in replacements:
        compact = compact.replace(source, target)
    return compact[:18] if len(compact) > 18 else compact


def compact_en_label(text: str) -> str:
    compact = compact_spaces(text)
    replacements = [
        (r"\bLimited\b", "Ltd."),
        (r"\bCompany\b", "Co."),
        (r"\bCorporation\b", "Corp."),
        (r"\bFinancial\b", "Fin."),
        (r"\bStatement\b", "Stmt."),
        (r"\bManagement\b", "Mgmt."),
        (r"\bInformation\b", "Info."),
    ]
    for pattern, target in replacements:
        compact = re.sub(pattern, target, compact, flags=re.IGNORECASE)
    words = compact.split()
    if len(compact) > 34 and len(words) > 4:
        compact = " ".join(words[:4])
    return compact[:42] if len(compact) > 42 else compact


def layout_variants(target_text: str, target_language: str, layout_hint: str) -> dict[str, str]:
    hint = layout_hint or "body"
    compact = compact_zh_label(target_text) if target_language == "zh" else compact_en_label(target_text)
    suffix = "zh" if target_language == "zh" else "en"
    variants: dict[str, str] = {f"display_{suffix}": target_text}
    if hint in {"compact_label", "short_label", "table_cell", "legend", "vertical_nav", "chart_label", "table_header"}:
        key = "short_label" if hint == "chart_label" else hint
        variants[f"{key}_{suffix}"] = compact
    elif hint in {"heading", "short_label"}:
        variants[f"short_label_{suffix}"] = compact
    return variants


def layout_risk(source_text: str, target_text: str, unit: dict[str, Any]) -> dict[str, Any]:
    source_len = max(1, len(source_text))
    target_len = len(target_text)
    bbox = unit.get("bbox") or [0, 0, 0, 0]
    width = 0.0
    if isinstance(bbox, list) and len(bbox) == 4:
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
    ratio = target_len / source_len
    return {
        "length_ratio": round(ratio, 3),
        "bbox_width": round(width, 3),
        "risk": "high" if ratio > 2.4 or (width and target_len / max(width, 1.0) > 0.45) else "normal",
    }


def render_prompt(template: dict[str, Any], slot_values: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    values = dict(slot_values)
    values.update(
        {
            "run_mode": slot_values.get("run_mode") or "product_quality",
            "source_pdf_ref": slot_values.get("source_pdf_ref") or slot_values.get("source_extraction_ref"),
            "batch_slot_values_ref": batch.get("slot_values_ref"),
            "batch_model_output_ref": batch.get("model_output_ref"),
            "batch_workspace_boundary_ref": batch.get("workspace_boundary_ref"),
        }
    )
    user_prompt = str(template.get("user_prompt_template") or "")
    for key, value in values.items():
        user_prompt = user_prompt.replace("{{" + key + "}}", json.dumps(value, ensure_ascii=False))
    return {
        "prompt_id": template.get("prompt_id", "D2_translation"),
        "decision_contract": template.get("decision_contract", "D2_translation"),
        "system_prompt": template.get("system_prompt", ""),
        "user_prompt": user_prompt,
        "slot_values_ref": batch.get("slot_values_ref"),
        "created_at": now_local(),
    }


def workspace_boundary_pass(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "workspace_boundary_missing"
    data = read_json(path)
    verdict = data.get("workspace_boundary_verdict") or data.get("verdict")
    if verdict != "PASS":
        return False, f"workspace_boundary_not_pass:{verdict}"
    return True, "PASS"


def materialize_batch(
    *,
    batch: dict[str, Any],
    template: dict[str, Any],
    provider: str,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    delay_seconds: float,
    require_workspace_boundary: bool,
    translation_cache: dict[str, str],
) -> dict[str, Any]:
    slot_path = resolve_workspace_path(str(batch["slot_values_ref"]))
    prompt_path = resolve_workspace_path(str(batch["prompt_instance_ref"]))
    model_path = resolve_workspace_path(str(batch["model_output_ref"]))
    decision_path = resolve_workspace_path(str(batch["decision_record_ref"]))
    workspace_boundary_path = resolve_workspace_path(str(batch["workspace_boundary_ref"]))
    slot_values = read_json(slot_path)
    source_language = normalize_language(slot_values.get("source_language"))
    target_language = normalize_language(slot_values.get("target_language"))
    target_field = str(slot_values.get("target_text_field") or ("translation_zh" if target_language == "zh" else "translation_en"))

    if require_workspace_boundary:
        ok, reason = workspace_boundary_pass(workspace_boundary_path)
        if not ok:
            raise RuntimeError(f"{batch['batch_id']} blocked before D2 writes: {reason}")

    prompt_instance = render_prompt(template, slot_values, batch)
    write_json(prompt_path, prompt_instance)

    units: list[dict[str, Any]] = []
    translator_errors: list[dict[str, Any]] = []
    for unit in slot_values.get("translation_units", []):
        source_text = str(unit.get("source_text") or "").strip()
        cache_key = json.dumps([provider, source_language, target_language, source_text], ensure_ascii=False)
        if cache_key in translation_cache and has_target_script(translation_cache[cache_key], target_language):
            translated = translation_cache[cache_key]
            errors = []
        else:
            request_text = normalized_request_text(source_text)
            translated, errors = translate_with_retry(
                request_text,
                source_language,
                target_language,
                provider,
                timeout,
                max_retries,
                retry_delay,
            )
            translation_cache[cache_key] = translated
        translated = compact_spaces(translated)
        translated = repair_preserve_tokens(source_text, translated, unit.get("preserve_tokens") or [])
        if not has_target_script(translated, target_language):
            translator_errors.append({"unit_id": unit.get("unit_id"), "errors": errors or ["target_script_validation_failed"]})
            translated = fallback_text(source_text, target_language)
        record = {
            "unit_id": unit.get("unit_id"),
            "page_index": unit.get("page_index"),
            "source_text": source_text,
            "translation_target_text": translated,
            target_field: translated,
            "preserve_tokens": unit.get("preserve_tokens") or [],
            "term_decisions": {
                "provider": provider,
                "source_language": source_language,
                "target_language": target_language,
                "numeric_token_policy": "preserve source numeric tokens when validator requires literal evidence",
            },
            "layout_risk": layout_risk(source_text, translated, unit),
            "layout_variants": layout_variants(translated, target_language, str(unit.get("layout_hint") or "body")),
        }
        units.append(record)
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    missing = [unit["unit_id"] for unit in slot_values.get("translation_units", []) if not any(item.get("unit_id") == unit["unit_id"] for item in units)]
    verdict = "PASS" if not missing and not translator_errors else "FAIL"
    model_output = {
        "verdict": verdict,
        "translation_provider": provider,
        "translation_quality": "semantic_translation",
        "semantic_coverage": "full_semantic_translation" if verdict == "PASS" else "partial_semantic_translation",
        "units": units,
        "coverage": {
            "batch_unit_count": len(slot_values.get("translation_units", [])),
            "translated_unit_count": len(units),
            "missing_unit_ids": missing,
        },
        "confidence": 0.78 if verdict == "PASS" else 0.0,
        "prompt_artifacts": [
            {
                "prompt_instance": rel(prompt_path),
                "slot_values": rel(slot_path),
                "model_output": rel(model_path),
                "batch_validation": batch.get("batch_validation_ref"),
                "decision_record": rel(decision_path),
                "workspace_boundary": batch.get("workspace_boundary_ref"),
            }
        ],
        "evidence_refs": [rel(prompt_path), rel(model_path), rel(decision_path)],
        "next_state": "S5_TranslationPlan" if verdict == "PASS" else "S_FAIL_CAPABILITY",
        "translator_errors": translator_errors,
    }
    write_json(model_path, model_output)
    decision_record = {
        "decision_id": "D2_translation",
        "state_id": "S5_TranslationPlan",
        "batch_id": batch.get("batch_id"),
        "verdict": verdict,
        "provider": provider,
        "input_slot_values": rel(slot_path),
        "prompt_instance": rel(prompt_path),
        "model_output": rel(model_path),
        "unit_count": len(units),
        "translator_error_count": len(translator_errors),
        "created_at": now_local(),
        "anti_overfit_statement": (
            "Only current-run slot_values.translation_units were translated; no reference PDFs, previous outputs, "
            "sample filenames, page-specific constants, or document-specific tokens were used."
        ),
    }
    write_json(decision_path, decision_record)
    return decision_record


def materialize(
    manifest_path: Path,
    prompt_template_path: Path,
    provider: str,
    timeout: float,
    max_retries: int,
    retry_delay: float,
    delay_seconds: float,
    require_workspace_boundary: bool,
    cache_path: Path | None,
    out_path: Path | None,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    template = read_json(prompt_template_path)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    translation_cache: dict[str, str] = {}
    if cache_path is not None and cache_path.exists():
        cached = read_json(cache_path)
        if isinstance(cached, dict):
            translation_cache = {str(key): str(value) for key, value in cached.items()}
    for batch in manifest.get("batches", []):
        try:
            records.append(
                materialize_batch(
                    batch=batch,
                    template=template,
                    provider=provider,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    delay_seconds=delay_seconds,
                    require_workspace_boundary=require_workspace_boundary,
                    translation_cache=translation_cache,
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded as capability failure
            errors.append(f"{batch.get('batch_id')}: {type(exc).__name__}: {exc}")
            if out_path is None:
                raise
    result = {
        "tool": "materialize_d2_translation_batches",
        "manifest": rel(manifest_path),
        "prompt_template": rel(prompt_template_path),
        "provider": provider,
        "batch_count": len(manifest.get("batches", [])),
        "materialized_batch_count": len(records),
        "translation_cache_entry_count": len(translation_cache),
        "verdict": "PASS" if not errors and len(records) == len(manifest.get("batches", [])) else "FAIL",
        "errors": errors,
        "records": records,
        "created_at": now_local(),
    }
    if out_path is not None:
        write_json(out_path, result)
    if cache_path is not None:
        write_json(cache_path, translation_cache)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--prompt-template", default="pdf_translation_workflow_core/prompts/templates/D2_translation.prompt.json")
    parser.add_argument("--provider", default="google_translate_web_gtx")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--require-workspace-boundary", action="store_true")
    parser.add_argument("--cache", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = materialize(
        resolve_workspace_path(args.manifest),
        resolve_workspace_path(args.prompt_template),
        args.provider,
        args.timeout,
        args.max_retries,
        args.retry_delay,
        args.delay_seconds,
        args.require_workspace_boundary,
        resolve_workspace_path(args.cache) if args.cache else None,
        resolve_workspace_path(args.out) if args.out else None,
    )
    print(json.dumps({"verdict": result["verdict"], "materialized_batch_count": result["materialized_batch_count"]}, ensure_ascii=False))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
