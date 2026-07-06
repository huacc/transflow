"""Collect region-level visual quality metrics for translated PDF candidates.

tool_name: collect_visual_region_metrics
category: validators
input_contract: source PDF, candidate PDF, candidate generation evidence JSON
output_contract: JSON with page metrics, region metrics, role gates, and optional crop evidence
failure_signals: unreadable PDFs, invalid evidence, crop/render errors
fallback: mark S_FAIL_QUALITY if required region evidence cannot be produced
anti_overfit_statement: classifies by current-run geometry, region roles, render pixels, and insertion evidence; never branches on filename, known page, exact text, or fixed coordinates
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ensure_dir, median, read_json, rel, resolve_workspace_path, write_json  # noqa: E402


ROLE_RULES: dict[str, dict[str, Any]] = {
    "hero_banner_title": {
        "gate_id": "hero_banner_text_readability",
        "fail_font_pt": 7.0,
        "warn_font_pt": 7.0,
        "fail_source_ratio": 0.28,
        "warn_source_ratio": 0.34,
        "critical": True,
        "repair_atom": "heading_frame_fit_or_short_title_variant",
    },
    "title": {
        "gate_id": "title_readability",
        "fail_font_pt": 6.2,
        "warn_font_pt": 7.0,
        "fail_source_ratio": 0.30,
        "warn_source_ratio": 0.42,
        "critical": True,
        "repair_atom": "heading_font_fit_curve_repair",
    },
    "body": {
        "gate_id": "body_paragraph_readability",
        "fail_font_pt": 5.2,
        "warn_font_pt": 6.8,
        "fail_source_ratio": 0.45,
        "warn_source_ratio": 0.62,
        "critical": True,
        "repair_atom": "target_composition_body_reflow_repair",
    },
    "table_text": {
        "gate_id": "table_text_legibility",
        "fail_font_pt": 3.2,
        "warn_font_pt": 3.8,
        "fail_source_ratio": 0.35,
        "warn_source_ratio": 0.50,
        "critical": True,
        "repair_atom": "D2_constrained_slot_layout_variants",
    },
    "footnote": {
        "gate_id": "footnote_readability",
        "fail_font_pt": 3.2,
        "warn_font_pt": 3.8,
        "fail_source_ratio": 0.35,
        "warn_source_ratio": 0.50,
        "critical": False,
        "repair_atom": "footnote_fit_curve_repair",
    },
    "legend": {
        "gate_id": "legend_label_alignment",
        "fail_font_pt": 3.4,
        "warn_font_pt": 4.0,
        "fail_source_ratio": 0.35,
        "warn_source_ratio": 0.50,
        "critical": True,
        "repair_atom": "D2_constrained_slot_layout_variants",
    },
    "sidebar": {
        "gate_id": "sidebar_navigation_legibility",
        "fail_font_pt": 3.4,
        "warn_font_pt": 4.0,
        "fail_source_ratio": 0.35,
        "warn_source_ratio": 0.50,
        "critical": True,
        "repair_atom": "side_navigation_rotated_image_repair",
    },
    "event_card": {
        "gate_id": "event_card_readability",
        "fail_font_pt": 3.8,
        "warn_font_pt": 4.2,
        "fail_source_ratio": 0.45,
        "warn_source_ratio": 0.60,
        "critical": True,
        "repair_atom": "event_card_local_fit_repair",
    },
    "short_label": {
        "gate_id": "short_label_legibility",
        "fail_font_pt": 3.4,
        "warn_font_pt": 4.4,
        "fail_source_ratio": 0.35,
        "warn_source_ratio": 0.55,
        "critical": False,
        "repair_atom": "D2_constrained_slot_layout_variants",
    },
}

FAIL_STATUSES = {"fallback_insert_text"}
WARN_STATUSES = {"point_fit"}
DENSE_TABLE_PAGE_TYPES = {"table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"}
IMAGE_DELTA_FAIL = 34.0
IMAGE_DELTA_WARN = 22.0
BACKGROUND_DELTA_FAIL = 42.0
BACKGROUND_DELTA_WARN = 26.0
BACKGROUND_RESIDUE_FAIL = 18.0
BACKGROUND_RESIDUE_WARN = 12.0
INNER_BACKGROUND_DELTA_FAIL = 30.0
INNER_BACKGROUND_DELTA_WARN = 20.0
TEXT_IMAGE_BACKGROUND_DELTA_FAIL = 12.0
TEXT_IMAGE_BACKGROUND_DELTA_WARN = 6.0
TEXT_IMAGE_STATUSES = {"constrained_text_image_fit", "rotated_horizontal_image_fit"}
REDACTION_FILL_DELTA_FAIL = 2.0
REDACTION_FILL_DELTA_WARN = 1.2
REDACTION_PATCH_SCORE_FAIL = 4.5
REDACTION_PATCH_SCORE_WARN = 2.8
BACKGROUND_COVER_SOLID_PATCH_SATURATION_FAIL = 18.0
BACKGROUND_COVER_SOLID_PATCH_AREA_FAIL = 600.0
BACKGROUND_COVER_SOLID_PATCH_AREA_WARN = 300.0
SOURCE_BASELINE_FAIL_COVERAGE = 0.90
SOURCE_BASELINE_WARN_COVERAGE = 0.97


def render_page(doc: fitz.Document, page_index: int, zoom: float) -> Image.Image:
    page = doc[page_index]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def scaled_box(rect: list[float], zoom: float, image: Image.Image) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    box = (
        max(0, min(image.width - 1, int(math.floor(x0 * zoom)))),
        max(0, min(image.height - 1, int(math.floor(y0 * zoom)))),
        max(1, min(image.width, int(math.ceil(x1 * zoom)))),
        max(1, min(image.height, int(math.ceil(y1 * zoom)))),
    )
    if box[2] <= box[0]:
        box = (box[0], box[1], min(image.width, box[0] + 1), box[3])
    if box[3] <= box[1]:
        box = (box[0], box[1], box[2], min(image.height, box[1] + 1))
    return box


def expand_box(box: tuple[int, int, int, int], image: Image.Image, pad: int) -> tuple[int, int, int, int]:
    return (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(image.width, box[2] + pad),
        min(image.height, box[3] + pad),
    )


def inner_box(box: tuple[int, int, int, int], image: Image.Image, inset_ratio: float = 0.18) -> tuple[int, int, int, int]:
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    inset_x = min(width // 3, max(1, int(round(width * inset_ratio))))
    inset_y = min(height // 3, max(1, int(round(height * inset_ratio))))
    return (
        max(0, min(image.width - 1, box[0] + inset_x)),
        max(0, min(image.height - 1, box[1] + inset_y)),
        max(1, min(image.width, box[2] - inset_x)),
        max(1, min(image.height, box[3] - inset_y)),
    )


def mean_rgb(image: Image.Image) -> tuple[float, float, float]:
    if image.width <= 0 or image.height <= 0:
        return (255.0, 255.0, 255.0)
    count = image.width * image.height
    sums = [0, 0, 0]
    for r, g, b in image.getdata():
        sums[0] += r
        sums[1] += g
        sums[2] += b
    return (sums[0] / count, sums[1] / count, sums[2] / count)


def sample_pixels(image: Image.Image, max_samples: int = 20000) -> list[tuple[int, int, int]]:
    total = image.width * image.height
    if total <= 0:
        return []
    step = max(1, int(math.sqrt(total / max_samples)))
    pixels: list[tuple[int, int, int]] = []
    for y in range(0, image.height, step):
        for x in range(0, image.width, step):
            pixels.append(image.getpixel((x, y)))
    return pixels


def dominant_rgb(image: Image.Image) -> tuple[int, int, int]:
    clusters: dict[tuple[int, int, int], int] = {}
    for r, g, b in sample_pixels(image):
        key = (round(r / 16) * 16, round(g / 16) * 16, round(b / 16) * 16)
        clusters[key] = clusters.get(key, 0) + 1
    if not clusters:
        return (255, 255, 255)
    key = max(clusters.items(), key=lambda item: (item[1], sum(item[0])))[0]
    return tuple(max(0, min(255, int(v))) for v in key)


def edge_dominant_rgb(image: Image.Image, edge_width: int = 3) -> tuple[int, int, int]:
    if image.width <= 0 or image.height <= 0:
        return (255, 255, 255)
    edge = max(1, min(edge_width, image.width, image.height))
    pixels: list[tuple[int, int, int]] = []
    for y in range(image.height):
        for x in range(image.width):
            if x < edge or x >= image.width - edge or y < edge or y >= image.height - edge:
                pixels.append(image.getpixel((x, y)))
    if not pixels:
        return dominant_rgb(image)
    clusters: dict[tuple[int, int, int], int] = {}
    for r, g, b in pixels:
        key = (round(r / 16) * 16, round(g / 16) * 16, round(b / 16) * 16)
        clusters[key] = clusters.get(key, 0) + 1
    key = max(clusters.items(), key=lambda item: (item[1], sum(item[0])))[0]
    return tuple(max(0, min(255, int(v))) for v in key)


def ring_dominant_rgb(
    image: Image.Image,
    box: tuple[int, int, int, int],
    pad: int,
    quantization_step: int = 8,
) -> tuple[int, int, int]:
    ring = expand_box(box, image, pad)
    pixels: list[tuple[int, int, int]] = []
    for y in range(ring[1], ring[3]):
        for x in range(ring[0], ring[2]):
            if box[0] <= x < box[2] and box[1] <= y < box[3]:
                continue
            pixels.append(image.getpixel((x, y)))
    if not pixels:
        return edge_dominant_rgb(image.crop(box))
    step = max(1, len(pixels) // 20000)
    clusters: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for r, g, b in pixels[::step]:
        key = (
            round(r / quantization_step) * quantization_step,
            round(g / quantization_step) * quantization_step,
            round(b / quantization_step) * quantization_step,
        )
        clusters.setdefault(key, []).append((r, g, b))
    cluster = max(clusters.items(), key=lambda item: (len(item[1]), sum(item[0])))[1]

    def median_channel(index: int) -> int:
        values = sorted(color[index] for color in cluster)
        mid = len(values) // 2
        if len(values) % 2:
            return int(values[mid])
        return int(round((values[mid - 1] + values[mid]) / 2))

    return (median_channel(0), median_channel(1), median_channel(2))


def color_delta(a: tuple[float, float, float] | tuple[int, int, int], b: tuple[float, float, float] | tuple[int, int, int]) -> float:
    return round(sum(abs(float(x) - float(y)) for x, y in zip(a, b)) / 3, 3)


def saturation(color: tuple[int, int, int] | tuple[float, float, float]) -> float:
    values = [float(v) for v in color]
    return max(values) - min(values)


def normalized_float_color_to_rgb(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        channels = [float(value[index]) for index in range(3)]
    except (TypeError, ValueError):
        return None
    if all(0.0 <= channel <= 1.0 for channel in channels):
        return tuple(max(0, min(255, int(round(channel * 255)))) for channel in channels)
    return tuple(max(0, min(255, int(round(channel)))) for channel in channels)


def crop_contact_sheet(source_crop: Image.Image, output_crop: Image.Image, out_path: Path, label: str) -> str:
    ensure_dir(out_path.parent)
    pad = 12
    label_h = 24
    height = max(source_crop.height, output_crop.height) + label_h + pad * 2
    width = source_crop.width + output_crop.width + pad * 3
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 6), f"source {label}", fill="black")
    draw.text((source_crop.width + pad * 2, 6), f"output {label}", fill="black")
    sheet.paste(source_crop, (pad, label_h + pad))
    sheet.paste(output_crop, (source_crop.width + pad * 2, label_h + pad))
    sheet.save(out_path)
    return rel(out_path)


def region_role(insertion: dict[str, Any], page_rect: fitz.Rect, source_bg: tuple[int, int, int]) -> str:
    kind = str(insertion.get("region_kind") or "")
    page_type = str(insertion.get("page_type_guess") or "")
    bbox = [float(v) for v in insertion.get("bbox", [0, 0, 0, 0])]
    y_ratio = bbox[1] / max(1.0, float(page_rect.height))
    width_ratio = (bbox[2] - bbox[0]) / max(1.0, float(page_rect.width))
    if kind == "heading" and y_ratio < 0.28 and saturation(source_bg) > 50:
        return "hero_banner_title"
    if kind == "heading":
        return "title"
    if page_type in DENSE_TABLE_PAGE_TYPES and kind in {"body", "short_label", "compact_label"} and width_ratio < 0.50:
        return "table_text"
    if kind in {"body", "body_flow"}:
        return "body"
    if kind in {"table_cell", "table_note"}:
        return "table_text"
    if kind == "footnote":
        return "footnote"
    if kind == "legend":
        return "legend"
    if kind == "vertical_nav":
        return "sidebar"
    if kind == "event_card":
        return "event_card"
    if kind in {"short_label", "compact_label"}:
        return "short_label"
    return "body"


def source_line_index(source_extraction: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(source_extraction, dict):
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for page in source_extraction.get("pages", []):
        if not isinstance(page, dict):
            continue
        for line in page.get("text_lines", []):
            if isinstance(line, dict) and line.get("line_id"):
                by_id[str(line["line_id"])] = line
    return by_id


def source_stats_for_insertion(insertion: dict[str, Any], source_lines: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sizes: list[float] = []
    bboxes: list[list[float]] = []
    matched_line_count = 0
    for unit_id in insertion.get("unit_ids", []):
        line = source_lines.get(str(unit_id))
        if not line:
            continue
        matched_line_count += 1
        size = line.get("font_size")
        if isinstance(size, (int, float)) and float(size) > 0:
            sizes.append(float(size))
        bbox = line.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            bboxes.append([float(v) for v in bbox])
    source_font_source = "source_extraction" if sizes else None
    if not sizes:
        for key in ("source_font_size", "source_size"):
            hinted_size = insertion.get(key)
            if isinstance(hinted_size, (int, float)) and float(hinted_size) > 0:
                sizes.append(float(hinted_size))
                source_font_source = "generation_evidence"
                break
    source_bbox = None
    if bboxes:
        source_bbox = [
            min(b[0] for b in bboxes),
            min(b[1] for b in bboxes),
            max(b[2] for b in bboxes),
            max(b[3] for b in bboxes),
        ]
    source_font = median(sizes)
    return {
        "source_median_font_size": None if source_font is None else round(float(source_font), 3),
        "source_font_source": source_font_source,
        "source_unit_count": len(bboxes),
        "source_matched_line_count": matched_line_count,
        "source_union_bbox": None if source_bbox is None else [round(v, 3) for v in source_bbox],
    }


def status_for_region(
    insertion: dict[str, Any],
    role: str,
    bg_delta: float,
    residue_delta: float,
    source_font_size: float | None,
) -> tuple[str, list[str], list[str], float | None]:
    rule = ROLE_RULES.get(role, ROLE_RULES["body"])
    status = str(insertion.get("status") or "")
    font_size = float(insertion.get("font_size") or 0)
    font_ratio = None
    if source_font_size and font_size:
        font_ratio = round(font_size / source_font_size, 3)
    reasons: list[str] = []
    repair_atoms: list[str] = []
    if status in FAIL_STATUSES:
        reasons.append(f"{status} is not acceptable for role {role}")
        repair_atoms.append(str(rule["repair_atom"]))
    elif status in WARN_STATUSES and bool(rule.get("critical")):
        reasons.append(f"{status} is only a warning-level fit for critical role {role}")
        repair_atoms.append(str(rule["repair_atom"]))
    if font_size and font_size < float(rule["fail_font_pt"]):
        reasons.append(f"font_size {font_size:.2f}pt is below {role} fail floor {float(rule['fail_font_pt']):.2f}pt")
        repair_atoms.append(str(rule["repair_atom"]))
    if font_ratio is not None and font_ratio < float(rule["fail_source_ratio"]):
        reasons.append(f"output_to_source_font_ratio {font_ratio:.2f} is below {role} fail ratio {float(rule['fail_source_ratio']):.2f}")
        repair_atoms.append(str(rule["repair_atom"]))
    source_residue_delta = float(insertion.get("source_residue_delta") or 0.0)
    inner_bg_delta = float(insertion.get("inner_background_delta") or 0.0)
    text_image_bg_delta = float(insertion.get("text_image_background_delta") or 0.0)
    residue_excess = max(0.0, residue_delta - source_residue_delta)
    residue_check_enabled = max(saturation(insertion.get("output_inner_background_rgb", (0, 0, 0))), saturation(insertion.get("output_background_rgb", (0, 0, 0)))) <= 72.0
    text_label_roles = {"event_card", "short_label", "legend", "sidebar"}
    background_delta_only = (
        inner_bg_delta < INNER_BACKGROUND_DELTA_FAIL
        and text_image_bg_delta < TEXT_IMAGE_BACKGROUND_DELTA_FAIL
        and not (residue_check_enabled and residue_delta >= BACKGROUND_RESIDUE_FAIL and residue_excess >= 8.0)
    )
    if bg_delta >= BACKGROUND_DELTA_FAIL:
        if role in text_label_roles and background_delta_only:
            # For compact labels and diagram captions, source-vs-output edge color can change
            # because translated glyphs replace source glyphs. Treat that as a warning unless
            # inner/background-residue evidence shows an actual patch artifact.
            pass
        else:
            reasons.append(f"background_delta {bg_delta:.1f} exceeds fail threshold {BACKGROUND_DELTA_FAIL:.1f}")
            repair_atoms.append("background_fill_resample")
    if status in TEXT_IMAGE_STATUSES and insertion.get("text_image_background_rgb") is None:
        reasons.append(f"{status} has no image_background_color evidence")
        repair_atoms.append("background_residue_fill_resample")
    if text_image_bg_delta >= TEXT_IMAGE_BACKGROUND_DELTA_FAIL:
        reasons.append(f"text_image_background_delta {text_image_bg_delta:.1f} exceeds fail threshold {TEXT_IMAGE_BACKGROUND_DELTA_FAIL:.1f}")
        repair_atoms.append("background_residue_fill_resample")
    if inner_bg_delta >= INNER_BACKGROUND_DELTA_FAIL:
        reasons.append(f"inner_background_delta {inner_bg_delta:.1f} exceeds fail threshold {INNER_BACKGROUND_DELTA_FAIL:.1f}")
        repair_atoms.append("background_residue_fill_resample")
    if residue_check_enabled and residue_delta >= BACKGROUND_RESIDUE_FAIL and residue_excess >= 8.0:
        reasons.append(f"background_residue_delta {residue_delta:.1f} exceeds fail threshold {BACKGROUND_RESIDUE_FAIL:.1f}")
        repair_atoms.append("background_residue_fill_resample")
    if reasons:
        return "fail", reasons, sorted(set(repair_atoms)), font_ratio
    warn_reasons: list[str] = []
    warn_repairs: list[str] = []
    if font_size and font_size < float(rule["warn_font_pt"]):
        warn_reasons.append(f"font_size {font_size:.2f}pt is below {role} recommended floor {float(rule['warn_font_pt']):.2f}pt")
        warn_repairs.append(str(rule["repair_atom"]))
    if font_ratio is not None and font_ratio < float(rule["warn_source_ratio"]):
        warn_reasons.append(f"output_to_source_font_ratio {font_ratio:.2f} is below {role} warn ratio {float(rule['warn_source_ratio']):.2f}")
        warn_repairs.append(str(rule["repair_atom"]))
    if warn_reasons:
        return "warn", warn_reasons, sorted(set(warn_repairs)), font_ratio
    if bg_delta >= BACKGROUND_DELTA_WARN:
        return "warn", [f"background_delta {bg_delta:.1f} exceeds warn threshold {BACKGROUND_DELTA_WARN:.1f}"], ["background_fill_resample"], font_ratio
    if inner_bg_delta >= INNER_BACKGROUND_DELTA_WARN:
        return "warn", [f"inner_background_delta {inner_bg_delta:.1f} exceeds warn threshold {INNER_BACKGROUND_DELTA_WARN:.1f}"], ["background_residue_fill_resample"], font_ratio
    if text_image_bg_delta >= TEXT_IMAGE_BACKGROUND_DELTA_WARN:
        return "warn", [f"text_image_background_delta {text_image_bg_delta:.1f} exceeds warn threshold {TEXT_IMAGE_BACKGROUND_DELTA_WARN:.1f}"], ["background_residue_fill_resample"], font_ratio
    if residue_check_enabled and residue_delta >= BACKGROUND_RESIDUE_WARN and residue_excess >= 6.0:
        return "warn", [f"background_residue_delta {residue_delta:.1f} exceeds warn threshold {BACKGROUND_RESIDUE_WARN:.1f}"], ["background_residue_fill_resample"], font_ratio
    return "pass", [], [], font_ratio


def page_color_metrics(source_image: Image.Image, output_image: Image.Image) -> dict[str, Any]:
    source_mean = mean_rgb(source_image)
    output_mean = mean_rgb(output_image)
    source_dom = dominant_rgb(source_image)
    output_dom = dominant_rgb(output_image)
    diff = ImageChops.difference(source_image.resize(output_image.size), output_image).convert("RGB")
    diff_mean = mean_rgb(diff)
    return {
        "source_mean_rgb": [round(v, 3) for v in source_mean],
        "output_mean_rgb": [round(v, 3) for v in output_mean],
        "mean_rgb_delta": color_delta(source_mean, output_mean),
        "source_dominant_rgb": list(source_dom),
        "output_dominant_rgb": list(output_dom),
        "dominant_rgb_delta": color_delta(source_dom, output_dom),
        "pixel_diff_mean_rgb": [round(v, 3) for v in diff_mean],
    }


def rect_contains(container: list[float], inner: list[float], tolerance: float = 0.75) -> bool:
    return (
        float(container[0]) <= float(inner[0]) + tolerance
        and float(container[1]) <= float(inner[1]) + tolerance
        and float(container[2]) >= float(inner[2]) - tolerance
        and float(container[3]) >= float(inner[3]) - tolerance
    )


def collect(
    source: Path,
    output: Path,
    generation_evidence: Path,
    source_extraction: Path | None,
    out: Path,
    crop_dir: Path | None,
    zoom: float,
) -> dict[str, Any]:
    evidence = read_json(generation_evidence)
    cover_rects_by_page: dict[int, list[list[float]]] = {}
    for cover in evidence.get("background_covers", []):
        if not isinstance(cover, dict):
            continue
        bbox = cover.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        page_index = int(cover.get("page_index") or 0)
        cover_rects_by_page.setdefault(page_index, []).append([float(v) for v in bbox])
    source_lines = source_line_index(read_json(source_extraction) if source_extraction is not None and source_extraction.exists() else None)
    source_doc = fitz.open(source)
    output_doc = fitz.open(output)
    page_count = min(source_doc.page_count, output_doc.page_count)
    source_images = [render_page(source_doc, i, zoom) for i in range(page_count)]
    output_images = [render_page(output_doc, i, zoom) for i in range(page_count)]
    page_metrics: list[dict[str, Any]] = []
    for page_index in range(page_count):
        source_page = source_doc[page_index]
        output_page = output_doc[page_index]
        metrics = page_color_metrics(source_images[page_index], output_images[page_index])
        src_img_count = len(source_page.get_images(full=True))
        out_img_count = len(output_page.get_images(full=True))
        image_status = "pass"
        image_reasons: list[str] = []
        if out_img_count < src_img_count:
            image_status = "fail"
            image_reasons.append("candidate has fewer embedded images than source")
        elif metrics["mean_rgb_delta"] >= IMAGE_DELTA_FAIL:
            image_status = "fail"
            image_reasons.append(f"mean_rgb_delta exceeds {IMAGE_DELTA_FAIL}")
        elif metrics["mean_rgb_delta"] >= IMAGE_DELTA_WARN:
            image_status = "warn"
            image_reasons.append(f"mean_rgb_delta exceeds {IMAGE_DELTA_WARN}")
        page_metrics.append(
            {
                "page_index": page_index,
                "page_number": page_index + 1,
                "source_image_count": src_img_count,
                "output_image_count": out_img_count,
                "image_color_status": image_status,
                "image_color_reasons": image_reasons,
                **metrics,
            }
        )

    region_metrics: list[dict[str, Any]] = []
    role_gate_items: dict[str, list[dict[str, Any]]] = {}
    for insertion in evidence.get("insertions", []):
        if not isinstance(insertion, dict):
            continue
        page_index = int(insertion.get("page_index") or 0)
        if page_index < 0 or page_index >= page_count:
            continue
        bbox = [float(v) for v in insertion.get("bbox", [0, 0, 0, 0])]
        source_image = source_images[page_index]
        output_image = output_images[page_index]
        box = scaled_box(bbox, zoom, source_image)
        crop_box = expand_box(box, source_image, max(4, int(round(4 * zoom))))
        source_crop = source_image.crop(crop_box)
        output_crop = output_image.crop(crop_box)
        source_bg = edge_dominant_rgb(source_crop)
        output_bg = edge_dominant_rgb(output_crop)
        bg_delta = color_delta(source_bg, output_bg)
        source_inner = source_image.crop(inner_box(box, source_image))
        source_inner_bg = dominant_rgb(source_inner)
        source_residue_delta = color_delta(source_inner_bg, source_bg)
        output_inner = output_image.crop(inner_box(box, output_image))
        output_inner_bg = dominant_rgb(output_inner)
        residue_delta = color_delta(output_inner_bg, output_bg)
        inner_bg_delta = color_delta(source_inner_bg, output_inner_bg)
        text_image_background_rgb = normalized_float_color_to_rgb(insertion.get("image_background_color"))
        text_image_bg_delta = color_delta(text_image_background_rgb, source_inner_bg) if text_image_background_rgb is not None else 0.0
        insertion["source_residue_delta"] = source_residue_delta
        insertion["output_background_rgb"] = output_bg
        insertion["output_inner_background_rgb"] = output_inner_bg
        insertion["inner_background_delta"] = inner_bg_delta
        insertion["text_image_background_rgb"] = text_image_background_rgb
        insertion["text_image_background_delta"] = text_image_bg_delta
        role = region_role(insertion, source_doc[page_index].rect, source_bg)
        source_stats = source_stats_for_insertion(insertion, source_lines)
        source_font_size = source_stats.get("source_median_font_size")
        status, reasons, repair_atoms, font_ratio = status_for_region(
            insertion,
            role,
            bg_delta,
            residue_delta,
            float(source_font_size) if isinstance(source_font_size, (int, float)) else None,
        )
        gate_id = ROLE_RULES.get(role, ROLE_RULES["body"])["gate_id"]
        crop_ref = None
        if crop_dir is not None and status in {"fail", "warn"}:
            safe_region_id = str(insertion.get("region_id") or "region").replace("/", "_").replace("\\", "_")
            crop_path = crop_dir / f"page_{page_index + 1:02d}_{safe_region_id}_{role}.png"
            crop_ref = crop_contact_sheet(source_crop, output_crop, crop_path, role)
        metric = {
            "region_id": insertion.get("region_id"),
            "page_index": page_index,
            "page_number": page_index + 1,
            "region_kind": insertion.get("region_kind"),
            "page_type_guess": insertion.get("page_type_guess"),
            "quality_role": role,
            "gate_id": gate_id,
            "status": status,
            "generation_status": insertion.get("status"),
            "font_size": insertion.get("font_size"),
            "source_median_font_size": source_stats.get("source_median_font_size"),
            "source_font_source": source_stats.get("source_font_source"),
            "source_matched_line_count": source_stats.get("source_matched_line_count"),
            "output_to_source_font_ratio": font_ratio,
            "fail_font_pt": ROLE_RULES.get(role, ROLE_RULES["body"])["fail_font_pt"],
            "warn_font_pt": ROLE_RULES.get(role, ROLE_RULES["body"])["warn_font_pt"],
            "fail_source_ratio": ROLE_RULES.get(role, ROLE_RULES["body"])["fail_source_ratio"],
            "warn_source_ratio": ROLE_RULES.get(role, ROLE_RULES["body"])["warn_source_ratio"],
            "bbox": [round(v, 3) for v in bbox],
            "source_union_bbox": source_stats.get("source_union_bbox"),
            "source_background_rgb": list(source_bg),
            "source_inner_background_rgb": list(source_inner_bg),
            "output_background_rgb": list(output_bg),
            "output_inner_background_rgb": list(output_inner_bg),
            "text_image_background_rgb": list(text_image_background_rgb) if text_image_background_rgb is not None else None,
            "background_delta": bg_delta,
            "source_residue_delta": source_residue_delta,
            "background_residue_delta": residue_delta,
            "inner_background_delta": inner_bg_delta,
            "text_image_background_delta": text_image_bg_delta,
            "reasons": reasons,
            "repair_atoms": repair_atoms,
            "crop_evidence": crop_ref,
            "target_text_sample": str(insertion.get("target_text") or insertion.get("translation_zh") or "")[:120],
        }
        region_metrics.append(metric)
        role_gate_items.setdefault(str(gate_id), []).append(metric)

    redaction_metrics: list[dict[str, Any]] = []
    for redaction in evidence.get("redactions", []):
        if not isinstance(redaction, dict):
            continue
        page_index = int(redaction.get("page_index") or 0)
        if page_index < 0 or page_index >= page_count:
            continue
        bbox = [float(v) for v in redaction.get("bbox", [0, 0, 0, 0])]
        source_image = source_images[page_index]
        output_image = output_images[page_index]
        box = scaled_box(bbox, zoom, source_image)
        pad = max(4, int(round(4 * zoom)))
        crop_box = expand_box(box, source_image, pad)
        source_crop = source_image.crop(crop_box)
        output_crop = output_image.crop(crop_box)
        fill_rgb = normalized_float_color_to_rgb(redaction.get("fill_color")) or (255, 255, 255)
        source_ring_bg = ring_dominant_rgb(source_image, box, pad)
        output_ring_bg = ring_dominant_rgb(output_image, box, pad)
        fill_delta = color_delta(fill_rgb, source_ring_bg)
        output_ring_delta = color_delta(output_ring_bg, source_ring_bg)
        area_pt2 = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        patch_score = round(fill_delta * min(4.0, math.sqrt(area_pt2) / 20.0), 3)
        covered_by_background_cover = any(rect_contains(cover_bbox, bbox) for cover_bbox in cover_rects_by_page.get(page_index, []))
        page_width = float(source_doc[page_index].rect.width)
        wide_line_patch_risk = (
            not covered_by_background_cover
            and (bbox[2] - bbox[0]) >= page_width * 0.45
            and (bbox[3] - bbox[1]) <= 18.0
            and saturation(source_ring_bg) >= 18.0
        )
        status = "pass"
        reasons: list[str] = []
        repair_atoms: list[str] = []
        if wide_line_patch_risk:
            status = "fail"
            reasons.append("wide colored-background line redaction has no region_background_cover and can render as a horizontal band")
            repair_atoms.append("background_residue_fill_resample")
        elif not covered_by_background_cover and fill_delta >= REDACTION_FILL_DELTA_FAIL and patch_score >= REDACTION_PATCH_SCORE_FAIL:
            status = "fail"
            reasons.append(
                f"redaction_fill_delta {fill_delta:.1f} with patch_score {patch_score:.1f} exceeds fail threshold"
            )
            repair_atoms.append("background_residue_fill_resample")
        elif not covered_by_background_cover and fill_delta >= REDACTION_FILL_DELTA_WARN and patch_score >= REDACTION_PATCH_SCORE_WARN:
            status = "warn"
            reasons.append(
                f"redaction_fill_delta {fill_delta:.1f} with patch_score {patch_score:.1f} exceeds warn threshold"
            )
            repair_atoms.append("background_residue_fill_resample")
        crop_ref = None
        if crop_dir is not None and status in {"fail", "warn"}:
            safe_id = str(redaction.get("unit_id") or "redaction").replace("/", "_").replace("\\", "_")
            crop_path = crop_dir / f"page_{page_index + 1:02d}_{safe_id}_redaction_fill.png"
            crop_ref = crop_contact_sheet(source_crop, output_crop, crop_path, "redaction_fill")
        redaction_metrics.append(
            {
                "unit_id": redaction.get("unit_id"),
                "page_index": page_index,
                "page_number": page_index + 1,
                "bbox": [round(v, 3) for v in bbox],
                "status": status,
                "fill_color_rgb": list(fill_rgb),
                "source_ring_background_rgb": list(source_ring_bg),
                "output_ring_background_rgb": list(output_ring_bg),
                "redaction_fill_delta": fill_delta,
                "output_ring_delta": output_ring_delta,
                "patch_score": patch_score,
                "covered_by_background_cover": covered_by_background_cover,
                "wide_line_patch_risk": wide_line_patch_risk,
                "fill_method": (redaction.get("fill_color_provenance") or {}).get("method"),
                "fill_color_provenance": redaction.get("fill_color_provenance"),
                "reasons": reasons,
                "repair_atoms": repair_atoms,
                "crop_evidence": crop_ref,
            }
        )

    background_cover_metrics: list[dict[str, Any]] = []
    for cover in evidence.get("background_covers", []):
        if not isinstance(cover, dict):
            continue
        bbox = cover.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        bbox_f = [float(v) for v in bbox]
        area_pt2 = max(0.0, (bbox_f[2] - bbox_f[0]) * (bbox_f[3] - bbox_f[1]))
        fill_rgb = normalized_float_color_to_rgb(cover.get("fill_color")) or (255, 255, 255)
        draw_mode = str(cover.get("draw_mode") or "solid_vector_fill")
        fill_saturation = saturation(fill_rgb)
        status = "pass"
        reasons: list[str] = []
        repair_atoms: list[str] = []
        if draw_mode == "solid_vector_fill" and fill_saturation >= BACKGROUND_COVER_SOLID_PATCH_SATURATION_FAIL:
            if area_pt2 >= BACKGROUND_COVER_SOLID_PATCH_AREA_FAIL:
                status = "fail"
                reasons.append(
                    "large solid_vector_fill background cover on a saturated/color background can create a visible rectangular block"
                )
                repair_atoms.append("background_residue_fill_resample")
            elif area_pt2 >= BACKGROUND_COVER_SOLID_PATCH_AREA_WARN:
                status = "warn"
                reasons.append(
                    "small solid_vector_fill background cover on a saturated/color background should prefer row_sampled_image_patch"
                )
                repair_atoms.append("background_residue_fill_resample")
        elif draw_mode == "row_sampled_image_patch" and not cover.get("patch_size_px"):
            status = "warn"
            reasons.append("row_sampled_image_patch cover is missing patch_size_px evidence")
            repair_atoms.append("background_residue_fill_resample")
        background_cover_metrics.append(
            {
                "region_id": cover.get("region_id"),
                "page_index": cover.get("page_index"),
                "page_number": int(cover.get("page_index") or 0) + 1,
                "bbox": [round(v, 3) for v in bbox_f],
                "status": status,
                "method": cover.get("method"),
                "draw_mode": draw_mode,
                "fill_color_rgb": list(fill_rgb),
                "fill_saturation": round(fill_saturation, 3),
                "area_pt2": round(area_pt2, 3),
                "patch_size_px": cover.get("patch_size_px"),
                "sample_zoom": cover.get("sample_zoom"),
                "reasons": reasons,
                "repair_atoms": repair_atoms,
            }
        )

    role_gates = []
    for gate_id, items in sorted(role_gate_items.items()):
        failures = [item for item in items if item["status"] == "fail"]
        warnings = [item for item in items if item["status"] == "warn"]
        status = "fail" if failures else "warn" if warnings else "pass"
        role_gates.append(
            {
                "gate_id": gate_id,
                "status": status,
                "blocking": bool(failures),
                "failure_count": len(failures),
                "warning_count": len(warnings),
                "region_count": len(items),
                "sample": (failures or warnings or items)[:8],
            }
        )
    residue_failures = [
        item
        for item in region_metrics
        if item.get("status") == "fail" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ] + [
        item
        for item in redaction_metrics
        if item.get("status") == "fail" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ] + [
        item
        for item in background_cover_metrics
        if item.get("status") == "fail" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ]
    residue_warnings = [
        item
        for item in region_metrics
        if item.get("status") == "warn" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ] + [
        item
        for item in redaction_metrics
        if item.get("status") == "warn" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ] + [
        item
        for item in background_cover_metrics
        if item.get("status") == "warn" and "background_residue_fill_resample" in set(item.get("repair_atoms") or [])
    ]
    role_gates.append(
        {
            "gate_id": "background_residue_artifact",
            "status": "fail" if residue_failures else "warn" if residue_warnings else "pass",
            "blocking": bool(residue_failures),
            "failure_count": len(residue_failures),
            "warning_count": len(residue_warnings),
            "region_count": len(region_metrics),
            "redaction_count": len(redaction_metrics),
            "background_cover_count": len(background_cover_metrics),
            "sample": (residue_failures or residue_warnings)[:8],
            "reason": "detects local redaction/fill rectangles whose interior background no longer matches the surrounding rendered region, including redaction-only bands not covered by translated text and colored-background cover blocks that create new visible rectangles",
        }
    )
    matrix_regions = [
        item
        for item in region_metrics
        if str(item.get("page_type_guess") or "") == "matrix_or_table_diagram"
    ]
    matrix_failures = [
        item
        for item in matrix_regions
        if item.get("generation_status") in FAIL_STATUSES
        or item.get("region_kind") in {"body_flow"}
        or item.get("status") == "fail"
    ]
    matrix_warnings = [
        item
        for item in matrix_regions
        if item.get("status") == "warn" and item not in matrix_failures
    ]
    role_gates.append(
        {
            "gate_id": "matrix_diagram_integrity",
            "status": "fail" if matrix_failures else "warn" if matrix_warnings else "pass",
            "blocking": bool(matrix_failures),
            "failure_count": len(matrix_failures),
            "warning_count": len(matrix_warnings),
            "region_count": len(matrix_regions),
            "sample": (matrix_failures or matrix_warnings)[:8],
            "reason": "matrix/table-diagram pages must preserve two-dimensional cell/label structure and must not route diagram text through body_flow or fallback body insertion",
        }
    )
    baseline_items = [
        item
        for item in region_metrics
        if item.get("source_median_font_size") is not None
        and item.get("source_font_source") == "source_extraction"
    ]
    baseline_missing = [
        item
        for item in region_metrics
        if item.get("source_median_font_size") is None
        or item.get("source_font_source") != "source_extraction"
    ]
    baseline_coverage = round(len(baseline_items) / max(1, len(region_metrics)), 4)
    baseline_reasons: list[str] = []
    if source_extraction is None or not source_extraction.exists():
        baseline_reasons.append("source_extraction_json is required for source-relative visual gates")
    if baseline_coverage < SOURCE_BASELINE_FAIL_COVERAGE:
        baseline_reasons.append(
            f"source baseline coverage {baseline_coverage:.2%} is below fail floor {SOURCE_BASELINE_FAIL_COVERAGE:.2%}"
        )
    baseline_warnings: list[str] = []
    if not baseline_reasons and baseline_coverage < SOURCE_BASELINE_WARN_COVERAGE:
        baseline_warnings.append(
            f"source baseline coverage {baseline_coverage:.2%} is below recommended floor {SOURCE_BASELINE_WARN_COVERAGE:.2%}"
        )
    role_gates.append(
        {
            "gate_id": "source_relative_visual_baseline",
            "status": "fail" if baseline_reasons else "warn" if baseline_warnings else "pass",
            "blocking": bool(baseline_reasons),
            "failure_count": len(baseline_missing) if baseline_reasons else 0,
            "warning_count": len(baseline_missing) if baseline_warnings else 0,
            "region_count": len(region_metrics),
            "source_extraction": None if source_extraction is None else rel(source_extraction),
            "baseline_coverage": baseline_coverage,
            "reasons": baseline_reasons or baseline_warnings,
            "sample": baseline_missing[:8],
        }
    )
    image_failures = [item for item in page_metrics if item["image_color_status"] == "fail"]
    image_warnings = [item for item in page_metrics if item["image_color_status"] == "warn"]
    role_gates.append(
        {
            "gate_id": "image_color_integrity",
            "status": "fail" if image_failures else "warn" if image_warnings else "pass",
            "blocking": bool(image_failures),
            "failure_count": len(image_failures),
            "warning_count": len(image_warnings),
            "region_count": len(page_metrics),
            "sample": (image_failures or image_warnings or page_metrics)[:8],
        }
    )

    result = {
        "tool": "collect_visual_region_metrics",
        "source_pdf": rel(source),
        "output_pdf": rel(output),
        "generation_evidence": rel(generation_evidence),
        "source_extraction": None if source_extraction is None else rel(source_extraction),
        "zoom": zoom,
        "crop_dir": None if crop_dir is None else rel(crop_dir),
        "page_count": page_count,
        "region_count": len(region_metrics),
        "fail_region_count": sum(1 for item in region_metrics if item["status"] == "fail"),
        "warn_region_count": sum(1 for item in region_metrics if item["status"] == "warn"),
        "redaction_count": len(redaction_metrics),
        "fail_redaction_count": sum(1 for item in redaction_metrics if item["status"] == "fail"),
        "warn_redaction_count": sum(1 for item in redaction_metrics if item["status"] == "warn"),
        "page_metrics": page_metrics,
        "region_metrics": region_metrics,
        "redaction_metrics": redaction_metrics,
        "background_cover_metrics": background_cover_metrics,
        "role_gates": role_gates,
    }
    write_json(out, result)
    source_doc.close()
    output_doc.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--generation-evidence", required=True)
    parser.add_argument("--source-extraction", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--crop-dir", default=None)
    parser.add_argument("--zoom", type=float, default=2.0)
    args = parser.parse_args()
    result = collect(
        resolve_workspace_path(args.source),
        resolve_workspace_path(args.output),
        resolve_workspace_path(args.generation_evidence),
        resolve_workspace_path(args.source_extraction) if args.source_extraction else None,
        Path(args.out),
        Path(args.crop_dir) if args.crop_dir else None,
        args.zoom,
    )
    print(args.out)
    print(f"fail_region_count={result['fail_region_count']}; warn_region_count={result['warn_region_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
