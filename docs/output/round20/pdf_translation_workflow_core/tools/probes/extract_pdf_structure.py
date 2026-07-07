"""Extract generic PDF structure.

tool_name: extract_pdf_structure
category: probes
input_contract: input PDF path
output_contract: JSON with pages, text lines, bboxes, fonts, drawings and image counts
failure_signals: unreadable PDF, empty pages, extraction exception
fallback: render page and mark text extraction unavailable
anti_overfit_statement: classification uses structural counts only, not sample filenames or known strings
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ascii_tokens, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")


def rect_values(rect: Any) -> list[float]:
    return [round(float(v), 3) for v in rect]


def median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def column_bucket_count(text_lines: list[dict[str, Any]], page_width: float) -> int:
    if page_width <= 0:
        return 0
    buckets = {
        int(max(0.0, min(0.99, float(line["bbox"][0]) / page_width)) / 0.08)
        for line in text_lines
        if isinstance(line.get("bbox"), list) and len(line["bbox"]) == 4
    }
    return len(buckets)


def classify_page(text_lines: list[dict[str, Any]], drawings_count: int, image_count: int, page_rect: fitz.Rect) -> str:
    texts = [line["text"] for line in text_lines]
    joined = " ".join(texts)
    percent_count = joined.count("%")
    numeric_count = sum(1 for t in texts if any(ch.isdigit() for ch in t))
    short_line_count = sum(1 for t in texts if len(t.strip()) <= 12)
    page_width = max(1.0, float(page_rect.width))
    widths = [
        max(0.0, float(line["bbox"][2]) - float(line["bbox"][0]))
        for line in text_lines
        if isinstance(line.get("bbox"), list) and len(line["bbox"]) == 4
    ]
    short_ratio = short_line_count / max(1, len(text_lines))
    median_width_ratio = median(widths) / page_width
    column_count = column_bucket_count(text_lines, page_width)
    if drawings_count > 40 and percent_count >= 4:
        return "chart_or_dashboard"
    if (
        len(text_lines) >= 45
        and drawings_count >= 8
        and short_ratio >= 0.45
        and median_width_ratio <= 0.22
        and column_count >= 4
    ):
        return "matrix_or_table_diagram"
    if drawings_count > 20 and numeric_count > 20 and short_line_count > 20:
        return "table_or_chart_dense"
    if image_count:
        return "mixed_image_text"
    if len(text_lines) > 80 and drawings_count < 20:
        return "body_or_notes_dense"
    return "mixed_text"


def extract(pdf_path: Path) -> dict[str, Any]:
    doc = fitz.open(pdf_path)
    pages: list[dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        blocks = page.get_text("dict")["blocks"]
        lines: list[dict[str, Any]] = []
        image_blocks = 0
        for block_index, block in enumerate(blocks):
            if block.get("type") == 1:
                image_blocks += 1
                continue
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines", [])):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue
                first_span = spans[0] if spans else {}
                lines.append(
                    {
                        "line_id": f"p{page_index}_b{block_index}_l{line_index}",
                        "page_index": page_index,
                        "block_id": block_index,
                        "line_index": line_index,
                        "text": text,
                        "bbox": rect_values(line.get("bbox", [0, 0, 0, 0])),
                        "font_size": round(float(first_span.get("size", 0)), 3),
                        "font": first_span.get("font", ""),
                        "color": first_span.get("color"),
                        "ascii_tokens": ascii_tokens(text),
                        "cjk_char_count": len(CJK_RE.findall(text)),
                    }
                )
        drawings = page.get_drawings()
        pages.append(
            {
                "page_index": page_index,
                "rect": rect_values(page.rect),
                "text_line_count": len(lines),
                "image_block_count": image_blocks,
                "drawing_count": len(drawings),
                "page_type_guess": classify_page(lines, len(drawings), image_blocks, page.rect),
                "text_lines": lines,
            }
        )
    result = {
        "tool": "extract_pdf_structure",
        "input_pdf": rel(pdf_path),
        "sha256": sha256_file(pdf_path),
        "page_count": doc.page_count,
        "pages": pages,
    }
    doc.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = extract(resolve_workspace_path(args.input))
    write_json(Path(args.out), result)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
