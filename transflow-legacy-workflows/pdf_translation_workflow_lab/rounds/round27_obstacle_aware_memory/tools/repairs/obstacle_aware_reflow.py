import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


PROTECTED_ROLES = {
    "table_cell",
    "chart_label",
    "chart_legend",
    "nav_footer",
    "image_caption",
}


def rect(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if not values or len(values) != 4:
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def rect_width(values: tuple[float, float, float, float]) -> float:
    return max(0.0, values[2] - values[0])


def rect_height(values: tuple[float, float, float, float]) -> float:
    return max(0.0, values[3] - values[1])


def horizontal_overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    base = max(1.0, min(rect_width(a), rect_width(b)))
    return overlap / base


def center_x(values: tuple[float, float, float, float]) -> float:
    return (values[0] + values[2]) / 2.0


def same_local_column(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    page_width: float,
) -> bool:
    overlap = horizontal_overlap_ratio(a, b)
    center_gap = abs(center_x(a) - center_x(b))
    local_width = max(rect_width(a), rect_width(b), page_width * 0.08)
    return overlap >= 0.18 or center_gap <= local_width * 0.62


def page_index(layout_plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(page.get("page_index") or 0): page for page in layout_plan.get("pages", [])}


def group_index(layout_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for page in layout_plan.get("pages", []):
        for group in page.get("groups", []):
            result[str(group.get("group_id"))] = group
    return result


def load_overlap_signals(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        signal
        for signal in data.get("overlap_signals") or []
        if signal.get("human_judgement") == "FAIL" and signal.get("failure_class") == "cross_slot_overlap"
    ]


def lower_group(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_rect = rect(first.get("target_rect")) or (0.0, 0.0, 0.0, 0.0)
    second_rect = rect(second.get("target_rect")) or (0.0, 0.0, 0.0, 0.0)
    return second if second_rect[1] >= first_rect[1] else first


def movable_group_ids(anchor_group: dict[str, Any]) -> list[str]:
    anchor_rect = rect(anchor_group.get("target_rect"))
    if not anchor_rect:
        return []
    role = str(anchor_group.get("role") or "")
    if role in PROTECTED_ROLES:
        return []
    return [str(anchor_group.get("group_id"))]


def bottom_room(page: dict[str, Any], group_ids: list[str], groups: dict[str, dict[str, Any]]) -> float:
    page_rect = rect(page.get("page_rect")) or (0.0, 0.0, 612.0, 792.0)
    limit = page_rect[3] - max(8.0, rect_height(page_rect) * 0.025)
    bottoms = []
    for group_id in group_ids:
        target = rect(groups.get(group_id, {}).get("target_rect"))
        if target:
            bottoms.append(target[3])
    if not bottoms:
        return 0.0
    return max(0.0, limit - max(bottoms))


def build_operations(layout_plan: dict[str, Any], overlap_signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = page_index(layout_plan)
    groups = group_index(layout_plan)
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    deferred = []

    for signal in overlap_signals:
        first = groups.get(str(signal.get("group_id")))
        second = groups.get(str(signal.get("other_group_id")))
        if not first or not second:
            deferred.append(
                {
                    "operation_id": f"defer_missing_{len(deferred)}",
                    "operation_type": "defer_unrepairable",
                    "failure_class": "cross_slot_overlap",
                    "unrepairable_reason": "missing_group_for_overlap_signal",
                    "evidence_ref": signal,
                }
            )
            continue
        anchor = lower_group(first, second)
        page_id = int(anchor.get("page_index") or 0)
        page = pages.get(page_id)
        anchor_rect = rect(anchor.get("target_rect"))
        if not page or not anchor_rect:
            continue
        page_rect = rect(page.get("page_rect")) or (0.0, 0.0, 612.0, 792.0)
        key = (page_id, str(anchor.get("group_id")))
        members = movable_group_ids(anchor)
        if not members:
            deferred.append(
                {
                    "operation_id": f"defer_nomove_{len(deferred)}",
                    "operation_type": "defer_unrepairable",
                    "failure_class": "cross_slot_overlap",
                    "unrepairable_reason": "no_movable_same_column_group",
                    "evidence_ref": signal,
                }
            )
            continue
        needed = max(float(signal.get("needed_shift_pt") or 0.0), rect_height(page_rect) * 0.004)
        record = grouped.setdefault(
            key,
            {
                "page_index": page_id,
                "anchor_group_id": str(anchor.get("group_id")),
                "anchor_y": anchor_rect[1],
                "group_ids": set(),
                "needed_shift_pt": 0.0,
                "signals": [],
            },
        )
        record["group_ids"].update(members)
        record["needed_shift_pt"] = max(record["needed_shift_pt"], needed)
        if len(record["signals"]) < 6:
            record["signals"].append(signal)

    operations = []
    already_moved: set[str] = set()
    ordered_items = sorted(grouped.values(), key=lambda item: (int(item["page_index"]), float(item.get("anchor_y") or 0.0)))
    for index, item in enumerate(ordered_items):
        page = pages.get(int(item["page_index"]))
        group_ids = sorted(group_id for group_id in item["group_ids"] if group_id not in already_moved)
        if not group_ids:
            deferred.append(
                {
                    "operation_id": f"defer_already_moved_{index:04d}",
                    "operation_type": "defer_unrepairable",
                    "failure_class": "cross_slot_overlap",
                    "unrepairable_reason": "anchor_group_already_moved_by_an_earlier_overlap_signal",
                    "evidence_ref": {
                        "page_index": item["page_index"],
                        "anchor_group_id": item["anchor_group_id"],
                    },
                }
            )
            continue
        room = bottom_room(page, group_ids, groups) if page else 0.0
        if room <= 0.5:
            deferred.append(
                {
                    "operation_id": f"defer_noroom_{index:04d}",
                    "operation_type": "defer_unrepairable",
                    "failure_class": "cross_slot_overlap",
                    "unrepairable_reason": "insufficient_downstream_space_under_page_boundary",
                    "evidence_ref": {
                        "page_index": item["page_index"],
                        "anchor_group_id": item["anchor_group_id"],
                        "group_ids": group_ids,
                        "needed_shift_pt": round(item["needed_shift_pt"], 3),
                        "available_room_pt": round(room, 3),
                    },
                }
            )
            continue
        delta = min(float(item["needed_shift_pt"]), room)
        already_moved.update(group_ids)
        operations.append(
            {
                "operation_id": f"obstacle_flow_{index:04d}",
                "operation_type": "move_region_group",
                "page_index": item["page_index"],
                "group_ids": group_ids,
                "delta_x_pt": 0.0,
                "delta_y_pt": round(delta, 3),
                "failure_class": "cross_slot_overlap",
                "reason": "Move only the lower same-column local flow that participates in current-run overlap signals; protected table/chart/footer roles are excluded.",
                "source_candidate_evidence": item["signals"],
            }
        )
    return operations, deferred


def run(layout_plan_path: Path, quality_signals_path: Path, previous_loop_path: Path, output: Path) -> None:
    layout_plan = json.loads(layout_plan_path.read_text(encoding="utf-8"))
    previous_loop = json.loads(previous_loop_path.read_text(encoding="utf-8")) if previous_loop_path.exists() else {}
    overlap_signals = load_overlap_signals(quality_signals_path)
    operations, deferred = build_operations(layout_plan, overlap_signals)
    patch = {
        "tool": "obstacle_aware_reflow",
        "patch_verdict": "PATCH_READY" if operations else "NO_EXECUTABLE_PATCH",
        "selected_failure_class": "cross_slot_overlap",
        "selected_repair_family": "obstacle_aware_reflow",
        "source_layout_plan": str(layout_plan_path),
        "source_quality_signals": str(quality_signals_path),
        "previous_loop": str(previous_loop_path),
        "previous_loop_verdict": previous_loop.get("loop_verdict"),
        "previous_hard_regressions": previous_loop.get("hard_failure_regressions"),
        "operation_count": len(operations),
        "deferred_operation_count": len(deferred),
        "operations": operations,
        "deferred_operations": deferred,
        "anti_overfit_statement": (
            "Operations are derived only from current-run overlap signals, group bboxes, roles, and page geometry. "
            "No filename, fixed page branch, text literal, or reference PDF is used."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(patch, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-plan", type=Path, required=True)
    parser.add_argument("--quality-signals", type=Path, required=True)
    parser.add_argument("--previous-loop", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.layout_plan, args.quality_signals, args.previous_loop, args.output)


if __name__ == "__main__":
    main()
