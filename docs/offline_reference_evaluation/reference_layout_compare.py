"""Post-hoc layout comparison against a human reference PDF.

This is deliberately outside pdf_translation_workflow_core because reference PDFs
are allowed only for offline diagnosis, not for production generation decisions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

import fitz
from PIL import Image, ImageChops, ImageStat


def render_page(doc: fitz.Document, page_index: int, zoom: float) -> Image.Image:
    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def dominant_background_rgb(image: Image.Image) -> tuple[int, int, int]:
    small = image.convert("RGB").resize((max(1, image.width // 4), max(1, image.height // 4)))
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b in small.getdata():
        key = (round(r / 16) * 16, round(g / 16) * 16, round(b / 16) * 16)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0] if counts else (255, 255, 255)


def foreground_mask(image: Image.Image) -> tuple[list[int], int, tuple[int, int, int, int] | None]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    bg = dominant_background_rgb(rgb)
    pixels = rgb.load()
    mask = []
    min_x, min_y, max_x, max_y = width, height, -1, -1
    count = 0
    for y in range(height):
        row_count = 0
        for x in range(width):
            r, g, b = pixels[x, y]
            is_fg = ((r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2) ** 0.5 > 42
            row_count += 1 if is_fg else 0
            if is_fg:
                count += 1
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
        mask.append(row_count)
    bbox = None if max_x < 0 else (min_x, min_y, max_x + 1, max_y + 1)
    return mask, count, bbox


def projection_similarity(a: list[int], b: list[int]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    a = a[:n]
    b = b[:n]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den_a = sum((x - mean_a) ** 2 for x in a)
    den_b = sum((y - mean_b) ** 2 for y in b)
    if den_a == 0 or den_b == 0:
        return 1.0 if den_a == den_b else 0.0
    return num / ((den_a * den_b) ** 0.5)


def page_metrics(candidate_img: Image.Image, reference_img: Image.Image) -> dict:
    if candidate_img.size != reference_img.size:
        reference_img = reference_img.resize(candidate_img.size)
    diff = ImageChops.difference(candidate_img.convert("L"), reference_img.convert("L"))
    stat = ImageStat.Stat(diff)
    cand_rows, cand_fg, cand_bbox = foreground_mask(candidate_img)
    ref_rows, ref_fg, ref_bbox = foreground_mask(reference_img)
    cand_cols = foreground_mask(candidate_img.transpose(Image.Transpose.ROTATE_90))[0]
    ref_cols = foreground_mask(reference_img.transpose(Image.Transpose.ROTATE_90))[0]
    total = candidate_img.size[0] * candidate_img.size[1]
    return {
        "mean_abs_delta": round(float(stat.mean[0]), 3),
        "foreground_coverage_candidate": round(cand_fg / total, 5),
        "foreground_coverage_reference": round(ref_fg / total, 5),
        "foreground_coverage_delta": round(abs(cand_fg - ref_fg) / total, 5),
        "row_projection_similarity": round(projection_similarity(cand_rows, ref_rows), 4),
        "column_projection_similarity": round(projection_similarity(cand_cols, ref_cols), 4),
        "candidate_content_bbox": cand_bbox,
        "reference_content_bbox": ref_bbox,
    }


def classify(metrics: dict) -> tuple[str, list[str]]:
    reasons = []
    if metrics["foreground_coverage_delta"] > 0.12:
        reasons.append("foreground_coverage_delta_gt_0.12")
    if metrics["row_projection_similarity"] < 0.55:
        reasons.append("row_projection_similarity_lt_0.55")
    if metrics["column_projection_similarity"] < 0.55:
        reasons.append("column_projection_similarity_lt_0.55")
    if metrics["mean_abs_delta"] > 42:
        reasons.append("mean_abs_delta_gt_42")
    return ("FAIL" if reasons else "PASS"), reasons


def compare(candidate: Path, reference: Path, out: Path, zoom: float) -> dict:
    cand_doc = fitz.open(candidate)
    ref_doc = fitz.open(reference)
    page_count = min(cand_doc.page_count, ref_doc.page_count)
    pages = []
    for page_index in range(page_count):
        metrics = page_metrics(render_page(cand_doc, page_index, zoom), render_page(ref_doc, page_index, zoom))
        status, reasons = classify(metrics)
        pages.append({"page_index": page_index, "page_number": page_index + 1, "status": status, "reasons": reasons, **metrics})
    cand_doc.close()
    ref_doc.close()
    failed = [page for page in pages if page["status"] == "FAIL"]
    result = {
        "tool": "reference_layout_compare",
        "scope": "post_hoc_reference_only_not_generation_input",
        "candidate_pdf": str(candidate),
        "reference_pdf": str(reference),
        "zoom": zoom,
        "candidate_page_count": len(pages) if candidate.exists() else None,
        "reference_page_count": ref_doc.page_count if False else page_count,
        "compared_page_count": page_count,
        "verdict": "PASS" if not failed else "FAIL",
        "failed_page_count": len(failed),
        "aggregate": {
            "mean_abs_delta_avg": round(mean(page["mean_abs_delta"] for page in pages), 3) if pages else None,
            "foreground_coverage_delta_avg": round(mean(page["foreground_coverage_delta"] for page in pages), 5) if pages else None,
            "row_projection_similarity_avg": round(mean(page["row_projection_similarity"] for page in pages), 4) if pages else None,
            "column_projection_similarity_avg": round(mean(page["column_projection_similarity"] for page in pages), 4) if pages else None,
        },
        "worst_pages": sorted(
            pages,
            key=lambda item: (
                item["status"] != "FAIL",
                item["row_projection_similarity"] + item["column_projection_similarity"],
                -item["foreground_coverage_delta"],
            ),
        )[:30],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--zoom", type=float, default=0.35)
    args = parser.parse_args()
    result = compare(Path(args.candidate), Path(args.reference), Path(args.out), args.zoom)
    print(json.dumps({k: result[k] for k in ["verdict", "failed_page_count", "compared_page_count", "aggregate"]}, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
