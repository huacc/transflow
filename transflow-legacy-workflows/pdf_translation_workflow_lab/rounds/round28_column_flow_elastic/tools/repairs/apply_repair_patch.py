import argparse
import json
from pathlib import Path
from typing import Any


def rect(values: list[float] | None) -> list[float] | None:
    if not values or len(values) != 4:
        return None
    return [float(value) for value in values]


def rect_height(values: list[float]) -> float:
    return max(0.0, values[3] - values[1])


def rect_width(values: list[float]) -> float:
    return max(0.0, values[2] - values[0])


def update_rect(item: dict[str, Any], target: list[float]) -> None:
    source = rect(item.get("source_rect")) or target
    erase = [
        min(source[0], target[0]) - 1.2,
        min(source[1], target[1]) - 1.2,
        max(source[2], target[2]) + 1.2,
        max(source[3], target[3]) + 1.2,
    ]
    item["target_rect"] = [round(value, 3) for value in target]
    item["erase_rect"] = [round(value, 3) for value in erase]


def page_by_index(layout_plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(page.get("page_index") or 0): page for page in layout_plan.get("pages", [])}


def groups_by_id(layout_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = {}
    for page in layout_plan.get("pages", []):
        for group in page.get("groups", []):
            groups[str(group.get("group_id"))] = group
    return groups


def apply_expand_slot(operation: dict[str, Any], group: dict[str, Any], page_rect: list[float]) -> dict[str, Any]:
    target = rect(group.get("target_rect"))
    if not target:
        return {"operation_id": operation.get("operation_id"), "status": "skipped_missing_rect"}
    before = list(target)
    max_x1 = page_rect[2] - max(4.0, rect_width(page_rect) * 0.015)
    max_y1 = page_rect[3] - max(8.0, rect_height(page_rect) * 0.025)
    target[2] = min(max_x1, target[2] + float(operation.get("grow_right_pt") or 0.0))
    target[3] = min(max_y1, target[3] + float(operation.get("grow_down_pt") or 0.0))
    group["font_start"] = round(max(float(group.get("font_start") or 0.0), float(operation.get("min_font_start") or 0.0)), 3)
    group["font_min"] = round(max(float(group.get("font_min") or 0.0), float(operation.get("min_font_min") or 0.0)), 3)
    update_rect(group, target)
    group.setdefault("repair_patch_applications", []).append(
        {
            "operation_id": operation.get("operation_id"),
            "operation_type": operation.get("operation_type"),
            "reason": operation.get("reason"),
        }
    )
    return {"operation_id": operation.get("operation_id"), "status": "applied", "before": before, "after": target}


def same_flow_scope(group: dict[str, Any], page_index: int, role_scope: str) -> bool:
    if int(group.get("page_index") or 0) != page_index:
        return False
    if role_scope and role_scope != "any" and str(group.get("role")) != role_scope:
        return False
    return True


def apply_vertical_flow(operation: dict[str, Any], page: dict[str, Any]) -> list[dict[str, Any]]:
    page_rect = rect(page.get("page_rect")) or [0.0, 0.0, 612.0, 792.0]
    page_index = int(operation.get("page_index") or page.get("page_index") or 0)
    role_scope = str(operation.get("role_scope") or "any")
    shift = float(operation.get("shift_down_pt") or 0.0)
    if shift <= 0:
        return [{"operation_id": operation.get("operation_id"), "status": "skipped_no_shift"}]

    anchors = operation.get("source_candidate_evidence") or []
    anchor_group_ids = [str(item.get("other_group_id") or item.get("group_id")) for item in anchors if item.get("other_group_id") or item.get("group_id")]
    group_positions = {}
    for group in page.get("groups", []):
        target = rect(group.get("target_rect"))
        if target:
            group_positions[str(group.get("group_id"))] = target[1]
    anchor_y = min((group_positions[group_id] for group_id in anchor_group_ids if group_id in group_positions), default=None)
    if anchor_y is None:
        anchor_y = min((rect(group.get("target_rect")) or [0, 0, 0, 0])[1] for group in page.get("groups", []) if same_flow_scope(group, page_index, role_scope))

    applied = []
    bottom_limit = page_rect[3] - max(8.0, rect_height(page_rect) * 0.025)
    for group in sorted(page.get("groups", []), key=lambda item: (item.get("target_rect") or [0, 0, 0, 0])[1]):
        if not same_flow_scope(group, page_index, role_scope):
            continue
        target = rect(group.get("target_rect"))
        if not target or target[1] < anchor_y - 0.2:
            continue
        available = bottom_limit - target[3]
        if available <= 0.4:
            continue
        actual_shift = min(shift, available)
        before = list(target)
        target[1] += actual_shift
        target[3] += actual_shift
        update_rect(group, target)
        group.setdefault("repair_patch_applications", []).append(
            {
                "operation_id": operation.get("operation_id"),
                "operation_type": operation.get("operation_type"),
                "shift_down_pt": round(actual_shift, 3),
                "reason": operation.get("reason"),
            }
        )
        applied.append({"group_id": group.get("group_id"), "before": before, "after": target, "shift_down_pt": round(actual_shift, 3)})
    return [{"operation_id": operation.get("operation_id"), "status": "applied", "affected_groups": applied}]


def apply_move_region_group(operation: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    page_rect = rect(page.get("page_rect")) or [0.0, 0.0, 612.0, 792.0]
    group_ids = {str(group_id) for group_id in operation.get("group_ids") or []}
    delta_x = float(operation.get("delta_x_pt") or 0.0)
    delta_y = float(operation.get("delta_y_pt") or 0.0)
    bottom_limit = page_rect[3] - max(8.0, rect_height(page_rect) * 0.025)
    right_limit = page_rect[2] - max(4.0, rect_width(page_rect) * 0.015)
    applied = []
    for group in page.get("groups", []):
        if str(group.get("group_id")) not in group_ids:
            continue
        target = rect(group.get("target_rect"))
        if not target:
            continue
        available_x = right_limit - target[2]
        available_y = bottom_limit - target[3]
        actual_x = max(-target[0], min(delta_x, available_x))
        actual_y = max(-target[1], min(delta_y, available_y))
        if abs(actual_x) <= 0.01 and abs(actual_y) <= 0.01:
            continue
        before = list(target)
        target[0] += actual_x
        target[2] += actual_x
        target[1] += actual_y
        target[3] += actual_y
        update_rect(group, target)
        group.setdefault("repair_patch_applications", []).append(
            {
                "operation_id": operation.get("operation_id"),
                "operation_type": operation.get("operation_type"),
                "delta_x_pt": round(actual_x, 3),
                "delta_y_pt": round(actual_y, 3),
                "reason": operation.get("reason"),
            }
        )
        applied.append(
            {
                "group_id": group.get("group_id"),
                "before": before,
                "after": target,
                "delta_x_pt": round(actual_x, 3),
                "delta_y_pt": round(actual_y, 3),
            }
        )
    return {"operation_id": operation.get("operation_id"), "status": "applied" if applied else "skipped_no_room", "affected_groups": applied}


def apply_flow_within_region(operation: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    target = rect(operation.get("target_rect"))
    if not target:
        return {"operation_id": operation.get("operation_id"), "status": "skipped_missing_target_rect"}
    before = rect(group.get("target_rect")) or target
    update_rect(group, target)
    if operation.get("font_start") is not None:
        group["font_start"] = round(float(operation.get("font_start")), 3)
    if operation.get("font_min") is not None:
        group["font_min"] = round(float(operation.get("font_min")), 3)
    group.setdefault("repair_patch_applications", []).append(
        {
            "operation_id": operation.get("operation_id"),
            "operation_type": operation.get("operation_type"),
            "reason": operation.get("reason"),
        }
    )
    return {"operation_id": operation.get("operation_id"), "status": "applied", "before": list(before), "after": target}


def run(layout_plan_path: Path, repair_patch_path: Path, output_plan_path: Path, output_report_path: Path) -> None:
    layout_plan = json.loads(layout_plan_path.read_text(encoding="utf-8"))
    patch = json.loads(repair_patch_path.read_text(encoding="utf-8"))
    pages = page_by_index(layout_plan)
    groups = groups_by_id(layout_plan)
    results = []
    for operation in patch.get("operations", []):
        operation_type = operation.get("operation_type")
        if operation_type == "expand_slot":
            group = groups.get(str(operation.get("group_id")))
            page = pages.get(int(operation.get("page_index") or (group or {}).get("page_index") or 0))
            if not group or not page:
                results.append({"operation_id": operation.get("operation_id"), "status": "skipped_missing_group_or_page"})
                continue
            results.append(apply_expand_slot(operation, group, rect(page.get("page_rect")) or [0.0, 0.0, 612.0, 792.0]))
        elif operation_type == "vertical_flow_relayout":
            page = pages.get(int(operation.get("page_index") or 0))
            if not page:
                results.append({"operation_id": operation.get("operation_id"), "status": "skipped_missing_page"})
                continue
            results.extend(apply_vertical_flow(operation, page))
        elif operation_type == "move_region_group":
            page = pages.get(int(operation.get("page_index") or 0))
            if not page:
                results.append({"operation_id": operation.get("operation_id"), "status": "skipped_missing_page"})
                continue
            results.append(apply_move_region_group(operation, page))
        elif operation_type == "flow_within_region":
            group = groups.get(str(operation.get("group_id")))
            if not group:
                results.append({"operation_id": operation.get("operation_id"), "status": "skipped_missing_group"})
                continue
            results.append(apply_flow_within_region(operation, group))
        elif operation_type == "defer_unrepairable":
            results.append(
                {
                    "operation_id": operation.get("operation_id"),
                    "status": "deferred_unrepairable",
                    "reason": operation.get("unrepairable_reason"),
                    "evidence_ref": operation.get("evidence_ref"),
                }
            )
        else:
            results.append({"operation_id": operation.get("operation_id"), "status": "skipped_unknown_operation"})

    layout_plan["repair_patch_source"] = str(repair_patch_path)
    layout_plan["repair_patch_applied"] = True
    output_plan_path.parent.mkdir(parents=True, exist_ok=True)
    output_plan_path.write_text(json.dumps(layout_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "tool": "apply_repair_patch",
        "source_layout_plan": str(layout_plan_path),
        "repair_patch": str(repair_patch_path),
        "output_layout_plan": str(output_plan_path),
        "applied_operation_count": sum(1 for result in results if result.get("status") == "applied"),
        "operation_results": results,
        "human_readable_result": "RepairPatch 已应用到布局计划；下一步需要重新生成候选并再次执行 S8 质量研判。",
    }
    output_report_path.parent.mkdir(parents=True, exist_ok=True)
    output_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-plan", type=Path, required=True)
    parser.add_argument("--repair-patch", type=Path, required=True)
    parser.add_argument("--output-layout-plan", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args()
    run(args.layout_plan, args.repair_patch, args.output_layout_plan, args.output_report)


if __name__ == "__main__":
    main()
