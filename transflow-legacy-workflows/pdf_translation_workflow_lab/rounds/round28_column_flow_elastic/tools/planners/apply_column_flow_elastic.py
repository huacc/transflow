import argparse
import json
from statistics import median
from pathlib import Path
from typing import Any


FLOW_ROLES = {"body", "section_heading", "red_heading", "red_note", "compact_panel"}
PROTECTED_ROLES = {"table_cell", "chart_label", "chart_legend", "metric_value", "nav_footer", "image_caption"}
PARAGRAPH_ROLES = {"body", "compact_panel"}


def rect(values: list[float] | None) -> list[float] | None:
    if not values or len(values) != 4:
        return None
    return [float(value) for value in values]


def width(values: list[float]) -> float:
    return max(0.0, values[2] - values[0])


def height(values: list[float]) -> float:
    return max(0.0, values[3] - values[1])


def x_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(width(left), width(right)))


def update_rects(group: dict[str, Any], target: list[float], erase_padding: float = 1.2) -> None:
    source = rect(group.get("source_rect")) or target
    group["target_rect"] = [round(value, 3) for value in target]
    # Background must not be used as a layout variable. Erase source text only;
    # do not wipe the expanded target area.
    group["erase_rect"] = [
        round(source[0] - erase_padding, 3),
        round(source[1] - erase_padding, 3),
        round(source[2] + erase_padding, 3),
        round(source[3] + erase_padding, 3),
    ]
    group["erase_policy"] = "source_text_only"


