"""Build a run-local layout policy from extracted PDF structure.

tool_name: build_layout_policy
category: planners
input_contract: source extraction JSON, optional semantic translations JSON, output policy path
output_contract: layout policy JSON consumed by generate_semantic_backfill.py
failure_signals: missing extraction, empty translatable units, invalid JSON
fallback: caller records S_FAIL_PROCESS_CONTRACT or requests D4/model policy revision
anti_overfit_statement: derives parameters from current-run geometry/font statistics and never branches on sample filename, known page number, exact text, or document identity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, write_json  # noqa: E402


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return float(ordered[index])


def median(values: list[float]) -> float:
    return quantile(values, 0.5)


def collect_units(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for page in extraction.get("pages", []):
        rect = page.get("rect", [0.0, 0.0, 1.0, 1.0])
        page_width = max(1.0, float(rect[2]) - float(rect[0]))
        page_height = max(1.0, float(rect[3]) - float(rect[1]))
        for line in page.get("text_lines", []):
            if not line.get("ascii_tokens"):
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


def build_policy(extraction_path: Path, translations_path: Path | None = None) -> dict[str, Any]:
    extraction = read_json(extraction_path)
    units = collect_units(extraction)
    if not units:
        raise ValueError("layout policy requires at least one extractable ASCII text unit")

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

    policy = {
        "tool": "build_layout_policy",
        "policy_version": "2026-07-05.region_reflow_policy_v1",
        "policy_source": "auto_from_current_extraction_statistics",
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
            },
            "footnote": {
                "max_median_font_size": round(max(font_q25, min(font_q50, font_q25 + 0.75)), 3),
                "min_y_ratio": 0.60,
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
            "heading": {
                "min_median_font_size": round(max(font_q75, font_q50 * 1.25), 3),
            },
        },
        "region_expansion": {
            "default": {"x_pad_pt": 1.0, "y_pad_min_pt": 0.6, "y_pad_source_size_ratio": 0.28},
            "footnote": {"x_pad_pt": 0.5, "y_pad_min_pt": 0.45, "y_pad_source_size_ratio": 0.22},
        },
        "reflow": {
            "reflow_kinds": ["body", "body_flow", "table_note", "footnote", "heading", "short_label"],
            "preserve_line_kinds": ["vertical_nav", "legend", "compact_label"],
            "min_items_for_reflow": 2,
        },
        "flow_grouping": {
            "body": {
                "enabled": True,
                "min_region_count": 4,
                "min_region_width_page_ratio": 0.45,
                "max_x0_delta_pt": round(max(12.0, font_q50 * 2.0), 3),
                "max_width_delta_ratio": 0.18,
                "paragraph_separator": "\n\n",
                "target_region_kind": "body_flow",
                "source": "current_run_width_alignment_and_user_feedback",
            }
        },
        "draw_modes": {
            "vertical_nav": {
                "mode": "rotated_text",
                "rotation_degrees": 90,
                "single_line": True,
                "center_on_source_bbox": True,
            }
        },
        "layout_text_variants": {
            "compact_label": ["compact_label_zh", "compact_zh", "display_zh"],
            "short_label": ["short_label_zh", "compact_zh", "display_zh"],
        },
        "font_profiles": {
            "footnote": {
                "source_scale": round(footnote_scale, 3),
                "min_pt": 3.8,
                "max_pt": round(max(4.8, min(font_q25 * footnote_scale, 5.8)), 3),
                "shrink_scales": [1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64],
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
            "heading": {
                "source_scale": 0.95,
                "min_pt": 5.0,
                "max_pt": round(max(8.0, min(max(font_q95 * 1.05, font_max * 0.98), font_max)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68],
            },
            "short_label": {
                "source_scale": 0.95,
                "min_pt": 5.0,
                "max_pt": round(max(6.0, min(font_q50 * 1.05, 11.0)), 3),
                "shrink_scales": [1.0, 0.92, 0.84, 0.76, 0.68],
            },
            "body": {
                "source_scale": round(body_scale, 3),
                "min_pt": 5.2,
                "max_pt": round(max(7.8, min(max(font_q50 * 1.15, font_q75 * 1.10), 11.5)), 3),
                "shrink_scales": [1.0, 0.94, 0.88, 0.82, 0.76, 0.70],
            },
            "body_flow": {
                "source_scale": round(max(body_scale, 1.12), 3),
                "min_pt": 5.6,
                "max_pt": round(max(8.4, min(max(font_q50 * 1.24, font_q75 * 1.16), 12.4)), 3),
                "shrink_scales": [1.0, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76],
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
    return policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-extraction", required=True)
    parser.add_argument("--semantic-translations", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    policy = build_policy(
        resolve_workspace_path(args.source_extraction),
        resolve_workspace_path(args.semantic_translations) if args.semantic_translations else None,
    )
    write_json(Path(args.out), policy)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
