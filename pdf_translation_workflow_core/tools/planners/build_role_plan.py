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


def is_reddish(value: Any) -> bool:
    rgb = rgb_from_int(value)
    if rgb is None:
        return False
    red, green, blue = rgb
    return red >= max(green, blue) + 32 and red >= 120


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
    return {
        "page_width": width,
        "page_height": height,
        "font_q25": quantile(fonts, 0.25),
        "font_q50": quantile(fonts, 0.50),
        "font_q75": quantile(fonts, 0.75),
        "font_q95": quantile(fonts, 0.95),
        "font_max": max(fonts) if fonts else 0.0,
        "width_q25": quantile(widths, 0.25),
        "width_q50": quantile(widths, 0.50),
        "width_q75": quantile(widths, 0.75),
        "line_count": len(lines),
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
        page_width = float(stat["page_width"])
        groups: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for line in page.get("text_lines", []):
            if not manifest_line_is_translatable(line, source_language):
                continue
            required_count += 1
            unit_id = str(line.get("line_id"))
            unit = by_id.get(unit_id)
            if not unit:
                missing_units.append(unit_id)
                continue
            target_text = get_target_text(unit, target_field)
            role, reason, features = classify_line(line, stat, target_text, target_language)
            rect = rect_values(line.get("bbox", [0, 0, 0, 0]))
            item = {
                "unit_id": unit_id,
                "line": line,
                "rect": rect,
                "role": role,
                "target_text": target_text,
                "reason": reason,
                "features": features,
            }
            if pending and can_merge(pending[-1], {"role": role, "lines": [line], "rect": rect}, page_width):
                pending[-1]["lines"].append(line)
                pending[-1]["rect"] = union_rect([pending[-1]["rect"], rect])
                pending[-1]["target_texts"].append(target_text)
                pending[-1]["unit_ids"].append(unit_id)
                pending[-1]["features"].append(features)
                continue
            pending.append(
                {
                    "role": role,
                    "lines": [line],
                    "rect": rect,
                    "target_texts": [target_text],
                    "unit_ids": [unit_id],
                    "reason": reason,
                    "features": [features],
                }
            )

        for index, pending_group in enumerate(pending):
            role = str(pending_group["role"])
            if role == "body" and len(pending_group["unit_ids"]) >= 2 and rect_width(pending_group["rect"]) / max(1.0, page_width) >= 0.26:
                role = "body_flow"
            source_font_sizes = [float(line.get("font_size") or 0.0) for line in pending_group["lines"]]
            source_rect = pending_group["rect"]
            text_joiner = "\n" if role in {"table_cell", "legend", "compact_panel", "vertical_nav"} else " "
            groups.append(
                {
                    "group_id": f"p{page.get('page_index', 0)}_g{index:04d}_{role}",
                    "line_ids": pending_group["unit_ids"],
                    "role": role,
                    "source_rect": source_rect,
                    "target_text": text_joiner.join(text for text in pending_group["target_texts"] if text).strip(),
                    "source_font_size": round(quantile(source_font_sizes, 0.5), 3),
                    "source_font_sizes": [round(value, 3) for value in source_font_sizes],
                    "source_colors": [line.get("dominant_text_color", line.get("color")) for line in pending_group["lines"]],
                    "role_evidence": {
                        "source_relative_features": pending_group["features"],
                        "decision_reason": pending_group["reason"],
                        "anti_overfit": "current page geometry, font quantiles, color, page type, and generic value/note patterns only",
                    },
                }
            )
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
