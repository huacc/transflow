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


CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_OR_DIGIT_RE = re.compile(r"[A-Za-z0-9]")
UNIT_ID_RE = re.compile(r"^(p\d+_b\d+)_l(\d+)$")
NOTE_LABEL_RE = re.compile(r"^(note|notes):$", re.IGNORECASE)
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


def line_is_translatable(line: dict[str, Any], source_language: str) -> bool:
    text = str(line.get("text", ""))
    if source_language == "zh":
        return bool(CJK_RE.search(text))
    if source_language == "en":
        return bool(line.get("ascii_tokens"))
    return bool(text.strip())


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


def median_float(values: list[float]) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def union_rect(rects: list[fitz.Rect]) -> fitz.Rect:
    rect = fitz.Rect(rects[0])
    for item in rects[1:]:
        rect.include_rect(item)
    return rect


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


def min_insert_point_size(profile: dict[str, Any], fallback: dict[str, Any]) -> float:
    if "min_insert_pt" in profile:
        return float(profile["min_insert_pt"])
    return policy_float(fallback, "min_insert_pt")


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
    compact_label = require_mapping(rules.get("compact_label"), "classification_rules.compact_label")
    if rect.width < policy_float(compact_label, "max_region_width_pt") or median_float([r.width for r in rects]) < policy_float(compact_label, "max_median_line_width_pt"):
        return "compact_label"
    short_label = require_mapping(rules.get("short_label"), "classification_rules.short_label")
    if len(items) <= policy_int(short_label, "max_line_count") and rect.width <= policy_float(short_label, "max_region_width_pt") and median_size >= policy_float(short_label, "min_median_font_size"):
        return "short_label"
    heading = require_mapping(rules.get("heading"), "classification_rules.heading")
    if median_size >= policy_float(heading, "min_median_font_size"):
        return "heading"
    return "body"


def should_reflow_region(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any], page_context: dict[str, Any] | None = None) -> bool:
    reflow = policy_section(policy, "reflow")
    if len(items) < policy_int(reflow, "min_items_for_reflow"):
        return False
    kind = region_kind(items, page_rect, policy, page_context)
    if page_type_guess(page_context) in DENSE_TABLE_PAGE_TYPES and kind not in {"table_note", "footnote"}:
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
                return value.strip()
    return str(item["target_text"]).strip()


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
        if crosses_separator:
            parts.append(current)
            current = []
        current.append(item)
        previous_index = int(current_index) if current_index is not None else None
        previous_block = current_block
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
        if (region.get("target_language_reflow_applied") or region.get("target_composition_applied"))
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
    if hard_disabled_page_type(profile, page_type):
        return
    disabled_page_types = {str(value) for value in profile.get("disable_page_type_guesses", [])}
    page_type_disabled = page_type in disabled_page_types
    if page_type_disabled:
        dense_page_y = profile.get("allow_dense_page_body_below_y_ratio")
        if dense_page_y is None:
            return
        if float(region["rect"].y0) / max(1.0, float(page_rect.height)) < float(dense_page_y):
            return

    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
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
    if hard_disabled_page_type(profile, page_type):
        return
    disabled_page_types = {str(value) for value in profile.get("disable_page_type_guesses", [])}
    page_type_disabled = page_type in disabled_page_types
    if page_type_disabled:
        dense_page_y = profile.get("allow_dense_page_body_below_y_ratio")
        if dense_page_y is None:
            return
        if float(region["rect"].y0) / max(1.0, float(page_rect.height)) < float(dense_page_y):
            return
    rect = fitz.Rect(region["rect"])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
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
    for key in order:
        for part in split_heading_prefix(grouped[key], policy):
            if should_reflow_region(part, page_rect, policy, page_context):
                rect = union_rect([item["rect"] for item in part])
                sizes = [float(item.get("font_size") or 6.0) for item in part]
                kind = region_kind(part, page_rect, policy, page_context)
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
                        }
                    )
    grouped_regions = apply_body_flow_grouping(regions, page_rect, policy)
    for region in grouped_regions:
        apply_target_language_reflow_rect(region, page_rect, policy)
    return grouped_regions


BACKGROUND_COVER_REGION_KINDS = {"body", "body_flow", "heading", "table_note", "footnote", "event_card"}
BACKGROUND_COVER_SKIP_PAGE_TYPES = {"chart_or_dashboard", "matrix_or_table_diagram"}
BACKGROUND_COVER_SKIP_KINDS = {"table_cell", "legend", "vertical_nav", "compact_label", "short_label"}
BACKGROUND_COVER_IMAGE_PATCH_SATURATION = 18.0
BACKGROUND_COVER_IMAGE_PATCH_MIN_AREA_PT2 = 600.0
BACKGROUND_COVER_SAMPLE_ZOOM = 2.0


def should_apply_region_background_cover(region: dict[str, Any]) -> bool:
    kind = str(region.get("region_kind") or "")
    if kind in BACKGROUND_COVER_SKIP_KINDS:
        return False
    page_type = str(region.get("page_type_guess") or "")
    if page_type in BACKGROUND_COVER_SKIP_PAGE_TYPES and kind not in {"table_note", "footnote"}:
        return False
    items = region.get("items") if isinstance(region.get("items"), list) else []
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


