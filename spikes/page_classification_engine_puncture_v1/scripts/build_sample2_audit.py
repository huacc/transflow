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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-manifest", default="manifests/sample2_source_manifest.jsonl")
    parser.add_argument("--exclusion-manifest", default="manifests/sample2_bilingual_body_exclusions.json")
    args = parser.parse_args()

    run_root = ROOT / "artifacts" / "runs" / args.run_id
    report_root = ROOT / "reports" / "runs" / args.run_id
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    routes = read_jsonl(run_root / "routes" / "final_routes.jsonl")
    evidence = {row["sample_id"]: row for row in read_jsonl(run_root / "evidence" / "page_evidence.jsonl")}
    resolutions = read_jsonl(run_root / "judgements" / "node_resolutions.jsonl")
    calls = read_jsonl(run_root / "calls" / "qwen_calls.jsonl")
    sources = {row["sample_id"]: row for row in read_jsonl((ROOT / args.source_manifest).resolve())}
    exclusions = json.loads((ROOT / args.exclusion_manifest).resolve().read_text(encoding="utf-8"))
    verdict = json.loads((report_root / "set_equality_verdict.json").read_text(encoding="utf-8"))

    flags: list[dict[str, Any]] = []
    for route in routes:
        sample_id = route["sample_id"]
        leaf = "/".join(route["final_path"]) if route["complete_to_leaf"] else f"INCONCLUSIVE/{route['failed_node']}"
        ev = evidence[sample_id]
        source = sources[sample_id]
        reasons: list[str] = []
        page_number = source.get("source_page_number")
        page_count = source.get("source_page_count")
        if leaf == "cover" and page_number != 1:
            reasons.append("cover_not_source_first_page")
        if leaf == "end" and page_number != page_count:
            reasons.append("end_not_source_last_page")
        if leaf == "visual_only" and int(ev["text"]["native_char_count"]) > 50:
            reasons.append("visual_only_has_substantial_native_text")
        if leaf == "body/table" and int(ev["tables"]["count"]) == 0:
            reasons.append("table_route_without_detected_table")
        if leaf == "body/composite/flow_text_table" and int(ev["tables"]["count"]) == 0:
            reasons.append("flow_text_table_without_detected_table")
        if leaf in {
            "body/chart",
            "body/composite/anchored_blocks_chart",
            "body/composite/chart_table",
            "body/composite/flow_text_chart",
        }:
            if int(ev["drawings"]["count"]) < 3 and int(ev["images"]["count"]) == 0:
                reasons.append("chart_route_without_visual_structure_signal")
        if reasons:
            flags.append(
                {
                    "sample_id": sample_id,
                    "leaf": leaf,
                    "reasons": reasons,
                    "source_page_number": page_number,
                    "source_page_count": page_count,
                    "report_id": source.get("report_id"),
                }
            )

    provider_errors = [
        {
            "sample_id": row["sample_id"],
            "node_key": row["node_key"],
            "stage": row["stage"],
            "error_code": row["response"]["error_code"],
        }
        for row in calls
        if row["response"]["error_code"] is not None
    ]
    resolution_counts = Counter(row["resolution"] for row in resolutions)
    flag_counts: Counter[str] = Counter(reason for item in flags for reason in item["reasons"])
    flags_by_reason: dict[str, list[str]] = defaultdict(list)
    for item in flags:
        for reason in item["reasons"]:
            flags_by_reason[reason].append(item["sample_id"])

    audit = {
        "run_id": args.run_id,
        "gold_accuracy_available": False,
        "engine_summary": summary,
        "sample_result_set_equality": verdict,
        "active_sample_count": len(sources),
        "excluded_bilingual_body_count": exclusions["excluded_count"],
        "selected_report_count": len({row.get("report_id") for row in sources.values()}),
        "qwen_call_count": len(calls),
        "qwen_provider_error_count": len(provider_errors),
        "qwen_provider_errors": provider_errors,
        "wrong_base_url_count": sum(row["provider"]["base_url"] != "http://112.30.139.26:19400/v1" for row in calls),
        "wrong_model_count": sum(row["provider"]["configured_model"] != "Qwen/Qwen3.6-35B-A3B" for row in calls),
        "resolution_counts": dict(resolution_counts),
        "automated_review_flag_count": len(flags),
        "automated_review_flag_counts": dict(flag_counts),
        "automated_review_flags": flags,
    }
    write_json(report_root / "sample2_audit.json", audit)

    distribution = "\n".join(f"| `{leaf}` | {count} |" for leaf, count in summary["classification_counts"].items())
    flag_lines = []
    for reason, sample_ids in sorted(flags_by_reason.items()):
        preview = ", ".join(f"`{sample_id}`" for sample_id in sample_ids[:30])
        suffix = " ..." if len(sample_ids) > 30 else ""
        flag_lines.append(f"- `{reason}`：{len(sample_ids)} 页；{preview}{suffix}")
    if not flag_lines:
        flag_lines.append("- 无自动审计可疑项。")
    provider_lines = [
        f"- `{item['sample_id']}` / `{item['node_key']}` / `{item['stage']}`：`{item['error_code']}`；后续路由仍完整。"
        for item in provider_errors
    ] or ["- 无模型接口错误。"]
    report = f"""# 样本2页面分类审计报告

- run_id：`{args.run_id}`
- 初始抽样：50 份年报 × 20 页 = 1000 页。
- 排除中英文近似对照正文：{exclusions['excluded_count']} 页。
- 实际分类：{len(sources)} 页，覆盖 {len({row.get('report_id') for row in sources.values()})} 份年报。
- 完整到达叶子：{summary['complete_route_count']}；停止路由：{summary['route_stopped_count']}；`freeform` 兜底：{summary['taxonomy_fallback_count']}。
- 自建千问调用：{len(calls)}；复核调用：{summary['review_call_count']}。
- 样本与分类结果集合一致：`{verdict['SAMPLE_RESULT_PDF_SET_EQUAL']}`。
- 本批没有人工 gold，不报告伪准确率；下面是全量结构审计和待看目录。

## 分类分布

| 分类叶子 | 页数 |
|---|---:|
{distribution}

## 模型接口

{chr(10).join(provider_lines)}

## 自动全量审计可疑项

自动审计逐页检查页码位置、原生文字量、表格检测和图形信号，共标记 {len(flags)} 页；这些是人工复核候选，不等于已确认误判。

{chr(10).join(flag_lines)}

## 归约方式

```json
{json.dumps(dict(resolution_counts), ensure_ascii=False, indent=2)}
```

## 诚实边界

- 文件名、源路径、人工分类标签未进入千问输入。
- 图片内部文字不翻译、不重排。
- 本批只验证分类树在大样本上的路由效果；没有人工 gold，不能把模型自洽当成准确率。
- 自动审计信号只能筛查明显矛盾，不能替代逐页人工排版判断。
"""
    (report_root / "sample2_audit.md").write_text(report, encoding="utf-8")
    print(json.dumps({"SAMPLE2_AUDIT_READY": True, "report": str(report_root / 'sample2_audit.md'), "flag_count": len(flags), "provider_error_count": len(provider_errors)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
