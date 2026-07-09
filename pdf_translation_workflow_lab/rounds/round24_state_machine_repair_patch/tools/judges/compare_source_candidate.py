import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PAIR_RE = re.compile(r"overlaps\s+(\S+)\s+by\s+([0-9.]+)pt;\s+source_baseline=([0-9.]+)pt")

DISPATCH_TABLE = {
    "text_fit_overflow": {
        "repair_family": "expand_or_reflow_slot",
        "target_state": "S6_LayoutPlan",
        "allowed_operation_types": ["expand_slot"],
        "tool": "tools/repairs/build_repair_patch.py",
    },
    "font_size_regression": {
        "repair_family": "reflow_before_shrink",
        "target_state": "S6_LayoutPlan",
        "allowed_operation_types": ["expand_slot"],
        "tool": "tools/repairs/build_repair_patch.py",
    },
    "cross_slot_overlap": {
        "repair_family": "vertical_flow_relayout",
        "target_state": "S6_LayoutPlan",
        "allowed_operation_types": ["vertical_flow_relayout"],
        "tool": "tools/repairs/build_repair_patch.py",
    },
}


def rect(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if not values or len(values) != 4:
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def width(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[2] - r[0])


def height(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[3] - r[1])


def x_overlap_ratio(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(width(left), width(right)))


def y_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def center_delta_ratio(source: tuple[float, float, float, float], output: tuple[float, float, float, float], page: tuple[float, float, float, float]) -> dict[str, float]:
    sx = (source[0] + source[2]) / 2.0
    sy = (source[1] + source[3]) / 2.0
    ox = (output[0] + output[2]) / 2.0
    oy = (output[1] + output[3]) / 2.0
    return {
        "dx_page_ratio": round(abs(ox - sx) / max(1.0, width(page)), 5),
        "dy_page_ratio": round(abs(oy - sy) / max(1.0, height(page)), 5),
    }


def source_relative_font_floor(role: str, source_size: float, page_font_median: float) -> float:
    base = max(page_font_median * 0.52, source_size * 0.52)
    if role == "table_cell":
        return max(page_font_median * 0.34, source_size * 0.40)
    if role in {"title", "metric_value"}:
        return max(page_font_median * 0.74, source_size * 0.48)
    if role in {"red_heading", "section_heading"}:
        return max(page_font_median * 0.60, source_size * 0.56)
    return base


def parse_quality_failures(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("blocking_failures") or [])


def page_font_medians(generation: dict[str, Any]) -> dict[int, float]:
    medians: dict[int, float] = {}
    for page in generation.get("pages", []):
        sizes = sorted(
            float(group.get("source_font_size") or 0)
            for group in page.get("groups", [])
            if float(group.get("source_font_size") or 0) > 0
        )
        if not sizes:
            medians[int(page.get("page_index") or 0)] = 8.0
        else:
            medians[int(page.get("page_index") or 0)] = sizes[len(sizes) // 2]
    return medians


def collect_group_signals(generation: dict[str, Any], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failed_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for failure in failures:
        failed_by_group[str(failure.get("group_id"))].append(failure)

    medians = page_font_medians(generation)
    signals: list[dict[str, Any]] = []
    for page in generation.get("pages", []):
        page_index = int(page.get("page_index") or 0)
        page_rect = None
        for group in page.get("groups", []):
            source = rect(group.get("source_rect"))
            output = rect(group.get("output_rect") or group.get("target_rect"))
            if source:
                page_rect = page_rect or (0.0, 0.0, max(source[2], 1.0), max(source[3], 1.0))
            if not source or not output:
                continue
            group_id = str(group.get("group_id"))
            source_size = float(group.get("source_font_size") or 0.0)
            output_size = float(group.get("output_font_size") or group.get("font_start") or 0.0)
            floor = source_relative_font_floor(str(group.get("role")), source_size, medians.get(page_index, 8.0))
            page_box = tuple(float(value) for value in (group.get("page_rect") or [0, 0, max(output[2], source[2]), max(output[3], source[3])]))
            if len(page_box) != 4:
                page_box = (0.0, 0.0, max(output[2], source[2]), max(output[3], source[3]))
            deltas = center_delta_ratio(source, output, page_box)
            signal = {
                "group_id": group_id,
                "page_index": page_index,
                "role": group.get("role"),
                "source_rect": list(source),
                "candidate_rect": list(output),
                "source_font_size": source_size,
                "candidate_font_size": output_size,
                "font_scale_ratio": round(output_size / source_size, 4) if source_size else None,
                "source_relative_font_floor": round(floor, 3),
                "source_candidate_width_ratio": round(width(output) / max(1.0, width(source)), 4),
                "source_candidate_height_ratio": round(height(output) / max(1.0, height(source)), 4),
                "anchor_delta": deltas,
                "fit_status": group.get("fit_status"),
                "source_candidate_failures": failed_by_group.get(group_id, []),
                "human_judgement": "PASS",
                "failure_class": None,
                "triage_reason": None,
            }
            if group.get("fit_status") != "fit":
                signal["human_judgement"] = "FAIL"
                signal["failure_class"] = "text_fit_overflow"
                signal["triage_reason"] = "译文没有装入候选文本框，需要优先扩大或重排该文本槽。"
            elif output_size and output_size < floor:
                signal["human_judgement"] = "FAIL"
                signal["failure_class"] = "font_size_regression"
                signal["triage_reason"] = "候选字号相对原文字号和本页字号基线过小，应优先重排，不应继续压缩。"
            signals.append(signal)
    return signals


def collect_overlap_signals(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overlaps = []
    for failure in failures:
        if failure.get("gate_id") != "local_text_overlap":
            continue
        evidence = str(failure.get("evidence") or "")
        match = PAIR_RE.search(evidence)
        if not match:
            continue
        overlap_pt = float(match.group(2))
        source_baseline = float(match.group(3))
        needed_shift = max(0.0, overlap_pt - source_baseline + 2.0)
        overlaps.append(
            {
                "group_id": failure.get("group_id"),
                "other_group_id": match.group(1),
                "overlap_pt": round(overlap_pt, 3),
                "source_baseline_pt": round(source_baseline, 3),
                "needed_shift_pt": round(needed_shift, 3),
                "human_judgement": "FAIL",
                "failure_class": "cross_slot_overlap",
                "triage_reason": "候选译文之间的垂直重叠超过原文同位置重叠基线，应移动下游同列文本流。",
            }
        )
    return overlaps


def summarize(signals: list[dict[str, Any]], overlaps: list[dict[str, Any]]) -> dict[str, Any]:
    failed_group_signals = [signal for signal in signals if signal["human_judgement"] == "FAIL"]
    failure_counts = Counter(signal.get("failure_class") for signal in failed_group_signals if signal.get("failure_class"))
    failure_counts.update(overlap.get("failure_class") for overlap in overlaps if overlap.get("failure_class"))
    selected_failure = failure_counts.most_common(1)[0][0] if failure_counts else None
    dispatch = DISPATCH_TABLE.get(str(selected_failure)) if selected_failure else None
    selected_repair = dispatch.get("repair_family") if dispatch else None
    page_counts = Counter()
    for signal in failed_group_signals:
        page_counts[int(signal["page_index"]) + 1] += 1
    for overlap in overlaps:
        group_id = str(overlap.get("group_id") or "")
        match = re.match(r"p(\d+)_", group_id)
        if match:
            page_counts[int(match.group(1)) + 1] += 1
    return {
        "tool": "compare_source_candidate",
        "judgement_basis": "候选译文与原文的源 bbox、候选 bbox、源字号、候选字号、fit 状态和候选相对原文的重叠增量对比。",
        "product_quality_verdict": "FAIL" if failed_group_signals or overlaps else "PASS",
        "failed_group_signal_count": len(failed_group_signals),
        "overlap_signal_count": len(overlaps),
        "failure_class_counts": dict(failure_counts),
        "selected_failure_class": selected_failure,
        "triage_result": {
            "selected_failure_class": selected_failure,
            "blocking_failure_classes": dict(failure_counts),
            "needs_more_evidence": False,
            "triage_rule": "highest_current_run_blocking_failure_count",
        },
        "dispatch_result": {
            "dispatch_table": "contracts/failure_dispatch_table.json",
            "selected_failure_class": selected_failure,
            "selected_repair_family": selected_repair,
            "target_state": dispatch.get("target_state") if dispatch else None,
            "allowed_operation_types": dispatch.get("allowed_operation_types") if dispatch else [],
            "tool": dispatch.get("tool") if dispatch else None,
        },
        "selected_repair_family": selected_repair,
        "page_failure_summary": [
            {"page_number": page, "failure_count": count}
            for page, count in sorted(page_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        ],
        "human_readable_result": (
            "候选译文与原文视觉关系存在阻塞差异，需要进入 RepairLoop。"
            if failed_group_signals or overlaps
            else "候选译文相对原文的文本框、字号和局部重叠均未触发阻塞阈值。"
        ),
        "tool_selection_reason": (
            f"先由 Triage 选出 {selected_failure}，再由静态 Dispatch 表映射到 {selected_repair}。"
            if selected_failure and selected_repair
            else "未选择修补工具，因为没有阻塞失败信号或 failure class 没有 dispatch 映射。"
        ),
    }


def run(generation_evidence: Path, quality_gates: Path, output_signals: Path, output_adjudication: Path) -> None:
    generation = json.loads(generation_evidence.read_text(encoding="utf-8"))
    failures = parse_quality_failures(quality_gates)
    group_signals = collect_group_signals(generation, failures)
    overlap_signals = collect_overlap_signals(failures)
    summary = summarize(group_signals, overlap_signals)
    output_signals.parent.mkdir(parents=True, exist_ok=True)
    output_signals.write_text(
        json.dumps(
            {
                "tool": "compare_source_candidate",
                "mechanism": {
                    "unit_of_comparison": "source_group_bbox_vs_candidate_group_bbox",
                    "dimensions": [
                        "fit_status",
                        "source_relative_font_floor",
                        "source_candidate_width_ratio",
                        "source_candidate_height_ratio",
                        "anchor_delta",
                        "candidate_overlap_minus_source_overlap",
                    ],
                    "triage_dispatch_separation": "QualitySignal and failure_class are triage outputs; repair_family and tool are resolved through a static dispatch table.",
                    "anti_overfit_statement": "所有阈值从当前源 PDF 的 bbox、字号和候选生成证据计算，不读取人工对照、不使用固定页码或固定文本。",
                },
                "group_signals": group_signals,
                "overlap_signals": overlap_signals,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    output_adjudication.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-evidence", type=Path, required=True)
    parser.add_argument("--quality-gates", type=Path, required=True)
    parser.add_argument("--output-signals", type=Path, required=True)
    parser.add_argument("--output-adjudication", type=Path, required=True)
    args = parser.parse_args()
    run(args.generation_evidence, args.quality_gates, args.output_signals, args.output_adjudication)


if __name__ == "__main__":
    main()
