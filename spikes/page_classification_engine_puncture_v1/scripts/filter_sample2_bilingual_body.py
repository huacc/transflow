from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, write_json


def language_metrics(page: fitz.Page, source_page_number: int | None) -> dict[str, Any]:
    data = page.get_text("dict", sort=True)
    cjk_chars = 0
    latin_words = 0
    cjk_area = 0.0
    latin_area = 0.0
    mixed_area = 0.0
    max_font_size = 0.0
    text_block_count = 0
    for block in data.get("blocks", []):
        if int(block.get("type", 1)) != 0:
            continue
        text_block_count += 1
        spans = [span for line in block.get("lines", []) for span in line.get("spans", [])]
        text = "".join(str(span.get("text", "")) for span in spans)
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        words = len(re.findall(r"[A-Za-z]+", text))
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        cjk_chars += cjk
        latin_words += words
        if cjk >= max(12, words):
            cjk_area += area
        elif words >= max(12, cjk):
            latin_area += area
        if cjk >= 20 and words >= 20:
            mixed_area += area
        for span in spans:
            max_font_size = max(max_font_size, float(span.get("size", 0.0)))

    page_area = max(1.0, float(page.rect.width * page.rect.height))
    count_ratio = min(cjk_chars, latin_words) / max(1, max(cjk_chars, latin_words))
    separate_area_ratio = min(cjk_area, latin_area) / max(1.0, max(cjk_area, latin_area))
    mixed_area_ratio = mixed_area / page_area
    title_like = bool(
        source_page_number == 1
        or (max_font_size >= 24 and cjk_chars + latin_words < 250 and text_block_count <= 10)
    )
    bilingual_body_like = bool(
        cjk_chars >= 50
        and latin_words >= 40
        and count_ratio >= 0.15
        and not title_like
    )
    return {
        "cjk_char_count": cjk_chars,
        "latin_word_count": latin_words,
        "language_count_ratio": round(count_ratio, 6),
        "cjk_dominant_area": round(cjk_area, 3),
        "latin_dominant_area": round(latin_area, 3),
        "separate_language_area_ratio": round(separate_area_ratio, 6),
        "mixed_language_area_ratio": round(mixed_area_ratio, 6),
        "max_font_size": round(max_font_size, 3),
        "text_block_count": text_block_count,
        "title_like_exception": title_like,
        "bilingual_body_like": bilingual_body_like,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", default="样本2")
    parser.add_argument("--source-manifest", default="manifests/sample2_source_manifest.jsonl")
    parser.add_argument("--selection-manifest", default="manifests/sample2_selection_manifest.json")
    parser.add_argument("--output", default="manifests/sample2_bilingual_body_exclusions.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    sample_root = (ROOT / args.sample_dir).resolve()
    source_manifest = (ROOT / args.source_manifest).resolve()
    selection_manifest = (ROOT / args.selection_manifest).resolve()
    output_path = (ROOT / args.output).resolve()
    rows = read_jsonl(source_manifest)
    excluded: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    borderline: list[dict[str, Any]] = []
    for row in rows:
        sample_path = sample_root / f"{row['sample_id']}.pdf"
        with fitz.open(sample_path) as document:
            metrics = language_metrics(document[0], row.get("source_page_number"))
        enriched = {**row, "language_filter_metrics": metrics}
        if metrics["bilingual_body_like"]:
            excluded.append(enriched)
        else:
            kept.append(row)
            if (
                not metrics["title_like_exception"]
                and metrics["cjk_char_count"] >= 50
                and metrics["latin_word_count"] >= 40
            ):
                area_signal = max(
                    metrics["separate_language_area_ratio"] / 0.20,
                    metrics["mixed_language_area_ratio"] / 0.08,
                )
                review_score = min(
                    metrics["cjk_char_count"] / 120,
                    metrics["latin_word_count"] / 80,
                    metrics["language_count_ratio"] / 0.30,
                    area_signal,
                )
                borderline.append({**enriched, "borderline_review_score": round(review_score, 6)})

    borderline.sort(key=lambda row: row["borderline_review_score"], reverse=True)

    output = {
        "filter": "exclude near-1-to-1 Chinese-English body pages; retain title-like pages",
        "thresholds": {
            "minimum_cjk_chars": 50,
            "minimum_latin_words": 40,
            "minimum_language_count_ratio": 0.15,
            "title_exception": "source page 1, or max font >=24 with <250 language units and <=10 text blocks",
        },
        "input_count": len(rows),
        "excluded_count": len(excluded),
        "retained_count": len(kept),
        "applied": args.apply,
        "excluded": excluded,
        "borderline_review": borderline[:100],
    }
    write_json(output_path, output)

    if args.apply:
        for row in excluded:
            target = sample_root / f"{row['sample_id']}.pdf"
            if target.parent.resolve() != sample_root:
                raise RuntimeError(f"unsafe_sample_target:{target}")
            target.unlink()
        source_manifest.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept),
            encoding="utf-8",
        )
        selection = json.loads(selection_manifest.read_text(encoding="utf-8"))
        selection["post_filter"] = {
            "rule": output["filter"],
            "excluded_count": len(excluded),
            "retained_count": len(kept),
            "exclusion_manifest": str(output_path),
        }
        write_json(selection_manifest, selection)
        remaining = sorted(sample_root.glob("*.pdf"))
        if len(remaining) != len(kept):
            raise RuntimeError("sample2_post_filter_count_mismatch")

    print(
        json.dumps(
            {
                "BILINGUAL_BODY_FILTER_READY": True,
                "applied": args.apply,
                "input_count": len(rows),
                "excluded_count": len(excluded),
                "retained_count": len(kept),
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
