"""Generate a target-language backfill PDF from validated semantic translations.

tool_name: generate_semantic_backfill
category: generators
input_contract: source PDF, source extraction JSON, semantic translations JSON, explicit layout policy JSON, output/evidence paths
output_contract: candidate PDF with source-language text redacted and semantic target-language translations inserted after temporary-page textbox fit probing, plus translations/layout/evidence JSON
failure_signals: missing/invalid semantic translations, font unavailable, insertion/output failure
fallback: mark S_FAIL_CAPABILITY or S_FAIL_QUALITY; never fall back to placeholder text in product_quality
anti_overfit_statement: consumes current-run unit ids and bboxes only and never branches on sample filename, page number, coordinates, exact text, or document identity
"""

from __future__ import annotations

import argparse
import io
import math
import re
import sys
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ascii_tokens, ensure_dir, read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402
from generate_backfill_candidate import choose_font, inflate_rect, sample_fill_detail, text_kind  # noqa: E402
from planners.build_translation_batch_manifest import line_is_translatable as manifest_line_is_translatable  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_OR_DIGIT_RE = re.compile(r"[A-Za-z0-9]")
UNIT_ID_RE = re.compile(r"^(p\d+_b\d+)_l(\d+)$")
NOTE_LABEL_RE = re.compile(r"^(note|notes):$", re.IGNORECASE)
DEFAULT_METRIC_VALUE_RE = re.compile(
    r"([%\uFF05$]|US\$|HK\$|GBP|EUR|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|million|m|bps|\u57fa\u9ede|\u57fa\u70b9)",
    re.IGNORECASE,
)
DEFAULT_METRIC_AMOUNT_RE = re.compile(
    r"((?:US\$|HK\$|\$|GBP|EUR)?\s*\d[\d,]*(?:\.\d+)?\s*(?:[%\uFF05]|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|millions?|m|bps|\u57fa\u9ede|\u57fa\u70b9)?)|"
    r"((?:US\$|HK\$|\$|GBP|EUR)\s*\d)",
    re.IGNORECASE,
)
DENSE_TABLE_PAGE_TYPES = {"table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"}
FORBIDDEN_PROVIDERS = {"", "deterministic_placeholder", "placeholder", "manual_placeholder", None}
FORBIDDEN_TRANSLATION_FRAGMENTS = ("中文回填", "中文标题", "中文标签", "待翻译", "占位", "placeholder", "tbd")
FORBIDDEN_TRANSLATION_PATTERNS = [
    (
        "meta_line_description_zh",
        re.compile(r"^本行(?:说明|列示|报告|描述|展示|表示)"),
    ),
    (
        "meta_line_description_en",
        re.compile(r"^this line (?:reports|describes|lists|shows|states|explains)\b", re.IGNORECASE),
    ),
    (
        "preservation_instruction_leaked_zh",
        re.compile(r"保留(?:数值|数字|标记|符号)"),
    ),
    (
        "preservation_instruction_leaked_en",
        re.compile(r"\bpreserv(?:e|ed|ing)\s+(?:figures|numbers|markers|tokens)\b", re.IGNORECASE),
    ),
    (
        "generic_page_description_zh",
        re.compile(r"当前页的(?:财务报告|治理|业务信息)"),
    ),
    (
        "generic_page_description_en",
        re.compile(r"\bcurrent page'?s? (?:financial report|governance|business information)\b", re.IGNORECASE),
    ),
]


def normalize_language(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "中文"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english", "英文"}:
        return "en"
    return text or default


def target_text_field(data: dict[str, Any]) -> str:
    target_language = normalize_language(data.get("target_language"), "zh")
    explicit = str(data.get("target_text_field") or "").strip()
    if explicit:
        return explicit
    if target_language == "zh":
        return "translation_zh"
    if target_language == "en":
        return "translation_en"
    return "translation_target_text"


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def latin_count(text: str) -> int:
    return sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))


