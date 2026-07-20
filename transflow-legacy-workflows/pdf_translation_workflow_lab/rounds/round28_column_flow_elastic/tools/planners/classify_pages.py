import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


NORMAL_TEXT_ROLES = {"body", "section_heading", "red_heading", "red_note", "compact_panel"}
TABLE_ROLES = {"table_cell"}
CHART_ROLES = {"chart_label", "chart_legend", "metric_value"}
VISUAL_ROLES = {"title", "metric_value", "nav_footer"}


def rect(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if not values or len(values) != 4:
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def width(values: tuple[float, float, float, float]) -> float:
    return max(0.0, values[2] - values[0])


def height(values: tuple[float, float, float, float]) -> float:
    return max(0.0, values[3] - values[1])


def cluster_columns(groups: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    candidates = []
    for group in groups:
        role = str(group.get("role") or "")
        if role not in NORMAL_TEXT_ROLES:
            continue
        r = rect(group.get("source_rect"))
        if not r or width(r) < page_width * 0.05:
            continue
        candidates.append({"x0": r[0], "x1": r[2], "y0": r[1], "y1": r[3], "role": role})
    candidates.sort(key=lambda item: (item["x0"], item["y0"]))
    clusters: list[list[dict[str, Any]]] = []
    threshold = max(24.0, page_width * 0.10)
    for item in candidates:
        if not clusters:
            clusters.append([item])
            continue
        current_x0 = sum(entry["x0"] for entry in clusters[-1]) / len(clusters[-1])
        if abs(item["x0"] - current_x0) > threshold:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    columns = []
    for index, cluster in enumerate(clusters):
        columns.append(
            {
                "column_id": f"c{index}",
                "x0": round(min(item["x0"] for item in cluster), 3),
                "x1": round(max(item["x1"] for item in cluster), 3),
                "y0": round(min(item["y0"] for item in cluster), 3),
                "y1": round(max(item["y1"] for item in cluster), 3),
                "group_count": len(cluster),
            }
        )
    return columns


def classify_page(page: dict[str, Any], source_language: str, target_language: str) -> dict[str, Any]:
    page_rect = rect(page.get("page_rect")) or (0.0, 0.0, 612.0, 792.0)
    page_width = width(page_rect)
    page_height = height(page_rect)
    groups = list(page.get("groups") or [])
    role_counts = Counter(str(group.get("role") or "unknown") for group in groups)
    text_count = sum(role_counts[role] for role in NORMAL_TEXT_ROLES)
    table_count = sum(role_counts[role] for role in TABLE_ROLES)
    chart_count = sum(role_counts[role] for role in CHART_ROLES)
    metric_count = role_counts.get("metric_value", 0)
    columns = cluster_columns(groups, page_width)
    density_score = len(groups) / max(1.0, page_height / 100.0)
    if len(groups) >= 80 or density_score >= 8.0:
        density = "high"
    elif len(groups) >= 32 or density_score >= 3.8:
        density = "medium"
    else:
        density = "low"

    table_ratio = table_count / max(1, len(groups))
    chart_ratio = chart_count / max(1, len(groups))
    if table_count >= 8 or table_ratio >= 0.22:
        page_role = "financial_table_page"
        layout_flow = "table_grid"
    elif metric_count >= 4 or chart_ratio >= 0.28:
        page_role = "chart_or_metric_page"
        layout_flow = "chart_metric_grid"
    elif text_count >= max(5, len(groups) * 0.45):
        page_role = "body_text_page"
        layout_flow = "multi_column_text" if len(columns) >= 2 else "single_column_text"
    elif role_counts.get("title", 0) and len(groups) <= 16:
        page_role = "cover_or_section_page"
        layout_flow = "visual_freeform"
    else:
        page_role = "mixed_page"
        layout_flow = "multi_column_text" if len(columns) >= 2 and text_count >= 4 else "visual_freeform"

    normal_flow_enabled = layout_flow in {"single_column_text", "multi_column_text"} and table_ratio < 0.20 and chart_ratio < 0.25
    if source_language.startswith("zh") and target_language.startswith("en"):
        expansion_prior = "target_longer_vertical_expand"
    elif source_language.startswith("en") and target_language.startswith("zh"):
        expansion_prior = "target_shorter_vertical_contract"
    else:
        expansion_prior = "measure_from_actual_translation"

    return {
        "page_index": page.get("page_index"),
        "page_role": page_role,
        "layout_flow": layout_flow,
        "density_level": density,
        "column_count": len(columns),
        "columns": columns,
        "normal_flow_enabled": normal_flow_enabled,
        "language_pair": {"source": source_language, "target": target_language, "expansion_prior": expansion_prior},
        "role_counts": dict(role_counts),
        "classification_basis": {
            "group_count": len(groups),
            "text_count": text_count,
            "table_count": table_count,
            "chart_count": chart_count,
            "table_ratio": round(table_ratio, 4),
            "chart_ratio": round(chart_ratio, 4),
            "density_score": round(density_score, 4),
        },
    }


def run(role_plan: Path, output: Path, source_language: str, target_language: str) -> None:
    data = json.loads(role_plan.read_text(encoding="utf-8"))
    profiles = [classify_page(page, source_language, target_language) for page in data.get("pages", [])]
    report = {
        "tool": "classify_pages",
        "source_role_plan": str(role_plan),
        "anti_overfit_statement": "Page classes are derived from current-run roles, bboxes, role counts, density, and language pair. No filename, fixed page number, text literal, or reference PDF is used.",
        "profiles": profiles,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--target-language", default="en")
    args = parser.parse_args()
    run(args.role_plan, args.output, args.source_language, args.target_language)


if __name__ == "__main__":
    main()
