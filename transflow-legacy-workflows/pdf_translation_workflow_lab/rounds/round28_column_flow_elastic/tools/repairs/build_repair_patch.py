import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def rect(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if not values or len(values) != 4:
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def y0(values: list[float] | None) -> float:
    r = rect(values)
    return r[1] if r else 0.0


def page_height(page_rect: list[float] | None) -> float:
    r = rect(page_rect)
    return max(1.0, (r[3] - r[1]) if r else 792.0)


def parse_group_page(group_id: str) -> int | None:
    match = re.match(r"p(\d+)_", group_id)
    return int(match.group(1)) if match else None


def group_index_by_id(layout_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index = {}
    for page in layout_plan.get("pages", []):
        for group in page.get("groups", []):
            index[str(group.get("group_id"))] = group
    return index


def load_signals(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("group_signals") or []), list(data.get("overlap_signals") or [])


def expand_operations(group_signals: list[dict[str, Any]], layout_plan: dict[str, Any]) -> list[dict[str, Any]]:
    groups = group_index_by_id(layout_plan)
    operations = []
    for signal in group_signals:
        if signal.get("human_judgement") != "FAIL":
            continue
        failure_class = signal.get("failure_class")
        if failure_class not in {"text_fit_overflow", "font_size_regression"}:
            continue
        group_id = str(signal.get("group_id"))
        group = groups.get(group_id)
        if not group:
            continue
        target = rect(group.get("target_rect"))
        source = rect(group.get("source_rect"))
        if not target or not source:
            continue
        source_size = float(signal.get("source_font_size") or group.get("source_font_size") or 8.0)
        font_floor = float(signal.get("source_relative_font_floor") or source_size * 0.52)
        grow_by = max(source_size * 1.25, (len(str(group.get("target_text") or "")) / max(20.0, (target[2] - target[0]) / max(1.0, source_size * 0.45))) * source_size * 0.22)
        operation = {
            "operation_id": f"expand_{group_id}",
            "operation_type": "expand_slot",
            "group_id": group_id,
            "page_index": signal.get("page_index"),
            "target_state": "S6_LayoutPlan",
            "grow_down_pt": round(grow_by, 3),
            "grow_right_pt": round(max(0.0, source_size * 1.8), 3),
            "min_font_start": round(max(font_floor, source_size * 0.60), 3),
            "min_font_min": round(max(font_floor * 0.92, source_size * 0.50), 3),
            "failure_class": failure_class,
            "reason": signal.get("triage_reason"),
            "source_candidate_evidence": {
                "source_rect": signal.get("source_rect"),
                "candidate_rect": signal.get("candidate_rect"),
                "source_font_size": signal.get("source_font_size"),
                "candidate_font_size": signal.get("candidate_font_size"),
                "font_floor": signal.get("source_relative_font_floor"),
            },
        }
        operations.append(operation)
    return operations


def vertical_flow_operations(overlap_signals: list[dict[str, Any]], layout_plan: dict[str, Any]) -> list[dict[str, Any]]:
    groups = group_index_by_id(layout_plan)
    page_shift: dict[tuple[int, str], float] = defaultdict(float)
    examples: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in overlap_signals:
        group_id = str(signal.get("group_id"))
        other_id = str(signal.get("other_group_id"))
        first = groups.get(group_id)
        second = groups.get(other_id)
        if not first or not second:
            continue
        first_y = y0(first.get("target_rect"))
        second_y = y0(second.get("target_rect"))
        lower = second if second_y >= first_y else first
        lower_id = str(lower.get("group_id"))
        page_index = int(lower.get("page_index") or parse_group_page(lower_id) or 0)
        role = str(lower.get("role") or "any")
        key = (page_index, role)
        page_shift[key] = max(page_shift[key], float(signal.get("needed_shift_pt") or 0.0))
        if len(examples[key]) < 5:
            examples[key].append(signal)
    operations = []
    for (page_index, role), shift in sorted(page_shift.items()):
        if shift <= 0:
            continue
        operations.append(
            {
                "operation_id": f"flow_page{page_index}_{role}",
                "operation_type": "vertical_flow_relayout",
                "failure_class": "cross_slot_overlap",
                "page_index": page_index,
                "role_scope": role,
                "shift_down_pt": round(min(shift, 18.0), 3),
                "target_state": "S6_LayoutPlan",
                "reason": "候选译文文本流相对原文出现新增重叠；移动同页同角色下游文本以恢复阅读间距。",
                "source_candidate_evidence": examples[(page_index, role)],
            }
        )
    return operations


def summarize_patch(
    operations: list[dict[str, Any]],
    deferred_operations: list[dict[str, Any]],
    adjudication: dict[str, Any],
) -> dict[str, Any]:
    counts = Counter(operation.get("operation_type") for operation in operations)
    deferred_counts = Counter(operation.get("failure_class") for operation in deferred_operations)
    dispatch = adjudication.get("dispatch_result") or {}
    return {
        "tool": "build_repair_patch",
        "patch_verdict": "PATCH_READY" if operations else "NO_EXECUTABLE_PATCH",
        "selected_failure_class": adjudication.get("selected_failure_class"),
        "dispatch_result": dispatch,
        "selected_repair_family": adjudication.get("selected_repair_family"),
        "operation_count": len(operations),
        "operation_type_counts": dict(counts),
        "deferred_failure_classes": dict(deferred_counts),
        "deferred_operation_count": len(deferred_operations),
        "human_readable_decision": (
            "已把源/候选对比失败绑定为可执行 RepairPatch；下一步应应用到 layout_plan 并重新生成候选。"
            if operations
            else "没有形成可执行 RepairPatch；应终止为质量失败或进入 AdaptiveChange。"
        ),
        "anti_overfit_statement": "RepairPatch 只引用当前运行中的 group_id、bbox、字号和重叠信号，不包含固定页码分支、固定文本或人工参考 PDF。",
    }


def run(layout_plan_path: Path, quality_signals_path: Path, visual_adjudication_path: Path, output: Path) -> None:
    layout_plan = json.loads(layout_plan_path.read_text(encoding="utf-8"))
    adjudication = json.loads(visual_adjudication_path.read_text(encoding="utf-8"))
    group_signals, overlap_signals = load_signals(quality_signals_path)
    all_operations = []
    all_operations.extend(expand_operations(group_signals, layout_plan))
    all_operations.extend(vertical_flow_operations(overlap_signals, layout_plan))
    selected_failure_class = adjudication.get("selected_failure_class")
    dispatch = adjudication.get("dispatch_result") or {}
    allowed_types = set(dispatch.get("allowed_operation_types") or [])
    operations = [
        operation
        for operation in all_operations
        if operation.get("failure_class") == selected_failure_class
        and (not allowed_types or operation.get("operation_type") in allowed_types)
    ]
    deferred_operations = [operation for operation in all_operations if operation not in operations]
    patch = summarize_patch(operations, deferred_operations, adjudication)
    patch.update(
        {
            "source_layout_plan": str(layout_plan_path),
            "source_quality_signals": str(quality_signals_path),
            "operations": operations,
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout-plan", type=Path, required=True)
    parser.add_argument("--quality-signals", type=Path, required=True)
    parser.add_argument("--visual-adjudication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.layout_plan, args.quality_signals, args.visual_adjudication, args.output)


if __name__ == "__main__":
    main()
