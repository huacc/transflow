"""Build run-local text role groups from source extraction and translations.

tool_name: build_role_plan
category: planners
input_contract: source extraction JSON, semantic translations JSON, optional layout policy JSON, output role plan path
output_contract: role_plan JSON with page/group roles, source rects, target text, and evidence
failure_signals: missing extraction, missing semantic translations, empty required units, invalid JSON
fallback: caller records S_FAIL_PROCESS_CONTRACT or keeps legacy layout policy path
anti_overfit_statement: derives roles from current-run bbox/font/color/page statistics and never branches on sample filename, known page number, exact text, fixed coordinates, or document identity
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402
from planners.build_translation_batch_manifest import line_is_translatable as manifest_line_is_translatable  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
VALUE_TOKEN_RE = re.compile(
    r"((?:US\$|HK\$|RMB|USD|HKD|GBP|EUR|\$)?\s*\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|bn|billion|million|m|bps|\u5104|\u4ebf|\u842c|\u4e07|\u7f8e\u5143|\u6e2f\u5143)?)",
    re.IGNORECASE,
)
NOTE_MARKER_RE = re.compile(r"^(note|notes|\u9644\u6ce8|\u8a3b)[:：]?", re.IGNORECASE)


def normalize_language(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "\u4e2d\u6587"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english", "\u82f1\u6587"}:
        return "en"
    return text or default


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return float(ordered[index])


def rect_values(values: list[Any]) -> list[float]:
    rect = [float(v) for v in values]
    if len(rect) != 4:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(v, 3) for v in rect]


def union_rect(rects: list[list[float]]) -> list[float]:
    if not rects:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(min(rect[0] for rect in rects), 3),
        round(min(rect[1] for rect in rects), 3),
        round(max(rect[2] for rect in rects), 3),
        round(max(rect[3] for rect in rects), 3),
    ]


def rect_width(rect: list[float]) -> float:
    return max(0.0, rect[2] - rect[0])


def rect_height(rect: list[float]) -> float:
    return max(0.0, rect[3] - rect[1])


def rgb_from_int(value: Any) -> tuple[int, int, int] | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return ((number >> 16) & 255, (number >> 8) & 255, number & 255)


def color_distance(left: tuple[int, int, int] | None, right: tuple[int, int, int] | None) -> float:
    if left is None or right is None:
        return 0.0
    return math.sqrt(sum((float(left[index]) - float(right[index])) ** 2 for index in range(3)))


def is_saturated_accent(rgb: tuple[int, int, int] | None) -> bool:
    if rgb is None:
        return False
    return max(rgb) - min(rgb) > 45


def is_reddish(value: Any) -> bool:
    rgb = rgb_from_int(value)
    if rgb is None:
        return False
    red, green, blue = rgb
    return red >= max(green, blue) + 32 and red >= 120


def is_accent_color(page_stat: dict[str, Any], value: Any) -> bool:
    try:
        return int(value) in {int(item) for item in page_stat.get("accent_colors", [])}
    except (TypeError, ValueError):
        return False


def has_word_content(text: str) -> bool:
    letters = sum(1 for char in text if char.isalpha())
    digits = sum(1 for char in text if char.isdigit())
    return letters >= 2 and letters + digits >= 3


def source_line_rect(line: dict[str, Any]) -> list[float]:
    return rect_values(line.get("bbox", [0, 0, 0, 0]))


def rect_x_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(rect_width(left), rect_width(right)))


def target_text_field(data: dict[str, Any], target_language: str) -> str:
    explicit = str(data.get("target_text_field") or "").strip()
    if explicit:
        return explicit
    return "translation_zh" if target_language == "zh" else "translation_en"


def get_target_text(unit: dict[str, Any], field: str) -> str:
    return str(
        unit.get(field)
        or unit.get("translation_target_text")
        or unit.get("translation_zh")
        or unit.get("translation_en")
        or ""
    ).strip()


def load_translation_map(translations: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(unit.get("unit_id")): unit for unit in translations.get("units", []) if unit.get("unit_id")}


def page_stats(page: dict[str, Any]) -> dict[str, Any]:
    rect = rect_values(page.get("rect", [0, 0, 1, 1]))
    width = max(1.0, rect[2] - rect[0])
    height = max(1.0, rect[3] - rect[1])
    lines = page.get("text_lines", [])
    fonts = [float(line.get("font_size") or 0.0) for line in lines if float(line.get("font_size") or 0.0) > 0]
    widths = [rect_width(rect_values(line.get("bbox", [0, 0, 0, 0]))) for line in lines]
    widths = [value for value in widths if value > 0]
    y_values = [rect_values(line.get("bbox", [0, 0, 0, 0]))[1] for line in lines]
    color_counts: dict[int, int] = {}
    for line in lines:
        rgb = rgb_from_int(line.get("dominant_text_color", line.get("color")))
        if rgb is None:
            continue
        try:
            color_int = int(line.get("dominant_text_color", line.get("color")))
        except (TypeError, ValueError):
            continue
        color_counts[color_int] = color_counts.get(color_int, 0) + 1
    body_color = max(color_counts.items(), key=lambda item: item[1])[0] if color_counts else None
    body_rgb = rgb_from_int(body_color)
    accent_colors = [
        color
        for color in color_counts
        if body_color is not None
        and color != body_color
        and color_distance(rgb_from_int(color), body_rgb) > 45
        and is_saturated_accent(rgb_from_int(color))
    ]
    return {
        "page_width": width,
        "page_height": height,
        "font_q25": quantile(fonts, 0.25),
        "font_q50": quantile(fonts, 0.50),
        "font_q75": quantile(fonts, 0.75),
        "font_q90": quantile(fonts, 0.90),
        "font_q95": quantile(fonts, 0.95),
        "font_max": max(fonts) if fonts else 0.0,
        "width_q25": quantile(widths, 0.25),
        "width_q50": quantile(widths, 0.50),
        "width_q75": quantile(widths, 0.75),
        "text_y_median": quantile(y_values, 0.50),
        "body_color": body_color,
        "accent_colors": accent_colors,
        "line_count": len(lines),
        "drawing_count": int(page.get("drawing_count") or 0),
        "page_type_guess": str(page.get("page_type_guess") or "unknown"),
    }


def text_len(text: str, language: str) -> int:
    if language == "zh":
        return len(CJK_RE.findall(text))
    return len(re.findall(r"[A-Za-z0-9]+", text))


def classify_line(line: dict[str, Any], page_stat: dict[str, Any], target_text: str, target_language: str) -> tuple[str, str, dict[str, Any]]:
    bbox = rect_values(line.get("bbox", [0, 0, 0, 0]))
    page_width = max(1.0, float(page_stat["page_width"]))
    page_height = max(1.0, float(page_stat["page_height"]))
    width = rect_width(bbox)
    height = rect_height(bbox)
    font_size = float(line.get("font_size") or 0.0)
    y_ratio = bbox[1] / page_height
    width_ratio = width / page_width
    source_text = str(line.get("text") or "")
    page_type = str(page_stat.get("page_type_guess") or "")
    q50 = max(1.0, float(page_stat["font_q50"]))
    q75 = max(q50, float(page_stat["font_q75"]))
    q95 = max(q75, float(page_stat["font_q95"]))
    features = {
        "font_to_q50": round(font_size / q50, 3) if q50 else 0.0,
        "font_to_q75": round(font_size / q75, 3) if q75 else 0.0,
        "width_page_ratio": round(width_ratio, 4),
        "height_page_ratio": round(height / page_height, 4),
        "y_page_ratio": round(y_ratio, 4),
        "page_type_guess": page_type,
        "target_source_length_ratio": round(text_len(target_text, target_language) / max(1, text_len(source_text, "zh" if CJK_RE.search(source_text) else "en")), 3),
        "reddish": is_reddish(line.get("dominant_text_color", line.get("color"))),
    }
    reddish = bool(features["reddish"])
    dense_page = page_type in {"table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"}
    short_text = len(source_text.strip()) <= max(6, round(q50 * 2.0))

    if width_ratio <= 0.045 and height / page_height >= 0.06:
        return "vertical_nav", "narrow tall text slot with current-page geometry", features
    if y_ratio <= 0.045 or bbox[3] / page_height >= 0.965:
        if font_size <= q75 * 1.05:
            return "nav_footer", "top/bottom repeated-band candidate by geometry", features
    if dense_page and (width_ratio <= 0.34 or font_size <= q75 * 1.10):
        return "table_cell", "dense page keeps compact cells separate", features
    if VALUE_TOKEN_RE.search(source_text) and font_size >= max(q75 * 1.18, q95 * 0.86):
        return "metric_value", "generic value token with source-relative large font", features
    if NOTE_MARKER_RE.search(source_text.strip()) or (y_ratio >= 0.58 and font_size <= q50 * 1.05 and width_ratio >= 0.35):
        return "footnote", "note/bottom small text from marker or geometry", features
    if reddish and font_size >= q75 * 1.08:
        return "red_heading", "red text with page-relative heading size", features
    if reddish:
        return "red_note", "red annotation text by color evidence", features
    if font_size >= max(q75 * 1.22, q95 * 0.88) and width_ratio >= 0.10:
        return "heading", "page-relative large font heading", features
    if page_type in {"chart_or_dashboard", "table_or_chart_dense"} and short_text and width_ratio <= 0.22:
        return "legend", "short compact label on chart/table-like page", features
    if width_ratio <= 0.24 and short_text:
        return "compact_panel", "short narrow text slot by width and length", features
    return "body", "default readable text role after other current-page evidence checks", features


def relative_font_rank(size: float, page_stat: dict[str, Any]) -> float:
    return size / max(float(page_stat.get("font_q75") or 1.0), 1.0)


def role_for_lines(lines: list[dict[str, Any]], page_stat: dict[str, Any], target_texts: list[str], target_language: str) -> tuple[str, str, dict[str, Any]]:
    rect = union_rect([source_line_rect(line) for line in lines])
    page_width = max(1.0, float(page_stat["page_width"]))
    page_height = max(1.0, float(page_stat["page_height"]))
    max_size = max((float(line.get("font_size") or 0.0) for line in lines), default=0.0)
    font_rank = relative_font_rank(max_size, page_stat)
    has_symbol = any(bool(line.get("has_symbol_span")) for line in lines)
    has_accent_symbol = any(is_accent_color(page_stat, (line.get("spans") or [{}])[0].get("color")) for line in lines)
    has_accent_text = any(
        is_accent_color(page_stat, line.get("dominant_text_color", line.get("color")))
        or is_accent_color(page_stat, (line.get("spans") or [{}])[0].get("color"))
        for line in lines
    )
    source_text = " ".join(str(line.get("text") or "") for line in lines)
    target_text = " ".join(text for text in target_texts if text)
    width_ratio = rect_width(rect) / page_width
    height_ratio = rect_height(rect) / page_height
    y_ratio = rect[1] / page_height
    q50 = max(1.0, float(page_stat["font_q50"]))
    q75 = max(q50, float(page_stat["font_q75"]))
    q90 = max(q75, float(page_stat.get("font_q90") or q75))
    font_max = max(q90, float(page_stat.get("font_max") or q90))
    before_body_median = rect[1] <= float(page_stat.get("text_y_median") or page_height * 0.50)
    largest_tier = font_max > 0 and max_size >= font_max * 0.78
    has_value_token = bool(VALUE_TOKEN_RE.search(source_text))
    features = {
        "line_count": len(lines),
        "font_to_q75": round(font_rank, 3),
        "width_page_ratio": round(width_ratio, 4),
        "height_page_ratio": round(height_ratio, 4),
        "y_page_ratio": round(y_ratio, 4),
        "has_symbol_span": has_symbol,
        "has_accent_symbol": has_accent_symbol,
        "has_accent_text": has_accent_text,
        "target_source_length_ratio": round(text_len(target_text, target_language) / max(1, text_len(source_text, "zh" if CJK_RE.search(source_text) else "en")), 3),
        "page_type_guess": page_stat.get("page_type_guess"),
    }
    if width_ratio <= 0.045 and height_ratio >= 0.06:
        return "vertical_nav", "narrow tall text slot with current-page geometry", features
    if y_ratio <= 0.045 or rect[3] / page_height >= 0.965:
        if max_size <= q75 * 1.05:
            return "nav_footer", "top/bottom repeated-band candidate by geometry", features
    if has_symbol and has_accent_symbol:
        return "red_note", "accent symbol bullet note in current page palette", features
    if has_value_token and font_rank >= max(1.05, q90 / q75 * 0.85):
        return "metric_value", "generic value token with source-relative large font", features
    if has_accent_text and font_rank >= 1.0 and not has_symbol:
        return "red_heading", "accent text with page-relative heading size", features
    if len(lines) == 1 and largest_tier and before_body_median and has_word_content(source_text):
        return "title", "largest source-relative text before body median", features
    compact_width_limit = max(float(page_stat.get("width_q50") or 0.0), float(page_stat.get("width_q75") or 0.0) * 0.72)
    if rect_width(rect) <= compact_width_limit and len(lines) <= 3 and font_rank <= 1.0:
        return "compact_panel", "compact source block by current-page width distribution", features
    if font_rank >= 1.0 and before_body_median:
        return "section_heading", "source-relative heading tier before body median", features
    return "body", "block-level readable text role after current-page evidence checks", features


def is_table_like_block(lines: list[dict[str, Any]], page_stat: dict[str, Any]) -> bool:
    if len(lines) < 10:
        return False
    page_width = max(1.0, float(page_stat["page_width"]))
    rect = union_rect([source_line_rect(line) for line in lines])
    if rect_width(rect) < page_width * 0.42:
        return False
    numeric_count = sum(1 for line in lines if VALUE_TOKEN_RE.search(str(line.get("text") or "")))
    numeric_ratio = numeric_count / max(1, len(lines))
    short_count = sum(1 for line in lines if rect_width(source_line_rect(line)) <= rect_width(rect) * 0.42)
    short_ratio = short_count / max(1, len(lines))
    bucket = max(4.0, float(page_stat.get("font_q50") or 6.0))
    x_columns = len({round((source_line_rect(line)[0] - rect[0]) / bucket) for line in lines})
    dense_financial_grid = numeric_ratio >= 0.28 and short_ratio >= 0.42 and x_columns >= 3
    large_matrix = len(lines) >= 24 and numeric_ratio >= 0.18 and short_ratio >= 0.35 and x_columns >= 4
    return dense_financial_grid or large_matrix


def is_table_like_source_block(lines: list[dict[str, Any]], page_stat: dict[str, Any]) -> bool:
    if len(lines) < 7:
        return False
    page_width = max(1.0, float(page_stat["page_width"]))
    rect = union_rect([source_line_rect(line) for line in lines])
    width_ratio = rect_width(rect) / page_width
    if width_ratio < 0.22:
        return False
    q75 = max(1.0, float(page_stat.get("font_q75") or 0.0))
    q50 = max(1.0, float(page_stat.get("font_q50") or q75))
    small_font_ratio = sum(1 for line in lines if float(line.get("font_size") or 0.0) <= q75 * 1.16) / max(1, len(lines))
    if small_font_ratio < 0.68:
        return False
    numericish_count = sum(1 for line in lines if re.search(r"[\d%$()—-]", str(line.get("text") or "")))
    numericish_ratio = numericish_count / max(1, len(lines))
    bucket = max(6.0, q50 * 2.4)
    x_bucket_count = len({round((source_line_rect(line)[0] - rect[0]) / bucket) for line in lines})
    y_bucket_count = len({round((source_line_rect(line)[1] - rect[1]) / max(3.0, q50 * 1.05)) for line in lines})
    dense_page = str(page_stat.get("page_type_guess") or "") in {"table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"}
    drawing_dense = int(page_stat.get("drawing_count") or 0) >= max(18, len(lines) // 2)
    table_grid = x_bucket_count >= 3 and y_bucket_count >= 4 and numericish_ratio >= 0.20
    sparse_header_grid = x_bucket_count >= 4 and y_bucket_count >= 2 and numericish_ratio >= 0.28
    line_grid = drawing_dense and dense_page and x_bucket_count >= 3 and y_bucket_count >= 5
    return table_grid or sparse_header_grid or line_grid


def infer_source_table_rects(page: dict[str, Any], page_stat: dict[str, Any]) -> list[list[float]]:
    by_block: dict[Any, list[dict[str, Any]]] = {}
    for line in page.get("text_lines", []):
        by_block.setdefault(line.get("block_id"), []).append(line)
    table_rects: list[list[float]] = []
    for block_lines in by_block.values():
        if is_table_like_source_block(block_lines, page_stat):
            table_rects.append(union_rect([source_line_rect(line) for line in block_lines]))
    if not table_rects:
        return []
    table_rects.sort(key=lambda rect: (rect[1], rect[0]))
    merged: list[list[float]] = []
    for rect in table_rects:
        if not merged:
            merged.append(rect)
            continue
        previous = merged[-1]
        close_vertical = rect[1] - previous[3] <= max(6.0, float(page_stat.get("font_q50") or 6.0) * 1.4)
        related_x = rect_x_overlap_ratio(rect, previous) >= 0.12 or rect_width(union_rect([rect, previous])) <= float(page_stat["page_width"]) * 0.90
        if close_vertical and related_x:
            merged[-1] = union_rect([previous, rect])
        else:
            merged.append(rect)
    return merged


def is_inside_table_rect(lines: list[dict[str, Any]], table_rects: list[list[float]], page_stat: dict[str, Any]) -> bool:
    if not table_rects:
        return False
    rect = union_rect([source_line_rect(line) for line in lines])
    q50 = max(1.0, float(page_stat.get("font_q50") or 6.0))
    for table_rect in table_rects:
        padded = [table_rect[0] - q50 * 0.8, table_rect[1] - q50 * 1.2, table_rect[2] + q50 * 0.8, table_rect[3] + q50 * 1.2]
        vertical_overlap = max(0.0, min(rect[3], padded[3]) - max(rect[1], padded[1]))
        if vertical_overlap / max(1.0, rect_height(rect)) >= 0.55 and rect_x_overlap_ratio(rect, padded) >= 0.10:
            return True
    return False


def is_table_neighbor_block(lines: list[dict[str, Any]], table_rects: list[list[float]], page_stat: dict[str, Any]) -> bool:
    if not table_rects or len(lines) > 6:
        return False
    rect = union_rect([source_line_rect(line) for line in lines])
    max_size = max((float(line.get("font_size") or 0.0) for line in lines), default=0.0)
    if max_size > max(float(page_stat.get("font_q75") or 0.0), float(page_stat.get("font_q50") or 0.0) * 1.2):
        return False
    for table_rect in table_rects:
        close_above = 0 <= table_rect[1] - rect[3] <= max(3.0, float(page_stat.get("font_q50") or 0.0) * 1.2)
        close_inside_top = table_rect[1] - max(3.0, float(page_stat.get("font_q50") or 0.0) * 1.2) <= rect[1] <= table_rect[1] + max(3.0, float(page_stat.get("font_q50") or 0.0) * 1.4)
        if (close_above or close_inside_top) and rect_x_overlap_ratio(rect, table_rect) >= 0.18:
            return True
    return False


def horizontal_row_clusters(lines: list[dict[str, Any]], page_stat: dict[str, Any]) -> list[list[dict[str, Any]]]:
    if len(lines) <= 1:
        return [lines]
    heights = [rect_height(source_line_rect(line)) for line in lines if rect_height(source_line_rect(line)) > 0]
    median_height = quantile(heights, 0.50) or max(float(page_stat.get("font_q50") or 1.0), 1.0)
    block_rect = union_rect([source_line_rect(line) for line in lines])
    same_row_block = rect_height(block_rect) <= max(median_height * 1.9, float(page_stat.get("font_q50") or 1.0) * 1.9)
    if not same_row_block:
        return [lines]
    ordered = sorted(lines, key=lambda item: source_line_rect(item)[0])
    gap_limit = max(float(page_stat.get("width_q25") or 0.0) * 0.45, median_height * 4.0)
    clusters: list[list[dict[str, Any]]] = [[ordered[0]]]
    for line in ordered[1:]:
        previous = clusters[-1][-1]
        current_rect = source_line_rect(line)
        previous_rect = source_line_rect(previous)
        gap = current_rect[0] - previous_rect[2]
        vertical_overlap = min(current_rect[3], previous_rect[3]) - max(current_rect[1], previous_rect[1])
        if gap > gap_limit and vertical_overlap > min(rect_height(current_rect), rect_height(previous_rect)) * 0.35:
            clusters.append([line])
        else:
            clusters[-1].append(line)
    return clusters if len(clusters) > 1 else [lines]


def make_group(
    page_index: int,
    group_index: int,
    lines: list[dict[str, Any]],
    page_stat: dict[str, Any],
    target_text_by_unit: dict[str, str],
    target_language: str,
    force_role: str | None = None,
) -> dict[str, Any]:
    source_rect = union_rect([source_line_rect(line) for line in lines])
    target_texts = [target_text_by_unit.get(str(line.get("line_id")), "") for line in lines]
    role, reason, features = role_for_lines(lines, page_stat, target_texts, target_language)
    if force_role:
        role = force_role
        reason = f"forced by current-page structural evidence: {force_role}"
    source_font_sizes = [float(line.get("font_size") or 0.0) for line in lines]
    text_joiner = "\n" if role in {"table_cell", "legend", "compact_panel", "vertical_nav", "red_note"} else " "
    source_colors = [line.get("dominant_text_color", line.get("color")) for line in lines]
    return {
        "group_id": f"p{page_index}_g{group_index:04d}_{role}",
        "line_ids": [str(line.get("line_id")) for line in lines],
        "role": role,
        "source_rect": source_rect,
        "target_text": text_joiner.join(text for text in target_texts if text).strip(),
        "source_font_size": round(quantile(source_font_sizes, 0.5), 3),
        "source_font_sizes": [round(value, 3) for value in source_font_sizes],
        "source_colors": source_colors,
        "role_evidence": {
            "source_relative_features": features,
            "decision_reason": reason,
            "line_count": len(lines),
            "block_ids": sorted({line.get("block_id") for line in lines}),
            "anti_overfit": "current page block geometry, font quantiles, color palette, page type, and generic value/note patterns only",
        },
    }


def can_merge(prev: dict[str, Any], current: dict[str, Any], page_width: float) -> bool:
    if prev["role"] not in {"body", "footnote", "red_note"} or current["role"] != prev["role"]:
        return False
    prev_line = prev["lines"][-1]
    current_line = current["lines"][0]
    if prev_line.get("block_id") != current_line.get("block_id"):
        return False
    prev_rect = prev["rect"]
    current_rect = current["rect"]
    x_delta = abs(prev_rect[0] - current_rect[0])
    widths = [rect_width(prev_rect), rect_width(current_rect)]
    if x_delta > max(4.0, page_width * 0.035):
        return False
    if min(widths) / max(1.0, max(widths)) < 0.55:
        return False
    vertical_gap = current_rect[1] - prev_rect[3]
    source_size = max(1.0, float(prev_line.get("font_size") or current_line.get("font_size") or 1.0))
    return -source_size * 0.4 <= vertical_gap <= source_size * 1.6


def build_role_plan(extraction_path: Path, translations_path: Path, layout_policy_path: Path | None = None) -> dict[str, Any]:
    extraction = read_json(extraction_path)
    translations = read_json(translations_path)
    layout_policy = read_json(layout_policy_path) if layout_policy_path else {}
    source_language = normalize_language(translations.get("source_language") or layout_policy.get("source_language"), "en")
    target_language = normalize_language(translations.get("target_language") or layout_policy.get("target_language"), "zh")
    target_field = target_text_field(translations, target_language)
    by_id = load_translation_map(translations)
    required_count = 0
    missing_units: list[str] = []
    pages_out: list[dict[str, Any]] = []

    for page in extraction.get("pages", []):
        stat = page_stats(page)
        groups: list[dict[str, Any]] = []
        translatable_lines: list[dict[str, Any]] = []
        target_text_by_unit: dict[str, str] = {}
        for line in page.get("text_lines", []):
            if not manifest_line_is_translatable(line, source_language):
                continue
            required_count += 1
            unit_id = str(line.get("line_id"))
            unit = by_id.get(unit_id)
            if not unit:
                missing_units.append(unit_id)
                continue
            translatable_lines.append(line)
            target_text_by_unit[unit_id] = get_target_text(unit, target_field)

        by_block: dict[Any, list[dict[str, Any]]] = {}
        for line in translatable_lines:
            by_block.setdefault(line.get("block_id"), []).append(line)
        table_rects = infer_source_table_rects(page, stat)
        table_rects.extend([
            union_rect([source_line_rect(line) for line in block_lines])
            for block_lines in by_block.values()
            if is_table_like_block(block_lines, stat)
        ])
        group_index = 0

        def append_group(lines: list[dict[str, Any]], force_role: str | None = None) -> None:
            nonlocal group_index
            if not lines:
                return
            groups.append(make_group(int(page.get("page_index", 0)), group_index, lines, stat, target_text_by_unit, target_language, force_role))
            group_index += 1

        for _block_id, block_lines in sorted(by_block.items(), key=lambda item: min(int(line.get("line_index") or 0) for line in item[1])):
            block_lines = sorted(block_lines, key=lambda line: (int(line.get("line_index") or 0), source_line_rect(line)[1]))
            block_rect = union_rect([source_line_rect(line) for line in block_lines])
            max_font = max((float(line.get("font_size") or 0.0) for line in block_lines), default=0.0)
            if is_inside_table_rect(block_lines, table_rects, stat) or is_table_like_block(block_lines, stat) or is_table_neighbor_block(block_lines, table_rects, stat):
                for line in block_lines:
                    append_group([line], "table_cell")
                continue
            row_clusters = horizontal_row_clusters(block_lines, stat)
            if len(row_clusters) > 1:
                for cluster in row_clusters:
                    append_group(cluster)
                continue
            top_small_cluster = (
                block_rect[3] < float(stat["page_height"]) * 0.08
                and max_font <= float(stat.get("font_q50") or 0.0)
                and rect_height(block_rect) <= max(float(stat.get("font_q50") or 0.0) * 3.0, rect_height(block_rect))
            )
            bottom_small_cluster = (
                block_rect[1] > float(stat["page_height"]) * 0.90
                and max_font <= float(stat.get("font_q50") or 0.0)
                and rect_width(block_rect) >= float(stat.get("width_q50") or 0.0)
            )
            if top_small_cluster or bottom_small_cluster:
                for line in block_lines:
                    append_group([line], "nav_footer")
                continue
            metric_cluster = any(
                VALUE_TOKEN_RE.search(str(line.get("text") or "")) and relative_font_rank(float(line.get("font_size") or 0.0), stat) >= 1.05
                for line in block_lines
            ) and len(block_lines) > 1
            if metric_cluster:
                current: list[dict[str, Any]] = []
                current_kind: str | None = None

                def flush_current() -> None:
                    nonlocal current, current_kind
                    if not current:
                        return
                    append_group(current, "metric_value" if current_kind == "metric" else None)
                    current = []
                    current_kind = None

                for line in block_lines:
                    is_metric_line = bool(VALUE_TOKEN_RE.search(str(line.get("text") or ""))) and relative_font_rank(float(line.get("font_size") or 0.0), stat) >= 1.05
                    kind = "metric" if is_metric_line else "text"
                    if current and (kind != current_kind or kind == "metric"):
                        flush_current()
                    current.append(line)
                    current_kind = kind
                    if kind == "metric":
                        flush_current()
                flush_current()
                continue
            red_note_lines = [
                line
                for line in block_lines
                if bool(line.get("has_symbol_span")) and is_accent_color(stat, (line.get("spans") or [{}])[0].get("color"))
            ]
            if len(red_note_lines) >= 2 and len(block_lines) >= 3:
                current = []
                for line in block_lines:
                    starts_note = bool(line.get("has_symbol_span")) and is_accent_color(stat, (line.get("spans") or [{}])[0].get("color"))
                    if starts_note and current:
                        append_group(current)
                        current = []
                    current.append(line)
                append_group(current)
                continue
            append_group(block_lines)
        pages_out.append(
            {
                "page_index": int(page.get("page_index", 0)),
                "page_rect": rect_values(page.get("rect", [0, 0, 0, 0])),
                "page_stats": {
                    key: round(value, 3) if isinstance(value, float) else value
                    for key, value in stat.items()
                    if key not in {"page_width", "page_height"}
                },
                "groups": groups,
            }
        )

    if required_count <= 0:
        raise ValueError("role plan requires at least one translatable source unit")
    if missing_units:
        raise ValueError(f"semantic translations missing required units for role plan: {missing_units[:20]}")

    return {
        "tool": "build_role_plan",
        "policy_version": "role_plan_v1.current_page_evidence",
        "source_extraction": rel(extraction_path),
        "source_extraction_sha256": sha256_file(extraction_path),
        "semantic_translations": rel(translations_path),
        "semantic_translations_sha256": sha256_file(translations_path),
        "layout_policy": None if layout_policy_path is None else rel(layout_policy_path),
        "layout_policy_sha256": None if layout_policy_path is None else sha256_file(layout_policy_path),
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": target_field,
        "required_unit_count": required_count,
        "group_count": sum(len(page["groups"]) for page in pages_out),
        "anti_overfit": "roles are derived from current extraction statistics and generic patterns; no filename, page number, exact text, fixed coordinate, or reference PDF is used",
        "pages": pages_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--semantic-translations", required=True)
    parser.add_argument("--layout-policy")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    extraction_path = resolve_workspace_path(args.source_extraction)
    translations_path = resolve_workspace_path(args.semantic_translations)
    layout_policy_path = resolve_workspace_path(args.layout_policy) if args.layout_policy else None
    out_path = resolve_workspace_path(args.out)
    write_json(out_path, build_role_plan(extraction_path, translations_path, layout_policy_path))


if __name__ == "__main__":
    main()
