"""Project a role plan into a run-local layout plan.

tool_name: build_layout_plan
category: planners
input_contract: role_plan JSON, layout_policy JSON, output layout plan path
output_contract: layout_plan JSON with target rects, erase rects, draw modes, font profiles, and validation hints
failure_signals: missing role plan, missing policy, invalid rects, empty groups
fallback: caller keeps legacy generator layout path or routes to S_FAIL_PROCESS_CONTRACT when layout plan is mandatory
anti_overfit_statement: computes target rects from current-page role_plan geometry, source font sizes, target text length, and policy metadata only; no sample filename, page number, text, fixed coordinate, or reference PDF is used
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


CONSTRAINED_ROLES = {"table_cell", "legend", "vertical_nav", "nav_footer"}
LOCAL_EXPAND_ROLES = {"heading", "red_heading", "red_note", "compact_panel", "metric_value"}
FLOW_ROLES = {"body", "body_flow", "footnote"}


def rect_values(values: list[Any]) -> list[float]:
    rect = [float(v) for v in values]
    if len(rect) != 4:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(v, 3) for v in rect]


def rect_width(rect: list[float]) -> float:
    return max(0.0, rect[2] - rect[0])


def rect_height(rect: list[float]) -> float:
    return max(0.0, rect[3] - rect[1])


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def expand_rect(rect: list[float], pad_x: float, pad_y: float, page_rect: list[float]) -> list[float]:
    return [
        round(clamp(rect[0] - pad_x, page_rect[0], page_rect[2]), 3),
        round(clamp(rect[1] - pad_y, page_rect[1], page_rect[3]), 3),
        round(clamp(rect[2] + pad_x, page_rect[0], page_rect[2]), 3),
        round(clamp(rect[3] + pad_y, page_rect[1], page_rect[3]), 3),
    ]


def overlap_area(left: list[float], right: list[float]) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def x_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(rect_width(left), rect_width(right)))


def text_length_factor(text: str, target_language: str) -> float:
    if target_language == "zh":
        return max(1.0, len(text) * 0.95)
    return max(1.0, len(text) * 0.52)


def estimate_text_height(text: str, width: float, font_size: float, target_language: str, role: str) -> float:
    usable_width = max(font_size * 2.0, width)
    avg_char_width = max(1.0, font_size * (0.92 if target_language == "zh" else 0.50))
    chars_per_line = max(1, int(usable_width / avg_char_width))
    explicit_lines = max(1, text.count("\n") + 1)
    estimated_lines = max(explicit_lines, math.ceil(text_length_factor(text.replace("\n", " "), target_language) / chars_per_line))
    leading = 1.24 if role in FLOW_ROLES else 1.14
    return estimated_lines * font_size * leading


def same_column_next_top(current: dict[str, Any], groups: list[dict[str, Any]]) -> float | None:
    current_rect = rect_values(current["source_rect"])
    candidates: list[float] = []
    for other in groups:
        if other is current:
            continue
        other_rect = rect_values(other["source_rect"])
        if other_rect[1] <= current_rect[1] + 0.5:
            continue
        if x_overlap_ratio(current_rect, other_rect) < 0.22:
            continue
        candidates.append(other_rect[1])
    return min(candidates) if candidates else None


def role_draw_mode(role: str, policy: dict[str, Any]) -> str:
    draw_modes = policy.get("draw_modes", {}) if isinstance(policy.get("draw_modes"), dict) else {}
    if isinstance(draw_modes.get(role), dict) and draw_modes[role].get("mode"):
        return str(draw_modes[role]["mode"])
    if role == "vertical_nav":
        return "rotated_horizontal_text_image"
    if role in CONSTRAINED_ROLES:
        return "textbox_or_constrained_text_image"
    return "textbox"


def target_rect_for_group(group: dict[str, Any], page_rect: list[float], groups: list[dict[str, Any]], target_language: str) -> tuple[list[float], list[dict[str, Any]], str]:
    role = str(group.get("role") or "body")
    source = rect_values(group.get("source_rect", [0, 0, 0, 0]))
    source_font = max(1.0, float(group.get("source_font_size") or 1.0))
    page_width = max(1.0, page_rect[2] - page_rect[0])
    page_height = max(1.0, page_rect[3] - page_rect[1])
    margin = max(source_font * 1.2, page_width * 0.035)
    adjustments: list[dict[str, Any]] = []

    if role in CONSTRAINED_ROLES:
        target = expand_rect(source, source_font * 0.10, source_font * 0.08, page_rect)
        return target, [{"reason": "constrained_role_preserve_source_slot"}], "constrained_slot"

    target = expand_rect(source, source_font * 0.20, source_font * 0.10, page_rect)
    width_cap = page_width - margin - target[0]
    if role in FLOW_ROLES:
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.45, page_width * 0.52)))
    elif role == "metric_value":
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.35, page_width * 0.22)))
    else:
        desired_width = max(rect_width(source), min(width_cap, max(rect_width(source) * 1.25, page_width * 0.34)))
    if desired_width > rect_width(target) + 0.5:
        target[2] = round(min(page_rect[2] - margin, target[0] + desired_width), 3)
        adjustments.append({"reason": "target_text_growth_width_expand", "desired_width": round(desired_width, 3)})

    target_text = str(group.get("target_text") or "")
    desired_height = estimate_text_height(target_text, rect_width(target), source_font, target_language, role)
    next_top = same_column_next_top(group, groups)
    local_gap = max(1.5, source_font * 0.35)
    bottom_limit = page_rect[3] - max(12.0, page_height * 0.025)
    if next_top is not None:
        bottom_limit = min(bottom_limit, next_top - local_gap)
    height_cap = max(rect_height(target), bottom_limit - target[1])
    desired_height = min(height_cap, max(rect_height(target), desired_height))
    if desired_height > rect_height(target) + 0.5:
        target[3] = round(target[1] + desired_height, 3)
        adjustments.append({"reason": "target_text_growth_height_expand", "desired_height": round(desired_height, 3)})

    if role in FLOW_ROLES:
        mode = "fluid_body" if role == "body_flow" else "source_anchor_expanded_flow"
    elif role in LOCAL_EXPAND_ROLES:
        mode = "local_expandable_slot"
    else:
        mode = "source_anchor_slot"
    return target, adjustments or [{"reason": "source_anchor_rect_retained"}], mode


def build_layout_plan(role_plan_path: Path, layout_policy_path: Path) -> dict[str, Any]:
    role_plan = read_json(role_plan_path)
    layout_policy = read_json(layout_policy_path)
    target_language = str(role_plan.get("target_language") or layout_policy.get("target_language") or "zh")
    pages_out: list[dict[str, Any]] = []
    total_groups = 0
    all_overlaps: list[dict[str, Any]] = []

    for page in role_plan.get("pages", []):
        page_rect = rect_values(page.get("page_rect", [0, 0, 0, 0]))
        source_groups = list(page.get("groups", []))
        planned_groups: list[dict[str, Any]] = []
        for group in source_groups:
            role = str(group.get("role") or "body")
            source_rect = rect_values(group.get("source_rect", [0, 0, 0, 0]))
            target_rect, adjustments, mode = target_rect_for_group(group, page_rect, source_groups, target_language)
            source_font = max(1.0, float(group.get("source_font_size") or 1.0))
            estimated_height = estimate_text_height(str(group.get("target_text") or ""), rect_width(target_rect), source_font, target_language, role)
            fit_status = "estimated_fit" if estimated_height <= rect_height(target_rect) + source_font * 0.30 else "estimated_overflow"
            planned_groups.append(
                {
                    "group_id": group.get("group_id"),
                    "line_ids": group.get("line_ids", []),
                    "role": role,
                    "layout_mode": mode,
                    "source_rect": source_rect,
                    "erase_rect": expand_rect(source_rect, source_font * 0.08, source_font * 0.08, page_rect),
                    "target_rect": target_rect,
                    "target_text": group.get("target_text", ""),
                    "font_profile": {
                        "sizing_mode": "source_relative",
                        "source_font_size": round(source_font, 3),
                        "target_start_source_ratio": 1.0,
                        "target_min_source_ratio": 0.62 if role not in {"metric_value", "heading", "red_heading"} else 0.70,
                        "page_quantile_reference": "current_page_from_role_plan",
                    },
                    "draw_mode": role_draw_mode(role, layout_policy),
                    "fit_estimate": {
                        "status": fit_status,
                        "estimated_text_height": round(estimated_height, 3),
                        "target_height": round(rect_height(target_rect), 3),
                    },
                    "layout_adjustments": adjustments,
                    "source_role_evidence": group.get("role_evidence", {}),
                }
            )

        for left_index, left in enumerate(planned_groups):
            for right in planned_groups[left_index + 1 :]:
                area = overlap_area(left["target_rect"], right["target_rect"])
                if area <= 0:
                    continue
                ratio = area / max(1.0, min(rect_width(left["target_rect"]) * rect_height(left["target_rect"]), rect_width(right["target_rect"]) * rect_height(right["target_rect"])))
                if ratio >= 0.08:
                    all_overlaps.append(
                        {
                            "page_index": int(page.get("page_index", 0)),
                            "left_group_id": left.get("group_id"),
                            "right_group_id": right.get("group_id"),
                            "overlap_ratio_of_smaller": round(ratio, 4),
                            "repair_hint": "layout_plan_overlap_requires_S6_relayout",
                        }
                    )

        total_groups += len(planned_groups)
        pages_out.append(
            {
                "page_index": int(page.get("page_index", 0)),
                "page_rect": page_rect,
                "groups": planned_groups,
            }
        )

    if total_groups <= 0:
        raise ValueError("layout plan requires at least one role group")

    return {
        "tool": "build_layout_plan",
        "policy_version": "layout_plan_v1.shadow_projection",
        "behavior_mode": "legacy_compatible_shadow",
        "role_plan": rel(role_plan_path),
        "role_plan_sha256": sha256_file(role_plan_path),
        "layout_policy": rel(layout_policy_path),
        "layout_policy_sha256": sha256_file(layout_policy_path),
        "source_language": role_plan.get("source_language"),
        "target_language": target_language,
        "target_text_field": role_plan.get("target_text_field"),
        "group_count": total_groups,
        "estimated_overlap_count": len(all_overlaps),
        "estimated_overlaps": all_overlaps[:200],
        "anti_overfit": "all target rects are projected from current role_plan geometry, source font size, target text length, and generic role classes only",
        "pages": pages_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-plan", required=True)
    parser.add_argument("--layout-policy", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    role_plan_path = resolve_workspace_path(args.role_plan)
    layout_policy_path = resolve_workspace_path(args.layout_policy)
    out_path = resolve_workspace_path(args.out)
    write_json(out_path, build_layout_plan(role_plan_path, layout_policy_path))


if __name__ == "__main__":
    main()
