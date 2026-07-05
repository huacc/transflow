"""Generate a low-fidelity Chinese backfill candidate PDF.

tool_name: generate_backfill_candidate
category: generators
input_contract: source PDF, source extraction JSON, output PDF path, evidence paths
output_contract: candidate PDF with English text redacted and Chinese placeholder text inserted, plus translations/layout/evidence JSON
failure_signals: source/extraction cannot be opened, font unavailable, output cannot be written
fallback: mark S_FAIL_TOOLING or use smoke generator only outside product-quality validation
mode_scope: backfill_candidate_validation only; not sufficient for product_quality
anti_overfit_statement: uses extracted current-run lines and bboxes only; does not branch on sample filename, known page, exact text, or document identity
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ensure_dir, read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
]


def choose_font() -> Path:
    for path in FONT_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("No CJK font found in Windows font candidates")


def text_kind(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= 14 and stripped.isupper():
        return "title_or_label"
    if len(stripped) <= 18:
        return "label"
    return "body"


def placeholder_translate(text: str) -> str:
    numbers = re.findall(r"\(?\d[\d,./%]*\)?", text)
    kind = text_kind(text)
    if kind == "title_or_label":
        base = "中文标题"
    elif kind == "label":
        base = "中文标签"
    else:
        base = "中文回填"
    if numbers:
        return f"{base} {' '.join(numbers[:4])}"
    return base


def sample_fill(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float, float]:
    pix = page.get_pixmap(alpha=False)
    candidates = [
        (rect.x0 + 1, rect.y0 + 1),
        (rect.x1 - 1, rect.y0 + 1),
        (rect.x0 + 1, rect.y1 - 1),
        (rect.x1 - 1, rect.y1 - 1),
    ]
    colors: list[tuple[int, int, int]] = []
    for x, y in candidates:
        ix = max(0, min(pix.width - 1, int(round(x))))
        iy = max(0, min(pix.height - 1, int(round(y))))
        offset = (iy * pix.width + ix) * pix.n
        rgb = tuple(pix.samples[offset : offset + 3])
        if len(rgb) == 3:
            colors.append(rgb)  # type: ignore[arg-type]
    if not colors:
        return (1.0, 1.0, 1.0)
    # Pick the brightest corner; for text bboxes this is usually the local background.
    r, g, b = max(colors, key=sum)
    return (r / 255, g / 255, b / 255)


def inflate_rect(bbox: list[float], page_rect: fitz.Rect) -> fitz.Rect:
    rect = fitz.Rect(bbox)
    rect.x0 = max(page_rect.x0, rect.x0 - 0.5)
    rect.y0 = max(page_rect.y0, rect.y0 - 0.4)
    rect.x1 = min(page_rect.x1, rect.x1 + 0.5)
    rect.y1 = min(page_rect.y1, rect.y1 + 0.4)
    return rect


def insert_chinese(page: fitz.Page, rect: fitz.Rect, text: str, fontfile: Path, source_size: float) -> dict[str, Any]:
    size = max(3.5, min(9.5, source_size * 0.88 if source_size else 6.0))
    attempts = []
    for scale in [1.0, 0.9, 0.8, 0.7, 0.6]:
        current_size = max(3.5, size * scale)
        rc = page.insert_textbox(
            rect,
            text,
            fontsize=current_size,
            fontname="cjk_backfill",
            fontfile=str(fontfile),
            color=(0.05, 0.05, 0.05),
            align=0,
        )
        attempts.append({"font_size": round(current_size, 3), "return_code": rc})
        if rc >= 0:
            return {"status": "fit", "font_size": round(current_size, 3), "attempts": attempts}
    fallback_point = fitz.Point(rect.x0, min(page.rect.y1 - 1, rect.y1))
    page.insert_text(
        fallback_point,
        text[:12],
        fontsize=3.5,
        fontname="cjk_backfill",
        fontfile=str(fontfile),
        color=(0.05, 0.05, 0.05),
    )
    return {"status": "fallback_insert_text", "font_size": 3.5, "attempts": attempts}


def generate(source: Path, extraction_path: Path, output: Path, translations_path: Path, layout_path: Path) -> dict[str, Any]:
    ensure_dir(output.parent)
    fontfile = choose_font()
    extraction = read_json(extraction_path)
    doc = fitz.open(source)
    translation_units: list[dict[str, Any]] = []
    layout_slots: list[dict[str, Any]] = []
    redaction_records: list[dict[str, Any]] = []
    insertion_records: list[dict[str, Any]] = []

    for page_info in extraction.get("pages", []):
        page_index = int(page_info["page_index"])
        page = doc[page_index]
        page_units = []
        for line in page_info.get("text_lines", []):
            if not line.get("ascii_tokens"):
                continue
            unit_id = line["line_id"]
            bbox = [float(v) for v in line["bbox"]]
            zh = placeholder_translate(line["text"])
            rect = inflate_rect(bbox, page.rect)
            fill = sample_fill(page, rect)
            translation_units.append(
                {
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "source_text": line["text"],
                    "translation_zh": zh,
                    "translation_mode": "deterministic_placeholder_zh",
                    "semantic_coverage": "placeholder_not_semantic",
                    "bbox": bbox,
                    "text_role": text_kind(line["text"]),
                }
            )
            layout_slots.append(
                {
                    "slot_id": f"slot_{unit_id}",
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "anchor_bbox": bbox,
                    "font_file": str(fontfile),
                    "font_size": line.get("font_size"),
                    "line_height": None,
                    "wrap_width": round(rect.width, 3),
                    "fill_color": [round(v, 4) for v in fill],
                    "overflow_policy": "shrink_then_fallback_insert_text",
                }
            )
            redaction_records.append(
                {
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "bbox": [round(v, 3) for v in rect],
                    "fill_color": [round(v, 4) for v in fill],
                }
            )
            page.add_redact_annot(rect, fill=fill)
            page_units.append((unit_id, rect, zh, float(line.get("font_size") or 6.0)))

        if page_units:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            for unit_id, rect, zh, source_size in page_units:
                insert_result = insert_chinese(page, rect, zh, fontfile, source_size)
                insertion_records.append(
                    {
                        "unit_id": unit_id,
                        "page_index": page_index,
                        "bbox": [round(v, 3) for v in rect],
                        "translation_zh": zh,
                        **insert_result,
                    }
                )

    doc.save(output, garbage=4, deflate=True)
    doc.close()

    translations = {
        "translation_provider": "deterministic_placeholder",
        "semantic_coverage": "placeholder_not_semantic",
        "unit_count": len(translation_units),
        "units": translation_units,
    }
    layout = {
        "layout_provider": "bbox_copy_placeholder_layout",
        "slot_count": len(layout_slots),
        "slots": layout_slots,
    }
    write_json(translations_path, translations)
    write_json(layout_path, layout)
    fit_warnings = [item for item in insertion_records if item["status"] != "fit"]
    return {
        "tool": "generate_backfill_candidate",
        "strategy": "redact_extractable_ascii_lines_and_insert_placeholder_chinese",
        "real_backfill_pdf": True,
        "translation_provider": "deterministic_placeholder",
        "translation_quality": "placeholder_not_semantic",
        "not_final_quality": True,
        "input_pdf": rel(source),
        "source_extraction": rel(extraction_path),
        "output_pdf": rel(output),
        "translations_json": rel(translations_path),
        "layout_plan_json": rel(layout_path),
        "output_sha256": sha256_file(output),
        "redacted_line_count": len(redaction_records),
        "inserted_line_count": len(insertion_records),
        "fit_warning_count": len(fit_warnings),
        "font_file": str(fontfile),
        "semantic_coverage": "placeholder_not_semantic",
        "expected_quality": "fail_semantic_coverage_or_visual_quality_in_product_quality_mode",
        "redactions": redaction_records,
        "insertions": insertion_records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--layout-plan", required=True)
    args = parser.parse_args()
    result = generate(
        resolve_workspace_path(args.input),
        resolve_workspace_path(args.source_extraction),
        Path(args.output),
        Path(args.translations),
        Path(args.layout_plan),
    )
    write_json(Path(args.evidence), result)
    print(args.evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