def dominant_patch_row_color(samples: list[tuple[int, int, int]], fallback_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if not samples:
        return fallback_rgb
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
) -> tuple[bytes, tuple[int, int]] | None:
    box = cover_pixel_box(rect, source_image, zoom)
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    if width <= 0 or height <= 0:
        return None
    pad = max(4, int(round(6 * zoom)))
    edge = max(2, int(round(3 * zoom)))
    patch = Image.new("RGB", (width, height), fallback_rgb)
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
        row_color = dominant_patch_row_color(samples, fallback_rgb)
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
) -> dict[str, Any]:
    fallback_rgb = float_color_to_rgba(fill)[:3]
    area = float(cover_rect.width) * float(cover_rect.height)
    if (
        source_background_image is not None
        and fill_saturation(fill) >= BACKGROUND_COVER_IMAGE_PATCH_SATURATION
        and area >= BACKGROUND_COVER_IMAGE_PATCH_MIN_AREA_PT2
    ):
        patch = background_cover_patch_png(source_background_image, cover_rect, fallback_rgb, sample_zoom)
        if patch is not None:
            png, patch_size = patch
            page.insert_image(cover_rect, stream=png, keep_proportion=False, overlay=True)
            return {
                "draw_mode": "row_sampled_image_patch",
                "sample_zoom": sample_zoom,
                "patch_size_px": list(patch_size),
                "fallback_rgb": list(fallback_rgb),
            }
    page.draw_rect(cover_rect, color=None, fill=fill, overlay=True)
    return {
        "draw_mode": "solid_vector_fill",
        "sample_zoom": None,
        "patch_size_px": None,
        "fallback_rgb": list(fallback_rgb),
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
    draw_result = draw_background_cover(page, cover_rect, fill, source_background_image, sample_zoom)
    return {
        "region_id": region.get("region_id"),
        "unit_ids": [item.get("unit_id") for item in region.get("items", [])],
        "page_index": region.get("page_index"),
        "bbox": [round(float(v), 3) for v in cover_rect],
        "fill_color": [round(float(v), 4) for v in fill],
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
        draw_result = draw_background_cover(page, cover_rect, fill, source_background_image, sample_zoom)
        records.append(
            {
                "region_id": f"residual_background_cover_{index:03d}",
                "unit_ids": [item.get("unit_id") for item in group],
                "page_index": group[0].get("page_index"),
                "bbox": [round(float(v), 3) for v in cover_rect],
                "fill_color": [round(float(v), 4) for v in fill],
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
    base_size = max(
        policy_float(profile, "min_pt"),
        min(policy_float(profile, "max_pt"), source_size * policy_float(profile, "source_scale")),
    )
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback), base_size * scale)
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
    allowed_kinds = {str(value) for value in profile.get("region_kinds", [])}
    if kind in allowed_kinds:
        return True
    dense_body_page_types = {str(value) for value in profile.get("dense_single_line_body_page_types", [])}
    if kind != "body" or len(region.get("items", [])) != 1:
        return False
    return str(region.get("page_type_guess") or "") in dense_body_page_types


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
    min_pt = float(profile.get("min_font_pt", min_insert_point_size(font_profile, fallback)))
    max_pt = float(profile.get("max_font_pt", base_size))
    font_size = max(min_pt, min(max_pt, base_size))
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
    page.insert_image(target, stream=png, keep_proportion=False)
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
    base_size = max(
        policy_float(profile, "min_pt"),
        min(policy_float(profile, "max_pt"), source_size * policy_float(profile, "source_scale")),
    )
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback), base_size * scale)
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
    base_size = max(
        policy_float(profile, "min_pt"),
        min(policy_float(profile, "max_pt"), source_size * policy_float(profile, "source_scale"), axis_limited_size),
    )
    scales = [float(item) for item in profile.get("shrink_scales", [])]
    if not scales:
        raise ValueError(f"layout policy font profile has no shrink_scales: {kind}")

    attempts = []
    for scale in scales:
        current_size = max(min_insert_point_size(profile, fallback), base_size * scale)
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
        source_background_image: Image.Image | None = None
        for line in page_info.get("text_lines", []):
            source_unit = line_is_translatable(line, source_language)
            preserve_target_span = line_is_already_target_language(line, source_language, target_language)
            if not source_unit and not preserve_target_span:
                continue
            unit_id = str(line["line_id"])
            source_text = str(line.get("text", ""))
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

            bbox = [float(v) for v in line["bbox"]]
            rect = inflate_rect(bbox, page.rect)
            fill_provenance = sample_fill_detail(page, rect)
            fill = fill_provenance["fill_color"]
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
                "font_size": float(line.get("font_size") or 6.0),
                "text_color": color_int_to_rgb(line.get("color")),
                "translated": translated,
            }
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
                    "fill_color": [round(v, 4) for v in fill],
                    "fill_color_provenance": fill_provenance,
                }
            )
            page.add_redact_annot(rect, fill=fill)
            page_units.append(page_unit)

        if page_units:
            source_background_image = page_background_sample_image(page, BACKGROUND_COVER_SAMPLE_ZOOM)
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
            }
            regions = build_regions(page_units, page.rect, layout_policy, page_context)
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
                            "source_anchor_bbox": region.get("source_anchor_bbox"),
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
                            "source_anchor_bbox": region.get("source_anchor_bbox"),
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