def is_neutral_identifier(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    upper_identifier_labels = {"CUSIP", "ISIN", "SEDOL", "RIC", "LEI"}
    if stripped.upper() in upper_identifier_labels:
        return True
    if re.fullmatch(r"[A-Z]", stripped):
        return True
    if re.fullmatch(r"[A-Z]{1,6}[\dA-Z.:-]{2,}", stripped):
        return True
    if re.fullmatch(r"\d+[A-Z.:-]+[A-Z\d.:-]*", stripped):
        return True
    return False


def line_is_translatable(line: dict[str, Any], source_language: str) -> bool:
    return manifest_line_is_translatable(line, source_language)


def line_is_already_target_language(line: dict[str, Any], source_language: str, target_language: str) -> bool:
    """Visible target-language spans still need redraw if a reflow region may cover them."""
    text = str(line.get("text", "")).strip()
    if not text or line_is_translatable(line, source_language):
        return False
    if target_language == "zh":
        return bool(CJK_RE.search(text))
    if target_language == "en":
        return bool(line.get("ascii_tokens")) and not CJK_RE.search(text)
    return False


def get_target_text(item: dict[str, Any], field: str) -> str:
    return str(item.get(field) or item.get("translation_target_text") or item.get("translation_zh") or item.get("translation_en") or "").strip()


def load_semantic_units(translations_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    data = read_json(translations_path)
    provider = data.get("translation_provider")
    if provider in FORBIDDEN_PROVIDERS:
        raise ValueError("semantic translations require a non-placeholder translation_provider")
    if data.get("translation_quality") != "semantic_translation":
        raise ValueError("translation_quality must be semantic_translation")
    if data.get("semantic_coverage") != "full_semantic_translation":
        raise ValueError("semantic_coverage must be full_semantic_translation")
    data["source_language"] = normalize_language(data.get("source_language"), "en")
    data["target_language"] = normalize_language(data.get("target_language"), "zh")
    data["target_text_field"] = target_text_field(data)
    by_id: dict[str, dict[str, Any]] = {}
    for item in data.get("units", []):
        if not isinstance(item, dict):
            continue
        unit_id = item.get("unit_id")
        if unit_id:
            by_id[str(unit_id)] = item
    return data, by_id


def reject_bad_translation(unit_id: str, source_text: str, translation_text: str, target_language: str, target_field: str) -> None:
    lowered = translation_text.strip().lower()
    if not lowered:
        raise ValueError(f"{unit_id}: missing {target_field}")
    if target_language == "zh" and not CJK_RE.search(translation_text):
        raise ValueError(f"{unit_id}: {target_field} has no CJK characters")
    if target_language == "en":
        if not ASCII_OR_DIGIT_RE.search(translation_text):
            raise ValueError(f"{unit_id}: {target_field} has no ASCII letters or digits")
        if CJK_RE.search(translation_text):
            raise ValueError(f"{unit_id}: {target_field} has CJK residue")
    if lowered == source_text.strip().lower():
        raise ValueError(f"{unit_id}: translation equals source text")
    if any(fragment in lowered for fragment in FORBIDDEN_TRANSLATION_FRAGMENTS):
        raise ValueError(f"{unit_id}: placeholder translation text is forbidden")
    for reason, pattern in FORBIDDEN_TRANSLATION_PATTERNS:
        if pattern.search(translation_text.strip()):
            raise ValueError(f"{unit_id}: placeholder/meta translation text is forbidden: {reason}")


def block_key(unit_id: str) -> str:
    match = UNIT_ID_RE.match(unit_id)
    return match.group(1) if match else unit_id


def unit_line_index(unit_id: str) -> int | None:
    match = UNIT_ID_RE.match(unit_id)
    return int(match.group(2)) if match else None


def item_has_symbol_span(item: dict[str, Any]) -> bool:
    if bool(item.get("has_symbol_span")):
        return True
    spans = item.get("source_spans")
    if isinstance(spans, list):
        return any(bool(span.get("is_symbol_font")) and int(span.get("char_count") or 0) > 0 for span in spans if isinstance(span, dict))
    return False


def source_explanatory_list_block_applies(
    items: list[dict[str, Any]],
    page_rect: fitz.Rect,
    policy: dict[str, Any],
    page_context: dict[str, Any] | None = None,
) -> bool:
    if str(policy.get("target_language") or "") != "en" or len(items) < 2:
        return False
    if len({item.get("block_id") for item in items}) > 1:
        return False
    marker_count = sum(1 for item in items if item_has_symbol_span(item))
    if marker_count <= 0:
        return False
    rects = [item["rect"] for item in items]
    rect = union_rect(rects)
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    if rect.width / page_width > 0.42 or rect.height / page_height > 0.22:
        return False
    sizes = [float(item.get("font_size") or 0.0) for item in items]
    page_stats = page_context_font_stats(page_context)
    font_ceiling = font_stat_value(page_stats, "q50", median_float(sizes)) * 1.15
    if median_float(sizes) > font_ceiling:
        return False
    x0_values = [float(item["rect"].x0) for item in items]
    if max(x0_values) - min(x0_values) < max(2.0, median_float(sizes) * 0.8):
        return False
    text = " ".join(str(item.get("target_text") or "").strip() for item in items)
    return non_numeric_wrappable_text(text, 4)


def median_float(values: list[float]) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def quantile_float(values: list[float], q: float) -> float:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * max(0.0, min(1.0, q))
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[int(pos)]
    ratio = pos - lower
    return ordered[lower] * (1 - ratio) + ordered[upper] * ratio


def font_stats(values: list[float]) -> dict[str, float]:
    clean = [float(value) for value in values if math.isfinite(float(value)) and float(value) > 0]
    if not clean:
        return {}
    return {
        "q25": quantile_float(clean, 0.25),
        "q50": quantile_float(clean, 0.50),
        "q75": quantile_float(clean, 0.75),
        "q90": quantile_float(clean, 0.90),
        "q95": quantile_float(clean, 0.95),
        "max": max(clean),
    }


def page_context_font_stats(page_context: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(page_context, dict):
        return {}
    stats = page_context.get("font_stats")
    if not isinstance(stats, dict):
        return {}
    output: dict[str, float] = {}
    for key, value in stats.items():
        try:
            output[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return output


def font_stat_value(stats: dict[str, float], key: Any, default: float) -> float:
    name = str(key or "").strip().lower()
    if not name:
        return default
    return float(stats.get(name, default))


def relative_source_threshold(rule: dict[str, Any], page_context: dict[str, Any] | None, fallback_key: str, fallback: float) -> float:
    stats = page_context_font_stats(page_context)
    quantile_name = rule.get("source_size_page_quantile")
    if quantile_name is not None and stats:
        base = font_stat_value(stats, quantile_name, fallback)
        ratio = float(rule.get("min_source_to_page_quantile_ratio", 1.0))
        return base * ratio
    if fallback_key in rule:
        return float(rule[fallback_key])
    return fallback


def metric_value_pattern(rule: dict[str, Any]) -> re.Pattern[str]:
    pattern = str(rule.get("value_token_regex") or rule.get("value_regex") or "").strip()
    if not pattern:
        return DEFAULT_METRIC_VALUE_RE
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return DEFAULT_METRIC_VALUE_RE


def metric_amount_pattern(rule: dict[str, Any]) -> re.Pattern[str]:
    pattern = str(rule.get("value_amount_regex") or "").strip()
    if not pattern:
        return DEFAULT_METRIC_AMOUNT_RE
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return DEFAULT_METRIC_AMOUNT_RE


def source_text_for_items(items: list[dict[str, Any]]) -> str:
    return " ".join(str(item.get("source_text") or "").strip() for item in items if str(item.get("source_text") or "").strip())


def compact_metric_value_text(text: str, target_language: str) -> str:
    """Compact only generic numeric/unit notation for metric callouts."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return compact
    if normalize_language(target_language, target_language) == "en":
        replacements = [
            (r"\b(US\$|HK\$)\s+(\d)", r"\1\2"),
            (r"(?<![A-Z])\$\s+(\d)", r"$\1"),
            (r"(\d+(?:\.\d+)?)\s+per\s+cent\b", r"\1%"),
            (r"(\d+(?:\.\d+)?)\s+percentage\s+points?\b", r"\1 pps"),
            (r"(\d+(?:\.\d+)?)\s+basis\s+points?\b", r"\1 bps"),
            (r"\b(billions|billion)\b", "bn"),
            (r"\b(millions|million)\b", "m"),
        ]
        for pattern, replacement in replacements:
            compact = re.sub(pattern, replacement, compact, flags=re.IGNORECASE)
    else:
        compact = re.sub(r"\s+", "", compact)
    return compact.strip()


def union_rect(rects: list[fitz.Rect]) -> fitz.Rect:
    rect = fitz.Rect(rects[0])
    for item in rects[1:]:
        rect.include_rect(item)
    return rect


def rect_is_usable(rect: fitz.Rect) -> bool:
    values = [rect.x0, rect.y0, rect.x1, rect.y1, rect.width, rect.height]
    return all(math.isfinite(float(value)) for value in values) and rect.is_valid and not rect.is_empty and rect.width > 0.25 and rect.height > 0.25


def clamp_rect_to_page(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    clamped = fitz.Rect(rect)
    clamped.x0 = max(page_rect.x0, min(page_rect.x1, clamped.x0))
    clamped.x1 = max(page_rect.x0, min(page_rect.x1, clamped.x1))
    clamped.y0 = max(page_rect.y0, min(page_rect.y1, clamped.y0))
    clamped.y1 = max(page_rect.y0, min(page_rect.y1, clamped.y1))
    return clamped


def sanitize_region_rect(region: dict[str, Any], page_rect: fitz.Rect) -> None:
    rect = fitz.Rect(region.get("rect", [0, 0, 0, 0]))
    if rect_is_usable(rect):
        region["rect"] = clamp_rect_to_page(rect, page_rect)
        return
    candidate_rects: list[fitz.Rect] = []
    source_anchor = region.get("source_anchor_bbox")
    if isinstance(source_anchor, list) and len(source_anchor) == 4:
        candidate_rects.append(fitz.Rect(source_anchor))
    for item in region.get("items", []):
        item_rect = item.get("rect")
        if isinstance(item_rect, fitz.Rect) and rect_is_usable(item_rect):
            candidate_rects.append(fitz.Rect(item_rect))
    if candidate_rects:
        repaired = union_rect(candidate_rects)
    else:
        repaired = fitz.Rect(page_rect.x0 + 1, page_rect.y0 + 1, min(page_rect.x0 + 60, page_rect.x1 - 1), min(page_rect.y0 + 18, page_rect.y1 - 1))
    source_size = float(region.get("source_size") or 6.0)
    x_pad = max(1.0, source_size * 0.25)
    y_pad = max(1.0, source_size * 0.25)
    repaired.x0 = max(page_rect.x0, repaired.x0 - x_pad)
    repaired.x1 = min(page_rect.x1, repaired.x1 + x_pad)
    repaired.y0 = max(page_rect.y0, repaired.y0 - y_pad)
    repaired.y1 = min(page_rect.y1, repaired.y1 + y_pad)
    min_width = max(6.0, source_size * 2.0)
    min_height = max(4.0, source_size * 1.2)
    if repaired.width < min_width:
        repaired.x1 = min(page_rect.x1, repaired.x0 + min_width)
    if repaired.height < min_height:
        repaired.y1 = min(page_rect.y1, repaired.y0 + min_height)
    repaired = clamp_rect_to_page(repaired, page_rect)
    region["rect"] = repaired
    region["rect_repair"] = {
        "reason": "empty_or_nonfinite_region_rect",
        "repaired_bbox": [round(float(v), 3) for v in repaired],
    }


def decorative_numeric_merge_repairs(lines: list[dict[str, Any]], page_rect: fitz.Rect) -> dict[str, dict[str, Any]]:
    repairs: dict[str, dict[str, Any]] = {}
    by_block: dict[Any, list[dict[str, Any]]] = {}
    sequence_by_id: dict[int, int] = {}
    page_normal_lines: list[tuple[int, dict[str, Any], fitz.Rect]] = []
    for sequence, line in enumerate(lines):
        sequence_by_id[id(line)] = sequence
        by_block.setdefault(line.get("block_id"), []).append(line)
        try:
            rect = fitz.Rect([float(v) for v in line.get("bbox", [])])
        except Exception:
            continue
        font_size = float(line.get("font_size") or 6.0)
        if rect.height <= max(font_size * 4.0, page_rect.height * 0.08):
            page_normal_lines.append((sequence, line, rect))
    for block_lines in by_block.values():
        normal_lines = []
        for line in block_lines:
            try:
                rect = fitz.Rect([float(v) for v in line.get("bbox", [])])
            except Exception:
                continue
            font_size = float(line.get("font_size") or 6.0)
            if rect.height <= max(font_size * 4.0, page_rect.height * 0.08):
                normal_lines.append((line, rect))
        if not normal_lines:
            continue
        heights = sorted(rect.height for _, rect in normal_lines)
        normal_height = heights[len(heights) // 2]
        for line in block_lines:
            text = str(line.get("text", "")).strip()
            match = re.match(r"^(.+?)(\d)$", text)
            if not match:
                continue
            try:
                rect = fitz.Rect([float(v) for v in line.get("bbox", [])])
            except Exception:
                continue
            font_size = float(line.get("font_size") or 6.0)
            if rect.height < max(font_size * 10.0, page_rect.height * 0.18):
                continue
            same_column = [
                (other, other_rect)
                for other, other_rect in normal_lines
                if abs(other_rect.x0 - rect.x0) <= max(4.0, font_size)
                and int(other.get("line_index") or -1) < int(line.get("line_index") or 0)
            ]
            if not same_column:
                current_sequence = sequence_by_id.get(id(line), 0)
                same_column = [
                    (other, other_rect)
                    for sequence, other, other_rect in page_normal_lines
                    if sequence < current_sequence and abs(other_rect.x0 - rect.x0) <= max(6.0, font_size * 1.5)
                ]
            if not same_column:
                continue
            previous, previous_rect = max(same_column, key=lambda item: sequence_by_id.get(id(item[0]), int(item[0].get("line_index") or 0)))
            sorted_same = sorted(same_column, key=lambda item: float(item[1].y0))
            gaps = []
            for (_, left_rect), (_, right_rect) in zip(sorted_same, sorted_same[1:]):
                gap = right_rect.y0 - left_rect.y0
                if 1.0 <= gap <= font_size * 3.0:
                    gaps.append(gap)
            line_step = sorted(gaps)[len(gaps) // 2] if gaps else max(normal_height, font_size * 1.35)
            y0 = min(page_rect.y1 - normal_height, previous_rect.y0 + line_step)
            base_text = match.group(1).strip()
            same_widths = [other_rect.width for _, other_rect in same_column if other_rect.width > 1.0]
            width_hint = sorted(same_widths)[len(same_widths) // 2] if same_widths else font_size * max(2, len(base_text))
            estimated_width = max(width_hint * 1.35, font_size * max(2.0, len(base_text) * 0.95))
            repaired = fitz.Rect(rect.x0, y0, min(page_rect.x1, rect.x0 + estimated_width), y0 + normal_height)
            if rect_is_usable(repaired):
                repairs[str(line.get("line_id"))] = {
                    "bbox": [round(float(v), 3) for v in repaired],
                    "original_bbox": [round(float(v), 3) for v in rect],
                    "trim_trailing_digit": match.group(2),
                    "reason": "line bbox merged a trailing decorative page or section numeral; repaired from same-block column rhythm",
                }
    return repairs


def trim_trailing_decorative_digit(text: str, digit: str) -> str:
    trimmed = re.sub(rf"\s*{re.escape(digit)}\s*$", "", text).strip()
    return trimmed or text


def color_int_to_rgb(color: Any) -> tuple[float, float, float]:
    if not isinstance(color, int):
        return (0.05, 0.05, 0.05)
    return (
        round(((color >> 16) & 255) / 255.0, 4),
        round(((color >> 8) & 255) / 255.0, 4),
        round((color & 255) / 255.0, 4),
    )


def dominant_text_color(items: list[dict[str, Any]]) -> tuple[float, float, float]:
    counts: dict[tuple[float, float, float], int] = {}
    for item in items:
        color = item.get("text_color", (0.05, 0.05, 0.05))
        counts[color] = counts.get(color, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0] if counts else (0.05, 0.05, 0.05)


def dominant_fill_color(items: list[dict[str, Any]]) -> tuple[float, float, float]:
    counts: dict[tuple[float, float, float], int] = {}
    for item in items:
        color = item.get("fill_color")
        if not isinstance(color, (list, tuple)) or len(color) < 3:
            continue
        key = tuple(round(float(channel), 4) for channel in color[:3])
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0] if counts else (1.0, 1.0, 1.0)


def require_mapping(data: Any, name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"layout policy section must be an object: {name}")
    return data


def policy_section(policy: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = policy
    path = ".".join(keys)
    for key in keys:
        current = require_mapping(current, path).get(key)
    return require_mapping(current, path)


def policy_float(section: dict[str, Any], key: str) -> float:
    if key not in section:
        raise ValueError(f"layout policy missing numeric key: {key}")
    return float(section[key])


def policy_int(section: dict[str, Any], key: str) -> int:
    if key not in section:
        raise ValueError(f"layout policy missing integer key: {key}")
    return int(section[key])


def min_insert_point_size(profile: dict[str, Any], fallback: dict[str, Any], source_size: float | None = None, page_font_stats: dict[str, float] | None = None) -> float:
    if "min_insert_pt" in profile:
        return float(profile["min_insert_pt"])
    if source_size is not None and "min_insert_source_ratio" in profile:
        return max(0.1, float(source_size) * float(profile["min_insert_source_ratio"]))
    if page_font_stats and "min_insert_page_quantile" in profile:
        return max(
            0.1,
            font_stat_value(page_font_stats, profile.get("min_insert_page_quantile"), policy_float(fallback, "min_insert_pt"))
            * float(profile.get("min_insert_page_quantile_scale", 1.0)),
        )
    return policy_float(fallback, "min_insert_pt")


def resolve_base_font_size(source_size: float, profile: dict[str, Any], fallback: dict[str, Any], page_font_stats: dict[str, float]) -> float:
    if str(profile.get("sizing_mode") or "") == "source_relative":
        source_scale = float(profile.get("source_scale", 1.0))
        desired = source_size * source_scale
        floor = source_size * float(profile.get("min_source_ratio", 0.0))
        floor_quantile = profile.get("page_quantile_floor")
        if floor_quantile is not None:
            floor = max(
                floor,
                font_stat_value(page_font_stats, floor_quantile, source_size)
                * float(profile.get("page_quantile_floor_scale", 1.0)),
            )
        ceiling = source_size * float(profile.get("max_source_ratio", max(source_scale, 1.0)))
        ceiling_quantile = profile.get("page_quantile_ceiling")
        if ceiling_quantile is not None:
            ceiling = min(
                ceiling,
                font_stat_value(page_font_stats, ceiling_quantile, ceiling)
                * float(profile.get("page_quantile_ceiling_scale", 1.0)),
            )
        if ceiling < floor:
            ceiling = max(floor, desired)
        return max(floor, min(ceiling, desired))
    return max(
        policy_float(profile, "min_pt"),
        min(policy_float(profile, "max_pt"), source_size * policy_float(profile, "source_scale")),
    )


def fallback_forbidden(region_kind: str, fallback: dict[str, Any]) -> bool:
    forbidden = {str(value) for value in fallback.get("forbid_region_kinds", [])}
    return region_kind in forbidden


def load_layout_policy(policy_path: Path) -> dict[str, Any]:
    policy = read_json(policy_path)
    if not isinstance(policy, dict):
        raise ValueError("layout policy must be a JSON object")
    for section in ["classification_rules", "region_expansion", "reflow", "font_profiles", "fallback"]:
        require_mapping(policy.get(section), section)
    return policy


def expand_region_rect(rect: fitz.Rect, page_rect: fitz.Rect, source_size: float, region_kind: str, policy: dict[str, Any]) -> fitz.Rect:
    expansion = policy_section(policy, "region_expansion")
    profile = require_mapping(expansion.get(region_kind) or expansion.get("default"), f"region_expansion.{region_kind}")
    x_pad = policy_float(profile, "x_pad_pt")
    y_pad = max(policy_float(profile, "y_pad_min_pt"), source_size * policy_float(profile, "y_pad_source_size_ratio"))
    expanded = fitz.Rect(rect)
    expanded.x0 = max(page_rect.x0, expanded.x0 - x_pad)
    expanded.x1 = min(page_rect.x1, expanded.x1 + x_pad)
    expanded.y0 = max(page_rect.y0, expanded.y0 - y_pad)
    expanded.y1 = min(page_rect.y1, expanded.y1 + y_pad)
    return expanded


def page_type_guess(page_context: dict[str, Any] | None) -> str:
    if not isinstance(page_context, dict):
        return ""
    return str(page_context.get("page_type_guess") or "")


def has_same_row_neighbor(rect: fitz.Rect, page_context: dict[str, Any] | None) -> bool:
    if not isinstance(page_context, dict):
        return False
    geometries = page_context.get("line_geometries")
    if not isinstance(geometries, list):
        return False
    for geometry in geometries:
        if not isinstance(geometry, dict):
            continue
        bbox = geometry.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        other = fitz.Rect([float(v) for v in bbox])
        if abs(float(other.x0) - float(rect.x0)) < 1.0 and abs(float(other.y0) - float(rect.y0)) < 1.0:
            continue
        if vertical_overlap_ratio(rect, other) < 0.55:
            continue
        separated_horizontally = other.x0 > rect.x1 + 8.0 or rect.x0 > other.x1 + 8.0
        if separated_horizontally:
            return True
    return False


def region_kind(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any], page_context: dict[str, Any] | None = None) -> str:
    rects = [item["rect"] for item in items]
    sizes = [float(item.get("font_size") or 0.0) for item in items]
    rect = union_rect(rects)
    median_size = median_float(sizes)
    rules = policy_section(policy, "classification_rules")
    table_note = require_mapping(rules.get("table_note"), "classification_rules.table_note")
    first_text = str(items[0].get("source_text", "")).strip()
    has_note_marker = bool(NOTE_LABEL_RE.match(first_text))
    y_ratio = float(rect.y0) / max(1.0, float(page_rect.height))
    dense_page_type = page_type_guess(page_context) in DENSE_TABLE_PAGE_TYPES
    wide_note_block = (
        len(items) >= policy_int(table_note, "min_line_count")
        and rect.width >= page_rect.width * policy_float(table_note, "min_region_width_page_ratio")
        and median_size <= policy_float(table_note, "max_median_font_size")
        and rect.y0 >= page_rect.height * policy_float(table_note, "min_y_ratio")
    )
    dense_page_min_y_ratio = float(table_note.get("dense_page_min_y_ratio", 0.72))
    dense_page_bottom_note = dense_page_type and wide_note_block and y_ratio >= dense_page_min_y_ratio
    if has_note_marker or (wide_note_block and not dense_page_type) or dense_page_bottom_note:
        return "table_note"
    event_card = rules.get("event_card")
    if isinstance(event_card, dict):
        page_types = {str(value) for value in event_card.get("page_type_guesses", [])}
        y_ratio = float(rect.y0) / max(1.0, float(page_rect.height))
        if (
            page_type_guess(page_context) in page_types
            and len(items) <= policy_int(event_card, "max_line_count")
            and rect.width <= float(page_rect.width) * policy_float(event_card, "max_region_width_page_ratio")
            and y_ratio >= policy_float(event_card, "min_y_ratio")
            and y_ratio <= policy_float(event_card, "max_y_ratio")
        ):
            return "event_card"
    vertical_nav = require_mapping(rules.get("vertical_nav"), "classification_rules.vertical_nav")
    if rect.width < policy_float(vertical_nav, "max_region_width_pt") and rect.height > rect.width * policy_float(vertical_nav, "min_height_width_ratio"):
        return "vertical_nav"
    table_cell = rules.get("table_cell")
    if isinstance(table_cell, dict):
        page_types = {str(value) for value in table_cell.get("page_type_guesses", [])}
        if (
            page_type_guess(page_context) in page_types
            and len(items) <= policy_int(table_cell, "max_line_count")
            and rect.width <= policy_float(table_cell, "max_region_width_pt")
            and median_size <= policy_float(table_cell, "max_median_font_size")
        ):
            return "table_cell"
    footnote = require_mapping(rules.get("footnote"), "classification_rules.footnote")
    footnote_min_y_ratio = policy_float(footnote, "min_y_ratio")
    if dense_page_type:
        footnote_min_y_ratio = float(footnote.get("dense_page_min_y_ratio", max(0.68, footnote_min_y_ratio)))
    if median_size <= policy_float(footnote, "max_median_font_size") and rect.y0 >= page_rect.height * footnote_min_y_ratio:
        return "footnote"
    legend = require_mapping(rules.get("legend"), "classification_rules.legend")
    if len(items) >= policy_int(legend, "min_line_count") and rect.width <= policy_float(legend, "max_region_width_pt") and median_float([r.width for r in rects]) <= policy_float(legend, "max_median_line_width_pt"):
        return "legend"
    heading = require_mapping(rules.get("heading"), "classification_rules.heading")
    heading_threshold = relative_source_threshold(heading, page_context, "min_median_font_size", 9999.0)
    heading_top_y = float(heading.get("top_y_ratio_for_priority", 1.0))
    if dense_page_type and median_size >= heading_threshold and has_same_row_neighbor(rect, page_context):
        return "table_cell"
    if median_size >= heading_threshold and y_ratio <= heading_top_y:
        return "heading"
    metric_value = rules.get("metric_value")
    if isinstance(metric_value, dict) and metric_value.get("enabled", True):
        metric_threshold = relative_source_threshold(metric_value, page_context, "min_median_font_size", 9999.0)
        source_text = source_text_for_items(items)
        if (
            median_size >= metric_threshold
            and metric_value_pattern(metric_value).search(source_text)
            and metric_amount_pattern(metric_value).search(source_text)
        ):
            return "metric_value"
    compact_label = require_mapping(rules.get("compact_label"), "classification_rules.compact_label")
    if rect.width < policy_float(compact_label, "max_region_width_pt") or median_float([r.width for r in rects]) < policy_float(compact_label, "max_median_line_width_pt"):
        return "compact_label"
    short_label = require_mapping(rules.get("short_label"), "classification_rules.short_label")
    if len(items) <= policy_int(short_label, "max_line_count") and rect.width <= policy_float(short_label, "max_region_width_pt") and median_size >= policy_float(short_label, "min_median_font_size"):
        return "short_label"
    if median_size >= heading_threshold:
        return "heading"
    return "body"


def should_reflow_region(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any], page_context: dict[str, Any] | None = None) -> bool:
    reflow = policy_section(policy, "reflow")
    if len(items) < policy_int(reflow, "min_items_for_reflow"):
        return False
    kind = region_kind(items, page_rect, policy, page_context)
    rect = union_rect([item["rect"] for item in items])
    sizes = [float(item.get("font_size") or 0.0) for item in items]
    heading = require_mapping(policy_section(policy, "classification_rules").get("heading"), "classification_rules.heading")
    heading_threshold = relative_source_threshold(heading, page_context, "min_median_font_size", median_float(sizes))
    large_top_heading = (
        kind == "heading"
        and median_float(sizes) >= heading_threshold
        and float(rect.y0) / max(1.0, float(page_rect.height)) <= 0.35
    )
    top_lead_body = (
        kind == "body"
        and median_float(sizes) >= 10.0
        and float(rect.y0) / max(1.0, float(page_rect.height)) <= 0.45
        and rect.width >= float(page_rect.width) * 0.25
    )
    if local_constrained_slot_flow_applies(items, kind, page_rect, policy, page_context):
        return True
    if page_type_guess(page_context) in DENSE_TABLE_PAGE_TYPES and kind not in {"table_note", "footnote"} and not large_top_heading and not top_lead_body:
        return False
    if kind in set(reflow.get("preserve_line_kinds", [])):
        return False
    return kind in set(reflow.get("reflow_kinds", []))


def explicit_layout_text(item: dict[str, Any], kind: str, policy: dict[str, Any]) -> str:
    target_language = str(policy.get("target_language") or "zh")
    variant_key = f"{kind}_{target_language}"
    variants_by_kind = policy.get("layout_text_variants", {})
    variant_keys = variants_by_kind.get(variant_key) or variants_by_kind.get(kind, [])
    variants = item.get("layout_variants")
    if isinstance(variants, dict):
        for key in variant_keys:
            value = variants.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                return compact_metric_value_text(text, target_language) if kind == "metric_value" else text
    text = str(item["target_text"]).strip()
    return compact_metric_value_text(text, target_language) if kind == "metric_value" else text


def reject_bad_layout_text(unit_id: str, layout_text: str, policy: dict[str, Any]) -> None:
    target_language = str(policy.get("target_language") or "zh")
    if not layout_text:
        raise ValueError(f"{unit_id}: empty layout text")
    if target_language == "zh" and not CJK_RE.search(layout_text):
        raise ValueError(f"{unit_id}: layout text has no CJK characters")
    if target_language == "en" and CJK_RE.search(layout_text):
        raise ValueError(f"{unit_id}: layout text has CJK residue")
    if any(fragment in layout_text.lower() for fragment in FORBIDDEN_TRANSLATION_FRAGMENTS):
        raise ValueError(f"{unit_id}: placeholder layout text is forbidden")
    for reason, pattern in FORBIDDEN_TRANSLATION_PATTERNS:
        if pattern.search(layout_text.strip()):
            raise ValueError(f"{unit_id}: placeholder/meta layout text is forbidden: {reason}")
    residue = ascii_tokens(layout_text) if target_language == "zh" else []
    if target_language == "zh" and residue:
        raise ValueError(f"{unit_id}: layout text contains ASCII residue: {','.join(residue[:8])}")


def join_translation_fragments(items: list[dict[str, Any]], kind: str, policy: dict[str, Any]) -> str:
    text = ""
    target_language = str(policy.get("target_language") or "zh")
    separator = " " if target_language == "en" else ""
    for item in items:
        fragment = explicit_layout_text(item, kind, policy)
        if fragment != str(item["target_text"]).strip():
            reject_bad_layout_text(str(item["unit_id"]), fragment, policy)
        if not fragment:
            continue
        if not text:
            text = fragment
            continue
        text += separator + fragment
    return re.sub(r"\s+", " ", text).strip()

def split_heading_prefix(items: list[dict[str, Any]], policy: dict[str, Any]) -> list[list[dict[str, Any]]]:
    if len(items) <= 1:
        return [items]
    separator_policy = policy.get("source_separator_policy", {})
    split_on_gap = not isinstance(separator_policy, dict) or bool(separator_policy.get("split_on_untranslated_visible_line_gap", True))
    max_gap = int(separator_policy.get("max_line_index_gap_without_split", 1)) if isinstance(separator_policy, dict) else 1
    parts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_index: int | None = None
    previous_block: Any = None
    previous_item: dict[str, Any] | None = None
    for item in items:
        current_index = item.get("line_index")
        if current_index is None:
            current_index = unit_line_index(str(item.get("unit_id", "")))
        current_block = item.get("block_id") if item.get("block_id") is not None else block_key(str(item.get("unit_id", "")))
        crosses_separator = (
            split_on_gap
            and current
            and previous_index is not None
            and current_index is not None
            and previous_block == current_block
            and int(current_index) - int(previous_index) > max_gap
        )
        if crosses_separator and previous_block == current_block and current and (item_has_symbol_span(item) or any(item_has_symbol_span(value) for value in current)):
            crosses_separator = False
        crosses_column = False
        if current and previous_item is not None:
            current_rect = fitz.Rect(item["rect"])
            previous_rect = fitz.Rect(previous_item["rect"])
            font_ref = max(
                1.0,
                float(item.get("font_size") or 0.0),
                float(previous_item.get("font_size") or 0.0),
            )
            horizontal_gap = float(current_rect.x0) - float(previous_rect.x1)
            same_row_delta = abs(float(current_rect.y0) - float(previous_rect.y0))
            crosses_column = horizontal_gap >= max(24.0, font_ref * 3.0) and same_row_delta <= font_ref * 1.35
        if crosses_separator:
            parts.append(current)
            current = []
        elif crosses_column:
            parts.append(current)
            current = []
        current.append(item)
        previous_index = int(current_index) if current_index is not None else None
        previous_block = current_block
        previous_item = item
    if current:
        parts.append(current)
    output: list[list[dict[str, Any]]] = []
    for part in parts:
        if len(part) <= 1:
            output.append(part)
            continue
        first_text = str(part[0]["source_text"]).strip().lower()
        if NOTE_LABEL_RE.match(first_text):
            output.append(part)
            continue
        rest_sizes = [float(item.get("font_size") or 0.0) for item in part[1:]]
        first_size = float(part[0].get("font_size") or 0.0)
        rest_median = median_float(rest_sizes)
        if rest_median and first_size >= rest_median * 1.25:
            output.append([part[0]])
            output.append(part[1:])
            continue
        output.append(part)
    return output


def numeric_list(values: list[float]) -> list[float]:
    return [float(value) for value in values]


def body_flow_profile(policy: dict[str, Any]) -> dict[str, Any]:
    grouping = policy.get("flow_grouping", {})
    body = grouping.get("body") if isinstance(grouping, dict) else None
    return body if isinstance(body, dict) else {}


def hard_disabled_page_type(profile: dict[str, Any], page_type: str) -> bool:
    return page_type in {str(value) for value in profile.get("hard_disable_page_type_guesses", [])}


def hard_page_left_label_expansion_allowed(region: dict[str, Any], page_rect: fitz.Rect, profile: dict[str, Any]) -> bool:
    if not bool(profile.get("allow_hard_page_left_label_expansion", False)):
        return False
    if str(region.get("region_kind") or "") not in {"heading", "short_label"}:
        return False
    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    return (
        float(rect.x0) / page_width <= float(profile.get("hard_page_left_label_max_x_ratio", 0.16))
        and float(rect.y0) / page_height <= float(profile.get("hard_page_left_label_max_y_ratio", 0.76))
    )


def large_top_heading_region(region: dict[str, Any], page_rect: fitz.Rect) -> bool:
    if str(region.get("region_kind")) != "heading":
        return False
    source_size = float(region.get("source_size") or 0.0)
    rect = fitz.Rect(region["rect"])
    page_stats = region.get("page_font_stats")
    stats = page_stats if isinstance(page_stats, dict) else {}
    threshold = font_stat_value({str(k): float(v) for k, v in stats.items()}, "q75", source_size) * 1.2
    return source_size >= threshold and float(rect.y0) / max(1.0, float(page_rect.height)) <= 0.35


def local_constrained_slot_flow_applies(
    items: list[dict[str, Any]],
    kind: str,
    page_rect: fitz.Rect,
    policy: dict[str, Any],
    page_context: dict[str, Any] | None,
) -> bool:
    profile = local_constrained_slot_flow_profile(policy)
    if not bool(profile.get("enabled", False)):
        return False
    page_types = {str(value) for value in profile.get("page_type_guesses", [])}
    local_kinds = {str(value) for value in profile.get("region_kinds", [])}
    rect = union_rect([item["rect"] for item in items])
    sizes = [float(item.get("font_size") or 0.0) for item in items]
    page_stats = page_context_font_stats(page_context)
    quantile_name = str(profile.get("max_median_font_size_page_quantile", "q50"))
    max_source_ratio = float(profile.get("max_source_to_page_quantile_ratio", 1.15))
    source_threshold = font_stat_value(page_stats, quantile_name, median_float(sizes)) * max_source_ratio
    x0_values = numeric_list([float(item["rect"].x0) for item in items])
    widths = numeric_list([float(item["rect"].width) for item in items])
    text = join_translation_fragments(items, kind, policy)
    return (
        page_type_guess(page_context) in page_types
        and kind in local_kinds
        and len(items) >= int(profile.get("min_line_count", 2))
        and rect.width <= float(page_rect.width) * float(profile.get("max_region_width_page_ratio", 0.32))
        and rect.height <= float(page_rect.height) * float(profile.get("max_region_height_page_ratio", 0.18))
        and median_float(sizes) <= source_threshold
        and max(x0_values, default=0.0) - min(x0_values, default=0.0) <= float(profile.get("max_x0_delta_pt", 18.0))
        and (max(widths, default=1.0) / max(1.0, min(widths, default=1.0))) <= float(profile.get("max_width_ratio", 2.2))
        and non_numeric_wrappable_text(text, int(profile.get("min_alpha_chars", 4)))
    )


def is_body_flow_candidate(region: dict[str, Any], page_rect: fitz.Rect, policy: dict[str, Any], *, active_group: bool = False) -> bool:
    profile = body_flow_profile(policy)
    if not profile.get("enabled"):
        return False
    page_type = str(region.get("page_type_guess") or "")
    if hard_disabled_page_type(profile, page_type):
        return False
    disabled_page_types = {str(value) for value in profile.get("disable_page_type_guesses", [])}
    page_type_disabled = page_type in disabled_page_types
    if page_type_disabled:
        dense_page_y = profile.get("allow_dense_page_body_below_y_ratio")
        if dense_page_y is None:
            return False
        if float(region["rect"].y0) / max(1.0, float(page_rect.height)) < float(dense_page_y):
            return False
    candidate_kinds = {str(value) for value in profile.get("candidate_region_kinds", ["body"])}
    if str(region.get("region_kind")) not in candidate_kinds:
        return False
    mode = str(region.get("layout_mode") or "")
    if mode == "line_preserve" and not bool(profile.get("include_line_preserve_body", False)):
        return False
    if mode not in {"region_reflow", "line_preserve"}:
        return False
    min_ratio = float(profile.get("min_region_width_page_ratio", 0.45))
    if float(region["rect"].width) >= float(page_rect.width) * min_ratio:
        return True
    if not active_group or not bool(profile.get("allow_short_continuation_lines", False)):
        return False
    continuation_min_ratio = float(profile.get("min_continuation_width_page_ratio", 0.10))
    return float(region["rect"].width) >= float(page_rect.width) * continuation_min_ratio


def can_join_body_flow(group: list[dict[str, Any]], candidate: dict[str, Any], policy: dict[str, Any]) -> bool:
    profile = body_flow_profile(policy)
    if not group:
        return True
    separator_policy = policy.get("source_separator_policy", {})
    if isinstance(separator_policy, dict) and bool(separator_policy.get("split_on_untranslated_visible_line_gap", False)):
        max_gap = int(separator_policy.get("max_line_index_gap_without_split", 1))
        previous_items = group[-1].get("items", [])
        candidate_items = candidate.get("items", [])
        previous_item = previous_items[-1] if previous_items else None
        candidate_item = candidate_items[0] if candidate_items else None
        if isinstance(previous_item, dict) and isinstance(candidate_item, dict):
            same_block = previous_item.get("block_id") is not None and previous_item.get("block_id") == candidate_item.get("block_id")
            if same_block:
                try:
                    previous_index = int(previous_item.get("line_index"))
                    candidate_index = int(candidate_item.get("line_index"))
                except (TypeError, ValueError):
                    previous_index = candidate_index = 0
                if candidate_index - previous_index > max_gap:
                    return False
    x0_values = numeric_list([item["rect"].x0 for item in group] + [candidate["rect"].x0])
    widths = numeric_list([item["rect"].width for item in group] + [candidate["rect"].width])
    max_x0_delta = float(profile.get("max_x0_delta_pt", 12.0))
    max_width_delta_ratio = float(profile.get("max_width_delta_ratio", 0.18))
    max_vertical_gap = float(profile.get("max_vertical_gap_pt", 99999.0))
    median_width = max(1.0, median_float(widths))
    previous = group[-1]["rect"]
    y_gap = max(0.0, float(candidate["rect"].y0) - float(previous.y1))
    aligned_and_close = (max(x0_values) - min(x0_values)) <= max_x0_delta and y_gap <= max_vertical_gap
    if bool(profile.get("allow_short_continuation_lines", False)) and candidate["rect"].width < median_width:
        continuation_min_width = float(profile.get("min_continuation_width_page_ratio", 0.10)) * max(1.0, float(group[0]["page_rect_width"]))
        if candidate["rect"].width >= continuation_min_width:
            return aligned_and_close
    return (
        aligned_and_close
        and ((max(widths) - min(widths)) / median_width) <= max_width_delta_ratio
    )


def join_body_flow_text(group: list[dict[str, Any]], text_key: str, policy: dict[str, Any]) -> str:
    profile = body_flow_profile(policy)
    target_language = str(policy.get("target_language") or "zh")
    line_joiner = str(profile.get(f"line_joiner_{target_language}", " " if target_language == "en" else ""))
    paragraph_separator = str(profile.get("paragraph_separator", "\n\n"))
    paragraph_gap = float(profile.get("paragraph_gap_pt", 12.0))
    parts: list[str] = []
    previous_rect: fitz.Rect | None = None
    for region in group:
        text = str(region.get(text_key) or "").strip()
        if not text:
            continue
        if not parts:
            parts.append(text)
        else:
            gap = 0.0 if previous_rect is None else max(0.0, float(region["rect"].y0) - float(previous_rect.y1))
            joiner = paragraph_separator if gap >= paragraph_gap else line_joiner
            parts.append(joiner + text)
        previous_rect = region["rect"]
    return "".join(parts).strip()


def make_body_flow_region(group: list[dict[str, Any]], index: int, policy: dict[str, Any]) -> dict[str, Any]:
    profile = body_flow_profile(policy)
    target_kind = str(profile.get("target_region_kind", "body_flow"))
    items = [item for region in group for item in region["items"]]
    rect = union_rect([region["rect"] for region in group])
    source_sizes = [float(region.get("source_size") or 0.0) for region in group]
    return {
        "region_id": f"body_flow_{index:03d}",
        "region_kind": target_kind,
        "items": items,
        "rect": rect,
        "translation_zh": join_body_flow_text(group, "translation_zh", policy),
        "target_text": join_body_flow_text(group, "target_text", policy),
        "text_color": dominant_text_color(items),
        "background_color": dominant_fill_color(items),
        "source_size": median_float(source_sizes),
        "layout_mode": "region_flow",
        "flow_source_region_count": len(group),
        "page_type_guess": group[0].get("page_type_guess"),
        "page_rect_width": group[0].get("page_rect_width"),
        "page_font_stats": group[0].get("page_font_stats"),
    }


def column_overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
    overlap = max(0.0, min(float(left.x1), float(right.x1)) - max(float(left.x0), float(right.x0)))
    return overlap / max(1.0, min(float(left.width), float(right.width)))


def overlap_guard_profile(policy: dict[str, Any]) -> dict[str, Any]:
    composition = target_composition_profile(policy)
    guard = composition.get("overlap_guard") if isinstance(composition.get("overlap_guard"), dict) else {}
    if guard and bool(guard.get("enabled", False)):
        return guard
    reflow = target_language_reflow_profile(policy)
    guard = reflow.get("overlap_guard") if isinstance(reflow.get("overlap_guard"), dict) else {}
    return guard if isinstance(guard, dict) else {}


def apply_reflow_overlap_guard(regions: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> None:
    guard = overlap_guard_profile(policy)
    if not guard or not bool(guard.get("enabled", False)):
        return
    min_column_overlap = float(guard.get("min_column_overlap_ratio", 0.55))
    min_gap = float(guard.get("min_vertical_gap_pt", 4.0))
    min_height = float(guard.get("min_remaining_height_pt", 12.0))
    text_regions = [
        region
        for region in regions
        if (region.get("target_language_reflow_applied") or region.get("target_composition_applied") or region.get("expandable_text_slot_applied"))
        and str(region.get("region_kind")) not in set(str(value) for value in guard.get("ignore_region_kinds", []))
    ]
    candidates = [
        region
        for region in regions
        if str(region.get("region_kind")) not in set(str(value) for value in guard.get("ignore_region_kinds", []))
    ]
    for region in text_regions:
        rect = fitz.Rect(region["rect"])
        nearest_top: float | None = None
        for other in candidates:
            if other is region:
                continue
            other_rect = fitz.Rect(other["rect"])
            if other_rect.y0 <= rect.y0:
                continue
            if column_overlap_ratio(rect, other_rect) < min_column_overlap:
                continue
            if nearest_top is None or other_rect.y0 < nearest_top:
                nearest_top = float(other_rect.y0)
        if nearest_top is None:
            continue
        capped_y1 = max(rect.y0 + min_height, nearest_top - min_gap)
        if capped_y1 < rect.y1:
            rect.y1 = min(float(page_rect.y1), capped_y1)
            region["rect"] = rect
            profile_evidence = region.setdefault("target_language_reflow_profile", {})
            if isinstance(profile_evidence, dict):
                profile_evidence["overlap_guard_applied"] = True
                profile_evidence["overlap_guard_capped_y1"] = round(capped_y1, 3)
            composition_evidence = region.setdefault("target_composition_profile", {})
            if isinstance(composition_evidence, dict) and region.get("target_composition_applied"):
                composition_evidence["overlap_guard_applied"] = True
                composition_evidence["overlap_guard_capped_y1"] = round(capped_y1, 3)


def target_language_reflow_profile(policy: dict[str, Any]) -> dict[str, Any]:
    profile = policy.get("target_language_reflow")
    return profile if isinstance(profile, dict) else {}


def target_composition_profile(policy: dict[str, Any]) -> dict[str, Any]:
    profile = policy.get("target_composition")
    return profile if isinstance(profile, dict) else {}


def expandable_text_slot_profile(policy: dict[str, Any]) -> dict[str, Any]:
    profile = policy.get("expandable_text_slots")
    return profile if isinstance(profile, dict) else {}


def local_constrained_slot_expansion_profile(policy: dict[str, Any]) -> dict[str, Any]:
    profile = policy.get("local_constrained_slot_expansion")
    return profile if isinstance(profile, dict) else {}


def local_constrained_slot_flow_profile(policy: dict[str, Any]) -> dict[str, Any]:
    profile = policy.get("local_constrained_slot_flow")
    return profile if isinstance(profile, dict) else {}


def normalized_text_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def region_source_text(region: dict[str, Any]) -> str:
    return " ".join(str(item.get("source_text") or "").strip() for item in region.get("items", []) if str(item.get("source_text") or "").strip())


def alpha_digit_counts(text: str) -> tuple[int, int]:
    alpha = sum(1 for char in text if char.isalpha())
    digit = sum(1 for char in text if char.isdigit())
    return alpha, digit


def non_numeric_wrappable_text(text: str, min_alpha_chars: int = 4) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    alpha, digit = alpha_digit_counts(normalized)
    return " " in normalized and alpha >= min_alpha_chars and alpha > digit


def vertical_overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
    overlap = max(0.0, min(float(left.y1), float(right.y1)) - max(float(left.y0), float(right.y0)))
    return overlap / max(1.0, min(float(left.height), float(right.height)))


def apply_expandable_text_slot_rect(region: dict[str, Any], regions: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> None:
    profile = expandable_text_slot_profile(policy)
    if not profile.get("enabled"):
        return
    if region.get("expandable_text_slot_applied"):
        return
    kind = str(region.get("region_kind") or "")
    allowed_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if allowed_kinds and kind not in allowed_kinds:
        return
    page_type = str(region.get("page_type_guess") or "")
    if hard_disabled_page_type(profile, page_type) and not hard_page_left_label_expansion_allowed(region, page_rect, profile):
        return
    if page_type in {str(value) for value in profile.get("disable_page_type_guesses", [])} and not large_top_heading_region(region, page_rect):
        return

    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    y_ratio = float(rect.y0) / page_height
    kind_min_y_key = f"{kind}_min_y_ratio"
    kind_max_y_key = f"{kind}_max_y_ratio"
    min_y_ratio = float(profile.get(kind_min_y_key, profile.get("min_y_ratio", 0.0)))
    max_y_ratio = float(profile.get(kind_max_y_key, profile.get("max_y_ratio", 1.0)))
    if y_ratio < min_y_ratio or y_ratio > max_y_ratio:
        return

    source_width_ratio = float(rect.width) / page_width
    min_source_width = float(profile.get("min_source_width_page_ratio", 0.0))
    max_source_width = float(profile.get("max_source_width_page_ratio", 1.0))
    if source_width_ratio < min_source_width or source_width_ratio > max_source_width:
        return

    target_text = str(region.get("target_text") or region.get("translation_zh") or "")
    source_text = region_source_text(region)
    target_len = normalized_text_len(target_text)
    source_len = max(1, normalized_text_len(source_text))
    min_target_key = f"{kind}_min_target_chars"
    if target_len < int(profile.get(min_target_key, profile.get("min_target_chars", 0))):
        return
    expansion_ratio = target_len / source_len
    expansion_ratio_key = f"{kind}_min_text_expansion_ratio"
    required_expansion_ratio = float(profile.get(expansion_ratio_key, profile.get("min_text_expansion_ratio", 1.0)))
    if expansion_ratio < required_expansion_ratio and kind != "heading":
        return

    right_margin = page_width * float(profile.get("right_margin_page_ratio", 0.06))
    bottom_margin = page_height * float(profile.get("bottom_margin_page_ratio", 0.05))
    max_x1 = float(page_rect.x1) - right_margin
    max_bottom = min(float(page_rect.y1) - bottom_margin, float(page_rect.y1))
    kind_min_width_key = f"{kind}_min_width_page_ratio"
    min_width_ratio_key = kind_min_width_key if profile.get(kind_min_width_key) is not None else (
        "heading_min_width_page_ratio" if kind == "heading" and profile.get("heading_min_width_page_ratio") is not None else "min_width_page_ratio"
    )
    kind_max_width_key = f"{kind}_max_width_page_ratio"
    max_width_ratio_key = kind_max_width_key if profile.get(kind_max_width_key) is not None else "max_width_page_ratio"
    kind_height_key = f"{kind}_height_expand_ratio"
    height_expand_key = kind_height_key if profile.get(kind_height_key) is not None else "height_expand_ratio"
    kind_min_height_key = f"{kind}_min_height_pt"
    min_height_key = kind_min_height_key if profile.get(kind_min_height_key) is not None else "min_height_pt"
    kind_min_height_source_ratio_key = f"{kind}_min_height_source_ratio"
    min_width = page_width * float(profile.get(min_width_ratio_key, profile.get("min_width_page_ratio", 0.0)))
    max_width = page_width * float(profile.get(max_width_ratio_key, profile.get("max_width_page_ratio", 1.0)))
    desired_width = min(max_width, max(float(rect.width), min_width))
    min_h_gap = float(profile.get("min_horizontal_gap_pt", 6.0))
    min_v_gap = float(profile.get("min_vertical_gap_pt", 4.0))
    obstacle_overlap = float(profile.get("obstacle_vertical_overlap_ratio", 0.35))

    obstacle_x1 = max_x1
    trial_vertical = fitz.Rect(rect.x0, rect.y0, max_x1, rect.y1)
    for other in regions:
        if other is region:
            continue
        other_rect = fitz.Rect(other["rect"])
        if other_rect.x0 <= rect.x1 + min_h_gap:
            continue
        if vertical_overlap_ratio(trial_vertical, other_rect) < obstacle_overlap:
            continue
        obstacle_x1 = min(obstacle_x1, float(other_rect.x0) - min_h_gap)
    target_x1 = min(max_x1, obstacle_x1, float(rect.x0) + desired_width)
    if target_x1 <= float(rect.x1) + 2.0:
        return

    min_height = float(profile.get(min_height_key, profile.get("min_height_pt", 0.0)))
    if profile.get("min_height_source_ratio") is not None:
        min_height = max(min_height, float(region.get("source_size") or 0.0) * float(profile.get("min_height_source_ratio")))
    if profile.get(kind_min_height_source_ratio_key) is not None:
        min_height = max(min_height, float(region.get("source_size") or 0.0) * float(profile.get(kind_min_height_source_ratio_key)))
    target_height = max(float(rect.height) * float(profile.get(height_expand_key, profile.get("height_expand_ratio", 1.0))), min_height)
    target_y1 = min(max_bottom, max(float(rect.y1), float(rect.y0) + target_height))
    trial_rect = fitz.Rect(rect.x0, rect.y0, target_x1, target_y1)
    nearest_top: float | None = None
    for other in regions:
        if other is region:
            continue
        other_rect = fitz.Rect(other["rect"])
        if other_rect.y0 <= rect.y0:
            continue
        if column_overlap_ratio(trial_rect, other_rect) < 0.25:
            continue
        if nearest_top is None or other_rect.y0 < nearest_top:
            nearest_top = float(other_rect.y0)
    if nearest_top is not None:
        target_y1 = min(target_y1, max(float(rect.y1), nearest_top - min_v_gap))
    if target_y1 <= float(rect.y0) + 2.0:
        return

    region["source_anchor_bbox"] = [round(v, 3) for v in rect]
    region["rect"] = fitz.Rect(float(rect.x0), float(rect.y0), target_x1, target_y1)
    region["expandable_text_slot_applied"] = True
    region["expandable_text_slot_profile"] = {
        "min_width_page_ratio": profile.get(min_width_ratio_key),
        "max_width_page_ratio": profile.get("max_width_page_ratio"),
        "height_expand_ratio": profile.get("height_expand_ratio"),
        "target_source_length_ratio": round(expansion_ratio, 3),
        "source_width_page_ratio": round(source_width_ratio, 3),
        "source": profile.get("source"),
    }


def apply_local_constrained_slot_expansion_rect(region: dict[str, Any], regions: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> None:
    profile = local_constrained_slot_expansion_profile(policy)
    if not bool(profile.get("enabled", False)):
        return
    if region.get("local_constrained_slot_expansion_applied"):
        return
    page_type = str(region.get("page_type_guess") or "")
    if page_type not in {str(value) for value in profile.get("page_type_guesses", [])}:
        return
    kind = str(region.get("region_kind") or "")
    allowed_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if allowed_kinds and kind not in allowed_kinds:
        return
    if kind == "metric_value":
        return
    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    source_width_ratio = float(rect.width) / page_width
    if source_width_ratio > float(profile.get("max_source_width_page_ratio", 0.32)):
        return
    if source_width_ratio < float(profile.get("min_source_width_page_ratio", 0.015)):
        return
    y_ratio = float(rect.y0) / page_height
    if y_ratio < float(profile.get("min_y_ratio", 0.0)) or y_ratio > float(profile.get("max_y_ratio", 1.0)):
        return
    source_size = float(region.get("source_size") or 0.0)
    stats = region.get("page_font_stats")
    page_stats = {str(k): float(v) for k, v in stats.items()} if isinstance(stats, dict) else {}
    source_threshold = font_stat_value(page_stats, profile.get("min_source_size_page_quantile", "q50"), source_size) * float(
        profile.get("min_source_to_page_quantile_ratio", 1.0)
    )
    if source_size < source_threshold:
        return
    target_text = str(region.get("target_text") or region.get("translation_zh") or "")
    source_text = region_source_text(region)
    target_len = normalized_text_len(target_text)
    source_len = max(1, normalized_text_len(source_text))
    expansion_ratio = target_len / source_len
    if target_len < int(profile.get("min_target_chars", 1)):
        return
    if expansion_ratio < float(profile.get("min_text_expansion_ratio", 1.0)):
        return
    alpha_count, digit_count = alpha_digit_counts(target_text)
    if alpha_count < int(profile.get("min_alpha_chars", 4)) or digit_count > alpha_count:
        return

    right_margin = page_width * float(profile.get("right_margin_page_ratio", 0.06))
    bottom_margin = page_height * float(profile.get("bottom_margin_page_ratio", 0.05))
    max_x1 = float(page_rect.x1) - right_margin
    min_h_gap = float(profile.get("min_horizontal_gap_pt", 6.0))
    min_v_gap = float(profile.get("min_vertical_gap_pt", 4.0))
    obstacle_overlap = float(profile.get("obstacle_vertical_overlap_ratio", 0.25))
    trial_vertical = fitz.Rect(rect.x0, rect.y0, max_x1, rect.y1)
    obstacle_x1 = max_x1
    for other in regions:
        if other is region:
            continue
        other_rect = fitz.Rect(other["rect"])
        if other_rect.x0 <= rect.x1 + min_h_gap:
            continue
        if vertical_overlap_ratio(trial_vertical, other_rect) < obstacle_overlap:
            continue
        obstacle_x1 = min(obstacle_x1, float(other_rect.x0) - min_h_gap)
    min_width = page_width * float(profile.get(f"{kind}_min_width_page_ratio", profile.get("min_width_page_ratio", 0.12)))
    max_width = page_width * float(profile.get(f"{kind}_max_width_page_ratio", profile.get("max_width_page_ratio", 0.28)))
    target_x1 = min(max_x1, obstacle_x1, float(rect.x0) + max(float(rect.width), min(max_width, min_width)))
    if target_x1 <= float(rect.x1) + 2.0:
        return
    max_bottom = min(float(page_rect.y1) - bottom_margin, float(page_rect.y1))
    target_height = max(
        float(rect.height) * float(profile.get(f"{kind}_height_expand_ratio", profile.get("height_expand_ratio", 1.6))),
        source_size * float(profile.get(f"{kind}_min_height_source_ratio", profile.get("min_height_source_ratio", 1.35))),
    )
    target_y1 = min(max_bottom, max(float(rect.y1), float(rect.y0) + target_height))
    trial_rect = fitz.Rect(rect.x0, rect.y0, target_x1, target_y1)
    nearest_top: float | None = None
    for other in regions:
        if other is region:
            continue
        other_rect = fitz.Rect(other["rect"])
        if other_rect.y0 <= rect.y0:
            continue
        if column_overlap_ratio(trial_rect, other_rect) < float(profile.get("below_column_overlap_ratio", 0.22)):
            continue
        if nearest_top is None or other_rect.y0 < nearest_top:
            nearest_top = float(other_rect.y0)
    if nearest_top is not None:
        target_y1 = min(target_y1, max(float(rect.y1), nearest_top - min_v_gap))
    if target_y1 <= float(rect.y0) + 2.0:
        return
    region["source_anchor_bbox"] = [round(v, 3) for v in rect]
    region["rect"] = fitz.Rect(float(rect.x0), float(rect.y0), target_x1, target_y1)
    region["local_constrained_slot_expansion_applied"] = True
    region["local_constrained_slot_expansion_profile"] = {
        "source_width_page_ratio": round(source_width_ratio, 3),
        "target_source_length_ratio": round(expansion_ratio, 3),
        "target_text_alpha_count": alpha_count,
        "source": profile.get("source"),
    }


def apply_target_composition_rect(region: dict[str, Any], page_rect: fitz.Rect, policy: dict[str, Any]) -> None:
    profile = target_composition_profile(policy)
    if not profile.get("enabled"):
        return
    if region.get("target_composition_applied"):
        return
    region_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if region_kinds and str(region.get("region_kind")) not in region_kinds:
        return
    page_type = str(region.get("page_type_guess") or "")
    if hard_disabled_page_type(profile, page_type) and not large_top_heading_region(region, page_rect):
        return
    disabled_page_types = {str(value) for value in profile.get("disable_page_type_guesses", [])}
    page_type_disabled = page_type in disabled_page_types
    if page_type_disabled and not large_top_heading_region(region, page_rect):
        dense_page_y = profile.get("allow_dense_page_body_below_y_ratio")
        if dense_page_y is None:
            return
        if float(region["rect"].y0) / max(1.0, float(page_rect.height)) < float(dense_page_y):
            return

    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    min_source_width_ratio = profile.get("min_source_width_page_ratio_for_composition")
    if min_source_width_ratio is not None and float(rect.width) / page_width < float(min_source_width_ratio):
        return
    left_margin = page_width * float(profile.get("left_margin_page_ratio", 0.08))
    right_margin = page_width * float(profile.get("right_margin_page_ratio", 0.08))
    top_margin = page_height * float(profile.get("top_margin_page_ratio", 0.04))
    bottom_margin = page_height * float(profile.get("bottom_margin_page_ratio", 0.05))
    min_width = page_width * float(profile.get("min_width_page_ratio", 0.0))
    max_width_ratio = float(profile.get("max_width_page_ratio", 1.0))
    max_width = page_width * max_width_ratio if max_width_ratio > 0 else page_width
    max_x1 = float(page_rect.x1) - right_margin
    min_x0 = float(page_rect.x0) + left_margin
    target_x0 = max(min_x0, min(float(rect.x0), max_x1 - min_width))
    target_width = min(max_width, max(float(rect.width), min_width))
    target_x1 = min(max_x1, target_x0 + target_width)
    if target_x1 - target_x0 < max(12.0, min_width * 0.6):
        return

    target_y0 = max(float(page_rect.y0) + top_margin, float(rect.y0))
    max_bottom = min(
        float(page_rect.y1) - bottom_margin,
        float(page_rect.y0) + page_height * float(profile.get("max_bottom_page_ratio", 0.96)),
    )
    min_height = page_height * float(profile.get("min_height_page_ratio", 0.0))
    height_expand_ratio = float(profile.get("height_expand_ratio", 1.0))
    target_height = max(float(rect.height) * height_expand_ratio, min_height)
    target_y1 = min(max_bottom, max(float(rect.y1), target_y0 + target_height))
    if target_y1 <= target_y0 + 1:
        return

    region["source_anchor_bbox"] = [round(v, 3) for v in rect]
    region["rect"] = fitz.Rect(target_x0, target_y0, target_x1, target_y1)
    region["target_composition_applied"] = True
    region["target_composition_profile"] = {
        "min_width_page_ratio": profile.get("min_width_page_ratio"),
        "max_width_page_ratio": profile.get("max_width_page_ratio"),
        "height_expand_ratio": profile.get("height_expand_ratio"),
        "max_bottom_page_ratio": profile.get("max_bottom_page_ratio"),
        "source": profile.get("source"),
    }


def apply_target_language_reflow_rect(region: dict[str, Any], page_rect: fitz.Rect, policy: dict[str, Any]) -> None:
    profile = target_language_reflow_profile(policy)
    if not profile.get("enabled"):
        return
    if region.get("target_composition_applied"):
        return
    if region.get("target_language_reflow_applied"):
        return
    region_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if region_kinds and str(region.get("region_kind")) not in region_kinds:
        return
    page_type = str(region.get("page_type_guess") or "")
    if hard_disabled_page_type(profile, page_type) and not large_top_heading_region(region, page_rect):
        return
    disabled_page_types = {str(value) for value in profile.get("disable_page_type_guesses", [])}
    page_type_disabled = page_type in disabled_page_types
    if page_type_disabled and not large_top_heading_region(region, page_rect):
        dense_page_y = profile.get("allow_dense_page_body_below_y_ratio")
        if dense_page_y is None:
            return
        if float(region["rect"].y0) / max(1.0, float(page_rect.height)) < float(dense_page_y):
            return
    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    min_source_width_ratio = profile.get("min_source_width_page_ratio_for_reflow")
    if (
        min_source_width_ratio is not None
        and str(region.get("region_kind")) in {"body", "body_flow"}
        and float(rect.width) / page_width < float(min_source_width_ratio)
    ):
        return
    min_width = page_width * float(profile.get("min_width_page_ratio", 0.0))
    right_margin = page_width * float(profile.get("right_margin_page_ratio", 0.08))
    bottom_margin = page_height * float(profile.get("bottom_margin_page_ratio", 0.10))
    max_bottom = min(
        float(page_rect.y1) - bottom_margin,
        float(page_rect.y0) + page_height * float(profile.get("max_bottom_page_ratio", 0.90)),
    )
    if rect.width < min_width:
        rect.x1 = min(float(page_rect.x1) - right_margin, max(rect.x1, rect.x0 + min_width))
    height_expand_ratio = float(profile.get("height_expand_ratio", 1.0))
    min_height = page_height * float(profile.get("min_height_page_ratio", 0.0))
    target_height = max(rect.height * height_expand_ratio, min_height)
    if target_height > rect.height:
        rect.y1 = min(max_bottom, max(rect.y1, rect.y0 + target_height))
    if rect.x1 > rect.x0 + 1 and rect.y1 > rect.y0 + 1:
        region["rect"] = rect
        region["target_language_reflow_applied"] = True
        region["target_language_reflow_profile"] = {
            "min_width_page_ratio": profile.get("min_width_page_ratio"),
            "height_expand_ratio": profile.get("height_expand_ratio"),
            "source": profile.get("source"),
        }


def apply_body_flow_grouping(regions: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> list[dict[str, Any]]:
    profile = body_flow_profile(policy)
    if not profile.get("enabled"):
        return regions
    min_count = int(profile.get("min_region_count", 4))
    output: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    flow_index = 0

    def flush_group() -> None:
        nonlocal flow_index
        if len(group) >= min_count:
            output.append(make_body_flow_region(group, flow_index, policy))
            flow_index += 1
        else:
            output.extend(group)
        group.clear()

    for region in regions:
        region["page_rect_width"] = float(page_rect.width)
        if is_body_flow_candidate(region, page_rect, policy, active_group=bool(group)) and can_join_body_flow(group, region, policy):
            group.append(region)
            continue
        flush_group()
        if is_body_flow_candidate(region, page_rect, policy, active_group=False):
            group.append(region)
        else:
            output.append(region)
    flush_group()
    for region in output:
        apply_target_composition_rect(region, page_rect, policy)
        apply_target_language_reflow_rect(region, page_rect, policy)
    for region in output:
        apply_expandable_text_slot_rect(region, output, page_rect, policy)
    apply_reflow_overlap_guard(output, page_rect, policy)
    return output


def build_regions(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any], page_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        key = block_key(item["unit_id"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    regions: list[dict[str, Any]] = []
    page_stats = page_context_font_stats(page_context)
    for key in order:
        for part in split_heading_prefix(grouped[key], policy):
            if should_reflow_region(part, page_rect, policy, page_context):
                rect = union_rect([item["rect"] for item in part])
                sizes = [float(item.get("font_size") or 6.0) for item in part]
                kind = region_kind(part, page_rect, policy, page_context)
                local_flow_applied = local_constrained_slot_flow_applies(part, kind, page_rect, policy, page_context)
                regions.append(
                    {
                        "region_id": f"region_{key}_{len(regions):03d}",
                        "region_kind": kind,
                        "items": part,
                        "rect": expand_region_rect(rect, page_rect, median_float(sizes), kind, policy),
                        "translation_zh": join_translation_fragments(part, kind, policy),
                        "target_text": join_translation_fragments(part, kind, policy),
                        "text_color": dominant_text_color(part),
                        "background_color": dominant_fill_color(part),
                        "source_size": median_float(sizes),
                        "layout_mode": "region_reflow",
                        "page_type_guess": page_type_guess(page_context),
                        "page_font_stats": page_stats,
                        "local_constrained_slot_flow_applied": local_flow_applied,
                        "local_constrained_slot_flow_profile": (
                            {
                                "source_region_width_page_ratio": round(float(rect.width) / max(1.0, float(page_rect.width)), 3),
                                "source_region_height_page_ratio": round(float(rect.height) / max(1.0, float(page_rect.height)), 3),
                                "source": local_constrained_slot_flow_profile(policy).get("source"),
                            }
                            if local_flow_applied
                            else None
                        ),
                    }
                )
            else:
                for item in part:
                    kind = region_kind([item], page_rect, policy, page_context)
                    regions.append(
                        {
                            "region_id": f"region_{item['unit_id']}",
                            "region_kind": kind,
                            "items": [item],
                            "rect": item["rect"],
                            "translation_zh": explicit_layout_text(item, kind, policy),
                            "target_text": explicit_layout_text(item, kind, policy),
                            "text_color": dominant_text_color([item]),
                            "background_color": dominant_fill_color([item]),
                            "source_size": float(item.get("font_size") or 6.0),
                            "layout_mode": "line_preserve",
                            "page_type_guess": page_type_guess(page_context),
                            "page_font_stats": page_stats,
                        }
                    )
    grouped_regions = apply_body_flow_grouping(regions, page_rect, policy)
    for region in grouped_regions:
        apply_target_language_reflow_rect(region, page_rect, policy)
    for region in grouped_regions:
        apply_expandable_text_slot_rect(region, grouped_regions, page_rect, policy)
    for region in grouped_regions:
        apply_local_constrained_slot_expansion_rect(region, grouped_regions, page_rect, policy)
    apply_reflow_overlap_guard(grouped_regions, page_rect, policy)
    return grouped_regions


BACKGROUND_COVER_REGION_KINDS = {"body", "body_flow", "heading", "table_note", "footnote", "event_card"}
BACKGROUND_COVER_SKIP_PAGE_TYPES = {"chart_or_dashboard", "matrix_or_table_diagram"}
BACKGROUND_COVER_SKIP_KINDS = {"table_cell", "legend", "vertical_nav", "compact_label", "short_label"}
BACKGROUND_COVER_IMAGE_PATCH_SATURATION = 18.0
BACKGROUND_COVER_IMAGE_PATCH_MIN_AREA_PT2 = 600.0
BACKGROUND_COVER_SAMPLE_ZOOM = 2.0
IMAGE_OVERLAY_MIN_OVERLAP_RATIO = 0.55
IMAGE_OVERLAY_BACKGROUND_COLOR_RANGE = 36.0
IMAGE_OVERLAY_BACKGROUND_SATURATION = 18.0
IMAGE_OVERLAY_TEXT_BACKGROUND_DELTA = 60.0


def should_apply_region_background_cover(region: dict[str, Any]) -> bool:
    kind = str(region.get("region_kind") or "")
    if kind in BACKGROUND_COVER_SKIP_KINDS:
        return False
    page_type = str(region.get("page_type_guess") or "")
    if page_type in BACKGROUND_COVER_SKIP_PAGE_TYPES and kind not in {"table_note", "footnote"}:
        return False
    items = region.get("items") if isinstance(region.get("items"), list) else []
    if any(bool(item.get("preserve_background_redaction")) for item in items):
        return False
    if len(items) < 2 and str(region.get("layout_mode") or "") == "line_preserve":
        return False
    return kind in BACKGROUND_COVER_REGION_KINDS or bool(region.get("target_composition_applied")) or str(region.get("layout_mode") or "") == "region_flow"


def source_background_cover_rect(region: dict[str, Any], page_rect: fitz.Rect) -> fitz.Rect:
    rect = union_rect([item["rect"] for item in region["items"]])
    source_size = float(region.get("source_size") or 6.0)
    x_pad = max(0.8, source_size * 0.18)
    y_pad = max(0.8, source_size * 0.14)
    rect.x0 = max(page_rect.x0, rect.x0 - x_pad)
    rect.x1 = min(page_rect.x1, rect.x1 + x_pad)
    rect.y0 = max(page_rect.y0, rect.y0 - y_pad)
    rect.y1 = min(page_rect.y1, rect.y1 + y_pad)
    return rect


def page_background_sample_image(page: fitz.Page, zoom: float) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def cover_pixel_box(rect: fitz.Rect, image: Image.Image, zoom: float) -> tuple[int, int, int, int]:
    return (
        max(0, min(image.width - 1, int(round(rect.x0 * zoom)))),
        max(0, min(image.height - 1, int(round(rect.y0 * zoom)))),
        max(1, min(image.width, int(round(rect.x1 * zoom)))),
        max(1, min(image.height, int(round(rect.y1 * zoom)))),
    )


def rgb_delta(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / 3.0


def color_excluded(pixel: tuple[int, int, int], exclude_rgb: list[tuple[int, int, int]]) -> bool:
    return any(rgb_delta(pixel, excluded) <= 42.0 for excluded in exclude_rgb)


def dominant_rgb(
    samples: list[tuple[int, int, int]],
    fallback_rgb: tuple[int, int, int],
    exclude_rgb: list[tuple[int, int, int]] | None = None,
) -> tuple[int, int, int]:
    if not samples:
        return fallback_rgb
    if exclude_rgb:
        filtered = [pixel for pixel in samples if not color_excluded(pixel, exclude_rgb)]
        if filtered:
            samples = filtered
    clusters: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for r, g, b in samples:
        key = (round(r / 8) * 8, round(g / 8) * 8, round(b / 8) * 8)
        clusters.setdefault(key, []).append((r, g, b))
    cluster = max(clusters.values(), key=len)
    return tuple(int(round(sum(pixel[i] for pixel in cluster) / len(cluster))) for i in range(3))


def rgb_saturation(rgb: tuple[int, int, int]) -> float:
    return float(max(rgb) - min(rgb))


def source_cover_background_stats(
    source_image: Image.Image | None,
    rect: fitz.Rect,
    fill_rgb: tuple[int, int, int],
    zoom: float,
    exclude_rgb: list[tuple[int, int, int]] | None = None,
) -> dict[str, Any]:
    if source_image is None:
        return {
            "source_background_rgb": list(fill_rgb),
            "source_background_saturation": rgb_saturation(fill_rgb),
            "source_background_color_range": 0.0,
            "source_fill_delta": 0.0,
            "image_patch_recommended": False,
        }
    box = cover_pixel_box(rect, source_image, zoom)
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    x_step = max(1, width // 12)
    y_step = max(1, height // 12)
    samples: list[tuple[int, int, int]] = []
    for y in range(box[1], box[3], y_step):
        for x in range(box[0], box[2], x_step):
            samples.append(source_image.getpixel((x, y)))
    # Always include the lower/right edge in case a narrow text box falls between grid points.
    for y in {box[1], max(box[1], box[3] - 1)}:
        for x in range(box[0], box[2], x_step):
            samples.append(source_image.getpixel((x, y)))
    for x in {box[0], max(box[0], box[2] - 1)}:
        for y in range(box[1], box[3], y_step):
            samples.append(source_image.getpixel((x, y)))
    filtered_samples = [pixel for pixel in samples if not color_excluded(pixel, exclude_rgb or [])] if exclude_rgb else samples
    if not filtered_samples:
        filtered_samples = samples
    source_rgb = dominant_rgb(filtered_samples, fill_rgb, exclude_rgb)
    ranges = [max(pixel) - min(pixel) for pixel in filtered_samples] or [0]
    channel_ranges = [
        max(pixel[i] for pixel in filtered_samples) - min(pixel[i] for pixel in filtered_samples)
        for i in range(3)
    ] if filtered_samples else [0, 0, 0]
    source_fill_delta = rgb_delta(source_rgb, fill_rgb)
    color_range = max(max(ranges), max(channel_ranges))
    image_patch_recommended = (
        source_fill_delta >= 24.0
        or rgb_saturation(source_rgb) >= BACKGROUND_COVER_IMAGE_PATCH_SATURATION
        or color_range >= 36.0
    )
    return {
        "source_background_rgb": list(source_rgb),
        "source_background_saturation": round(rgb_saturation(source_rgb), 3),
        "source_background_color_range": round(float(color_range), 3),
        "source_fill_delta": round(source_fill_delta, 3),
        "excluded_text_color_count": len(exclude_rgb or []),
        "source_sample_count": len(samples),
        "source_filtered_sample_count": len(filtered_samples),
        "image_patch_recommended": image_patch_recommended,
    }


def page_image_background_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return rects
    for block in blocks:
        if block.get("type") != 1:
            continue
        bbox = block.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        rect = fitz.Rect([float(v) for v in bbox])
        if rect_is_usable(rect):
            rects.append(rect)
    return rects


def fitz_rect_area(rect: fitz.Rect) -> float:
    return max(0.0, float(rect.width)) * max(0.0, float(rect.height))


def fitz_rect_overlap_ratio(inner: fitz.Rect, outer: fitz.Rect) -> float:
    overlap = fitz.Rect(
        max(inner.x0, outer.x0),
        max(inner.y0, outer.y0),
        min(inner.x1, outer.x1),
        min(inner.y1, outer.y1),
    )
    inner_area = fitz_rect_area(inner)
    if inner_area <= 0:
        return 0.0
    return max(0.0, fitz_rect_area(overlap)) / inner_area


def rect_center_inside(rect: fitz.Rect, container: fitz.Rect) -> bool:
    center_x = (rect.x0 + rect.x1) / 2.0
    center_y = (rect.y0 + rect.y1) / 2.0
    return container.x0 <= center_x <= container.x1 and container.y0 <= center_y <= container.y1


def image_overlay_background_protection_decision(
    source_image: Image.Image | None,
    image_rects: list[fitz.Rect],
    rect: fitz.Rect,
    fill_rgb: tuple[int, int, int],
    text_rgb: tuple[int, int, int],
    zoom: float,
) -> dict[str, Any]:
    if source_image is None or not image_rects:
        return {"protect_background": False, "reason": "no_source_image_background"}
    overlap_ratios = [fitz_rect_overlap_ratio(rect, image_rect) for image_rect in image_rects]
    max_overlap = max(overlap_ratios or [0.0])
    center_inside = any(rect_center_inside(rect, image_rect) for image_rect in image_rects)
    if max_overlap < IMAGE_OVERLAY_MIN_OVERLAP_RATIO and not center_inside:
        return {
            "protect_background": False,
            "reason": "text_bbox_not_on_image_block",
            "image_overlap_ratio": round(max_overlap, 4),
            "image_center_inside": center_inside,
        }
    stats = source_cover_background_stats(source_image, rect, fill_rgb, zoom, [text_rgb])
    background_rgb = tuple(int(v) for v in stats.get("source_background_rgb", list(fill_rgb))[:3])
    background_luma = sum(background_rgb) / 3.0
    background_saturation = float(stats.get("source_background_saturation") or 0.0)
    background_color_range = float(stats.get("source_background_color_range") or 0.0)
    text_background_delta = rgb_delta(background_rgb, text_rgb)
    light_plain_background = background_luma >= 220.0 and background_saturation < 15.0 and background_color_range < 28.0
    complex_or_nonplain = (
        background_color_range >= IMAGE_OVERLAY_BACKGROUND_COLOR_RANGE
        or background_saturation >= IMAGE_OVERLAY_BACKGROUND_SATURATION
        or text_background_delta >= IMAGE_OVERLAY_TEXT_BACKGROUND_DELTA
        or float(stats.get("source_fill_delta") or 0.0) >= 24.0
    )
    protect_background = bool(not light_plain_background and complex_or_nonplain)
    return {
        "protect_background": protect_background,
        "reason": "image_overlay_background_protected" if protect_background else "image_block_background_is_plain",
        "image_overlap_ratio": round(max_overlap, 4),
        "image_center_inside": center_inside,
        "text_background_delta": round(text_background_delta, 3),
        "background_luma": round(background_luma, 3),
        "light_plain_background": light_plain_background,
        **stats,
    }


def dominant_patch_row_color(
    samples: list[tuple[int, int, int]],
    fallback_rgb: tuple[int, int, int],
    exclude_rgb: list[tuple[int, int, int]] | None = None,
) -> tuple[int, int, int]:
    if not samples:
        return fallback_rgb
    if exclude_rgb:
        filtered = [pixel for pixel in samples if not color_excluded(pixel, exclude_rgb)]
        if filtered:
            samples = filtered
    close_samples = [pixel for pixel in samples if rgb_delta(pixel, fallback_rgb) <= 18.0]
    usable = close_samples or samples
    clusters: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for r, g, b in usable:
        key = (round(r / 2) * 2, round(g / 2) * 2, round(b / 2) * 2)
        clusters.setdefault(key, []).append((r, g, b))
    cluster = max(clusters.values(), key=len)
    return tuple(int(round(sum(pixel[i] for pixel in cluster) / len(cluster))) for i in range(3))


def background_cover_patch_png(
    source_image: Image.Image,
    rect: fitz.Rect,
    fallback_rgb: tuple[int, int, int],
    zoom: float,
    exclude_rgb: list[tuple[int, int, int]] | None = None,
) -> tuple[bytes, tuple[int, int]] | None:
    box = cover_pixel_box(rect, source_image, zoom)
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    if width <= 0 or height <= 0:
        return None
    pad = max(4, int(round(6 * zoom)))
    edge = max(2, int(round(3 * zoom)))
    patch = Image.new("RGB", (width, height), fallback_rgb)
    row_colors: list[tuple[int, int, int]] = []
    for row in range(height):
        y = max(0, min(source_image.height - 1, box[1] + row))
        samples: list[tuple[int, int, int]] = []
        left_x0 = max(0, box[0] - pad)
        left_x1 = max(0, box[0] - 1)
        right_x0 = min(source_image.width, box[2] + 1)
        right_x1 = min(source_image.width, box[2] + pad)
        for x in range(left_x0, left_x1):
            samples.append(source_image.getpixel((x, y)))
        for x in range(right_x0, right_x1):
            samples.append(source_image.getpixel((x, y)))
        if len(samples) < edge:
            for dy in range(1, pad + 1):
                top_y = box[1] - dy
                bottom_y = box[3] + dy
                if top_y >= 0:
                    for x in range(box[0], box[2], max(1, width // 300)):
                        samples.append(source_image.getpixel((x, top_y)))
                if bottom_y < source_image.height:
                    for x in range(box[0], box[2], max(1, width // 300)):
                        samples.append(source_image.getpixel((x, bottom_y)))
                if len(samples) >= edge:
                    break
        row_color = dominant_patch_row_color(samples, fallback_rgb, exclude_rgb)
        row_colors.append(row_color)
    if row_colors:
        smooth_radius = max(2, min(18, height // 18))
        smoothed_colors: list[tuple[int, int, int]] = []
        for row in range(height):
            start = max(0, row - smooth_radius)
            end = min(height, row + smooth_radius + 1)
            window = row_colors[start:end]
            smoothed_colors.append(
                tuple(int(round(sum(color[channel] for color in window) / len(window))) for channel in range(3))
            )
    else:
        smoothed_colors = [fallback_rgb for _ in range(height)]
    for row, row_color in enumerate(smoothed_colors):
        for x in range(width):
            patch.putpixel((x, row), row_color)
    buf = io.BytesIO()
    patch.save(buf, format="PNG")
    return buf.getvalue(), (width, height)


def draw_background_cover(
    page: fitz.Page,
    cover_rect: fitz.Rect,
    fill: Any,
    source_background_image: Image.Image | None,
    sample_zoom: float,
    exclude_rgb: list[tuple[int, int, int]] | None = None,
) -> dict[str, Any]:
    fallback_rgb = float_color_to_rgba(fill)[:3]
    area = float(cover_rect.width) * float(cover_rect.height)
    source_stats = source_cover_background_stats(source_background_image, cover_rect, fallback_rgb, sample_zoom, exclude_rgb)
    patch_fallback_rgb = tuple(source_stats.get("source_background_rgb") or fallback_rgb)
    should_use_patch = (
        source_background_image is not None
        and area >= BACKGROUND_COVER_IMAGE_PATCH_MIN_AREA_PT2
        and (
            fill_saturation(fill) >= BACKGROUND_COVER_IMAGE_PATCH_SATURATION
            or bool(source_stats.get("image_patch_recommended"))
        )
    )
    if (
        should_use_patch
    ):
        patch = background_cover_patch_png(source_background_image, cover_rect, patch_fallback_rgb, sample_zoom, exclude_rgb)
        if patch is not None:
            png, patch_size = patch
            page.insert_image(cover_rect, stream=png, keep_proportion=False, overlay=True)
            return {
                "draw_mode": "row_sampled_image_patch",
                "sample_zoom": sample_zoom,
                "patch_size_px": list(patch_size),
                "fallback_rgb": list(fallback_rgb),
                "patch_fallback_rgb": list(patch_fallback_rgb),
                **source_stats,
            }
    page.draw_rect(cover_rect, color=None, fill=fill, overlay=True)
    return {
        "draw_mode": "solid_vector_fill",
        "sample_zoom": None,
        "patch_size_px": None,
        "fallback_rgb": list(fallback_rgb),
        "patch_fallback_rgb": None,
        **source_stats,
    }


def apply_region_background_cover(
    page: fitz.Page,
    region: dict[str, Any],
    source_background_image: Image.Image | None,
    sample_zoom: float,
) -> dict[str, Any] | None:
    if not should_apply_region_background_cover(region):
        return None
    cover_rect = source_background_cover_rect(region, page.rect)
    fill = region.get("background_color", (1.0, 1.0, 1.0))
    exclude_rgb = [float_color_to_rgba(item.get("text_color", (0.05, 0.05, 0.05)))[:3] for item in region.get("items", [])]
    draw_result = draw_background_cover(page, cover_rect, fill, source_background_image, sample_zoom, exclude_rgb)
    return {
        "region_id": region.get("region_id"),
        "unit_ids": [item.get("unit_id") for item in region.get("items", [])],
        "page_index": region.get("page_index"),
        "bbox": [round(float(v), 3) for v in cover_rect],
        "fill_color": [round(float(v), 4) for v in fill],
        "excluded_text_colors_rgb": [list(color) for color in exclude_rgb],
        **draw_result,
        "method": "region_background_cover",
        "region_kind": region.get("region_kind"),
        "layout_mode": region.get("layout_mode"),
        "page_type_guess": region.get("page_type_guess"),
        "reason": "cover multiline source redaction bands with one continuous local background before target text insertion",
    }


def fill_saturation(fill: Any) -> float:
    if not isinstance(fill, (list, tuple)) or len(fill) < 3:
        return 0.0
    channels = [float(value) * 255.0 if float(value) <= 1.0 else float(value) for value in fill[:3]]
    return max(channels) - min(channels)


def should_apply_residual_background_cover(
    item: dict[str, Any],
    page_rect: fitz.Rect,
    policy: dict[str, Any],
    page_context: dict[str, Any] | None,
) -> bool:
    if bool(item.get("preserve_background_redaction")):
        return False
    rect = item["rect"]
    if rect.width < float(page_rect.width) * 0.45:
        return False
    if rect.height > 18.0:
        return False
    if fill_saturation(item.get("fill_color")) < 18.0:
        return False
    kind = region_kind([item], page_rect, policy, page_context)
    return kind not in BACKGROUND_COVER_SKIP_KINDS


def residual_cover_rect(group: list[dict[str, Any]], page_rect: fitz.Rect) -> fitz.Rect:
    rect = union_rect([item["rect"] for item in group])
    sizes = [float(item.get("font_size") or 6.0) for item in group]
    source_size = median_float(sizes)
    x_pad = max(0.8, source_size * 0.18)
    y_pad = max(0.9, source_size * 0.18)
    rect.x0 = max(page_rect.x0, rect.x0 - x_pad)
    rect.x1 = min(page_rect.x1, rect.x1 + x_pad)
    rect.y0 = max(page_rect.y0, rect.y0 - y_pad)
    rect.y1 = min(page_rect.y1, rect.y1 + y_pad)
    return rect


def group_residual_cover_items(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: (float(value["rect"].y0), float(value["rect"].x0))):
        if not current:
            current = [item]
            continue
        previous = current[-1]
        gap = float(item["rect"].y0) - float(previous["rect"].y1)
        same_column = column_overlap_ratio(item["rect"], previous["rect"]) >= 0.40
        max_gap = max(24.0, median_float([float(previous.get("font_size") or 6.0), float(item.get("font_size") or 6.0)]) * 3.0)
        if same_column and gap <= max_gap:
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    if current:
        groups.append(current)
    return groups


def apply_residual_background_covers(
    page: fitz.Page,
    page_units: list[dict[str, Any]],
    page_context: dict[str, Any],
    policy: dict[str, Any],
    excluded_unit_ids: set[str],
    source_background_image: Image.Image | None,
    sample_zoom: float,
) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in page_units
        if str(item.get("unit_id")) not in excluded_unit_ids
        and should_apply_residual_background_cover(item, page.rect, policy, page_context)
    ]
    records: list[dict[str, Any]] = []
    for index, group in enumerate(group_residual_cover_items(candidates)):
        cover_rect = residual_cover_rect(group, page.rect)
        fill = dominant_fill_color(group)
        exclude_rgb = [float_color_to_rgba(item.get("text_color", (0.05, 0.05, 0.05)))[:3] for item in group]
        draw_result = draw_background_cover(page, cover_rect, fill, source_background_image, sample_zoom, exclude_rgb)
        records.append(
            {
                "region_id": f"residual_background_cover_{index:03d}",
                "unit_ids": [item.get("unit_id") for item in group],
                "page_index": group[0].get("page_index"),
                "bbox": [round(float(v), 3) for v in cover_rect],
                "fill_color": [round(float(v), 4) for v in fill],
                "excluded_text_colors_rgb": [list(color) for color in exclude_rgb],
                **draw_result,
                "method": "residual_wide_line_background_cover",
                "region_kind": "wide_colored_background_line_group",
                "layout_mode": "residual_cover_before_insertion",
                "page_type_guess": page_context.get("page_type_guess"),
                "reason": "cover remaining wide source-line redactions on colored backgrounds so viewer scaling does not expose line-band patches",
            }
        )
    return records


def probe_textbox_return_code(
    probe_page: fitz.Page,
    region: dict[str, Any],
    fontfile: Path,
    font_size: float,
) -> float:
    return probe_page.insert_textbox(
        region["rect"],
        region["translation_zh"],
        fontsize=font_size,
        fontname="cjk_backfill",
        fontfile=str(fontfile),
        color=region.get("text_color", (0.05, 0.05, 0.05)),
        align=0,
    )


def insert_region(page: fitz.Page, probe_page: fitz.Page, region: dict[str, Any], fontfile: Path, policy: dict[str, Any]) -> dict[str, Any]:
    kind = region["region_kind"]
    draw_mode = policy.get("draw_modes", {}).get(kind, {})
    if isinstance(draw_mode, dict) and draw_mode.get("mode") == "rotated_horizontal_text_image":
        return insert_rotated_horizontal_text_image(page, region, fontfile, policy, draw_mode)
    if isinstance(draw_mode, dict) and draw_mode.get("mode") == "rotated_text":
        return insert_rotated_region(page, region, fontfile, policy, draw_mode)
    source_size = float(region.get("source_size") or 6.0)
    profiles = policy_section(policy, "font_profiles")
    profile = require_mapping(profiles.get(kind) or profiles.get("body"), f"font_profiles.{kind}")
    fallback = policy_section(policy, "fallback")
    page_stats = region.get("page_font_stats")
    page_font_stats = {str(k): float(v) for k, v in page_stats.items()} if isinstance(page_stats, dict) else {}
    base_size = resolve_base_font_size(source_size, profile, fallback, page_font_stats)
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback, source_size, page_font_stats), base_size * scale)
        rc = probe_textbox_return_code(probe_page, region, fontfile, current_size)
        attempts.append({"font_size": round(current_size, 3), "return_code": rc})
        if rc >= 0:
            page.insert_textbox(
                region["rect"],
                region["translation_zh"],
                fontsize=current_size,
                fontname="cjk_backfill",
                fontfile=str(fontfile),
                color=region.get("text_color", (0.05, 0.05, 0.05)),
                align=0,
            )
            return {"status": "fit", "font_size": round(current_size, 3), "attempts": attempts}

    constrained_result = insert_constrained_text_image(page, region, fontfile, policy, profile, fallback, base_size, attempts)
    if constrained_result is not None:
        return constrained_result

    if fallback_forbidden(kind, fallback):
        return {
            "status": "fallback_insert_forbidden",
            "font_size": 0.0,
            "attempts": attempts,
            "fallback_forbidden": True,
            "fallback_forbidden_reason": fallback.get("forbid_reason"),
        }

    fallback_point = fitz.Point(region["rect"].x0, min(page.rect.y1 - 1, region["rect"].y1))
    if kind in set(fallback.get("point_fit_status_kinds", [])):
        page.insert_text(
            fallback_point,
            region["translation_zh"][: policy_int(fallback, "point_fit_max_chars")],
            fontsize=policy_float(fallback, "point_fit_font_pt"),
            fontname="cjk_backfill",
            fontfile=str(fontfile),
            color=region.get("text_color", (0.05, 0.05, 0.05)),
        )
        return {"status": "point_fit", "font_size": policy_float(fallback, "point_fit_font_pt"), "attempts": attempts}
    page.insert_text(
        fallback_point,
        region["translation_zh"][: policy_int(fallback, "fallback_max_chars")],
        fontsize=policy_float(fallback, "fallback_insert_font_pt"),
        fontname="cjk_backfill",
        fontfile=str(fontfile),
        color=region.get("text_color", (0.05, 0.05, 0.05)),
    )
    return {"status": "fallback_insert_text", "font_size": policy_float(fallback, "fallback_insert_font_pt"), "attempts": attempts}


def constrained_image_profile(policy: dict[str, Any]) -> dict[str, Any]:
    data = policy.get("constrained_text_image_fit")
    return data if isinstance(data, dict) else {}


def constrained_image_allowed(region: dict[str, Any], policy: dict[str, Any]) -> bool:
    profile = constrained_image_profile(policy)
    if not bool(profile.get("enabled", False)):
        return False
    kind = str(region.get("region_kind") or "")
    forbidden_kinds = {str(value) for value in profile.get("forbid_region_kinds", [])}
    if kind in forbidden_kinds:
        return False
    allowed_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if kind in allowed_kinds:
        return True
    dense_body_page_types = {str(value) for value in profile.get("dense_single_line_body_page_types", [])}
    if kind != "body" or len(region.get("items", [])) != 1:
        return False
    return str(region.get("page_type_guess") or "") in dense_body_page_types


def constrained_image_wrap_allowed(region: dict[str, Any], policy: dict[str, Any]) -> bool:
    profile = constrained_image_profile(policy)
    kind = str(region.get("region_kind") or "")
    wrapped_kinds = {str(value) for value in profile.get("wrapped_region_kinds", [])}
    return kind in wrapped_kinds


def text_has_wrappable_words(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if " " not in normalized:
        return False
    alpha_count = sum(1 for char in normalized if char.isalpha())
    digit_count = sum(1 for char in normalized if char.isdigit())
    return alpha_count >= 4 and alpha_count > digit_count


def constrained_text_image_png(
    text: str,
    fontfile: Path,
    font_size_pt: float,
    color: tuple[float, float, float],
    background_color: tuple[float, float, float],
) -> tuple[bytes, int, int]:
    scale = 4
    font_px = max(8, int(round(font_size_pt * scale)))
    font = ImageFont.truetype(str(fontfile), font_px)
    normalized = re.sub(r"\s+", " ", text).strip()
    probe = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), normalized, font=font)
    width = max(1, bbox[2] - bbox[0] + scale * 2)
    height = max(1, bbox[3] - bbox[1] + scale * 2)
    image = Image.new("RGBA", (width, height), float_color_to_rgba(background_color))
    draw = ImageDraw.Draw(image)
    draw.text((scale - bbox[0], scale - bbox[1]), normalized, font=font, fill=float_color_to_rgba(color))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), width, height


def wrap_text_for_image(text: str, font: ImageFont.FreeTypeFont, max_width_px: int) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return [""]
    words = normalized.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if font.getlength(candidate) <= max_width_px or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines or [normalized]


def constrained_wrapped_text_image_png(
    text: str,
    fontfile: Path,
    font_size_pt: float,
    color: tuple[float, float, float],
    background_color: tuple[float, float, float],
    target_width_pt: float,
) -> tuple[bytes, int, int]:
    scale = 4
    font_px = max(8, int(round(font_size_pt * scale)))
    font = ImageFont.truetype(str(fontfile), font_px)
    pad = max(4, int(round(font_px * 0.22)))
    max_width = max(12, int(round(target_width_pt * scale)) - pad * 2)
    lines = wrap_text_for_image(text, font, max_width)
    line_gap = max(2, int(round(font_px * 0.28)))
    probe = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    draw = ImageDraw.Draw(probe)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    width = max(1, min(max_width, max((box[2] - box[0] for box in line_boxes), default=max_width))) + pad * 2
    line_heights = [max(1, box[3] - box[1]) for box in line_boxes]
    height = sum(line_heights) + line_gap * max(0, len(lines) - 1) + pad * 2
    image = Image.new("RGBA", (width, max(1, height)), float_color_to_rgba(background_color))
    draw = ImageDraw.Draw(image)
    y = pad
    for line, box, line_height in zip(lines, line_boxes, line_heights):
        draw.text((pad - box[0], y - box[1]), line, font=font, fill=float_color_to_rgba(color))
        y += line_height + line_gap
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), image.width, image.height


def insert_constrained_text_image(
    page: fitz.Page,
    region: dict[str, Any],
    fontfile: Path,
    policy: dict[str, Any],
    font_profile: dict[str, Any],
    fallback: dict[str, Any],
    base_size: float,
    attempts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not constrained_image_allowed(region, policy):
        return None
    profile = constrained_image_profile(policy)
    kind = str(region.get("region_kind") or "")
    source_size = float(region.get("source_size") or 6.0)
    page_stats = region.get("page_font_stats")
    page_font_stats = {str(k): float(v) for k, v in page_stats.items()} if isinstance(page_stats, dict) else {}
    min_pt = float(profile.get("min_font_pt", min_insert_point_size(font_profile, fallback, source_size, page_font_stats)))
    if "min_font_source_ratio" in profile:
        min_pt = max(min_pt, source_size * float(profile.get("min_font_source_ratio")))
    if "min_font_page_quantile" in profile:
        min_pt = max(
            min_pt,
            font_stat_value(page_font_stats, profile.get("min_font_page_quantile"), min_pt)
            * float(profile.get("min_font_page_quantile_scale", 1.0)),
        )
    max_pt = float(profile.get("max_font_pt", base_size))
    if "max_font_source_ratio" in profile:
        max_pt = min(max_pt, source_size * float(profile.get("max_font_source_ratio")))
    if "max_font_page_quantile" in profile:
        max_pt = min(
            max_pt,
            font_stat_value(page_font_stats, profile.get("max_font_page_quantile"), max_pt)
            * float(profile.get("max_font_page_quantile_scale", 1.0)),
        )
    if max_pt < min_pt:
        max_pt = min_pt
    font_size = max(min_pt, min(max_pt, base_size))
    wrap_allowed = constrained_image_wrap_allowed(region, policy)
    png, image_width, image_height = constrained_text_image_png(
        str(region.get("translation_zh") or ""),
        fontfile,
        font_size,
        region.get("text_color", (0.05, 0.05, 0.05)),
        region.get("background_color", (1.0, 1.0, 1.0)),
    )
    target = fitz.Rect(region["rect"])
    if target.width <= 0 or target.height <= 0:
        return None
    if wrap_allowed:
        png, image_width, image_height = constrained_wrapped_text_image_png(
            str(region.get("translation_zh") or ""),
            fontfile,
            font_size,
            region.get("text_color", (0.05, 0.05, 0.05)),
            region.get("background_color", (1.0, 1.0, 1.0)),
            float(target.width),
        )
    else:
        source_aspect = image_width / max(1, image_height)
        target_aspect = float(target.width) / max(0.001, float(target.height))
        unwrapped_compression_ratio = target_aspect / max(0.001, source_aspect)
        wrap_on_compression_kinds = {str(value) for value in profile.get("wrap_on_compression_region_kinds", [])}
        min_ratio = float(profile.get("min_horizontal_compression_ratio", 0.0) or 0.0)
        if (
            kind in wrap_on_compression_kinds
            and min_ratio > 0
            and unwrapped_compression_ratio < min_ratio
            and text_has_wrappable_words(str(region.get("translation_zh") or ""))
        ):
            png, image_width, image_height = constrained_wrapped_text_image_png(
                str(region.get("translation_zh") or ""),
                fontfile,
                font_size,
                region.get("text_color", (0.05, 0.05, 0.05)),
                region.get("background_color", (1.0, 1.0, 1.0)),
                float(target.width),
            )
            wrap_allowed = True
    keep_proportion = bool(profile.get("keep_proportion_for_wrapped", False) and wrap_allowed)
    page.insert_image(target, stream=png, keep_proportion=keep_proportion)
    source_aspect = image_width / max(1, image_height)
    target_aspect = float(target.width) / max(0.001, float(target.height))
    compression_ratio = round(target_aspect / max(0.001, source_aspect), 3)
    return {
        "status": "constrained_text_image_fit",
        "font_size": round(font_size, 3),
        "attempts": attempts,
        "image_width_px": image_width,
        "image_height_px": image_height,
        "target_width_pt": round(float(target.width), 3),
        "target_height_pt": round(float(target.height), 3),
        "horizontal_compression_ratio": compression_ratio,
        "wrapped_text_image": wrap_allowed,
        "keep_proportion": keep_proportion,
        "image_background_color": [round(float(v), 4) for v in region.get("background_color", (1.0, 1.0, 1.0))],
    }


def float_color_to_rgba(color: Any) -> tuple[int, int, int, int]:
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return (13, 13, 13, 255)
    return (
        max(0, min(255, int(round(float(color[0]) * 255)))),
        max(0, min(255, int(round(float(color[1]) * 255)))),
        max(0, min(255, int(round(float(color[2]) * 255)))),
        255,
    )


def target_rect_for_image(slot: fitz.Rect, image_width: int, image_height: int) -> fitz.Rect:
    aspect = max(0.001, image_width / max(1, image_height))
    available_aspect = slot.width / max(0.001, slot.height)
    if aspect > available_aspect:
        width = slot.width
        height = width / aspect
    else:
        height = slot.height
        width = height * aspect
    x0 = slot.x0 + (slot.width - width) / 2
    y0 = slot.y0 + (slot.height - height) / 2
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def rotated_horizontal_text_png(
    text: str,
    fontfile: Path,
    font_size_pt: float,
    color: tuple[float, float, float],
    background_color: tuple[float, float, float],
    rotation: int,
) -> tuple[bytes, int, int]:
    scale = 4
    font_px = max(8, int(round(font_size_pt * scale)))
    font = ImageFont.truetype(str(fontfile), font_px)
    probe = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), text, font=font)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    pad = max(4, int(round(font_px * 0.25)))
    image = Image.new("RGBA", (width + pad * 2, height + pad * 2), float_color_to_rgba(background_color))
    draw = ImageDraw.Draw(image)
    draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=float_color_to_rgba(color))
    rotated = image.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
    stream = io.BytesIO()
    rotated.save(stream, format="PNG")
    return stream.getvalue(), rotated.width, rotated.height


def insert_rotated_horizontal_text_image(
    page: fitz.Page,
    region: dict[str, Any],
    fontfile: Path,
    policy: dict[str, Any],
    draw_mode: dict[str, Any],
) -> dict[str, Any]:
    kind = region["region_kind"]
    source_size = float(region.get("source_size") or 6.0)
    profiles = policy_section(policy, "font_profiles")
    profile = require_mapping(profiles.get(kind) or profiles.get("body"), f"font_profiles.{kind}")
    fallback = policy_section(policy, "fallback")
    text = region["translation_zh"].replace("\n", "")
    rect = region["rect"]
    rotation = int(draw_mode.get("rotation_degrees", 90))
    page_stats = region.get("page_font_stats")
    page_font_stats = {str(k): float(v) for k, v in page_stats.items()} if isinstance(page_stats, dict) else {}
    base_size = resolve_base_font_size(source_size, profile, fallback, page_font_stats)
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback, source_size, page_font_stats), base_size * scale)
        png, image_width, image_height = rotated_horizontal_text_png(
            text,
            fontfile,
            current_size,
            region.get("text_color", (0.05, 0.05, 0.05)),
            region.get("background_color", (1.0, 1.0, 1.0)),
            rotation,
        )
        target = target_rect_for_image(rect, image_width, image_height)
        if target.width <= rect.width + 0.01 and target.height <= rect.height + 0.01:
            page.insert_image(target, stream=png, keep_proportion=True, overlay=True)
            attempts.append(
                {
                    "font_size": round(current_size, 3),
                    "rotation_degrees": rotation,
                    "return_code": 0,
                    "target_bbox": [round(v, 3) for v in target],
                    "image_size": [image_width, image_height],
                }
            )
            return {
                "status": "rotated_horizontal_image_fit",
                "font_size": round(current_size, 3),
                "attempts": attempts,
                "rotation_degrees": rotation,
                "image_bbox": [round(v, 3) for v in target],
                "image_background_color": [round(float(v), 4) for v in region.get("background_color", (1.0, 1.0, 1.0))],
            }
        attempts.append(
            {
                "font_size": round(current_size, 3),
                "rotation_degrees": rotation,
                "return_code": -1,
                "target_bbox": [round(v, 3) for v in target],
                "image_size": [image_width, image_height],
            }
        )
    return {"status": "rotated_horizontal_image_overflow", "font_size": round(base_size, 3), "attempts": attempts, "rotation_degrees": rotation}


def insert_rotated_region(
    page: fitz.Page,
    region: dict[str, Any],
    fontfile: Path,
    policy: dict[str, Any],
    draw_mode: dict[str, Any],
) -> dict[str, Any]:
    kind = region["region_kind"]
    source_size = float(region.get("source_size") or 6.0)
    profiles = policy_section(policy, "font_profiles")
    profile = require_mapping(profiles.get(kind) or profiles.get("body"), f"font_profiles.{kind}")
    fallback = policy_section(policy, "fallback")
    text = region["translation_zh"].replace("\n", "")
    rect = region["rect"]
    rotation = int(draw_mode.get("rotation_degrees", 90))
    max_along_axis = rect.height if rotation in {90, 270} else rect.width
    estimated_char_count = max(1, len(text))
    axis_limited_size = max_along_axis / estimated_char_count * 0.92
    page_stats = region.get("page_font_stats")
    page_font_stats = {str(k): float(v) for k, v in page_stats.items()} if isinstance(page_stats, dict) else {}
    base_size = min(resolve_base_font_size(source_size, profile, fallback, page_font_stats), axis_limited_size)
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback, source_size, page_font_stats), base_size * scale)
        if rotation == 90:
            point = fitz.Point(rect.x0, rect.y1)
        elif rotation == 270:
            point = fitz.Point(rect.x1, rect.y0)
        else:
            point = fitz.Point(rect.x0, rect.y0 + current_size)
        page.insert_text(
            point,
            text,
            fontsize=current_size,
            fontname="cjk_backfill",
            fontfile=str(fontfile),
            color=region.get("text_color", (0.05, 0.05, 0.05)),
            rotate=rotation,
        )
        attempts.append({"font_size": round(current_size, 3), "rotation_degrees": rotation, "return_code": 0})
        return {"status": "rotated_fit", "font_size": round(current_size, 3), "attempts": attempts, "rotation_degrees": rotation}

    return {"status": "rotated_fit", "font_size": round(base_size, 3), "attempts": attempts, "rotation_degrees": rotation}


def generate(
    source: Path,
    extraction_path: Path,
    semantic_translations_path: Path,
    layout_policy_path: Path,
    output: Path,
    translations_path: Path,
    layout_path: Path,
) -> dict[str, Any]:
    ensure_dir(output.parent)
    fontfile = choose_font()
    extraction = read_json(extraction_path)
    semantic_data, semantic_by_id = load_semantic_units(semantic_translations_path)
    layout_policy = load_layout_policy(layout_policy_path)
    source_language = str(semantic_data.get("source_language") or "en")
    target_language = str(semantic_data.get("target_language") or "zh")
    target_field = str(semantic_data.get("target_text_field") or target_text_field(semantic_data))
    layout_policy.setdefault("source_language", source_language)
    layout_policy.setdefault("target_language", target_language)
    layout_policy.setdefault("target_text_field", target_field)
    doc = fitz.open(source)
    translation_units: list[dict[str, Any]] = []
    layout_slots: list[dict[str, Any]] = []
    background_cover_records: list[dict[str, Any]] = []
    redaction_records: list[dict[str, Any]] = []
    insertion_records: list[dict[str, Any]] = []
    missing_unit_ids: list[str] = []
    semantic_translated_unit_count = 0
    preserved_target_language_unit_count = 0

    for page_info in extraction.get("pages", []):
        page_index = int(page_info["page_index"])
        page = doc[page_index]
        page_units: list[dict[str, Any]] = []
        source_background_image: Image.Image | None = page_background_sample_image(page, BACKGROUND_COVER_SAMPLE_ZOOM)
        image_background_rects = page_image_background_rects(page)
        line_repairs = decorative_numeric_merge_repairs(page_info.get("text_lines", []), page.rect)
        for line in page_info.get("text_lines", []):
            source_unit = line_is_translatable(line, source_language)
            preserve_target_span = line_is_already_target_language(line, source_language, target_language)
            if not source_unit and not preserve_target_span:
                continue
            unit_id = str(line["line_id"])
            source_text = str(line.get("text", ""))
            repair = line_repairs.get(unit_id)
            bbox = [float(v) for v in (repair.get("bbox") if repair else line["bbox"])]
            rect = inflate_rect(bbox, page.rect)
            fill_provenance = sample_fill_detail(page, rect)
            fill = fill_provenance["fill_color"]
            text_color = color_int_to_rgb(line.get("color"))
            text_rgb = float_color_to_rgba(text_color)[:3]
            fill_rgb = float_color_to_rgba(fill)[:3]
            image_overlay_background_decision = image_overlay_background_protection_decision(
                source_background_image,
                image_background_rects,
                rect,
                fill_rgb,
                text_rgb,
                BACKGROUND_COVER_SAMPLE_ZOOM,
            )
            translation_mode = "semantic_translation"
            translated = semantic_by_id.get(unit_id)
            if source_unit:
                if translated is None:
                    missing_unit_ids.append(unit_id)
                    continue
                if str(translated.get("source_text", "")).strip() != source_text.strip():
                    raise ValueError(f"{unit_id}: source_text mismatch")
                target_text = get_target_text(translated, target_field)
                reject_bad_translation(unit_id, source_text, target_text, target_language, target_field)
                semantic_translated_unit_count += 1
            else:
                target_text = source_text.strip()
                translated = {
                    "unit_id": unit_id,
                    "source_text": source_text,
                    target_field: target_text,
                    "translation_mode": "preserve_already_target_language_span",
                    "layout_variants": {},
                    "term_decisions": [],
                    "layout_risk": "already_target_language_visible_span",
                }
                translation_mode = "preserve_already_target_language_span"
                preserved_target_language_unit_count += 1

            if repair and repair.get("trim_trailing_digit"):
                target_text = trim_trailing_decorative_digit(target_text, str(repair["trim_trailing_digit"]))
            background_preserve_stats = source_cover_background_stats(
                source_background_image,
                rect,
                fill_rgb,
                BACKGROUND_COVER_SAMPLE_ZOOM,
                [text_rgb],
            )
            preserve_background_redaction = bool(background_preserve_stats.get("image_patch_recommended"))
            preserve_background_redaction = bool(
                preserve_background_redaction
                or image_overlay_background_decision.get("protect_background")
            )
            page_unit = {
                "unit_id": unit_id,
                "page_index": page_index,
                "block_id": line.get("block_id"),
                "line_index": line.get("line_index"),
                "source_text": source_text,
                "translation_zh": target_text,
                "target_text": target_text,
                "target_text_field": target_field,
                "bbox": bbox,
                    "rect": rect,
                    "fill_color": fill,
                    "fill_color_provenance": fill_provenance,
                    "preserve_background_redaction": preserve_background_redaction,
                    "background_preserve_stats": background_preserve_stats,
                    "image_overlay_background_decision": image_overlay_background_decision,
                "font_size": float(line.get("font_size") or 6.0),
                "text_color": text_color,
                "translated": translated,
            }
            if repair:
                page_unit["bbox_repair"] = repair
            translation_units.append(
                {
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "source_text": source_text,
                    "translation_zh": target_text,
                    "target_text": target_text,
                    target_field: target_text,
                    "source_language": source_language,
                    "target_language": target_language,
                    "translation_mode": translation_mode,
                    "semantic_coverage": "full_semantic_translation",
                    "bbox": bbox,
                    "bbox_repair": repair,
                    "text_role": text_kind(source_text),
                    "term_decisions": translated.get("term_decisions", []),
                    "layout_variants": translated.get("layout_variants", {}),
                    "layout_risk": translated.get("layout_risk", "unknown"),
                }
            )
            redaction_records.append(
                {
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "bbox": [round(v, 3) for v in rect],
                    "fill_color": None if preserve_background_redaction else [round(v, 4) for v in fill],
                    "sampled_fill_color": [round(v, 4) for v in fill],
                    "redaction_fill_mode": "text_only_preserve_background" if preserve_background_redaction else "solid_fill",
                    "fill_color_provenance": fill_provenance,
                    "background_preserve_stats": background_preserve_stats,
                    "image_overlay_background_decision": image_overlay_background_decision,
                }
            )
            page.add_redact_annot(rect, fill=None if preserve_background_redaction else fill)
            page_units.append(page_unit)

        if page_units:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            page_context = {
                "page_type_guess": page_info.get("page_type_guess"),
                "text_line_count": page_info.get("text_line_count"),
                "drawing_count": page_info.get("drawing_count"),
                "image_block_count": page_info.get("image_block_count"),
                "font_stats": font_stats([float(unit.get("font_size") or 0.0) for unit in page_units]),
                "line_geometries": [
                    {
                        "bbox": [round(float(value), 3) for value in unit["rect"]],
                        "font_size": round(float(unit.get("font_size") or 0.0), 3),
                    }
                    for unit in page_units
                ],
            }
            regions = build_regions(page_units, page.rect, layout_policy, page_context)
            for region in regions:
                sanitize_region_rect(region, page.rect)
            for region in regions:
                region["page_index"] = page_index
            planned_cover_unit_ids = {
                str(item.get("unit_id"))
                for region in regions
                if should_apply_region_background_cover(region)
                for item in region.get("items", [])
            }
            background_cover_records.extend(
                apply_residual_background_covers(
                    page,
                    page_units,
                    page_context,
                    layout_policy,
                    planned_cover_unit_ids,
                    source_background_image,
                    BACKGROUND_COVER_SAMPLE_ZOOM,
                )
            )
            probe_doc = fitz.open()
            try:
                probe_page = probe_doc.new_page(width=float(page.rect.width), height=float(page.rect.height))
                for region in regions:
                    background_cover = apply_region_background_cover(
                        page,
                        region,
                        source_background_image,
                        BACKGROUND_COVER_SAMPLE_ZOOM,
                    )
                    if background_cover is not None:
                        background_cover_records.append(background_cover)
                    insert_result = insert_region(page, probe_page, region, fontfile, layout_policy)
                    unit_ids = [item["unit_id"] for item in region["items"]]
                    layout_slots.append(
                        {
                            "slot_id": region["region_id"],
                            "unit_ids": unit_ids,
                            "page_index": page_index,
                            "source_block_ids": sorted({str(item.get("block_id")) for item in region["items"] if item.get("block_id") is not None}),
                            "source_line_indexes": [item.get("line_index") for item in region["items"]],
                            "anchor_bbox": [round(v, 3) for v in region["rect"]],
                            "font_file": str(fontfile),
                            "font_size": insert_result["font_size"],
                            "source_font_size": round(float(region.get("source_size") or 0), 3),
                            "line_height": None,
                            "wrap_width": round(region["rect"].width, 3),
                            "fill_color": None,
                            "image_background_color": [round(float(v), 4) for v in region.get("background_color", (1.0, 1.0, 1.0))],
                            "region_kind": region["region_kind"],
                            "layout_mode": region["layout_mode"],
                            "page_type_guess": region.get("page_type_guess"),
                            "target_language_reflow_applied": bool(region.get("target_language_reflow_applied")),
                            "target_language_reflow_profile": region.get("target_language_reflow_profile"),
                            "target_composition_applied": bool(region.get("target_composition_applied")),
                            "target_composition_profile": region.get("target_composition_profile"),
                            "expandable_text_slot_applied": bool(region.get("expandable_text_slot_applied")),
                            "expandable_text_slot_profile": region.get("expandable_text_slot_profile"),
                            "local_constrained_slot_expansion_applied": bool(region.get("local_constrained_slot_expansion_applied")),
                            "local_constrained_slot_expansion_profile": region.get("local_constrained_slot_expansion_profile"),
                            "local_constrained_slot_flow_applied": bool(region.get("local_constrained_slot_flow_applied")),
                            "local_constrained_slot_flow_profile": region.get("local_constrained_slot_flow_profile"),
                            "source_anchor_bbox": region.get("source_anchor_bbox"),
                            "rect_repair": region.get("rect_repair"),
                            "background_cover": background_cover,
                            "layout_policy": rel(layout_policy_path),
                            "draw_mode": layout_policy.get("draw_modes", {}).get(region["region_kind"], {}).get("mode", "textbox"),
                            "rotation_degrees": insert_result.get("rotation_degrees"),
                            "overflow_policy": "probe_then_region_reflow_shrink_then_fallback_insert_text",
                        }
                    )
                    insertion_records.append(
                        {
                            "region_id": region["region_id"],
                            "unit_ids": unit_ids,
                            "page_index": page_index,
                            "source_block_ids": sorted({str(item.get("block_id")) for item in region["items"] if item.get("block_id") is not None}),
                            "source_line_indexes": [item.get("line_index") for item in region["items"]],
                            "bbox": [round(v, 3) for v in region["rect"]],
                            "translation_zh": region["translation_zh"],
                            "target_text": region.get("target_text", region["translation_zh"]),
                            "target_text_field": target_field,
                            "region_kind": region["region_kind"],
                            "layout_mode": region["layout_mode"],
                            "page_type_guess": region.get("page_type_guess"),
                            "target_language_reflow_applied": bool(region.get("target_language_reflow_applied")),
                            "target_language_reflow_profile": region.get("target_language_reflow_profile"),
                            "target_composition_applied": bool(region.get("target_composition_applied")),
                            "target_composition_profile": region.get("target_composition_profile"),
                            "expandable_text_slot_applied": bool(region.get("expandable_text_slot_applied")),
                            "expandable_text_slot_profile": region.get("expandable_text_slot_profile"),
                            "local_constrained_slot_expansion_applied": bool(region.get("local_constrained_slot_expansion_applied")),
                            "local_constrained_slot_expansion_profile": region.get("local_constrained_slot_expansion_profile"),
                            "local_constrained_slot_flow_applied": bool(region.get("local_constrained_slot_flow_applied")),
                            "local_constrained_slot_flow_profile": region.get("local_constrained_slot_flow_profile"),
                            "source_anchor_bbox": region.get("source_anchor_bbox"),
                            "rect_repair": region.get("rect_repair"),
                            "background_cover": background_cover,
                            "redaction_fill_provenance": [
                                {
                                    "unit_id": item.get("unit_id"),
                                    "fill_color": [round(v, 4) for v in item.get("fill_color", [])],
                                    "fill_color_provenance": item.get("fill_color_provenance"),
                                }
                                for item in region["items"]
                            ],
                            **insert_result,
                        }
                    )
            finally:
                probe_doc.close()

    if missing_unit_ids:
        doc.close()
        raise ValueError(f"missing semantic translations for units: {missing_unit_ids[:20]}")

    doc.save(output, garbage=4, deflate=True)
    doc.close()

    translations = {
        "translation_provider": semantic_data.get("translation_provider"),
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": target_field,
        "translation_quality": "semantic_translation",
        "semantic_coverage": "full_semantic_translation",
        "prompt_artifacts": semantic_data.get("prompt_artifacts", []),
        "unit_count": len(translation_units),
        "semantic_translated_unit_count": semantic_translated_unit_count,
        "preserved_target_language_unit_count": preserved_target_language_unit_count,
        "units": translation_units,
    }
    layout = {
        "layout_provider": "region_reflow_semantic_layout",
        "layout_policy": rel(layout_policy_path),
        "layout_policy_version": layout_policy.get("policy_version"),
        "layout_policy_source": layout_policy.get("policy_source"),
        "language_pair_profile": layout_policy.get("language_pair_profile"),
        "language_profile_json": layout_policy.get("language_profile_json"),
        "layout_strategy": layout_policy.get("layout_strategy"),
        "layout_policy_statistics": layout_policy.get("statistics"),
        "slot_count": len(layout_slots),
        "slots": layout_slots,
    }
    write_json(translations_path, translations)
    write_json(layout_path, layout)
    fit_warnings = [
        item
        for item in insertion_records
        if item["status"] not in {"fit", "point_fit", "rotated_fit", "rotated_horizontal_image_fit", "constrained_text_image_fit"}
    ]
    return {
        "tool": "generate_semantic_backfill",
        "strategy": f"redact_extractable_{source_language}_lines_and_insert_semantic_{target_language}_regions",
        "real_backfill_pdf": True,
        "translation_provider": semantic_data.get("translation_provider"),
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": target_field,
        "translation_quality": "semantic_translation",
        "input_semantic_translations": rel(semantic_translations_path),
        "semantic_translation_validation": "PASS",
        "input_pdf": rel(source),
        "source_extraction": rel(extraction_path),
        "layout_policy_json": rel(layout_policy_path),
        "layout_policy_sha256": sha256_file(layout_policy_path),
        "layout_policy_version": layout_policy.get("policy_version"),
        "layout_policy_source": layout_policy.get("policy_source"),
        "language_pair_profile": layout_policy.get("language_pair_profile"),
        "language_profile_json": layout_policy.get("language_profile_json"),
        "layout_strategy": layout_policy.get("layout_strategy"),
        "output_pdf": rel(output),
        "translations_json": rel(translations_path),
        "layout_plan_json": rel(layout_path),
        "output_sha256": sha256_file(output),
        "source_unit_count": len(translation_units),
        "redacted_line_count": len(redaction_records),
        "inserted_line_count": len(redaction_records),
        "inserted_unit_count": len(redaction_records),
        "inserted_region_count": len(insertion_records),
        "semantic_translated_unit_count": semantic_translated_unit_count,
        "preserved_target_language_unit_count": preserved_target_language_unit_count,
        "fit_warning_count": len(fit_warnings),
        "font_file": str(fontfile),
        "semantic_coverage": "full_semantic_translation",
        "layout_policy_statistics": layout_policy.get("statistics"),
        "prompt_artifacts": semantic_data.get("prompt_artifacts", []),
        "background_covers": background_cover_records,
        "redactions": redaction_records,
        "insertions": insertion_records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--semantic-translations", required=True)
    parser.add_argument("--layout-policy", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--translations", required=True)
    parser.add_argument("--layout-plan", required=True)
    args = parser.parse_args()
    result = generate(
        resolve_workspace_path(args.input),
        resolve_workspace_path(args.source_extraction),
        resolve_workspace_path(args.semantic_translations),
        resolve_workspace_path(args.layout_policy),
        Path(args.output),
        Path(args.translations),
        Path(args.layout_plan),
    )
    write_json(Path(args.evidence), result)
    print(args.evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