def estimated_text_height(group: dict[str, Any], target_width: float, expansion_prior: str) -> float:
    text = str(group.get("target_text") or "")
    source_size = max(4.0, float(group.get("source_font_size") or 8.0))
    font_size = max(4.0, float(group.get("font_start") or source_size))
    role = str(group.get("role") or "")
    avg_factor = 0.43 if expansion_prior == "target_longer_vertical_expand" else 0.50
    if role in {"section_heading", "red_heading"}:
        avg_factor *= 1.08
    chars_per_line = max(4, int(max(12.0, target_width) / max(2.0, font_size * avg_factor)))
    explicit_lines = max(1, text.count("\n") + 1)
    estimated_lines = max(explicit_lines, (len(text.replace("\n", " ")) + chars_per_line - 1) // chars_per_line)
    leading = 1.24 if role in {"body", "compact_panel"} else 1.16
    return max(font_size * leading, estimated_lines * font_size * leading)


def text_len(group: dict[str, Any]) -> int:
    return len(str(group.get("target_text") or "").strip())


def page_paragraph_font(groups: list[dict[str, Any]]) -> dict[str, float]:
    paragraph_groups = [
        group
        for group in groups
        if str(group.get("role") or "") in PARAGRAPH_ROLES and text_len(group) > 0
    ]
    if not paragraph_groups:
        paragraph_groups = [
            group
            for group in groups
            if str(group.get("role") or "") in FLOW_ROLES - {"red_heading", "red_note"} and text_len(group) > 0
        ]
    font_starts = [float(group.get("font_start") or group.get("source_font_size") or 0.0) for group in paragraph_groups]
    source_sizes = [float(group.get("source_font_size") or group.get("font_start") or 0.0) for group in paragraph_groups]
    lengths = [text_len(group) for group in paragraph_groups]
    font_starts = [value for value in font_starts if value > 0]
    source_sizes = [value for value in source_sizes if value > 0]
    lengths = [value for value in lengths if value > 0]
    return {
        "font_start": float(median(font_starts)) if font_starts else 8.0,
        "source_font": float(median(source_sizes)) if source_sizes else 8.0,
        "target_text_len": float(median(lengths)) if lengths else 0.0,
    }


def is_paragraph_like(group: dict[str, Any], paragraph_font: dict[str, float]) -> bool:
    role = str(group.get("role") or "")
    if role in PARAGRAPH_ROLES:
        return True
    if role != "section_heading":
        return False
    source_size = float(group.get("source_font_size") or group.get("font_start") or 0.0)
    source_median = max(1.0, paragraph_font.get("source_font") or source_size or 1.0)
    median_len = max(1.0, paragraph_font.get("target_text_len") or 1.0)
    # Source-derived demotion: long section-heading-labelled runs whose font is
    # close to paragraph font are body text, not standalone headings.
    return source_size <= source_median * 1.18 and text_len(group) >= median_len * 0.72


def apply_uniform_paragraph_font(group: dict[str, Any], paragraph_font: dict[str, float]) -> bool:
    if not is_paragraph_like(group, paragraph_font):
        return False
    font_start = max(4.0, float(paragraph_font.get("font_start") or group.get("font_start") or 8.0))
    group["font_start"] = round(font_start, 3)
    group["font_min"] = round(max(float(group.get("font_min") or 0.0), font_start * 0.94), 3)
    group["font_policy"] = {
        "policy_id": "uniform_paragraph_font_within_page_flow",
        "font_start": round(font_start, 3),
        "font_min": group["font_min"],
        "basis": "median current-page paragraph font from body/compact flow groups",
    }
    return True


def column_for_group(group: dict[str, Any], columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    source = rect(group.get("source_rect"))
    if not source or not columns:
        return None
    best = None
    best_score = -1.0
    for column in columns:
        c_rect = [float(column["x0"]), source[1], float(column["x1"]), source[3]]
        score = x_overlap_ratio(source, c_rect)
        if score > best_score:
            best = column
            best_score = score
    return best if best_score >= 0.12 else None


def source_gap(previous: dict[str, Any], current: dict[str, Any]) -> float:
    prev = rect(previous.get("source_rect"))
    cur = rect(current.get("source_rect"))
    if not prev or not cur:
        return 4.0
    return max(2.0, cur[1] - prev[3])


def reflow_page(page: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    page_rect = rect(page.get("page_rect")) or [0.0, 0.0, 612.0, 792.0]
    columns = profile.get("columns") or []
    expansion_prior = ((profile.get("language_pair") or {}).get("expansion_prior") or "measure_from_actual_translation")
    if not profile.get("normal_flow_enabled") or not columns:
        for group in page.get("groups") or []:
            target = rect(group.get("target_rect"))
            if target:
                update_rects(group, target)
        return {
            "page_index": page.get("page_index"),
            "applied": False,
            "reason": "page_profile_not_normal_flow_or_no_columns",
            "page_role": profile.get("page_role"),
            "layout_flow": profile.get("layout_flow"),
        }

    paragraph_font = page_paragraph_font(page.get("groups") or [])
    uniform_font_updates = 0
    flow_groups_by_column: dict[str, list[dict[str, Any]]] = {column["column_id"]: [] for column in columns}
    protected_count = 0
    for group in page.get("groups") or []:
        role = str(group.get("role") or "")
        target = rect(group.get("target_rect"))
        if not target:
            continue
        if role in FLOW_ROLES:
            if apply_uniform_paragraph_font(group, paragraph_font):
                uniform_font_updates += 1
            column = column_for_group(group, columns)
            if column:
                flow_groups_by_column[str(column["column_id"])].append(group)
                continue
        protected_count += 1
        update_rects(group, target)

    operations = []
    overflow_groups = []
    bottom_limit = page_rect[3] - max(12.0, height(page_rect) * 0.025)
    for column in columns:
        column_id = str(column["column_id"])
        groups = sorted(flow_groups_by_column[column_id], key=lambda item: ((rect(item.get("source_rect")) or [0, 0, 0, 0])[1], (rect(item.get("source_rect")) or [0, 0, 0, 0])[0]))
        cursor_y: float | None = None
        previous: dict[str, Any] | None = None
        for group in groups:
            source = rect(group.get("source_rect"))
            if not source:
                continue
            top = source[1] if cursor_y is None else max(source[1], cursor_y + source_gap(previous, group))
            col_x0 = float(column["x0"])
            col_x1 = float(column["x1"])
            desired_h = max(height(source), estimated_text_height(group, col_x1 - col_x0, expansion_prior) * 1.04)
            target = [col_x0, top, col_x1, top + desired_h]
            if target[3] > bottom_limit:
                overflow_groups.append(
                    {
                        "group_id": group.get("group_id"),
                        "column_id": column_id,
                        "bottom_limit": round(bottom_limit, 3),
                        "target_bottom": round(target[3], 3),
                    }
                )
                target[3] = bottom_limit
            update_rects(group, target)
            group.setdefault("flow_adjustments", []).append(
                {
                    "reason": "column_width_invariant_vertical_flow_elastic",
                    "column_id": column_id,
                    "source_width": round(width(source), 3),
                    "target_width": round(width(target), 3),
                    "estimated_text_height": round(desired_h, 3),
                    "background_policy": "source_text_only",
                }
            )
            operations.append(
                {
                    "group_id": group.get("group_id"),
                    "column_id": column_id,
                    "source_rect": group.get("source_rect"),
                    "target_rect": group.get("target_rect"),
                }
            )
            cursor_y = target[3]
            previous = group

    return {
        "page_index": page.get("page_index"),
        "applied": True,
        "page_role": profile.get("page_role"),
        "layout_flow": profile.get("layout_flow"),
        "column_count": len(columns),
        "operation_count": len(operations),
        "uniform_font_update_count": uniform_font_updates,
        "uniform_paragraph_font": {key: round(value, 3) for key, value in paragraph_font.items()},
        "protected_group_count": protected_count,
        "overflow_group_count": len(overflow_groups),
        "overflow_groups": overflow_groups[:20],
    }


def run(layout_plan: Path, page_profiles: Path, output: Path, evidence: Path) -> None:
    plan = json.loads(layout_plan.read_text(encoding="utf-8"))
    profiles_data = json.loads(page_profiles.read_text(encoding="utf-8"))
    profiles = {int(item["page_index"]): item for item in profiles_data.get("profiles", [])}
    page_reports = []
    for page in plan.get("pages", []):
        page_index = int(page.get("page_index") or 0)
        profile = profiles.get(page_index, {"page_index": page_index, "normal_flow_enabled": False, "columns": []})
        page_reports.append(reflow_page(page, profile))
    plan["layout_policy"] = {
        "policy_id": "column_width_invariant_vertical_flow_elastic",
        "background_mutation_policy": "source_text_only_no_target_background_wipe",
        "anti_overfit_statement": "Only current-run page profiles, columns, source bboxes, roles, and translated text length are used.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    report = {
        "tool": "apply_column_flow_elastic",
        "source_layout_plan": str(layout_plan),
        "page_profiles": str(page_profiles),
        "output_layout_plan": str(output),
        "page_reports": page_reports,
        "summary": {
            "applied_pages": sum(1 for item in page_reports if item.get("applied")),
            "skipped_pages": sum(1 for item in page_reports if not item.get("applied")),
            "overflow_pages": sum(1 for item in page_reports if item.get("overflow_group_count")),
            "uniform_font_updates": sum(int(item.get("uniform_font_update_count") or 0) for item in page_reports),
        },
    }
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-plan", type=Path, required=True)
    parser.add_argument("--page-profiles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    run(args.layout_plan, args.page_profiles, args.output, args.evidence)


if __name__ == "__main__":
    main()
