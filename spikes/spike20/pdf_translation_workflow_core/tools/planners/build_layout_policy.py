"""Build a run-local layout policy from extracted PDF structure.

tool_name: build_layout_policy
category: planners
input_contract: source extraction JSON, optional semantic translations JSON, optional generic language profile JSON, output policy path
output_contract: layout policy JSON consumed by generate_semantic_backfill.py, including language_pair_profile/layout_strategy when profile is supplied
failure_signals: missing extraction, empty translatable units, invalid JSON, language profile mismatch
fallback: caller records S_FAIL_PROCESS_CONTRACT or requests D4/model policy revision
anti_overfit_statement: derives parameters from current-run geometry/font statistics and never branches on sample filename, known page number, exact text, or document identity
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


CJK_RE = re.compile(r"[\u3400-\u9fff]")


def normalize_language(value: Any, default: str) -> str:
    text = str(value or default).strip().lower()
    if text in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "中文"}:
        return "zh"
    if text in {"en", "en-us", "en-gb", "english", "英文"}:
        return "en"
    return text or default


def line_is_translatable(line: dict[str, Any], source_language: str) -> bool:
    text = str(line.get("text", ""))
    if source_language == "zh":
        return bool(CJK_RE.search(text))
    if source_language == "en":
        return bool(line.get("ascii_tokens"))
    return bool(text.strip())


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return float(ordered[index])


def median(values: list[float]) -> float:
    return quantile(values, 0.5)


def collect_units(extraction: dict[str, Any], source_language: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for page in extraction.get("pages", []):
        rect = page.get("rect", [0.0, 0.0, 1.0, 1.0])
        page_width = max(1.0, float(rect[2]) - float(rect[0]))
        page_height = max(1.0, float(rect[3]) - float(rect[1]))
        for line in page.get("text_lines", []):
            if not line_is_translatable(line, source_language):
                continue
            bbox = [float(v) for v in line.get("bbox", [0, 0, 0, 0])]
            units.append(
                {
                    "font_size": float(line.get("font_size") or 0.0),
                    "width": max(0.0, bbox[2] - bbox[0]),
                    "height": max(0.0, bbox[3] - bbox[1]),
                    "x_ratio": bbox[0] / page_width,
                    "y_ratio": bbox[1] / page_height,
                    "page_width": page_width,
                    "page_height": page_height,
                }
            )
    return units


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_language_profile(profile_path: Path | None) -> dict[str, Any]:
    if profile_path is None:
        return {}
    profile = read_json(profile_path)
    if not isinstance(profile, dict):
        raise ValueError("language profile must be a JSON object")
    return profile


def build_policy(extraction_path: Path, translations_path: Path | None = None, language_profile_path: Path | None = None) -> dict[str, Any]:
    extraction = read_json(extraction_path)
    translations = read_json(translations_path) if translations_path is not None else {}
    language_profile = load_language_profile(language_profile_path)
    source_language = normalize_language(
        translations.get("source_language") or language_profile.get("source_language"),
        "en",
    )
    target_language = normalize_language(
        translations.get("target_language") or language_profile.get("target_language"),
        "zh",
    )
    target_text_field = (
        translations.get("target_text_field")
        or language_profile.get("target_text_field")
        or ("translation_zh" if target_language == "zh" else "translation_en")
    )
    if language_profile:
        profile_source = normalize_language(language_profile.get("source_language"), source_language)
        profile_target = normalize_language(language_profile.get("target_language"), target_language)
        if profile_source != source_language or profile_target != target_language:
            raise ValueError(
                f"language profile does not match translations: profile={profile_source}->{profile_target}; "
                f"translations={source_language}->{target_language}"
            )
    units = collect_units(extraction, source_language)
    if not units:
        raise ValueError("layout policy requires at least one extractable source text unit")

    font_sizes = [unit["font_size"] for unit in units if unit["font_size"] > 0]
    widths = [unit["width"] for unit in units if unit["width"] > 0]
    heights = [unit["height"] for unit in units if unit["height"] > 0]
    page_widths = [unit["page_width"] for unit in units]
    page_heights = [unit["page_height"] for unit in units]

    font_q25 = quantile(font_sizes, 0.25)
    font_q50 = quantile(font_sizes, 0.50)
    font_q75 = quantile(font_sizes, 0.75)
    font_q95 = quantile(font_sizes, 0.95)
    font_max = max(font_sizes)
    width_q25 = quantile(widths, 0.25)
    width_q50 = quantile(widths, 0.50)

    body_scale = 1.04 if font_q50 >= 8.0 else 1.0
    footnote_scale = 0.78 if font_q25 <= 7.5 else 0.84
    compact_scale = 0.66 if width_q25 < 32 else 0.74
    body_flow_min_region_count = 2 if target_language == "en" else 4
    body_min_pt = 4.4 if target_language == "en" else 5.2
    body_flow_min_pt = 4.8 if target_language == "en" else 5.6

    policy = {
        "tool": "build_layout_policy",
        "policy_version": "2026-07-05.region_reflow_policy_v1",
        "policy_source": "auto_from_current_extraction_statistics",
        "source_language": source_language,
        "target_language": target_language,
        "target_text_field": target_text_field,
        "language_pair_profile": language_profile.get("profile_id") or f"{source_language}_to_{target_language}",
        "language_profile_json": None if language_profile_path is None else rel(language_profile_path),
        "language_profile_sha256": None if language_profile_path is None else sha256_file(language_profile_path),
        "layout_strategy": language_profile.get("layout_strategy") or "source_anchor_preserving_region_reflow",
        "source_extraction": rel(extraction_path),
        "semantic_translations": None if translations_path is None else rel(translations_path),
        "statistics": {
            "unit_count": len(units),
            "font_size": {
                "q25": round(font_q25, 3),
                "q50": round(font_q50, 3),
                "q75": round(font_q75, 3),
                "q95": round(font_q95, 3),
                "min": round(min(font_sizes), 3),
                "max": round(font_max, 3),
            },
            "line_width": {
                "q25": round(width_q25, 3),
                "q50": round(width_q50, 3),
                "min": round(min(widths), 3),
                "max": round(max(widths), 3),
            },
            "line_height": {
                "q50": round(median(heights), 3),
                "min": round(min(heights), 3),
                "max": round(max(heights), 3),
            },
            "page": {
                "width_q50": round(median(page_widths), 3),
                "height_q50": round(median(page_heights), 3),
            },
        },
        "classification_rules": {
            "table_note": {
                "marker_regex": "^(note|notes):$",
                "min_line_count": 2,
                "min_region_width_page_ratio": 0.45,
                "max_median_font_size": round(max(font_q25, min(font_q50, font_q25 + 0.75)), 3),
                "min_y_ratio": 0.40,
                "dense_page_min_y_ratio": 0.72,
            },
            "footnote": {
                "max_median_font_size": round(max(font_q25, min(font_q50, font_q25 + 0.75)), 3),
                "min_y_ratio": 0.60,
                "dense_page_min_y_ratio": 0.68,
            },
            "event_card": {
                "page_type_guesses": ["mixed_image_text"],
                "max_line_count": 6,
                "max_region_width_page_ratio": 0.24,
                "min_y_ratio": 0.12,
                "max_y_ratio": 0.92,
            },
            "vertical_nav": {
                "max_region_width_pt": round(max(12.0, min(width_q25, 22.0)), 3),
                "min_height_width_ratio": 1.8,
            },
            "compact_label": {
                "max_region_width_pt": round(max(18.0, min(width_q25 * 1.25, 42.0)), 3),
                "max_median_line_width_pt": round(max(18.0, min(width_q25 * 1.25, 42.0)), 3),
            },
            "short_label": {
                "max_line_count": 2,
                "max_region_width_pt": round(max(90.0, min(width_q50 * 1.45, 160.0)), 3),
                "min_median_font_size": round(max(6.0, font_q50 * 0.85), 3),
            },
            "legend": {
                "min_line_count": 3,
                "max_region_width_pt": round(max(80.0, min(width_q50 * 1.25, 140.0)), 3),
                "max_median_line_width_pt": round(max(52.0, min(width_q25 * 3.0, 95.0)), 3),
            },
            "table_cell": {
                "page_type_guesses": ["table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"],
                "max_line_count": 2,
                "max_region_width_pt": round(max(70.0, min(width_q50 * 1.85, 160.0)), 3),
                "max_median_font_size": round(max(font_q50, font_q75), 3),
            },
            "heading": {
                "min_median_font_size": round(max(font_q75, font_q50 * 1.25), 3),
            },
        },
        "region_expansion": {
            "default": {"x_pad_pt": 1.0, "y_pad_min_pt": 0.6, "y_pad_source_size_ratio": 0.28},
            "footnote": {"x_pad_pt": 0.5, "y_pad_min_pt": 0.45, "y_pad_source_size_ratio": 0.22},
        },
        "reflow": {
            "reflow_kinds": ["body", "body_flow", "event_card", "table_note", "footnote", "heading", "short_label"],
            "preserve_line_kinds": ["vertical_nav", "legend", "compact_label", "table_cell"],
            "min_items_for_reflow": 2,
        },
        "flow_grouping": {
            "body": {
                "enabled": True,
                "min_region_count": body_flow_min_region_count,
                "min_region_width_page_ratio": 0.45,
                "max_x0_delta_pt": round(max(12.0, font_q50 * 2.0), 3),
                "max_width_delta_ratio": 0.18,
                "max_vertical_gap_pt": round(max(18.0, font_q50 * 3.0), 3),
                "paragraph_gap_pt": round(max(10.0, font_q50 * 1.8), 3),
                "line_joiner_en": " ",
                "line_joiner_zh": "",
                "include_line_preserve_body": target_language == "en",
                "hard_disable_page_type_guesses": ["matrix_or_table_diagram"],
                "disable_page_type_guesses": ["table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"],
                "paragraph_separator": "\n\n",
                "target_region_kind": "body_flow",
                "source": "current_run_width_alignment_and_user_feedback",
            }
        },
        "source_separator_policy": {
            "split_on_untranslated_visible_line_gap": True,
            "max_line_index_gap_without_split": 1,
            "reason": "do not reflow translated text across visible source headings, years, bullets, or separators that are not translation units",
        },
        "draw_modes": {
            "vertical_nav": {
                "mode": "rotated_horizontal_text_image",
                "render_backend": "PIL_transparent_png",
                "writing_mode": "horizontal_line_rotated_as_unit",
                "glyph_orientation": "rotate_glyphs_with_line",
                "rotation_degrees": 90,
                "single_line": True,
                "center_on_source_bbox": True,
            }
        },
        "constrained_text_image_fit": {
            "enabled": True,
            "region_kinds": ["table_cell", "compact_label", "short_label", "legend", "table_note", "footnote"],
            "wrapped_region_kinds": ["table_note", "footnote"],
            "dense_single_line_body_page_types": ["table_or_chart_dense", "chart_or_dashboard", "matrix_or_table_diagram"],
            "min_font_pt": 3.2,
            "max_font_pt": 5.2,
            "reason": "Use a transparent text image only for constrained label slots after textbox probing fails; preserve full target text and record compression evidence.",
        },
        "layout_text_variants": {
            "compact_label": ["compact_label_zh", "compact_zh", "display_zh"],
            "short_label": ["short_label_zh", "compact_zh", "display_zh"],
            "table_cell": ["table_cell_zh", "compact_label_zh", "compact_zh", "display_zh"],
            "legend": ["legend_zh", "compact_label_zh", "compact_zh", "display_zh"],
            "compact_label_en": ["compact_label_en", "compact_en", "display_en"],
            "short_label_en": ["short_label_en", "compact_en", "display_en"],
            "table_cell_en": ["table_cell_en", "compact_label_en", "compact_en", "display_en"],
            "legend_en": ["legend_en", "compact_label_en", "compact_en", "display_en"],
            "event_card_en": ["event_card_en", "short_label_en", "compact_en", "display_en"],
            "event_card_zh": ["event_card_zh", "short_label_zh", "compact_zh", "display_zh"],
            "heading_en": ["heading_en", "short_label_en", "compact_label_en", "display_en"],
            "heading_zh": ["heading_zh", "short_label_zh", "compact_label_zh", "display_zh"],
        },
        "font_profiles": {
            "footnote": {
                "source_scale": round(footnote_scale, 3),
                "min_pt": 3.8,
                "max_pt": round(max(4.8, min(font_q25 * footnote_scale, 5.8)), 3),
                "shrink_scales": [1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64, 0.58],
            },
            "table_note": {
                "source_scale": 1.05,
                "min_pt": 5.2,
                "max_pt": round(max(6.2, min(font_q50 * 0.95, 8.2)), 3),
                "shrink_scales": [1.0, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76],
            },
            "compact_label": {
                "source_scale": round(compact_scale, 3),
                "min_pt": 3.5,
                "max_pt": round(max(4.2, min(font_q25 * compact_scale, 5.2)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68, 0.60],
            },
            "table_cell": {
                "source_scale": round(max(0.72, min(0.92, compact_scale + 0.14)), 3),
                "min_pt": 3.2,
                "max_pt": round(max(4.8, min(font_q50 * 0.92, 7.4)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68, 0.60, 0.54, 0.48],
            },
            "legend": {
                "source_scale": round(max(0.78, min(0.98, compact_scale + 0.18)), 3),
                "min_pt": 4.0,
                "max_pt": round(max(5.2, min(font_q50 * 0.98, 8.0)), 3),
                "shrink_scales": [1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64],
            },
            "heading": {
                "source_scale": 0.95,
                "min_pt": 5.0,
                "min_insert_pt": 6.2,
                "max_pt": round(max(8.0, min(max(font_q95 * 1.05, font_max * 0.98), font_max)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52, 0.44, 0.36, 0.30],
            },
            "short_label": {
                "source_scale": 0.95,
                "min_pt": 5.0,
                "max_pt": round(max(6.0, min(font_q50 * 1.05, 11.0)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68],
            },
            "event_card": {
                "source_scale": 0.86,
                "min_pt": 4.4,
                "min_insert_pt": 4.0,
                "max_pt": round(max(5.8, min(font_q50 * 0.92, 7.4)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68, 0.60],
            },
            "body": {
                "source_scale": round(body_scale, 3),
                "min_pt": body_min_pt,
                "max_pt": round(max(7.8, min(max(font_q50 * 1.15, font_q75 * 1.10), 11.5)), 3),
                "shrink_scales": [1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64, 0.58, 0.52, 0.46],
            },
            "body_flow": {
                "source_scale": round(max(body_scale, 1.12), 3),
                "min_pt": body_flow_min_pt,
                "max_pt": round(max(8.4, min(max(font_q50 * 1.24, font_q75 * 1.16), 12.4)), 3),
                "shrink_scales": [1.0, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76, 0.70, 0.64, 0.58, 0.52, 0.46],
            },
        },
        "fallback": {
            "min_insert_pt": 3.5,
            "point_fit_status_kinds": ["compact_label", "short_label"],
            "point_fit_font_pt": 3.3,
            "point_fit_max_chars": 18,
            "fallback_insert_font_pt": 3.5,
            "fallback_max_chars": 24,
        },
        "anti_overfit": {
            "no_filename_branch": True,
            "no_known_text_branch": True,
            "no_fixed_page_number_branch": True,
            "parameters_are_current_run_policy": True,
        },
    }
    overrides = language_profile.get("policy_overrides")
    if isinstance(overrides, dict):
        deep_merge(policy, overrides)
    return policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--semantic-translations", default=None)
    parser.add_argument("--language-profile", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    policy = build_policy(
        resolve_workspace_path(args.source_extraction),
        resolve_workspace_path(args.semantic_translations) if args.semantic_translations else None,
        resolve_workspace_path(args.language_profile) if args.language_profile else None,
    )
    write_json(Path(args.out), policy)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
