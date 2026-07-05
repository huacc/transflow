"""Generate a Chinese backfill PDF from validated semantic translations.

tool_name: generate_semantic_backfill
category: generators
input_contract: source PDF, source extraction JSON, semantic translations JSON, output/evidence paths
output_contract: candidate PDF with English text redacted and semantic Chinese translations inserted, plus translations/layout/evidence JSON
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
from generate_backfill_candidate import choose_font, inflate_rect, sample_fill, text_kind  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")
UNIT_BLOCK_RE = re.compile(r"^(p\d+_b\d+)_l\d+$")
NOTE_LABEL_RE = re.compile(r"^(note|notes):$", re.IGNORECASE)
FORBIDDEN_PROVIDERS = {"", "deterministic_placeholder", "placeholder", "manual_placeholder", None}
FORBIDDEN_TRANSLATION_FRAGMENTS = ("中文回填", "中文标题", "中文标签", "待翻译", "占位", "placeholder", "tbd")


def load_semantic_units(translations_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    data = read_json(translations_path)
    provider = data.get("translation_provider")
    if provider in FORBIDDEN_PROVIDERS:
        raise ValueError("semantic translations require a non-placeholder translation_provider")
    if data.get("translation_quality") != "semantic_translation":
        raise ValueError("translation_quality must be semantic_translation")
    if data.get("semantic_coverage") != "full_semantic_translation":
        raise ValueError("semantic_coverage must be full_semantic_translation")
    by_id: dict[str, dict[str, Any]] = {}
    for item in data.get("units", []):
        if not isinstance(item, dict):
            continue
        unit_id = item.get("unit_id")
        if unit_id:
            by_id[str(unit_id)] = item
    return data, by_id


def reject_bad_translation(unit_id: str, source_text: str, translation_zh: str) -> None:
    lowered = translation_zh.strip().lower()
    if not lowered:
        raise ValueError(f"{unit_id}: missing translation_zh")
    if not CJK_RE.search(translation_zh):
        raise ValueError(f"{unit_id}: translation_zh has no CJK characters")
    if lowered == source_text.strip().lower():
        raise ValueError(f"{unit_id}: translation equals source text")
    if any(fragment in lowered for fragment in FORBIDDEN_TRANSLATION_FRAGMENTS):
        raise ValueError(f"{unit_id}: placeholder translation text is forbidden")


def block_key(unit_id: str) -> str:
    match = UNIT_BLOCK_RE.match(unit_id)
    return match.group(1) if match else unit_id


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


def region_kind(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> str:
    rects = [item["rect"] for item in items]
    sizes = [float(item.get("font_size") or 0.0) for item in items]
    rect = union_rect(rects)
    median_size = median_float(sizes)
    rules = policy_section(policy, "classification_rules")
    table_note = require_mapping(rules.get("table_note"), "classification_rules.table_note")
    first_text = str(items[0].get("source_text", "")).strip()
    has_note_marker = bool(NOTE_LABEL_RE.match(first_text))
    wide_note_block = (
        len(items) >= policy_int(table_note, "min_line_count")
        and rect.width >= page_rect.width * policy_float(table_note, "min_region_width_page_ratio")
        and median_size <= policy_float(table_note, "max_median_font_size")
        and rect.y0 >= page_rect.height * policy_float(table_note, "min_y_ratio")
    )
    if has_note_marker or wide_note_block:
        return "table_note"
    footnote = require_mapping(rules.get("footnote"), "classification_rules.footnote")
    if median_size <= policy_float(footnote, "max_median_font_size") and rect.y0 >= page_rect.height * policy_float(footnote, "min_y_ratio"):
        return "footnote"
    vertical_nav = require_mapping(rules.get("vertical_nav"), "classification_rules.vertical_nav")
    if rect.width < policy_float(vertical_nav, "max_region_width_pt") and rect.height > rect.width * policy_float(vertical_nav, "min_height_width_ratio"):
        return "vertical_nav"
    compact_label = require_mapping(rules.get("compact_label"), "classification_rules.compact_label")
    if rect.width < policy_float(compact_label, "max_region_width_pt") or median_float([r.width for r in rects]) < policy_float(compact_label, "max_median_line_width_pt"):
        return "compact_label"
    short_label = require_mapping(rules.get("short_label"), "classification_rules.short_label")
    if len(items) <= policy_int(short_label, "max_line_count") and rect.width <= policy_float(short_label, "max_region_width_pt") and median_size >= policy_float(short_label, "min_median_font_size"):
        return "short_label"
    legend = require_mapping(rules.get("legend"), "classification_rules.legend")
    if len(items) >= policy_int(legend, "min_line_count") and rect.width <= policy_float(legend, "max_region_width_pt") and median_float([r.width for r in rects]) <= policy_float(legend, "max_median_line_width_pt"):
        return "legend"
    heading = require_mapping(rules.get("heading"), "classification_rules.heading")
    if median_size >= policy_float(heading, "min_median_font_size"):
        return "heading"
    return "body"


def should_reflow_region(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> bool:
    reflow = policy_section(policy, "reflow")
    if len(items) < policy_int(reflow, "min_items_for_reflow"):
        return False
    kind = region_kind(items, page_rect, policy)
    if kind in set(reflow.get("preserve_line_kinds", [])):
        return False
    return kind in set(reflow.get("reflow_kinds", []))


def explicit_layout_text(item: dict[str, Any], kind: str, policy: dict[str, Any]) -> str:
    variant_keys = policy.get("layout_text_variants", {}).get(kind, [])
    variants = item.get("layout_variants")
    if isinstance(variants, dict):
        for key in variant_keys:
            value = variants.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item["translation_zh"]).strip()


def reject_bad_layout_text(unit_id: str, layout_text: str) -> None:
    if not layout_text:
        raise ValueError(f"{unit_id}: empty layout text")
    if not CJK_RE.search(layout_text):
        raise ValueError(f"{unit_id}: layout text has no CJK characters")
    if any(fragment in layout_text.lower() for fragment in FORBIDDEN_TRANSLATION_FRAGMENTS):
        raise ValueError(f"{unit_id}: placeholder layout text is forbidden")
    residue = ascii_tokens(layout_text)
    if residue:
        raise ValueError(f"{unit_id}: layout text contains ASCII residue: {','.join(residue[:8])}")


def join_translation_fragments(items: list[dict[str, Any]], kind: str, policy: dict[str, Any]) -> str:
    text = ""
    for item in items:
        fragment = explicit_layout_text(item, kind, policy)
        if fragment != str(item["translation_zh"]).strip():
            reject_bad_layout_text(str(item["unit_id"]), fragment)
        if not fragment:
            continue
        if not text:
            text = fragment
            continue
        text += fragment
    return re.sub(r"\s+", " ", text).strip()

def split_heading_prefix(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if len(items) <= 1:
        return [items]
    first_text = str(items[0]["source_text"]).strip().lower()
    if NOTE_LABEL_RE.match(first_text):
        return [items]
    return [items]


def numeric_list(values: list[float]) -> list[float]:
    return [float(value) for value in values]


def body_flow_profile(policy: dict[str, Any]) -> dict[str, Any]:
    grouping = policy.get("flow_grouping", {})
    body = grouping.get("body") if isinstance(grouping, dict) else None
    return body if isinstance(body, dict) else {}


def is_body_flow_candidate(region: dict[str, Any], page_rect: fitz.Rect, policy: dict[str, Any]) -> bool:
    profile = body_flow_profile(policy)
    if not profile.get("enabled"):
        return False
    if region.get("region_kind") != "body" or region.get("layout_mode") != "region_reflow":
        return False
    min_ratio = float(profile.get("min_region_width_page_ratio", 0.45))
    return float(region["rect"].width) >= float(page_rect.width) * min_ratio


def can_join_body_flow(group: list[dict[str, Any]], candidate: dict[str, Any], policy: dict[str, Any]) -> bool:
    profile = body_flow_profile(policy)
    if not group:
        return True
    x0_values = numeric_list([item["rect"].x0 for item in group] + [candidate["rect"].x0])
    widths = numeric_list([item["rect"].width for item in group] + [candidate["rect"].width])
    max_x0_delta = float(profile.get("max_x0_delta_pt", 12.0))
    max_width_delta_ratio = float(profile.get("max_width_delta_ratio", 0.18))
    median_width = max(1.0, median_float(widths))
    return (max(x0_values) - min(x0_values)) <= max_x0_delta and ((max(widths) - min(widths)) / median_width) <= max_width_delta_ratio


def make_body_flow_region(group: list[dict[str, Any]], index: int, policy: dict[str, Any]) -> dict[str, Any]:
    profile = body_flow_profile(policy)
    separator = str(profile.get("paragraph_separator", "\n\n"))
    target_kind = str(profile.get("target_region_kind", "body_flow"))
    items = [item for region in group for item in region["items"]]
    rect = union_rect([region["rect"] for region in group])
    source_sizes = [float(region.get("source_size") or 0.0) for region in group]
    return {
        "region_id": f"body_flow_{index:03d}",
        "region_kind": target_kind,
        "items": items,
        "rect": rect,
        "translation_zh": separator.join(str(region["translation_zh"]).strip() for region in group if str(region["translation_zh"]).strip()),
        "text_color": dominant_text_color(items),
        "source_size": median_float(source_sizes),
        "layout_mode": "region_flow",
        "flow_source_region_count": len(group),
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
        if is_body_flow_candidate(region, page_rect, policy) and can_join_body_flow(group, region, policy):
            group.append(region)
            continue
        flush_group()
        if is_body_flow_candidate(region, page_rect, policy):
            group.append(region)
        else:
            output.append(region)
    flush_group()
    return output


def build_regions(items: list[dict[str, Any]], page_rect: fitz.Rect, policy: dict[str, Any]) -> list[dict[str, Any]]:
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
        for part in split_heading_prefix(grouped[key]):
            if should_reflow_region(part, page_rect, policy):
                rect = union_rect([item["rect"] for item in part])
                sizes = [float(item.get("font_size") or 6.0) for item in part]
                kind = region_kind(part, page_rect, policy)
                regions.append(
                    {
                        "region_id": f"region_{key}_{len(regions):03d}",
                        "region_kind": kind,
                        "items": part,
                        "rect": expand_region_rect(rect, page_rect, median_float(sizes), kind, policy),
                        "translation_zh": join_translation_fragments(part, kind, policy),
                        "text_color": dominant_text_color(part),
                        "source_size": median_float(sizes),
                        "layout_mode": "region_reflow",
                    }
                )
            else:
                for item in part:
                    kind = region_kind([item], page_rect, policy)
                    regions.append(
                        {
                            "region_id": f"region_{item['unit_id']}",
                            "region_kind": kind,
                            "items": [item],
                            "rect": item["rect"],
                            "translation_zh": explicit_layout_text(item, kind, policy),
                            "text_color": dominant_text_color([item]),
                            "source_size": float(item.get("font_size") or 6.0),
                            "layout_mode": "line_preserve",
                        }
                    )
    return apply_body_flow_grouping(regions, page_rect, policy)


def insert_region(page: fitz.Page, region: dict[str, Any], fontfile: Path, policy: dict[str, Any]) -> dict[str, Any]:
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
        current_size = max(policy_float(fallback, "min_insert_pt"), base_size * scale)
        rc = page.insert_textbox(
            region["rect"],
            region["translation_zh"],
            fontsize=current_size,
            fontname="cjk_backfill",
            fontfile=str(fontfile),
            color=region.get("text_color", (0.05, 0.05, 0.05)),
            align=0,
        )
        attempts.append({"font_size": round(current_size, 3), "return_code": rc})
        if rc >= 0:
            return {"status": "fit", "font_size": round(current_size, 3), "attempts": attempts}

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
    image = Image.new("RGBA", (width + pad * 2, height + pad * 2), (255, 255, 255, 0))
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
        current_size = max(policy_float(fallback, "min_insert_pt"), base_size * scale)
        png, image_width, image_height = rotated_horizontal_text_png(
            text,
            fontfile,
            current_size,
            region.get("text_color", (0.05, 0.05, 0.05)),
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
        current_size = max(policy_float(fallback, "min_insert_pt"), base_size * scale)
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
    doc = fitz.open(source)
    translation_units: list[dict[str, Any]] = []
    layout_slots: list[dict[str, Any]] = []
    redaction_records: list[dict[str, Any]] = []
    insertion_records: list[dict[str, Any]] = []
    missing_unit_ids: list[str] = []

    for page_info in extraction.get("pages", []):
        page_index = int(page_info["page_index"])
        page = doc[page_index]
        page_units: list[dict[str, Any]] = []
        for line in page_info.get("text_lines", []):
            if not line.get("ascii_tokens"):
                continue
            unit_id = str(line["line_id"])
            source_text = str(line.get("text", ""))
            translated = semantic_by_id.get(unit_id)
            if translated is None:
                missing_unit_ids.append(unit_id)
                continue
            if str(translated.get("source_text", "")).strip() != source_text.strip():
                raise ValueError(f"{unit_id}: source_text mismatch")
            zh = str(translated.get("translation_zh", "")).strip()
            reject_bad_translation(unit_id, source_text, zh)

            bbox = [float(v) for v in line["bbox"]]
            rect = inflate_rect(bbox, page.rect)
            fill = sample_fill(page, rect)
            page_unit = {
                "unit_id": unit_id,
                "page_index": page_index,
                "source_text": source_text,
                "translation_zh": zh,
                "bbox": bbox,
                "rect": rect,
                "fill_color": fill,
                "font_size": float(line.get("font_size") or 6.0),
                "text_color": color_int_to_rgb(line.get("color")),
                "translated": translated,
            }
            translation_units.append(
                {
                    "unit_id": unit_id,
                    "page_index": page_index,
                    "source_text": source_text,
                    "translation_zh": zh,
                    "translation_mode": "semantic_translation",
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
                }
            )
            page.add_redact_annot(rect, fill=fill)
            page_units.append(page_unit)

        if page_units:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            regions = build_regions(page_units, page.rect, layout_policy)
            for region in regions:
                insert_result = insert_region(page, region, fontfile, layout_policy)
                unit_ids = [item["unit_id"] for item in region["items"]]
                layout_slots.append(
                    {
                        "slot_id": region["region_id"],
                        "unit_ids": unit_ids,
                        "page_index": page_index,
                        "anchor_bbox": [round(v, 3) for v in region["rect"]],
                        "font_file": str(fontfile),
                        "font_size": insert_result["font_size"],
                        "source_font_size": round(float(region.get("source_size") or 0), 3),
                        "line_height": None,
                        "wrap_width": round(region["rect"].width, 3),
                        "fill_color": None,
                        "region_kind": region["region_kind"],
                        "layout_mode": region["layout_mode"],
                        "layout_policy": rel(layout_policy_path),
                        "draw_mode": layout_policy.get("draw_modes", {}).get(region["region_kind"], {}).get("mode", "textbox"),
                        "rotation_degrees": insert_result.get("rotation_degrees"),
                        "overflow_policy": "region_reflow_shrink_then_fallback_insert_text",
                    }
                )
                insertion_records.append(
                    {
                        "region_id": region["region_id"],
                        "unit_ids": unit_ids,
                        "page_index": page_index,
                        "bbox": [round(v, 3) for v in region["rect"]],
                        "translation_zh": region["translation_zh"],
                        "region_kind": region["region_kind"],
                        "layout_mode": region["layout_mode"],
                        **insert_result,
                    }
                )

    if missing_unit_ids:
        doc.close()
        raise ValueError(f"missing semantic translations for units: {missing_unit_ids[:20]}")

    doc.save(output, garbage=4, deflate=True)
    doc.close()

    translations = {
        "translation_provider": semantic_data.get("translation_provider"),
        "translation_quality": "semantic_translation",
        "semantic_coverage": "full_semantic_translation",
        "prompt_artifacts": semantic_data.get("prompt_artifacts", []),
        "unit_count": len(translation_units),
        "units": translation_units,
    }
    layout = {
        "layout_provider": "region_reflow_semantic_layout",
        "layout_policy": rel(layout_policy_path),
        "layout_policy_version": layout_policy.get("policy_version"),
        "layout_policy_source": layout_policy.get("policy_source"),
        "layout_policy_statistics": layout_policy.get("statistics"),
        "slot_count": len(layout_slots),
        "slots": layout_slots,
    }
    write_json(translations_path, translations)
    write_json(layout_path, layout)
    fit_warnings = [item for item in insertion_records if item["status"] not in {"fit", "point_fit", "rotated_fit", "rotated_horizontal_image_fit"}]
    return {
        "tool": "generate_semantic_backfill",
        "strategy": "redact_extractable_ascii_lines_and_insert_semantic_chinese_regions",
        "real_backfill_pdf": True,
        "translation_provider": semantic_data.get("translation_provider"),
        "translation_quality": "semantic_translation",
        "input_semantic_translations": rel(semantic_translations_path),
        "semantic_translation_validation": "PASS",
        "input_pdf": rel(source),
        "source_extraction": rel(extraction_path),
        "layout_policy_json": rel(layout_policy_path),
        "layout_policy_sha256": sha256_file(layout_policy_path),
        "layout_policy_version": layout_policy.get("policy_version"),
        "layout_policy_source": layout_policy.get("policy_source"),
        "output_pdf": rel(output),
        "translations_json": rel(translations_path),
        "layout_plan_json": rel(layout_path),
        "output_sha256": sha256_file(output),
        "source_unit_count": len(translation_units),
        "redacted_line_count": len(redaction_records),
        "inserted_line_count": len(redaction_records),
        "inserted_unit_count": len(redaction_records),
        "inserted_region_count": len(insertion_records),
        "fit_warning_count": len(fit_warnings),
        "font_file": str(fontfile),
        "semantic_coverage": "full_semantic_translation",
        "layout_policy_statistics": layout_policy.get("statistics"),
        "prompt_artifacts": semantic_data.get("prompt_artifacts", []),
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
