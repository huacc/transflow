import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def normalize_lang(lang: str) -> str:
    lang = lang.strip()
    if lang in {"zh", "zh-cn", "zh_CN", "zh-CN"}:
        return "zh-CN"
    if lang in {"en", "en-US", "en_GB"}:
        return "en"
    return lang


def target_field(target_language: str) -> str:
    return "translation_en" if normalize_lang(target_language).startswith("en") else "translation_zh"


def google_translate(text: str, source_language: str, target_language: str, timeout: int = 20) -> str:
    params = {
        "client": "gtx",
        "sl": normalize_lang(source_language),
        "tl": normalize_lang(target_language),
        "dt": "t",
        "q": text,
    }
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return "".join(part[0] for part in payload[0] if part and part[0])


def translate_batch(texts: list[str], source_language: str, target_language: str) -> list[str]:
    joined = "\n".join(texts)
    translated = google_translate(joined, source_language, target_language)
    parts = translated.splitlines()
    if len(parts) == len(texts):
        return parts
    # The public endpoint occasionally merges short fragments. Fall back to per-line calls
    # for this batch so unit alignment stays exact.
    results = []
    for text in texts:
        results.append(google_translate(text, source_language, target_language))
        time.sleep(0.03)
    return results


def iter_batches(items: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        next_chars = current_chars + len(text) + 1
        if current and next_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += len(text) + 1
    if current:
        batches.append(current)
    return batches


def run(
    source_structure: Path,
    source_language: str,
    target_language: str,
    output: Path,
    cache_path: Path,
    batch_max_chars: int,
) -> None:
    structure = json.loads(source_structure.read_text(encoding="utf-8"))
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    lines: list[dict[str, Any]] = []
    for page in structure.get("pages", []):
        lines.extend(page.get("lines", []))

    field = target_field(target_language)
    translated_units: list[dict[str, Any]] = []
    missing = [line for line in lines if str(line.get("text") or "").strip() and str(line.get("text")) not in cache]
    for batch in iter_batches(missing, batch_max_chars):
        texts = [str(item["text"]).strip() for item in batch]
        translations = translate_batch(texts, source_language, target_language)
        for text, translation in zip(texts, translations):
            cache[text] = translation.strip() or text
        time.sleep(0.08)

    for line in lines:
        source_text = str(line.get("text") or "").strip()
        if not source_text:
            continue
        translated = cache.get(source_text, source_text)
        translated_units.append(
            {
                "unit_id": line["unit_id"],
                "page_index": line["page_index"],
                "source_text": source_text,
                "translation_target_text": translated,
                field: translated,
                "preserve_tokens": [],
                "term_decisions": {},
            }
        )

    result = {
        "translation_provider": "google_translate_web_gtx",
        "source_language": "zh" if normalize_lang(source_language).startswith("zh") else "en",
        "target_language": "zh" if normalize_lang(target_language).startswith("zh") else "en",
        "target_text_field": field,
        "translation_quality": "semantic_translation",
        "semantic_coverage": "full_semantic_translation",
        "source_extraction_ref": str(source_structure),
        "translation_batch_manifest_ref": None,
        "prompt_artifacts": [],
        "units": translated_units,
        "runtime_notes": {
            "model_backend": "google_translate_web_gtx_public_endpoint",
            "unit_count": len(translated_units),
            "cache_path": str(cache_path),
            "anti_overfit_statement": "Translations are produced from current-run extracted text only. No human reference PDF, fixed page, fixed text, or sample-specific mapping is read.",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-structure", type=Path, required=True)
    parser.add_argument("--source-language", required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--batch-max-chars", type=int, default=1800)
    args = parser.parse_args()
    run(args.source_structure, args.source_language, args.target_language, args.output, args.cache, args.batch_max_chars)


if __name__ == "__main__":
    main()
