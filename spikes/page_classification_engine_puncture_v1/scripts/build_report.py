from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, write_json


def metrics_for_node(
    node_key: str,
    gold_rows: list[dict[str, Any]],
    resolution_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "page.role": ("role", "role_gold_status"),
        "body.layout_owner": ("layout_owner", "layout_gold_status"),
        "body.flow.topology": ("flow_topology", "flow_topology_gold_status"),
        "body.composite.kind": ("composite_kind", "composite_kind_gold_status"),
    }
    gold_field, status_field = fields[node_key]
    rows = [
        row
        for row in gold_rows
        if row["split"] == "test" and row[status_field] == "CONFIRMED" and row.get(gold_field) is not None
    ]
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    correct = 0
    unresolved = 0
    details = []
    for gold in rows:
        resolution = resolution_by_key.get((gold["sample_id"], node_key))
        predicted = None if resolution is None else resolution["final"]["selected_child"]
        predicted_key = predicted or "INCONCLUSIVE"
        confusion[str(gold[gold_field])][predicted_key] += 1
        correct += predicted == gold[gold_field]
        unresolved += predicted is None
        details.append({"sample_id": gold["sample_id"], "gold": gold[gold_field], "predicted": predicted_key, "correct": predicted == gold[gold_field]})
    return {
        "node_key": node_key,
        "confirmed_test_count": len(rows),
        "correct_count": correct,
        "accuracy": correct / len(rows) if rows else None,
        "inconclusive_count": unresolved,
        "confusion_matrix": {gold: dict(predicted) for gold, predicted in sorted(confusion.items())},
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_id = args.run_id
    run_root = ROOT / "artifacts" / "runs" / run_id
    report_root = ROOT / "reports" / "runs" / run_id
    report_root.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    resolutions = read_jsonl(run_root / "judgements" / "node_resolutions.jsonl")
    routes = read_jsonl(run_root / "routes" / "final_routes.jsonl")
    gold = read_jsonl(ROOT / "manifests" / "gold_manifest.jsonl")
    sources = {row["sample_id"]: row for row in read_jsonl(ROOT / "manifests" / "source_manifest.jsonl")}
    resolution_by_key = {(row["sample_id"], row["node_key"]): row for row in resolutions}
    node_metrics = {
        node: metrics_for_node(node, gold, resolution_by_key)
        for node in ("page.role", "body.layout_owner", "body.flow.topology", "body.composite.kind")
    }
    resolution_counts = Counter(row["resolution"] for row in resolutions)
    errors = []
    for node, metrics in node_metrics.items():
        for item in metrics["details"]:
            if not item["correct"]:
                source = sources[item["sample_id"]]
                errors.append(
                    {
                        "node_key": node,
                        **item,
                        "source_file": Path(source["source_path"]).name,
                        "source_page_number": source["source_page_number"],
                    }
                )
    verdict = json.loads((report_root / "set_equality_verdict.json").read_text(encoding="utf-8"))
    report_json = {
        "run_id": run_id,
        "engine_summary": summary,
        "node_metrics": node_metrics,
        "resolution_counts": dict(resolution_counts),
        "sample_result_set_equality": verdict,
        "error_cases": errors,
    }
    write_json(report_root / "classification_report.json", report_json)
    metric_lines = []
    for node, metrics in node_metrics.items():
        accuracy = "N/A" if metrics["accuracy"] is None else f"{metrics['accuracy']:.2%}"
        metric_lines.append(f"| `{node}` | {metrics['confirmed_test_count']} | {metrics['correct_count']} | {accuracy} | {metrics['inconclusive_count']} |")
    error_lines = [
        f"- `{item['sample_id']}` / `{item['node_key']}`：gold=`{item['gold']}`，predicted=`{item['predicted']}`；来源 `{item['source_file']}` 第 {item['source_page_number'] or '?'} 页。"
        for item in errors
    ] or ["- 无 confirmed test 误判。"]
    report_md = f"""# 页面分类引擎穿刺报告

- run_id：`{run_id}`
- 样本：{summary['sample_count']} 个匿名单页 PDF。
- 千问调用：{summary['qwen_call_count']}；细粒度复核：{summary['review_call_count']}。
- 完整到达叶子：{summary['complete_route_count']}；停止路由：{summary['route_stopped_count']}。
- 任一正文子节点无法稳定分类后进入 `body/freeform`：{summary['taxonomy_fallback_count']}。
- 样本与分类结果 PDF 集合相等：`{verdict['SAMPLE_RESULT_PDF_SET_EQUAL']}`。

## 节点级 confirmed test 指标

| 节点 | 样本数 | 正确数 | 准确率 | INCONCLUSIVE |
|---|---:|---:|---:|---:|
{chr(10).join(metric_lines)}

## 归约方式

```json
{json.dumps(dict(resolution_counts), ensure_ascii=False, indent=2)}
```

## confirmed test 误判

{chr(10).join(error_lines)}

## 诚实边界

- `PROVISIONAL` 标签不进入准确率分母。
- exemplar 样本不进入 test 指标。
- 图片内部内容只用于分类，不进入翻译或排版修复。
- 一次复核后仍不确定的节点保留 `INCONCLUSIVE` 裁决结果，不伪造确定类别。
- `page.role` 无法确定时进入 `INCONCLUSIVE`；已确定为 `body` 但 `body.layout_owner`、`body.flow.topology` 或 `body.composite.kind` 仍不确定时进入可追踪的 `body/freeform` 兜底目录，并在 route 中记录 `failed_node`。
"""
    (report_root / "classification_report.md").write_text(report_md, encoding="utf-8")
    print(json.dumps({"run_id": run_id, "report": str(report_root / 'classification_report.md'), "error_count": len(errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
