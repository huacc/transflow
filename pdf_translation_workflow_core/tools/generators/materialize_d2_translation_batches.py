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
UNIT_MARKER_RE = re.compile(r"<<<UNIT_(\d{6})>>>")
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


def unit_marker(index: int) -> str:
    return f"<<<UNIT_{index:06d}>>>"


def split_marker_translation(text: str, expected_indexes: list[int]) -> dict[int, str]:
    matches = list(UNIT_MARKER_RE.finditer(text))
    result: dict[int, str] = {}
    for pos, match in enumerate(matches):
        index = int(match.group(1))
        start = match.end()
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
        result[index] = text[start:end].strip()
    return {index: result[index] for index in expected_indexes if index in result}


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


def is_neutral_identifier_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if CJK_RE.search(stripped):
        return False
    if re.fullmatch(r"https?://\S+|www\.\S+|\S+@\S+|[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/\S*)?", stripped, re.IGNORECASE):
        return True
    if not re.search(r"[A-Za-z]", stripped) and re.fullmatch(r"[\d\s,.;:()/%$€£¥+\-–—†*]+", stripped):
        return True
    if re.fullmatch(r"[A-Za-z]", stripped):
        return True
    if re.fullmatch(r"[A-Z]", stripped):
        return True
    if re.fullmatch(r"[A-Z]{2,4}", stripped):
        return True
    if re.fullmatch(r"[a-z]\.[a-z]\.", stripped):
        return True
    superscripts = "¹²³⁴⁵⁶⁷⁸⁹⁰"
    if len(stripped) <= 12 and re.fullmatch(rf"[A-Za-z+*]+[{superscripts}]*", stripped):
        upper_count = sum(1 for char in stripped if "A" <= char <= "Z")
        lower_count = sum(1 for char in stripped if "a" <= char <= "z")
        if upper_count >= 2 and lower_count <= 2:
            return True
    if re.fullmatch(rf"[A-Z]{{2,8}}\s+[IVX]{{1,6}}[{superscripts}]*", stripped):
        return True
    if re.fullmatch(r"[A-Z]{1,6}[\dA-Z.:-]{2,}", stripped):
        return True
    if re.fullmatch(r"[A-Z0-9]+[/.-][A-Z0-9/-]+(?:\s+[A-Z])?", stripped):
        return True
    if re.fullmatch(r"\d+[A-Z.:-]+[A-Z\d.:-]*", stripped):
        return True
    if re.fullmatch(r"[A-D]{1,3}[+-]?", stripped):
        return True
    if re.fullmatch(r"[A-Z]{2,8}(?:\s+[A-Z]{2,8}){1,5}", stripped):
        return True
    if re.fullmatch(r"\)?(?:[A-Z]{2,8}:\s*[\d,().-]+;?\s*)+\)?\.?", stripped):
        return True
    if re.fullmatch(r"<?\d+(?:\.\d+)?(?:bps|bp|pps|x|%)\.?", stripped, re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Za-z]{1,8}\s+\d+(?:\.\d+)?(?:bps|bp|pps|x|%)\.?", stripped, re.IGNORECASE):
        return True
    short_marker_re = re.compile(r"[\(\)\[\]'\"\u2018\u2019\u201c\u201d\u00f8]")
    if len(stripped) <= 40 and short_marker_re.search(stripped) and re.fullmatch(r"[\(\)\[\]'\"\u2018\u2019\u201c\u201dA-Za-z0-9.\s/\\-]+\u00f8?", stripped):
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", stripped)
        if len(words) <= 2:
            return True
    if len(stripped) <= 32 and re.fullmatch(r"[A-Za-z0-9().,%/°˚℃$€£¥\s:'’+\\-–—†*]+", stripped):
        letters = re.findall(r"[A-Za-z]", stripped)
        lower = re.findall(r"[a-z]", stripped)
        if letters and len(lower) <= max(2, len(letters) // 2) and re.search(r"[%\d()/:°˚℃†]", stripped):
            return True
        if re.fullmatch(r"[A-Za-z]+\s+[A-Z][.]?[A-Z.]?[.]?", stripped):
            return True
    legal_suffix_re = re.compile(
        r"\b(Inc\.?|Ltd\.?|Limited|LLC|LLP|L\.?P\.?|PLC|Corp\.?|Corporation|"
        r"Company|Trust|Fund|Fonds|HoldCo|LendCo|Feeder|GP|SICAV|SCSp|SNC|"
        r"GmbH|AG|B\.?V\.?|N\.?V\.?|S\.?A\.?|S\.?C\.?A\.?|r\.?\s*l\.?)\b",
        re.IGNORECASE,
    )
    if len(stripped) <= 96 and legal_suffix_re.search(stripped):
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", stripped)
        allowed_lower = {"a", "an", "and", "de", "del", "du", "of", "the", "r", "l"}
        content_words = [word for word in words if word.lower() not in allowed_lower]
        title_like = sum(1 for word in content_words if word[:1].isupper() or word.isupper())
        if content_words and title_like >= max(1, len(content_words) - 1):
            return True
    address_keyword_re = re.compile(
        r"\b(Building|Street|Road|Suite|Floor|Unit|Avenue|Drive|Place|Plaza|"
        r"Esplanade|Hamilton|Wilmington|Corporate\s+Services|Trust\s+Company)\b",
        re.IGNORECASE,
    )
    if len(stripped) <= 120 and "," in stripped and re.search(r"\d", stripped) and address_keyword_re.search(stripped):
        return True
    return False


def is_neutral_metric_text(text: str) -> bool:
    stripped = compact_spaces(text)
    if not stripped or CJK_RE.search(stripped) or not re.search(r"\d", stripped):
        return False
    metric_unit_re = re.compile(
        r"\b(per\s+cent|percent|percentage\s+points?|pps?|basis\s+points?|bps?|bp|"
        r"million|billion|trillion|thousand|dollars?|yuan|renminbi|months?|years?)\b",
        re.IGNORECASE,
    )
    metric_symbol_re = re.compile(r"[%$€£¥]|US\$|HK\$|RMB|CNY|HKD|USD", re.IGNORECASE)
    if not (metric_unit_re.search(stripped) or metric_symbol_re.search(stripped)):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9\s,.;:()/%$€£¥+\\\-–—']+", stripped))


def normalize_metric_phrase_for_zh(text: str) -> str:
    normalized = compact_spaces(text)
    normalized = normalized.replace("。", ".").replace("，", ",").replace("：", ":")
    if not is_neutral_metric_text(normalized):
        return normalized
    replacements = [
        (r"\bper\s+cent\b", "%"),
        (r"\bpercent\b", "%"),
        (r"\bpercentage\s+points?\b", "个百分点"),
        (r"\bpps?\b", "个百分点"),
        (r"\bbasis\s+points?\b", "个基点"),
        (r"\bbps?\b", "个基点"),
        (r"\btrillion\b", "万亿"),
        (r"\bbillion\b", "十亿"),
        (r"\bmillion\b", "百万"),
        (r"\bthousand\b", "千"),
        (r"\bdollars?\b", "美元"),
        (r"\byuan\b", "元"),
        (r"\brenminbi\b", "人民币"),
        (r"\bmonths?\b", "个月"),
        (r"\byears?\b", "年"),
    ]
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+%", "%", normalized)
    normalized = re.sub(r"([0-9])\s+([\u3400-\u9fff%])", r"\1\2", normalized)
    normalized = re.sub(r"([\u3400-\u9fff%])\.$", r"\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def zh_neutral_metric_target_acceptable(text: str) -> bool:
    stripped = compact_spaces(text)
    if not stripped or not re.search(r"\d", stripped):
        return False
    if CJK_RE.search(stripped):
        return True
    if re.search(r"\b(per\s+cent|percent|percentage|basis|points?|million|billion|trillion|thousand|dollars?|yuan|renminbi|months?|years?)\b", stripped, re.IGNORECASE):
        return False
    return bool(re.fullmatch(r"[A-Z]{0,4}[$]?\s*[\d\s,.;:()/%$€£¥+\\\-–—]+", stripped))


def target_text_acceptable(text: str, target_language: str, source_text: str) -> bool:
    if has_target_script(text, target_language):
        return True
    if target_language == "zh" and is_neutral_identifier_text(source_text) and text.strip():
        return True
    if target_language == "zh" and is_neutral_metric_text(source_text) and zh_neutral_metric_target_acceptable(text):
        return True
    return False


def latin_dominant_mixed_identifier(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text))
    cjk = len(CJK_RE.findall(text))
    if cjk == 0:
        return False
    if latin < max(8, cjk * 4):
        return False
    return bool(re.search(r"\b(?:Ltd\.?|Limited|LLC|LLP|PLC|Corp\.?|Corporation|Company|Holdings?|Bank|Bhd|plc)\b", text, re.IGNORECASE))


def clean_english_cjk_residue(source_text: str, target_text: str) -> str:
    if not latin_dominant_mixed_identifier(source_text) and not latin_dominant_mixed_identifier(target_text):
        return target_text
    if not CJK_RE.search(target_text):
        return target_text
    cleaned = re.sub(r"[\(（][^\(\)（）]*[\u3400-\u9fff][^\(\)（）]*$", "", target_text).strip()
    cleaned = re.sub(r"[\u3400-\u9fff]+", "", cleaned)
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    cleaned = re.sub(r"[\s(（]+$", "", cleaned).strip()
    return cleaned or target_text


def normalize_translated_text(source_text: str, target_text: str, target_language: str) -> str:
    compact = compact_spaces(target_text)
    if target_language == "zh" and not CJK_RE.search(compact):
        compact = normalize_metric_phrase_for_zh(compact)
    if target_language == "en":
        compact = clean_english_cjk_residue(source_text, compact)
    return compact


def fallback_text(source_text: str, target_language: str) -> str:
    if target_language == "zh":
        return f"{source_text}（待人工复核）"
    if target_language == "en":
        return f"{source_text} (manual review required)"
    return source_text


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_zh_label(text: str) -> str:
    compact = re.sub(r"\s+", "", text).strip()
    if len(compact) <= 18:
        return compact
    parts = [part for part in re.split(r"[，,；;：:、/\\|\n]+", compact) if part]
    first = parts[0] if parts else compact
    return first[:18] if len(first) >= 6 else compact[:18]


def compact_en_label(text: str) -> str:
    compact = compact_spaces(text)
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


def chunk_units(
    items: list[tuple[int, dict[str, Any], str]],
    max_units: int,
    max_chars: int,
) -> list[list[tuple[int, dict[str, Any], str]]]:
    chunks: list[list[tuple[int, dict[str, Any], str]]] = []
    current: list[tuple[int, dict[str, Any], str]] = []
    current_chars = 0
    max_units = max(1, max_units)
    max_chars = max(200, max_chars)
    for item in items:
        marker_chars = len(unit_marker(item[0])) + 2
        item_chars = len(item[2]) + marker_chars
        if current and (len(current) >= max_units or current_chars + item_chars > max_chars):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        chunks.append(current)
    return chunks


def build_chunk_request(chunk: list[tuple[int, dict[str, Any], str]]) -> str:
    parts: list[str] = []
    for index, _unit, source_text in chunk:
        parts.append(unit_marker(index))
        parts.append(source_text)
    return "\n".join(parts)


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
        "normalization_policy": template.get("normalization_policy", ""),
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
    chunk_units_limit: int,
    chunk_chars_limit: int,
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

    translator_errors: list[dict[str, Any]] = []
    translation_units = list(slot_values.get("translation_units", []))
    pending: list[tuple[int, dict[str, Any], str]] = []
    translated_by_index: dict[int, str] = {}

    for index, unit in enumerate(translation_units, start=1):
        source_text = str(unit.get("source_text") or "").strip()
        cache_key = json.dumps([provider, source_language, target_language, source_text], ensure_ascii=False)
        cached = normalize_translated_text(source_text, translation_cache.get(cache_key, ""), target_language)
        if cache_key in translation_cache and target_text_acceptable(cached, target_language, source_text):
            translated_by_index[index] = cached
        else:
            pending.append((index, unit, normalized_request_text(source_text)))

    chunk_records: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunk_units(pending, chunk_units_limit, chunk_chars_limit), start=1):
        request_text = build_chunk_request(chunk)
        translated_chunk, chunk_errors = translate_with_retry(
            request_text,
            source_language,
            target_language,
            provider,
            timeout,
            max_retries,
            retry_delay,
        )
        expected_indexes = [item[0] for item in chunk]
        split = split_marker_translation(translated_chunk, expected_indexes)
        fallback_count = 0
        for index, unit, source_text in chunk:
            source_raw = str(unit.get("source_text") or "").strip()
            cache_key = json.dumps([provider, source_language, target_language, source_raw], ensure_ascii=False)
            translated = split.get(index, "")
            normalized_candidate = normalize_translated_text(source_raw, translated, target_language)
            if not translated or not target_text_acceptable(normalized_candidate, target_language, source_raw):
                fallback_count += 1
                translated, unit_errors = translate_with_retry(
                    source_text,
                    source_language,
                    target_language,
                    provider,
                    timeout,
                    max_retries,
                    retry_delay,
                )
            translated = normalize_translated_text(source_raw, translated, target_language)
            translation_cache[cache_key] = translated
            translated_by_index[index] = translated
        chunk_records.append(
            {
                "chunk_index": chunk_index,
                "unit_count": len(chunk),
                "request_char_count": len(request_text),
                "translated_char_count": len(translated_chunk),
                "marker_split_count": len(split),
                "fallback_unit_count": fallback_count,
                "error_count": len(chunk_errors),
            }
        )
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    units: list[dict[str, Any]] = []
    for index, unit in enumerate(translation_units, start=1):
        source_text = str(unit.get("source_text") or "").strip()
        translated = normalize_translated_text(source_text, translated_by_index.get(index, ""), target_language)
        if not target_text_acceptable(translated, target_language, source_text):
            translator_errors.append({"unit_id": unit.get("unit_id"), "errors": ["target_script_validation_failed"]})
            translated = fallback_text(source_text, target_language)
        translated = repair_preserve_tokens(source_text, translated, unit.get("preserve_tokens") or [])
        if not target_text_acceptable(translated, target_language, source_text):
            translator_errors.append({"unit_id": unit.get("unit_id"), "errors": ["target_script_validation_failed_after_token_repair"]})
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
        "chunk_translation": {
            "enabled": True,
            "chunk_units_limit": chunk_units_limit,
            "chunk_chars_limit": chunk_chars_limit,
            "chunk_count": len(chunk_records),
            "fallback_unit_count": sum(item["fallback_unit_count"] for item in chunk_records),
            "chunks": chunk_records,
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
    chunk_units_limit: int,
    chunk_chars_limit: int,
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
                    chunk_units_limit=chunk_units_limit,
                    chunk_chars_limit=chunk_chars_limit,
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
        "failed_batch_count": sum(1 for record in records if record.get("verdict") != "PASS"),
        "verdict": (
            "PASS"
            if not errors
            and len(records) == len(manifest.get("batches", []))
            and all(record.get("verdict") == "PASS" for record in records)
            else "FAIL"
        ),
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
    parser.add_argument("--chunk-units", type=int, default=20)
    parser.add_argument("--chunk-chars", type=int, default=3500)
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
        args.chunk_units,
        args.chunk_chars,
        args.require_workspace_boundary,
        resolve_workspace_path(args.cache) if args.cache else None,
        resolve_workspace_path(args.out) if args.out else None,
    )
    print(json.dumps({"verdict": result["verdict"], "materialized_batch_count": result["materialized_batch_count"]}, ensure_ascii=False))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
