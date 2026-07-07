import argparse
import json
from pathlib import Path


def rect_from_values(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if not values or len(values) != 4:
        return None
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


def x_overlap_ratio(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    left_width = max(1.0, left[2] - left[0])
    right_width = max(1.0, right[2] - right[0])
    return overlap / max(1.0, min(left_width, right_width))


def y_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def validate(generation_evidence: Path, output: Path) -> None:
    data = json.loads(generation_evidence.read_text(encoding="utf-8"))
    groups = [group for page in data.get("pages", []) for group in page.get("groups", [])]
    blocking = []
    for group in groups:
        if group.get("role") == "nav_footer":
            continue
        if group.get("fit_status") != "fit":
            blocking.append(
                {
                    "gate_id": "all_groups_fit",
                    "group_id": group.get("group_id"),
                    "dimension": "overflow",
                    "repair_family": "expand_or_reflow_slot",
                    "evidence": f"fit_status={group.get('fit_status')}",
                }
            )
        source_size = float(group.get("source_font_size") or 0)
        output_size = float(group.get("output_font_size") or 0)
        if output_size and source_size and output_size < max(4.8, source_size * 0.52):
            blocking.append(
                {
                    "gate_id": "source_relative_font_floor",
                    "group_id": group.get("group_id"),
                    "dimension": "font_floor",
                    "repair_family": "reflow_before_shrink",
                    "evidence": f"source={source_size}; output={output_size}",
                }
            )
    for page in data.get("pages", []):
        page_groups = [group for group in page.get("groups", []) if group.get("role") != "nav_footer" and group.get("output_rect")]
        for index, group in enumerate(page_groups):
            group_rect = rect_from_values(group.get("output_rect"))
            if group_rect is None:
                continue
            for other in page_groups[index + 1 :]:
                other_rect = rect_from_values(other.get("output_rect"))
                if other_rect is None:
                    continue
                overlap_y = y_overlap(group_rect, other_rect)
                group_source = rect_from_values(group.get("source_rect"))
                other_source = rect_from_values(other.get("source_rect"))
                source_overlap_y = y_overlap(group_source, other_source) if group_source and other_source else 0.0
                allowed_extra = max(1.0, min(float(group.get("output_font_size") or 6.0), float(other.get("output_font_size") or 6.0)) * 0.18)
                if x_overlap_ratio(group_rect, other_rect) >= 0.36 and overlap_y > source_overlap_y + allowed_extra:
                    blocking.append(
                        {
                            "gate_id": "local_text_overlap",
                            "group_id": group.get("group_id"),
                            "dimension": "visual_crowding",
                            "repair_family": "vertical_flow_relayout",
                            "evidence": f"overlaps {other.get('group_id')} by {round(overlap_y, 3)}pt; source_baseline={round(source_overlap_y, 3)}pt",
                        }
                    )
    verdict = "FAIL" if blocking else "PASS"
    result = {
        "tool": "validate_quality",
        "product_quality_verdict": verdict,
        "blocking_failure_count": len(blocking),
        "blocking_failures": blocking,
        "gates": [
            {
                "gate_id": "all_groups_fit",
                "status": "fail" if any(item["gate_id"] == "all_groups_fit" for item in blocking) else "pass",
                "blocking": True,
            },
            {
                "gate_id": "source_relative_font_floor",
                "status": "fail" if any(item["gate_id"] == "source_relative_font_floor" for item in blocking) else "pass",
                "blocking": True,
            },
            {
                "gate_id": "local_text_overlap",
                "status": "fail" if any(item["gate_id"] == "local_text_overlap" for item in blocking) else "pass",
                "blocking": True,
            },
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate(args.generation_evidence, args.output)


if __name__ == "__main__":
    main()
