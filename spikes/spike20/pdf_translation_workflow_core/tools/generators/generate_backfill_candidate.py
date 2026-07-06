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


def sample_fill_detail(page: fitz.Page, rect: fitz.Rect) -> dict[str, Any]:
    pix = page.get_pixmap(alpha=False)
    page_rect = page.rect
    samples: list[tuple[int, int, int, str]] = []

    def add_sample(x: float, y: float, source: str) -> None:
        ix = max(0, min(pix.width - 1, int(round(x))))
        iy = max(0, min(pix.height - 1, int(round(y))))
        offset = (iy * pix.width + ix) * pix.n
        rgb = tuple(pix.samples[offset : offset + 3])
        if len(rgb) == 3:
            samples.append((int(rgb[0]), int(rgb[1]), int(rgb[2]), source))

    inner_x_count = 5 if rect.width >= 12 else 3
    inner_y_count = 3 if rect.height >= 6 else 2
    for x_index in range(inner_x_count):
        x = rect.x0 + (rect.width * (x_index + 0.5) / inner_x_count)
        for y_index in range(inner_y_count):
            y = rect.y0 + (rect.height * (y_index + 0.5) / inner_y_count)
            add_sample(x, y, "inner")

    for gap in [1.5, 3.0, 5.0, 8.0]:
        source_name = "near_ring" if gap <= 5.0 else "outer_ring"
        x_mid = (rect.x0 + rect.x1) / 2
        y_mid = (rect.y0 + rect.y1) / 2
        if rect.y0 - gap >= page_rect.y0:
            for point in [(rect.x0 + 1, rect.y0 - gap), (x_mid, rect.y0 - gap), (rect.x1 - 1, rect.y0 - gap)]:
                add_sample(point[0], point[1], source_name)
        if rect.y1 + gap <= page_rect.y1:
            for point in [(rect.x0 + 1, rect.y1 + gap), (x_mid, rect.y1 + gap), (rect.x1 - 1, rect.y1 + gap)]:
                add_sample(point[0], point[1], source_name)
        if rect.x0 - gap >= page_rect.x0:
            for point in [(rect.x0 - gap, rect.y0 + 1), (rect.x0 - gap, y_mid), (rect.x0 - gap, rect.y1 - 1)]:
                add_sample(point[0], point[1], source_name)
        if rect.x1 + gap <= page_rect.x1:
            for point in [(rect.x1 + gap, rect.y0 + 1), (rect.x1 + gap, y_mid), (rect.x1 + gap, rect.y1 - 1)]:
                add_sample(point[0], point[1], source_name)
    if not samples:
        return {
            "fill_color": (1.0, 1.0, 1.0),
            "method": "outside_ring_median_pixel_cluster",
            "sample_count": 0,
            "selected_cluster_count": 0,
            "quantization_step": 8,
        }
    clusters: dict[tuple[int, int, int], list[tuple[int, int, int, str]]] = {}
    for rgb in samples:
        key = tuple(int(round(channel / 8) * 8) for channel in rgb[:3])
        clusters.setdefault(key, []).append(rgb)
    def source_weight(sample_source: str) -> int:
        if sample_source == "near_ring":
            return 3
        if sample_source == "outer_ring":
            return 1
        return 2

    cluster_key, cluster = max(
        clusters.items(),
        key=lambda item: (
            sum(source_weight(sample[3]) for sample in item[1]),
            len(item[1]),
            -sum(item[0]),
        ),
    )
    representative_samples = [sample for sample in cluster if sample[3] != "inner"] or cluster

    def median_channel(channel_index: int) -> int:
        values = sorted(sample[channel_index] for sample in representative_samples)
        mid = len(values) // 2
        if len(values) % 2:
            return int(values[mid])
        return int(round((values[mid - 1] + values[mid]) / 2))

    r = median_channel(0)
    g = median_channel(1)
    b = median_channel(2)
    return {
        "fill_color": (r / 255, g / 255, b / 255),
        "method": "outside_ring_median_pixel_cluster",
        "sample_count": len(samples),
        "selected_cluster_count": len(cluster),
        "selected_cluster_key": list(cluster_key),
        "selected_cluster_inner_count": sum(1 for sample in cluster if sample[3] == "inner"),
        "selected_cluster_near_ring_count": sum(1 for sample in cluster if sample[3] == "near_ring"),
        "selected_cluster_outer_ring_count": sum(1 for sample in cluster if sample[3] == "outer_ring"),
        "selected_cluster_representative_source": "ring" if any(sample[3] != "inner" for sample in cluster) else "inner",
        "quantization_step": 8,
    }


def sample_fill(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float, float]:
    return sample_fill_detail(page, rect)["fill_color"]


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
            fill_provenance = sample_fill_detail(page, rect)
            fill = fill_provenance["fill_color"]
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
                    "fill_color_provenance": fill_provenance,
                }
            )
            page.add_redact_annot(rect, fill=fill)
            page_units.append((unit_id, rect, zh, float(line.get("font_size") or 6.0), fill, fill_provenance))

        if page_units:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            for unit_id, rect, zh, source_size, fill, fill_provenance in page_units:
                insert_result = insert_chinese(page, rect, zh, fontfile, source_size)
                insertion_records.append(
                    {
                        "unit_id": unit_id,
                        "page_index": page_index,
                        "bbox": [round(v, 3) for v in rect],
                        "translation_zh": zh,
                        "redaction_fill_provenance": [
                            {
                                "unit_id": unit_id,
                                "fill_color": [round(v, 4) for v in fill],
                                "fill_color_provenance": fill_provenance,
                            }
                        ],
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
