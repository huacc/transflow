from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    run_root = ROOT / "artifacts" / "runs" / args.run_id
    report_root = ROOT / "reports" / "runs" / args.run_id
    report_root.mkdir(parents=True, exist_ok=True)

    gold_rows = read_jsonl(ROOT / "manifests" / "borderless_rule_regression_gold.jsonl")
    routes = {
        row["sample_id"]: "/".join(row["final_path"])
        for row in read_jsonl(run_root / "routes" / "final_routes.jsonl")
    }
    resolutions = read_jsonl(run_root / "judgements" / "node_resolutions.jsonl")

    details: list[dict[str, Any]] = []
    cohort_counts: dict[str, Counter[str]] = {}
    for gold in gold_rows:
        cohort = gold["cohort"]
        counts = cohort_counts.setdefault(cohort, Counter())
        predicted = routes.get(gold["sample_id"], "INCONCLUSIVE")
        correct = predicted == gold["expected_leaf"]
        counts["total"] += 1
        counts["correct"] += int(correct)
        details.append(
            {
                **gold,
                "predicted_leaf": predicted,
                "correct": correct,
                "sample_pdf": str(Path("样本_无边框规则回归") / f"{gold['sample_id']}.pdf"),
                "result_pdf": str(Path("分类结果_无边框规则回归") / predicted / f"{gold['sample_id']}.pdf"),
            }
        )

    direct_rows = [
        row
        for row in resolutions
        if row["node_key"] == "body.layout_owner"
        and row["resolution"] == "HIGH_CONFIDENCE_RULE"
        and any(ref in {"TABLE1", "BTABLE1"} for ref in row["final"]["evidence_refs"])
    ]
    direct_ids = {row["sample_id"] for row in direct_rows}
    direct_details = [row for row in details if row["sample_id"] in direct_ids]

    cohort_metrics = {}
    for cohort, counts in sorted(cohort_counts.items()):
        cohort_metrics[cohort] = {
            "total": counts["total"],
            "correct": counts["correct"],
            "incorrect": counts["total"] - counts["correct"],
            "accuracy": counts["correct"] / counts["total"],
        }

    overall_correct = sum(item["correct"] for item in details)
    report = {
        "run_id": args.run_id,
        "overall": {
            "total": len(details),
            "correct": overall_correct,
            "incorrect": len(details) - overall_correct,
            "accuracy": overall_correct / len(details),
        },
        "cohorts": cohort_metrics,
        "high_confidence_direct_table": {
            "count": len(direct_details),
            "correct": sum(item["correct"] for item in direct_details),
            "accuracy": (
                sum(item["correct"] for item in direct_details) / len(direct_details)
                if direct_details
                else None
            ),
            "details": direct_details,
        },
        "mismatches": [item for item in details if not item["correct"]],
        "details": details,
    }
    write_json(report_root / "borderless_rule_regression_report.json", report)

    lines = [
        "# 无边框表格规则回归报告",
        "",
        f"- 运行：`{args.run_id}`",
        f"- 总体：{overall_correct}/{len(details)}（{report['overall']['accuracy']:.2%}）",
    ]
    labels = {
        "problem": "用户指出的问题页",
        "confirmed_correct": "原本正确页",
        "unrelated": "无关页",
    }
    for cohort in ("problem", "confirmed_correct", "unrelated"):
        metric = cohort_metrics[cohort]
        lines.append(
            f"- {labels[cohort]}：{metric['correct']}/{metric['total']}（{metric['accuracy']:.2%}）"
        )
    direct = report["high_confidence_direct_table"]
    lines.extend(
        [
            f"- 高置信度直接表格规则：{direct['correct']}/{direct['count']}（{direct['accuracy']:.2%}）",
            "",
            "## 误判明细",
            "",
        ]
    )
    mismatches = report["mismatches"]
    if not mismatches:
        lines.append("无。")
    else:
        for item in mismatches:
            lines.append(
                f"- `{item['sample_id']}`：期望 `{item['expected_leaf']}`，实际 `{item['predicted_leaf']}`；结果 `{item['result_pdf']}`"
            )
    (report_root / "无边框表格规则回归报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
